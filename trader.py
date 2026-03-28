"""
交易引擎 — 策略A v4

架構：
- 交易所介面：BaseExchange（不認識幣安細節）
- 價格/成交/餘額：WebSocket 推送
- REST：只用於下單、取消單、啟動初始化
- ML：平倉後背景 task 補填 1h/4h 價格，計算 reward_score
"""

import asyncio
import aiohttp
import json
import time
import math
import logging
from datetime import datetime, timezone, timedelta

TZ_TAIPEI = timezone(timedelta(hours=8))
from exchanges.base import BaseExchange
from exchanges.binance import BinanceExchange
from database import (
    write_log, record_trade_close, add_trade_analytics, ml_fill_task
)
from config import load_config, get_notional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

FILTERS_CACHE_TTL = 3600
_filters_last_refresh = 0

# ===== 全局狀態 =====
state = {
    "running": True,
    "paused": False,
    "last_pool_scan": 0,
    "margin_pause": False,
    "candidate_pool": [],
    "scanner_latest_result": [],

    "tp_sl_orders": {},

    "hidden_grids": {},
    "known_fills": {},
    "symbol_sell_count": {},    # symbol -> SELL 成交次數（開倉+加碼）
    "symbol_total_margin": {},  # symbol -> 累積總保證金（每次SELL成交後累加）
    "symbol_avg_entry": {},     # symbol -> 最新均入價（每次SELL成交後更新，平倉後清除）
    "symbol_realized_pnl": {},  # symbol -> 累積已實現損益（分批平倉時累加，完全平倉後記DB）
    "pending_open": set(),      # 已掛開倉單但尚未成交的幣種（防止超過 max_symbols）
    "closing_symbols": set(),   # 正在由 close_symbol 處理平倉的幣種（防止 handle_close_fill 重複寫DB）
    "black_k_targets": {},      # symbol -> target_price（黑K目標掛單價）
    "black_k_last_k_time": {},  # symbol -> k_open_time（防同一根K棒重複觸發）
    "symbol_sl_order": {},      # symbol -> order_id（單一止損限價單）
    "symbol_open_time": {},     # symbol -> 第一筆開倉時間（ISO string）
    "tp_tier1_done": set(),      # 第一段止盈已成交的幣種
    "tp_tier2_guard": {},        # symbol -> 第二段追蹤保底價（=第一段目標價）
    "symbol_setup_done": set(),
    "symbol_filters_cache": {},

    "price_cache": {},
    "price_cache_time": 0,
    "balance_cache": None,
    "balance_cache_time": 0,
    "_binance_positions_cache": {},

    "triggered_symbols": set(),
    "symbol_last_close_time": {},  # symbol -> 上次平倉時間戳（用於冷卻期）
    "symbol_open_paused": set(),   # 因價格上漲暫停加碼的幣種（可自動恢復）
    "ws_price_connected": False,
    "ws_user_connected": False,

    "upper_1m_cache": {},       # symbol -> {"upper": float, "ts": float, "history": [float,...]}，1分K上軌快取
}


def get_exchange(cfg) -> BaseExchange:
    """工廠函式：根據 config 建立對應交易所實例"""
    if cfg.get("paper_trading", False):
        from exchanges.paper import PaperExchange
        ex = PaperExchange(leverage=cfg.get("leverage", 30))
        ex.set_price_ref(state["price_cache"])
        return ex
    exchange_name = cfg.get("exchange", "binance")
    if exchange_name == "binance":
        return BinanceExchange(cfg["api_key"], cfg["api_secret"], cfg.get("testnet", True))
    raise ValueError(f"不支援的交易所: {exchange_name}")


# 向後相容（app.py 用）
def get_client(cfg) -> BaseExchange:
    return get_exchange(cfg)


# ===== 精度工具 =====

def align_price(price, tick_size):
    if not tick_size or tick_size <= 0:
        return round(price, 8)
    precision = max(0, -int(math.log10(tick_size)))
    return round(price - (price % tick_size), precision)


def align_qty(qty, step_size):
    if not step_size or step_size <= 0:
        return round(qty, 8)
    precision = max(0, -int(math.log10(step_size)))
    return round(qty - (qty % step_size), precision)


async def get_filters_cached(exchange: BaseExchange, symbol: str):
    if symbol in state["symbol_filters_cache"]:
        return state["symbol_filters_cache"][symbol]
    try:
        filters = await exchange.get_symbol_filters(symbol)
        if filters:
            state["symbol_filters_cache"][symbol] = filters
        return filters
    except Exception as e:
        logger.error(f"取得精度失敗 {symbol}: {e}")
        return None


async def refresh_all_filters(exchange: BaseExchange):
    global _filters_last_refresh
    now = time.time()
    if now - _filters_last_refresh < FILTERS_CACHE_TTL and state["symbol_filters_cache"]:
        return
    try:
        data = await exchange.get_exchange_info()
        for s in data.get("symbols", []):
            sym = s["symbol"]
            step_size, tick_size = 0.001, 0.0001
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step_size = float(f["stepSize"])
                elif f["filterType"] == "PRICE_FILTER":
                    tick_size = float(f["tickSize"])
            state["symbol_filters_cache"][sym] = {
                "step_size": step_size,
                "tick_size": tick_size,
            }
        _filters_last_refresh = now
        logger.info(f"精度快取完成，共 {len(state['symbol_filters_cache'])} 個幣種")
    except Exception as e:
        logger.error(f"精度快取失敗: {e}")


def get_cached_price(symbol: str) -> float | None:
    return state["price_cache"].get(symbol)


def get_balance_cached() -> dict | None:
    return state["balance_cache"]


# ===== 1分K上軌批次更新 =====

def _calc_bb_upper(klines: list, period: int = 20, std_mult: float = 2.0) -> float | None:
    if not klines or len(klines) < period:
        return None
    closes = [float(k[4]) for k in klines]
    window = closes[-period:]
    mean = sum(window) / period
    std = math.sqrt(sum((x - mean) ** 2 for x in window) / period)
    return mean + std_mult * std


async def refresh_upper_1m(exchange: BaseExchange, symbols: list):
    """批次並發拉1分K，更新 upper_1m_cache（每輪主循環呼叫，10秒一次）"""
    if not symbols:
        return

    async def fetch_one(sym):
        try:
            klines = await exchange.get_klines(sym, "1m", limit=25)
            upper = _calc_bb_upper(klines)
            if upper:
                # 同時計算中軌（BB中軌 = 20期移動平均）
                closes = [float(k[4]) for k in klines]
                period = 20
                middle = sum(closes[-period:]) / period if len(closes) >= period else None
                entry = state["upper_1m_cache"].get(sym, {})
                history = entry.get("history", [])
                history.append(upper)
                if len(history) > 10:
                    history = history[-10:]
                state["upper_1m_cache"][sym] = {
                    "upper": upper,
                    "middle": middle,
                    "ts": time.time(),
                    "history": history
                }
        except Exception as e:
            logger.warning(f"1分K上軌更新失敗 {sym}: {e}")

    await asyncio.gather(*[fetch_one(sym) for sym in symbols])


def get_upper_1m(symbol: str) -> float | None:
    """取得1分K布林上軌快取值"""
    entry = state["upper_1m_cache"].get(symbol)
    if entry:
        return entry["upper"]
    return None


def get_upper_1m_slope(symbol: str, lookback: int = 5) -> float | None:
    """
    計算1分K上軌斜率（每根K的平均漲幅%）
    正值=上軌上漲，負值=上軌下跌
    """
    entry = state["upper_1m_cache"].get(symbol)
    if not entry:
        return None
    history = entry.get("history", [])
    if len(history) < lookback + 1:
        return None
    recent = history[-lookback - 1:]
    start = recent[0]
    end = recent[-1]
    if start <= 0:
        return None
    slope = (end - start) / start * 100 / lookback  # 每根K平均漲幅%
    return round(slope, 5)


# ===== WebSocket: 價格推送 =====

async def ws_price_stream(exchange: BaseExchange):
    url = f"{exchange.ws_base}/stream?streams=!miniTicker@arr"
    while state["running"]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=20) as ws:
                    state["ws_price_connected"] = True
                    logger.info(f"✅ [{exchange.name}] 價格 WS 已連線")
                    async for msg in ws:
                        if not state["running"]:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            tickers = data.get("data", data)
                            if isinstance(tickers, list):
                                for t in tickers:
                                    sym = t.get("s")
                                    price = t.get("c")
                                    if sym and price:
                                        state["price_cache"][sym] = float(price)
                                state["price_cache_time"] = time.time()
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except Exception as e:
            logger.error(f"價格 WS 斷線: {e}")
        finally:
            state["ws_price_connected"] = False
        if state["running"]:
            await asyncio.sleep(3)


# ===== WebSocket: User Data Stream =====

async def ws_user_stream(exchange: BaseExchange, cfg: dict):
    """訂閱 User Data Stream，或 paper mode 用本地模擬"""
    from exchanges.paper import PaperExchange
    if isinstance(exchange, PaperExchange):
        async def paper_fill_handler(event):
            # 先同步 paper 持倉到 state，再呼叫 handle_user_event
            # 這樣 handle_close_fill 檢查 pos_after 時已是成交後的最新狀態
            # 避免誤判「部分平倉」導致完全平倉後不記錄DB
            paper_pos = await exchange.get_positions()
            state["_binance_positions_cache"] = {
                p["symbol"]: {
                    "qty": abs(float(p["positionAmt"])),
                    "avg_entry": float(p["entryPrice"]),
                    "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
                    "initial_margin": float(p.get("initialMargin", 0)),
                } for p in paper_pos if abs(float(p["positionAmt"])) > 0
            }
            bal = await exchange.get_balance()
            if bal:
                state["balance_cache"] = bal
            await handle_user_event(event, cfg, exchange)
        exchange.register_fill_callback(paper_fill_handler)
        state["ws_user_connected"] = True
        logger.info("✅ [PAPER] User Data 本地模擬已就緒")
        while state["running"]:
            await asyncio.sleep(10)
        return

    while state["running"]:
        try:
            listen_key = await exchange.get_listen_key()
            if not listen_key:
                logger.error("取得 listen key 失敗，30秒後重試")
                await asyncio.sleep(30)
                continue

            url = f"{exchange.ws_base}/stream?streams={listen_key}"
            keepalive_task = asyncio.create_task(
                _keepalive_loop(exchange, listen_key)
            )
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=20) as ws:
                        state["ws_user_connected"] = True
                        logger.info(f"✅ [{exchange.name}] User Data WS 已連線")
                        async for msg in ws:
                            if not state["running"]:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await handle_user_event(
                                    json.loads(msg.data), cfg, exchange
                                )
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            finally:
                keepalive_task.cancel()
        except Exception as e:
            logger.error(f"User Data WS 斷線: {e}")
        finally:
            state["ws_user_connected"] = False
        if state["running"]:
            await asyncio.sleep(5)


async def _keepalive_loop(exchange: BaseExchange, listen_key: str):
    while True:
        await asyncio.sleep(1800)
        await exchange.keepalive_listen_key(listen_key)
        logger.info("Listen key keepalive 完成")


async def handle_user_event(data: dict, cfg: dict, exchange: BaseExchange):
    event = data.get("e")

    if event == "ACCOUNT_UPDATE":
        update = data.get("a", {})
        for asset in update.get("B", []):
            if asset.get("a") == "USDT":
                old = state["balance_cache"] or {}
                state["balance_cache"] = {
                    "total": float(asset.get("wb", 0)),
                    "available": float(asset.get("cw", 0)),
                    "unrealized_pnl": old.get("unrealized_pnl", 0),
                    "margin_used": old.get("margin_used", 0),
                    "margin_ratio": old.get("margin_ratio", 0),
                }
                state["balance_cache_time"] = time.time()

        # 更新持倉快取，同時累計 margin_used
        total_margin = 0.0
        for pos in update.get("P", []):
            sym = pos.get("s")
            amt = float(pos.get("pa", 0))
            if sym:
                if amt == 0:
                    state["_binance_positions_cache"].pop(sym, None)
                else:
                    im = float(pos.get("iw", 0))  # initialMargin（幣安 ACCOUNT_UPDATE 有此欄位）
                    state["_binance_positions_cache"][sym] = {
                        "qty": abs(amt),
                        "avg_entry": float(pos.get("ep", 0)),
                        "unrealized_pnl": float(pos.get("up", 0)),
                        "initial_margin": im,
                    }
                    total_margin += im

        # 若有持倉更新，同步 margin_used 到 balance_cache
        if update.get("P") and state["balance_cache"]:
            # 重算所有持倉的 margin_used
            all_margin = sum(
                p.get("initial_margin", 0)
                for p in state["_binance_positions_cache"].values()
            )
            state["balance_cache"]["margin_used"] = round(all_margin, 4)
            total = state["balance_cache"]["total"]
            if total > 0:
                state["balance_cache"]["margin_ratio"] = round(all_margin / total * 100, 2)

    elif event == "ORDER_TRADE_UPDATE":
        order = data.get("o", {})
        status = order.get("X")
        if status not in ("FILLED", "PARTIALLY_FILLED"):
            return

        symbol = order.get("s")
        side = order.get("S")
        order_id = str(order.get("i"))
        fill_price = float(order.get("ap", 0) or order.get("L", 0))
        fill_qty = float(order.get("z", 0))
        realized_pnl = float(order.get("rp", 0))

        if not symbol or not fill_price:
            return

        known = state["known_fills"].setdefault(symbol, set())
        fill_key = f"{order_id}_{status}"
        if fill_key in known:
            return
        known.add(fill_key)

        logger.info(f"📨 成交 {symbol} {side} @ {fill_price} qty={fill_qty} pnl={realized_pnl}")
        write_log("FILL", f"WS成交 {side} @ {fill_price} qty={fill_qty}",
                  symbol=symbol, detail={
                      "order_id": order_id, "side": side,
                      "price": fill_price, "qty": fill_qty,
                      "realized_pnl": realized_pnl, "status": status,
                  })

        if side == "SELL" and status == "FILLED":
            state["symbol_sell_count"][symbol] = state["symbol_sell_count"].get(symbol, 0) + 1
            state["pending_open"].discard(symbol)  # 已確認成交，移出待確認集合
            # 第一筆開倉時記錄時間
            if symbol not in state["symbol_open_time"]:
                state["symbol_open_time"][symbol] = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
            # 累計總保證金
            balance = get_balance_cached()
            if balance and balance.get("total", 0) > 0:
                cfg_now = load_config()
                notional = fill_price * fill_qty
                lev = cfg_now.get("leverage", 30)
                margin_this_fill = notional / lev
                state["symbol_total_margin"][symbol] = (
                    state["symbol_total_margin"].get(symbol, 0) + margin_this_fill
                )
            # 同步均入價（從持倉快取取最新值）
            pos_cache = state["_binance_positions_cache"].get(symbol, {})
            if pos_cache.get("avg_entry", 0) > 0:
                state["symbol_avg_entry"][symbol] = pos_cache["avg_entry"]
            update_hidden_grids(symbol, fill_price, cfg)
            await asyncio.sleep(0.5)
            await place_tp_sl(exchange, cfg, symbol)
            await place_sl(exchange, cfg, symbol)

        elif side == "BUY" and status == "FILLED" and realized_pnl != 0:
            # 累積已實現損益
            state["symbol_realized_pnl"][symbol] = (
                state["symbol_realized_pnl"].get(symbol, 0) + realized_pnl
            )
            total_pnl_now = state["symbol_realized_pnl"][symbol]

            pos_after = state["_binance_positions_cache"].get(symbol)
            fully_closed = not pos_after or pos_after.get("qty", 0) <= 0

            if fully_closed:
                # 完全平倉：記DB、清狀態
                await handle_close_fill(exchange, cfg, symbol, fill_price, fill_qty)
            else:
                # 部分平倉：判斷原因，更新狀態，不記DB（只在完全平倉時才記）
                sl_order_id = state["symbol_sl_order"].get(symbol)
                tp1_order_id = state["tp_sl_orders"].get(symbol, {}).get("tp1_limit")
                tp2_order_id = state["tp_sl_orders"].get(symbol, {}).get("tp2_limit")
                if sl_order_id and str(order_id) == sl_order_id:
                    close_reason = "SL_WS"
                elif str(order_id) in [tp1_order_id, tp2_order_id]:
                    close_reason = "TP_WS"
                elif fill_price < (state["symbol_avg_entry"].get(symbol) or fill_price):
                    close_reason = "TP_WS"
                else:
                    close_reason = "SL_WS"

                write_log("CLOSE", f"部分平倉({close_reason}) 累積PnL={total_pnl_now:.4f}",
                          symbol=symbol, detail={"close_price": fill_price, "qty": fill_qty,
                                                  "total_pnl": round(total_pnl_now, 4),
                                                  "close_reason": close_reason})

                # 第一段止盈成交標記
                if tp1_order_id and str(order_id) == tp1_order_id:
                    state["tp_tier1_done"].add(symbol)
                    write_log("TP_TIER", f"第一段止盈成交 @ {fill_price}，重掛第二段", symbol=symbol)

                # 重掛止盈止損
                await asyncio.sleep(0.5)
                await place_tp_sl(exchange, cfg, symbol)
                await place_sl(exchange, cfg, symbol)


async def handle_close_fill(exchange: BaseExchange, cfg: dict, symbol: str,
                             close_price: float, qty: float):
    """持倉已確認歸零後呼叫，記錄平倉歷史"""
    # 若 close_symbol 已在處理（手動/強制平倉），跳過避免重複寫DB
    if symbol in state["closing_symbols"]:
        write_log("CLOSE", f"平倉完成(WS) 已由close_symbol處理，跳過重複記錄", symbol=symbol)
        return
    # 用累積已實現損益（含所有分批平倉）
    total_pnl = state["symbol_realized_pnl"].get(symbol, 0)

    cached = state["_binance_positions_cache"].get(symbol, {})
    avg_entry = (state["symbol_avg_entry"].get(symbol)
                 or cached.get("avg_entry")
                 or close_price)
    total_margin = state["symbol_total_margin"].get(symbol, 0)
    if total_margin <= 0:
        total_margin = cached.get("initial_margin", 0)
    roe_pct = (total_pnl / total_margin * 100) if total_margin > 0 else 0
    order_count = state["symbol_sell_count"].get(symbol, 1)

    if avg_entry > 0 and close_price < avg_entry:
        close_reason = "TP_WS"
    elif avg_entry > 0 and close_price > avg_entry:
        close_reason = "SL_WS"
    else:
        close_reason = "CLOSE_WS"

    open_time = state["symbol_open_time"].get(symbol)
    candidate_info = next((c for c in state["candidate_pool"] if c["symbol"] == symbol), {})
    record_trade_close(
        symbol=symbol, avg_entry=avg_entry, close_price=close_price,
        total_qty=qty, total_margin=total_margin, total_pnl=total_pnl,
        roe_pct=roe_pct, close_reason=close_reason,
        exchange=exchange.name, order_count=order_count, open_time=open_time,
        market_snapshot=candidate_info
    )

    analytics_id = add_trade_analytics(
        symbol=symbol, avg_entry=avg_entry, close_price=close_price,
        total_qty=qty, total_margin=total_margin, total_pnl=total_pnl,
        roe_pct=roe_pct, close_reason=close_reason,
        market_snapshot=candidate_info, exchange=exchange.name,
        open_time=open_time
    )
    if analytics_id:
        asyncio.create_task(ml_fill_task(exchange, analytics_id, symbol))

    write_log("CLOSE", f"平倉完成(WS) PnL={total_pnl:.4f}", symbol=symbol,
              detail={"avg_entry": avg_entry, "close_price": close_price,
                      "pnl": round(total_pnl, 4), "roe_pct": round(roe_pct, 2)})

    # 取消所有殘留掛單（止盈/止損 BUY 單 + 隱形網格 SELL 單），並補平已成交的 SELL 單
    try:
        open_orders = await exchange.get_open_orders(symbol)
        # 先取消所有掛單
        try:
            await exchange.cancel_all_orders(symbol)
        except Exception:
            pass
        # 檢查哪些 SELL 單已成交（取消失敗代表已成交），需補市價平
        sell_orders = [o for o in open_orders if o.get("side") == "SELL"]
        accidental_qty = 0.0
        for order in sell_orders:
            oid = str(order["orderId"])
            result = await exchange.cancel_order(symbol, oid)
            if isinstance(result, dict) and result.get("code") == -2011:
                qty = float(order.get("origQty", 0))
                accidental_qty += qty
                write_log("WARN", f"平倉後殘留SELL單已成交，補市價平 qty={qty}", symbol=symbol)
        if accidental_qty > 0:
            filters = state["symbol_filters_cache"].get(symbol, {})
            step = filters.get("step_size", 0.001)
            accidental_qty = align_qty(accidental_qty, step)
            await exchange.place_market_order(symbol, "BUY", accidental_qty, reduce_only=True)
            write_log("CLOSE", f"補市價平倉 qty={accidental_qty}", symbol=symbol)
    except Exception as e:
        logger.error(f"平倉後清理殘留掛單失敗 {symbol}: {e}")

    _clear_symbol_state(symbol)


# ===== REST 持倉查詢（只在必要時用）=====

async def get_position_rest(exchange: BaseExchange, symbol: str) -> dict | None:
    try:
        positions = await exchange.get_positions(symbol)
        if not positions:
            return None
        p = positions[0]
        qty = abs(float(p["positionAmt"]))
        if qty <= 0:
            return None
        return {
            "qty": qty,
            "avg_entry": float(p["entryPrice"]),
            "unrealized_pnl": float(p["unRealizedProfit"]),
            "initial_margin": float(p["initialMargin"]),
        }
    except Exception as e:
        logger.error(f"REST 持倉查詢失敗 {symbol}: {e}")
        return None


async def get_all_binance_positions(exchange: BaseExchange) -> dict:
    try:
        positions = await exchange.get_positions()
        return {p["symbol"]: {
            "qty": abs(float(p["positionAmt"])),
            "avg_entry": float(p["entryPrice"]),
            "unrealized_pnl": float(p["unRealizedProfit"]),
            "initial_margin": float(p["initialMargin"]),
        } for p in positions if abs(float(p["positionAmt"])) > 0}
    except Exception as e:
        logger.error(f"取得所有持倉失敗: {e}")
        return {}


# ===== 幣種初始化 =====

async def ensure_symbol_setup(exchange: BaseExchange, cfg: dict, symbol: str):
    if symbol in state["symbol_setup_done"]:
        return True
    try:
        await exchange.set_margin_type(symbol, "CROSSED")
        await exchange.set_leverage(symbol, cfg["leverage"])
        state["symbol_setup_done"].add(symbol)
        return True
    except Exception as e:
        logger.error(f"幣種初始化失敗 {symbol}: {e}")
        return False


# ===== 隱形網格 =====

def calc_hidden_grids(entry_price, grid_spacing_pct, count=4):
    return [round(entry_price * (1 - grid_spacing_pct / 100 * i), 8)
            for i in range(1, count + 1)]


def update_hidden_grids(symbol: str, entry_price: float, cfg: dict):
    count = cfg.get("grid_count", 2)
    # 基準價取成交價與平均成本價兩者之高，避免成本被往下拉
    avg_entry = state["symbol_avg_entry"].get(symbol, 0)
    base_price = max(entry_price, avg_entry) if avg_entry > 0 else entry_price
    grids = calc_hidden_grids(base_price, cfg["grid_spacing_pct"], count)
    state["hidden_grids"][symbol] = grids
    logger.info(f"隱形網格更新 {symbol}: {grids} (基準={base_price}, 成交={entry_price}, 均入={avg_entry})")
    write_log("HIDDEN_GRID_UPDATE", f"隱形網格重算，基準價={base_price}（成交={entry_price}，均入={avg_entry}）",
              symbol=symbol, detail={"base_price": base_price, "entry_price": entry_price,
                                     "avg_entry": avg_entry, "grids": grids})


async def check_and_place_hidden_grids(exchange: BaseExchange, cfg: dict, symbol: str):
    grids = state["hidden_grids"].get(symbol, [])
    if not grids:
        return
    current_price = get_cached_price(symbol)
    if not current_price:
        return
    filters = await get_filters_cached(exchange, symbol)
    if not filters:
        return
    balance = get_balance_cached()
    if not balance:
        return

    # 檢查單幣加碼次數上限（已成交 + 已掛出未成交網格數）
    max_orders = cfg.get("max_orders_per_symbol", 20)
    filled_count = state["symbol_sell_count"].get(symbol, 0)
    pending_grid_count = len(grids)  # 目前 hidden_grids 裡還有幾格待掛
    # 放寬條件：虧損在 extend_loss_pct 內可加碼到 extend_max_orders
    extend_max = cfg.get("extend_orders_max", max_orders)
    extend_loss = cfg.get("extend_loss_pct", 0)
    if extend_loss > 0:
        pos = state["_binance_positions_cache"].get(symbol, {})
        avg_entry = pos.get("avg_entry", 0)
        current_price = get_cached_price(symbol) or avg_entry
        if avg_entry > 0:
            loss_pct = (current_price - avg_entry) / avg_entry * 100  # SHORT：上漲=虧損
            if loss_pct <= extend_loss:  # 虧損未達門檻，使用放寬上限
                max_orders = max(max_orders, extend_max)
    current_count = filled_count
    if current_count >= max_orders:
        return

    notional = get_notional(cfg, balance["total"])
    # 已成交筆數超過門檻後放大每單保證金
    filled_count = state["symbol_sell_count"].get(symbol, 0)
    scale_after = cfg.get("scale_up_after_order", 10)
    scale_mult = cfg.get("scale_up_multiplier", 1.0)
    if filled_count >= scale_after and scale_mult != 1.0:
        notional = notional * scale_mult
    remaining = []

    # 取1分K中軌作為掛單下限（由 refresh_upper_1m 每輪快取）
    middle_1m = state["upper_1m_cache"].get(symbol, {}).get("middle")

    for grid_price in grids:
        if current_price < grid_price:
            # 中軌濾網：網格價格低於1分K中軌則暫緩掛出，等回到中軌以上再掛
            if middle_1m is not None and grid_price < middle_1m:
                remaining.append(grid_price)
                write_log("GRID_HOLD", f"隱形網格暫緩（低於1分K中軌{middle_1m:.6f}）@ {grid_price}",
                          symbol=symbol, detail={"grid_price": grid_price, "middle_1m": middle_1m})
                continue
            qty = align_qty(notional / grid_price, filters["step_size"])
            aligned_price = align_price(grid_price, filters["tick_size"])
            if qty <= 0:
                continue
            result = await exchange.place_limit_order(symbol, "SELL", qty, aligned_price)
            if result and "orderId" in result:
                logger.info(f"📌 隱形網格掛出 {symbol} @ {aligned_price}")
                write_log("GRID_PLACE", f"隱形網格掛出 @ {aligned_price}",
                          symbol=symbol, detail={"price": aligned_price, "qty": qty,
                                                 "order_id": str(result["orderId"])})
            else:
                remaining.append(grid_price)
                write_log("ERROR", f"隱形網格掛單失敗 @ {aligned_price}",
                          symbol=symbol, detail={"resp": result})
        else:
            remaining.append(grid_price)

    state["hidden_grids"][symbol] = remaining


# ===== 止盈止損 =====

def calc_tp_tier_price(avg_entry, roi_pct, leverage):
    """本金獲利率轉換為價格：SHORT 跌幅 = roi_pct / leverage"""
    price_drop_pct = roi_pct / leverage
    return avg_entry * (1 - price_drop_pct / 100)


async def place_tp_sl(exchange: BaseExchange, cfg: dict, symbol: str):
    """分段止盈：第一段限價，第二段限價+追蹤保底"""
    pos = state["_binance_positions_cache"].get(symbol)
    if not pos or pos.get("qty", 0) <= 0:
        pos = await get_position_rest(exchange, symbol)
        if not pos:
            return
        state["_binance_positions_cache"][symbol] = pos

    if pos.get("initial_margin", 0) <= 0:
        pos_rest = await get_position_rest(exchange, symbol)
        if pos_rest:
            pos = pos_rest
            state["_binance_positions_cache"][symbol] = pos

    avg_entry = pos["avg_entry"]
    total_qty = pos["qty"]
    leverage = cfg.get("leverage", 30)

    tp1_roi = cfg.get("tp_tier1_roi", 10.0)
    tp1_qty_pct = cfg.get("tp_tier1_qty", 50.0)
    tp2_roi = cfg.get("tp_tier2_roi", 20.0)

    tp1_price_raw = calc_tp_tier_price(avg_entry, tp1_roi, leverage)
    tp2_price_raw = calc_tp_tier_price(avg_entry, tp2_roi, leverage)

    if tp1_price_raw >= avg_entry or tp2_price_raw >= avg_entry:
        write_log("ERROR", f"止盈價格方向錯誤，跳過", symbol=symbol)
        return

    filters = await get_filters_cached(exchange, symbol)
    if not filters:
        return

    tp1_price = align_price(tp1_price_raw, filters["tick_size"])
    tp2_price = align_price(tp2_price_raw, filters["tick_size"])

    # 取消舊掛單
    try:
        await exchange.cancel_all_orders(symbol)
    except Exception:
        pass
    state["tp_sl_orders"][symbol] = {}

    tier1_done = symbol in state.get("tp_tier1_done", set())
    new_orders = {}

    if not tier1_done:
        # 第一段：平 tp1_qty_pct% 倉位
        t1_qty = align_qty(total_qty * tp1_qty_pct / 100, filters["step_size"])
        if t1_qty > 0:
            r = await exchange.place_limit_order(symbol, "BUY", t1_qty, tp1_price, reduce_only=True)
            if r and "orderId" in r:
                new_orders["tp1_limit"] = str(r["orderId"])
                logger.info(f"✅ 第一段止盈 {symbol} @ {tp1_price} (ROI={tp1_roi}%)")
            else:
                write_log("ERROR", f"第一段止盈掛單失敗", symbol=symbol, detail={"resp": r})

    # 第二段：用持倉快取的實際數量，確保不留尾巴
    pos_now = state["_binance_positions_cache"].get(symbol, {})
    actual_qty = pos_now.get("qty", total_qty)
    if tier1_done:
        t2_qty = align_qty(actual_qty, filters["step_size"])
    else:
        # 第一段還沒成交時，用總量減去第一段數量
        t2_qty = align_qty(actual_qty - t1_qty, filters["step_size"])
        if t2_qty <= 0:
            t2_qty = align_qty(actual_qty * (1 - tp1_qty_pct / 100), filters["step_size"])
    if t2_qty > 0:
        r = await exchange.place_limit_order(symbol, "BUY", t2_qty, tp2_price, reduce_only=True)
        if r and "orderId" in r:
            new_orders["tp2_limit"] = str(r["orderId"])
            # 記錄第二段追蹤保底價（= 第一段目標價）
            state.setdefault("tp_tier2_guard", {})[symbol] = tp1_price
            logger.info(f"✅ 第二段止盈 {symbol} @ {tp2_price} (ROI={tp2_roi}%) 保底={tp1_price}")
        else:
            write_log("ERROR", f"第二段止盈掛單失敗", symbol=symbol, detail={"resp": r})

    state["tp_sl_orders"][symbol] = new_orders
    write_log("TP_SL_ORDER",
              f"止盈更新 avg={avg_entry:.6f} tp1={tp1_price}(ROI={tp1_roi}%) tp2={tp2_price}(ROI={tp2_roi}%)",
              symbol=symbol, detail={"avg_entry": avg_entry, "tp1_price": tp1_price,
                                     "tp2_price": tp2_price, "orders": new_orders})


async def place_sl(exchange: BaseExchange, cfg: dict, symbol: str):
    """掛單一止損限價單，每次開倉/加碼後重置"""
    pos = state["_binance_positions_cache"].get(symbol, {})
    total_qty = pos.get("qty", 0)
    avg_entry = state["symbol_avg_entry"].get(symbol) or pos.get("avg_entry", 0)
    total_margin = state["symbol_total_margin"].get(symbol, 0) or pos.get("initial_margin", 0)
    if total_qty <= 0 or avg_entry <= 0 or total_margin <= 0:
        return

    leverage = cfg.get("leverage", 30)
    sl_loss_pct = cfg.get("sl_loss_pct", 40.0)

    # 停損價 = 均入價 × (1 + sl_loss_pct / leverage / 100)
    sl_price_raw = avg_entry * (1 + sl_loss_pct / leverage / 100)

    filters = await get_filters_cached(exchange, symbol)
    if not filters:
        return

    sl_price = align_price(sl_price_raw, filters["tick_size"])
    close_qty = align_qty(total_qty, filters["step_size"])
    if close_qty <= 0:
        return

    # 取消舊止損單
    old_oid = state["symbol_sl_order"].get(symbol)
    if old_oid:
        try:
            await exchange.cancel_order(symbol, old_oid)
        except Exception:
            pass
        state["symbol_sl_order"].pop(symbol, None)

    r = await exchange.place_limit_order(symbol, "BUY", close_qty, sl_price, reduce_only=True)
    if r and "orderId" in r:
        state["symbol_sl_order"][symbol] = str(r["orderId"])
        logger.info(f"🔴 止損掛出 {symbol} @ {sl_price} qty={close_qty} (虧損{sl_loss_pct}%本金)")
        write_log("SL_ORDER", f"止損單掛出 @ {sl_price} (虧損{sl_loss_pct}%本金)",
                  symbol=symbol, detail={"sl_price": sl_price, "qty": close_qty, "avg_entry": avg_entry})
    else:
        write_log("ERROR", f"止損掛單失敗", symbol=symbol, detail={"resp": r})


# ===== 開倉 =====

async def try_open_position(exchange: BaseExchange, cfg: dict, symbol: str,
                             entry_price: float, trigger_type: str = "UPPER") -> bool:
    if state["paused"] or state["margin_pause"]:
        return False

    # 該幣種正在平倉中，禁止開新倉
    if symbol in state["closing_symbols"]:
        return False

    # 該幣種因漲幅超限暫停加碼
    if symbol in state["symbol_open_paused"]:
        return False


    open_syms = set(state["_binance_positions_cache"].keys())
    # 加上 pending_open：已掛單但尚未成交的幣種也計入持倉數
    effective_open = open_syms | (state["pending_open"] - open_syms)
    if symbol not in effective_open and len(effective_open) >= cfg["max_symbols"]:
        return False

    # 檢查單幣加碼次數上限
    max_orders = cfg.get("max_orders_per_symbol", 20)
    current_count = state["symbol_sell_count"].get(symbol, 0)
    # 放寬條件：虧損在 extend_loss_pct 內可放寬到 extend_orders_max
    extend_max = cfg.get("extend_orders_max", max_orders)
    extend_loss = cfg.get("extend_loss_pct", 0)
    if extend_loss > 0:
        pos = state["_binance_positions_cache"].get(symbol, {})
        avg_entry = pos.get("avg_entry", 0)
        current_price = get_cached_price(symbol) or avg_entry
        if avg_entry > 0:
            loss_pct = (current_price - avg_entry) / avg_entry * 100
            if loss_pct <= extend_loss:
                max_orders = max(max_orders, extend_max)
    if current_count >= max_orders:
        write_log("BLOCKED", f"已達最大加碼次數({current_count}/{max_orders})", symbol=symbol)
        return False

    balance = get_balance_cached()
    if not balance:
        write_log("ERROR", "餘額快取為空，跳過開倉", symbol=symbol)
        return False

    total = balance["total"]
    margin_used = balance.get("margin_used", 0)
    if total > 0 and margin_used > 0 and (margin_used / total * 100) >= cfg["margin_usage_limit_pct"]:
        state["margin_pause"] = True
        write_log("MARGIN_PAUSE", "保證金使用率超限", symbol=symbol)
        return False

    await ensure_symbol_setup(exchange, cfg, symbol)

    filters = await get_filters_cached(exchange, symbol)
    if not filters:
        return False

    notional = get_notional(cfg, total)
    # 已成交筆數超過門檻後放大每單保證金
    filled_count = state["symbol_sell_count"].get(symbol, 0)
    scale_after = cfg.get("scale_up_after_order", 10)
    scale_mult = cfg.get("scale_up_multiplier", 1.0)
    if filled_count >= scale_after and scale_mult != 1.0:
        notional = notional * scale_mult
        logger.info(f"[SCALE_UP] {symbol} 已成交{filled_count}筆，notional × {scale_mult}")
    qty = align_qty(notional / entry_price, filters["step_size"])
    price = align_price(entry_price, filters["tick_size"])

    if qty <= 0:
        return False

    result = await exchange.place_limit_order(symbol, "SELL", qty, price)
    if "orderId" not in result:
        write_log("ERROR", f"下單失敗: {result.get('msg','')}", symbol=symbol,
                  detail={"price": price, "qty": qty, "resp": result})
        return False

    logger.info(f"✅ 掛單 {symbol} @ {price} qty={qty} trigger={trigger_type}")
    # 新幣種掛單成功，加入 pending_open 防止同輪循環重複開倉超過 max_symbols
    if symbol not in state["_binance_positions_cache"]:
        state["pending_open"].add(symbol)
    candidate_info = next((c for c in state["candidate_pool"] if c["symbol"] == symbol), {})
    write_log("ORDER", f"掛限價空單 @ {price} [{trigger_type}]", symbol=symbol,
              detail={"order_id": str(result["orderId"]), "price": price, "qty": qty,
                      "notional": notional, "trigger_type": trigger_type,
                      "account_balance": total,
                      "market_snapshot": {
                          "upper_15m": candidate_info.get("upper_15m", 0),
                          "dist_15m_pct": candidate_info.get("dist_15m", 0),
                          "dist_1h_pct": candidate_info.get("dist_1h", 0),
                          "band_width_pct": candidate_info.get("band_width_pct", 0),
                          "volume_usdt": candidate_info.get("volume_usdt", 0),
                          "funding_rate": candidate_info.get("funding_rate"),
                          "btc_change_1h": candidate_info.get("btc_change_1h"),
                      }})
    return True


# ===== 平倉（手動/強制）=====

async def close_symbol(exchange: BaseExchange, cfg: dict, symbol: str, reason: str = "TP"):
    logger.info(f"平倉 {symbol} reason={reason}")
    state["closing_symbols"].add(symbol)  # 標記平倉中，防止 handle_close_fill 重複寫DB

    # 先取持倉資料（必須在 place_market_order 之前，paper mode 成交後持倉會清空）
    pos = await get_position_rest(exchange, symbol)
    # Paper mode fallback：get_position_rest 可能已清空，改從快取取
    if not pos:
        cached = state["_binance_positions_cache"].get(symbol)
        if cached and cached.get("qty", 0) > 0:
            pos = cached

    if pos:
        total_qty = pos["qty"]
        avg_entry = pos.get("avg_entry", state["symbol_avg_entry"].get(symbol, 0))
        margin = state["symbol_total_margin"].get(symbol, 0) or pos.get("initial_margin", 0)

        # 下單前抓標記價格（用於滑點計算）
        mark_price = await exchange.get_mark_price(symbol)

        result = await exchange.place_market_order(symbol, "BUY", total_qty, reduce_only=True)
        actual_close_price = None
        if result and "avgPrice" in result:
            try:
                actual_close_price = float(result["avgPrice"])
            except Exception:
                pass
        if not actual_close_price or actual_close_price <= 0:
            await asyncio.sleep(0.5)
            actual_close_price = get_cached_price(symbol) or avg_entry

        # 計算滑點（只有市價單才有意義）
        slippage_pct = None
        if mark_price and mark_price > 0 and actual_close_price:
            slippage_pct = round(abs(actual_close_price - mark_price) / mark_price * 100, 4)
            write_log("SLIPPAGE", f"滑點={slippage_pct:.4f}% 標記={mark_price} 成交={actual_close_price}",
                      symbol=symbol, detail={"mark_price": mark_price,
                                             "close_price": actual_close_price,
                                             "slippage_pct": slippage_pct})

        # 累計已實現損益（手動/強制平倉也要加總）
        pnl_from_market = (avg_entry - actual_close_price) * total_qty
        accumulated_pnl = state["symbol_realized_pnl"].get(symbol, 0)
        total_pnl = accumulated_pnl + pnl_from_market
        roe_pct = (total_pnl / margin * 100) if margin > 0 else 0
        order_count = state["symbol_sell_count"].get(symbol, 1)

        open_time = state["symbol_open_time"].get(symbol)
        record_trade_close(
            symbol=symbol, avg_entry=avg_entry, close_price=actual_close_price,
            total_qty=total_qty, total_margin=margin, total_pnl=total_pnl,
            roe_pct=roe_pct, close_reason=reason, exchange=exchange.name,
            order_count=order_count, open_time=open_time,
            mark_price=mark_price, slippage_pct=slippage_pct
        )

        candidate_info = next((c for c in state["candidate_pool"] if c["symbol"] == symbol), {})
        analytics_id = add_trade_analytics(
            symbol=symbol, avg_entry=avg_entry, close_price=actual_close_price,
            total_qty=total_qty, total_margin=margin, total_pnl=total_pnl,
            roe_pct=roe_pct, close_reason=reason,
            market_snapshot=candidate_info, exchange=exchange.name
        )
        if analytics_id:
            asyncio.create_task(ml_fill_task(exchange, analytics_id, symbol))

        write_log("CLOSE", f"平倉完成 PnL={total_pnl:.4f} ROE={roe_pct:.2f}%",
                  symbol=symbol, detail={"avg_entry": avg_entry,
                                         "close_price": actual_close_price,
                                         "total_pnl": round(total_pnl, 4),
                                         "roe_pct": round(roe_pct, 2),
                                         "reason": reason})

    try:
        await exchange.cancel_all_orders(symbol)
    except Exception as e:
        logger.error(f"取消掛單失敗 {symbol}: {e}")

    _clear_symbol_state(symbol)


def _clear_symbol_state(symbol: str):
    state["tp_sl_orders"].pop(symbol, None)
    state["hidden_grids"].pop(symbol, None)
    state["known_fills"].pop(symbol, None)
    state["_binance_positions_cache"].pop(symbol, None)
    state["triggered_symbols"].discard(symbol)
    state["symbol_open_paused"].discard(symbol)
    state["pending_open"].discard(symbol)
    state["symbol_last_close_time"][symbol] = time.time()  # 記錄平倉時間供冷卻期使用
    state["symbol_sell_count"].pop(symbol, None)
    state["symbol_total_margin"].pop(symbol, None)
    state["symbol_avg_entry"].pop(symbol, None)
    state["symbol_realized_pnl"].pop(symbol, None)
    state["closing_symbols"].discard(symbol)
    state["black_k_targets"].pop(symbol, None)
    state["black_k_last_k_time"].pop(symbol, None)
    state["symbol_sl_order"].pop(symbol, None)
    state["symbol_open_time"].pop(symbol, None)
    state["tp_tier1_done"].discard(symbol)
    state["tp_tier2_guard"].pop(symbol, None)
    state["margin_pause"] = False


# ===== 黑K偵測 =====

async def check_black_k(exchange: BaseExchange, symbol: str, cfg: dict = None) -> float | None:
    """偵測黑K，回傳目標掛單價（前幾根最高點），None=不觸發"""
    try:
        klines = await exchange.get_klines(symbol, "1m", limit=10)
    except Exception:
        return None
    if not klines or len(klines) < 4:
        return None

    last_k = klines[-2]
    k_open_time = last_k[0]
    open_p = float(last_k[1])
    high_p = float(last_k[2])
    close_p = float(last_k[4])

    # 防同一根K棒重複觸發
    if state["black_k_last_k_time"].get(symbol) == k_open_time:
        return None
    # 必須是陰線
    if close_p >= open_p:
        return None

    body_pct = round((open_p - close_p) / open_p * 100, 3)

    # 濾網：斜率過陡 AND 高點在上軌上方 → 擋
    if cfg:
        max_slope = cfg.get("black_k_max_upper_slope_pct", 0.05)
        lookback = cfg.get("black_k_upper_slope_lookback", 5)
        slope = get_upper_1m_slope(symbol, lookback)
        slope_too_steep = slope is not None and slope > max_slope

        if slope_too_steep and cfg.get("black_k_require_below_upper", True):
            upper_1m = get_upper_1m(symbol)
            if upper_1m and high_p >= upper_1m:
                write_log("BLACK_K_SKIP",
                          f"斜率{slope:.4f}%/根過陡且高點{high_p}在上軌{upper_1m:.6f}上方，跳過",
                          symbol=symbol)
                return None

    state["black_k_last_k_time"][symbol] = k_open_time
    three_ks = klines[-4:-1]
    highest = max(float(k[2]) for k in three_ks)

    write_log("BLACK_K", f"黑K確認，目標={highest} body={body_pct}%", symbol=symbol,
              detail={"open": open_p, "close": close_p, "high": high_p,
                      "body_pct": body_pct, "highest": highest})
    return highest


# ===== 持倉保護（漲幅暫停加碼 + 分階停損）=====

async def check_position_protection(exchange: BaseExchange, cfg: dict, symbol: str):
    """
    兩層保護，每輪都跑：
    1. 現價上漲超過 pause_open_rise_pct% → 停止對該幣加碼（可恢復）
    2. 本金虧損達三個門檻 → 分階停損（限價單，各觸發一次）
    """
    cached_pos = state["_binance_positions_cache"].get(symbol)
    if not cached_pos:
        return

    current_price = get_cached_price(symbol)
    if not current_price:
        return

    avg_entry = cached_pos["avg_entry"]
    if avg_entry <= 0:
        return

    # === 層1：漲幅暫停加碼（可恢復）===
    pause_rise_pct = cfg.get("pause_open_rise_pct", 2.0)
    rise_pct = (current_price - avg_entry) / avg_entry * 100

    if rise_pct >= pause_rise_pct:
        if symbol not in state["symbol_open_paused"]:
            state["symbol_open_paused"].add(symbol)
            write_log("PAUSE_OPEN", f"現價上漲{rise_pct:.2f}%超過{pause_rise_pct}%，暫停{symbol}加碼",
                      symbol=symbol, detail={"avg_entry": avg_entry,
                                             "current_price": current_price,
                                             "rise_pct": round(rise_pct, 3)})
    else:
        if symbol in state["symbol_open_paused"]:
            state["symbol_open_paused"].discard(symbol)
            write_log("RESUME_OPEN", f"現價回落{rise_pct:.2f}%，{symbol}恢復加碼",
                      symbol=symbol, detail={"avg_entry": avg_entry,
                                             "current_price": current_price,
                                             "rise_pct": round(rise_pct, 3)})




# ===== 候選池 =====

async def scan_candidates(cfg: dict, scanner_data: list = None) -> list:
    if not scanner_data:
        return []
    candidates = []
    for item in scanner_data:
        try:
            sym = item.get("full_symbol", "")
            if not sym:
                raw = item.get("symbol", "")
                sym = raw if "USDT" in raw else raw + "USDT"
            candidates.append({
                "symbol": sym,
                "price": float(item.get("price", 0)),
                "upper_15m": float(item.get("upper", 0)),
                "dist_15m": float(item.get("dist_to_upper", item.get("dist_to_upper_pct", 0))),
                "dist_1h": float(item.get("dist_1h_pct", 0)) if item.get("dist_1h_pct") is not None else 0,
                "band_width_pct": float(item.get("band_width_pct", 0)),
                "volume_usdt": float(item.get("volume_usdt", 0)),
                "prev_high_score": float(item.get("prev_high_score", 0)),
                "funding_rate": item.get("funding_rate"),
                "btc_change_1h": item.get("btc_change_1h"),
            })
        except Exception:
            continue
    write_log("SCAN", f"候選池更新 {len(candidates)} 個",
              detail={"from_scanner": len(scanner_data), "candidates": len(candidates)})
    return candidates


# ===== Reset =====

async def reset_system(exchange: BaseExchange, cfg: dict) -> dict:
    logger.info("🔄 Reset 開始")
    all_positions = await get_all_binance_positions(exchange)
    for symbol in list(all_positions.keys()):
        try:
            await exchange.cancel_all_orders(symbol)
        except Exception as e:
            logger.error(f"取消掛單失敗 {symbol}: {e}")

    state["tp_sl_orders"].clear()
    state["hidden_grids"].clear()
    state["known_fills"].clear()
    state["triggered_symbols"].clear()
    state["symbol_setup_done"].clear()
    state["symbol_filters_cache"].clear()
    state["balance_cache"] = None
    state["closing_symbols"].discard(symbol)
    state["black_k_targets"].pop(symbol, None)
    state["black_k_last_k_time"].pop(symbol, None)
    state["symbol_sl_order"].pop(symbol, None)
    state["symbol_open_time"].pop(symbol, None)
    state["tp_tier1_done"].discard(symbol)
    state["tp_tier2_guard"].pop(symbol, None)
    state["margin_pause"] = False
    state["_binance_positions_cache"] = all_positions

    for symbol in all_positions.keys():
        await place_tp_sl(exchange, cfg, symbol)
        await place_sl(exchange, cfg, symbol)

    write_log("RESET", f"Reset完成，持倉幣種: {list(all_positions.keys())}")
    return {"status": "ok", "open_symbols": list(all_positions.keys())}


# ===== 暫停 =====

async def handle_pause(exchange: BaseExchange, cfg: dict):
    for symbol in state["_binance_positions_cache"].keys():
        try:
            open_orders = await exchange.get_open_orders(symbol)
            protected = set(state["tp_sl_orders"].get(symbol, {}).values())
            for order in open_orders:
                oid = str(order["orderId"])
                if oid not in protected:
                    await exchange.cancel_order(symbol, oid)
            state["hidden_grids"].pop(symbol, None)
        except Exception as e:
            logger.error(f"暫停處理失敗 {symbol}: {e}")
    write_log("PAUSE", "系統暫停，已取消開倉掛單，保留止盈止損")


# ===== 主循環 =====

async def trading_loop():
    logger.info("🚀 交易引擎 v4 啟動（WebSocket + 多交易所架構）")

    cfg = load_config()
    exchange = get_exchange(cfg)
    logger.info(f"交易所：{exchange.name} | testnet={cfg.get('testnet', True)}")

    # 啟動時 REST 初始化（只做一次）
    await refresh_all_filters(exchange)
    all_pos = await get_all_binance_positions(exchange)
    state["_binance_positions_cache"] = all_pos
    logger.info(f"啟動時持倉: {list(all_pos.keys())}")

    try:
        balance = await exchange.get_balance()
        if balance:
            state["balance_cache"] = balance
            state["balance_cache_time"] = time.time()
    except Exception as e:
        logger.error(f"啟動時餘額取得失敗: {e}")

    # 啟動 WS tasks
    asyncio.create_task(ws_price_stream(exchange))
    asyncio.create_task(ws_user_stream(exchange, cfg))

    # 等價格 WS 就緒
    for _ in range(20):
        if state["ws_price_connected"]:
            break
        await asyncio.sleep(0.5)
    logger.info("價格 WS 就緒，開始主循環")

    # Paper mode：保存初始 exchange 實例，主循環重用（避免每輪重建丟失持倉）
    from exchanges.paper import PaperExchange
    _paper_exchange = exchange if isinstance(exchange, PaperExchange) else None
    if _paper_exchange is not None:
        state["_paper_exchange"] = _paper_exchange  # 供 app.py 強制平倉使用

    while True:
        try:
            cfg = load_config()
            if not cfg.get("system_running", True):
                await asyncio.sleep(5)
                continue

            # Paper mode 重用同一實例，正式版每輪重建（可能切換 testnet/live）
            if _paper_exchange is not None:
                exchange = _paper_exchange
            else:
                exchange = get_exchange(cfg)

            open_syms = set(state["_binance_positions_cache"].keys())

            # Paper mode：每輪檢查掛單是否應該成交（補漏：掛單後價格繼續移動的情況）
            if isinstance(exchange, PaperExchange):
                await exchange.check_and_trigger_orders(cfg)
                # 同步 paper 持倉到 state
                paper_pos = await exchange.get_positions()
                state["_binance_positions_cache"] = {
                    p["symbol"]: {
                        "qty": abs(float(p["positionAmt"])),
                        "avg_entry": float(p["entryPrice"]),
                        "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
                        "initial_margin": float(p.get("initialMargin", 0)),
                    } for p in paper_pos if abs(float(p["positionAmt"])) > 0
                }
                # 同步餘額
                bal = await exchange.get_balance()
                if bal:
                    state["balance_cache"] = bal

            # 1. 監控現有持倉
            for symbol in list(open_syms):
                try:
                    # 時間停損檢查
                    time_stop = cfg.get("time_stop_minutes", 0)
                    if time_stop > 0:
                        open_time_str = state["symbol_open_time"].get(symbol)
                        if open_time_str:
                            try:
                                t_open = datetime.strptime(open_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_TAIPEI)
                                elapsed = (datetime.now(TZ_TAIPEI) - t_open).total_seconds() / 60
                                if elapsed >= time_stop:
                                    write_log("TIME_STOP", f"持倉超過{time_stop}分鐘（已過{elapsed:.1f}分），時間停損出清",
                                              symbol=symbol, detail={"elapsed_min": round(elapsed, 1)})
                                    await close_symbol(exchange, cfg, symbol, reason="TIME_STOP")
                                    continue
                            except Exception as e:
                                logger.error(f"時間停損計算失敗 {symbol}: {e}")

                    await check_and_place_hidden_grids(exchange, cfg, symbol)
                    await check_position_protection(exchange, cfg, symbol)

                    # 精度殘留檢查：持倉數量極小（不足一個 step_size）→ 強制市價清除
                    pos_now = state["_binance_positions_cache"].get(symbol, {})
                    remain_qty = pos_now.get("qty", 0)
                    if remain_qty > 0:
                        filters_now = state["symbol_filters_cache"].get(symbol, {})
                        step = filters_now.get("step_size", 0.001)
                        if remain_qty < step:
                            write_log("WARN", f"精度殘留 qty={remain_qty} < step={step}，強制市價清除",
                                      symbol=symbol)
                            try:
                                await exchange.place_market_order(symbol, "BUY", remain_qty, reduce_only=True)
                            except Exception as e:
                                logger.error(f"清除精度殘留失敗 {symbol}: {e}")

                    # 黑K偵測加碼（持倉幣才需要，不限候選池）
                    if symbol not in state["black_k_targets"]:
                        target = await check_black_k(exchange, symbol, cfg)
                        if target:
                            state["black_k_targets"][symbol] = target
                            update_hidden_grids(symbol, target, cfg)

                    # 黑K目標觸價：掛限價空單
                    if symbol in state["black_k_targets"]:
                        target_price = state["black_k_targets"][symbol]
                        current_price_now = get_cached_price(symbol)
                        if current_price_now and current_price_now >= target_price * 0.9995:
                            open_syms_now = set(state["_binance_positions_cache"].keys())
                            if len(open_syms_now) <= cfg["max_symbols"]:
                                success = await try_open_position(exchange, cfg, symbol, target_price, "BLACK_K")
                                if success:
                                    state["black_k_targets"].pop(symbol, None)
                            else:
                                write_log("BLOCKED", f"持倉已滿，黑K目標跳過", symbol=symbol)
                                state["black_k_targets"].pop(symbol, None)

                    # 第二段止盈追蹤保底：價格反彈回第一段目標價 → 取消第二段，改市價全出
                    guard_price = state["tp_tier2_guard"].get(symbol)
                    if guard_price and symbol in state["tp_tier1_done"]:
                        current_price = get_cached_price(symbol)
                        if current_price and current_price >= guard_price:
                            tp2_order_id = state["tp_sl_orders"].get(symbol, {}).get("tp2_limit")
                            if tp2_order_id:
                                try:
                                    await exchange.cancel_order(symbol, tp2_order_id)
                                except Exception:
                                    pass
                            # 市價平倉剩餘部位
                            pos_now = state["_binance_positions_cache"].get(symbol, {})
                            remain_qty = pos_now.get("qty", 0)
                            if remain_qty > 0:
                                filters = state["symbol_filters_cache"].get(symbol, {})
                                remain_qty = align_qty(remain_qty, filters.get("step_size", 0.001))
                                await exchange.place_market_order(symbol, "BUY", remain_qty, reduce_only=True)
                                write_log("TP_TIER", f"第二段追蹤保底觸發，市價平剩餘 qty={remain_qty} @ {current_price}",
                                          symbol=symbol, detail={"guard_price": guard_price,
                                                                  "current_price": current_price})
                            state["tp_tier2_guard"].pop(symbol, None)
                except Exception as e:
                    logger.error(f"監控失敗 {symbol}: {e}")

            # 2. 更新候選池
            pool_refresh_sec = cfg.get("candidate_pool_refresh_min", 3) * 60
            if (time.time() - state["last_pool_scan"]) >= pool_refresh_sec and not state["paused"]:
                scanner_data = state.get("scanner_latest_result", [])
                candidates = await scan_candidates(cfg, scanner_data=scanner_data)
                state["candidate_pool"] = candidates
                state["last_pool_scan"] = time.time()

            # 2.5 批次更新1分K上軌（候選池 + 現有持倉）
            all_watch_syms = list({c["symbol"] for c in state["candidate_pool"]} | open_syms)
            await refresh_upper_1m(exchange, all_watch_syms)

            # 3. 候選池開倉監控
            if not state["paused"] and not state["margin_pause"]:
                current_open_count = len(open_syms)

                for candidate in state["candidate_pool"]:
                    sym = candidate["symbol"]
                    current_price = get_cached_price(sym)
                    if not current_price:
                        continue

                    # 開倉判斷使用1分K上軌，fallback到15分K上軌
                    upper_1m = get_upper_1m(sym)
                    upper = upper_1m or candidate["upper_15m"]
                    upper_src = "1m" if upper_1m else "15m(fallback)"
                    already_has_position = sym in open_syms
                    at_max = not already_has_position and current_open_count >= cfg["max_symbols"]

                    # 診斷日誌：記錄每個候選幣的現價和上軌（每30秒一次）
                    if int(time.time()) % 30 == 0:
                        write_log("DEBUG", f"監控中 price={current_price} upper_{upper_src}={upper} diff={(upper-current_price)/upper*100:.3f}%", symbol=sym)

                    # 觸碰上軌開倉
                    if current_price >= upper * 0.9995:
                        if sym not in state["triggered_symbols"]:
                            write_log("TRIGGER", f"觸碰上軌({upper_src}) price={current_price} upper={upper}", symbol=sym)
                            state["triggered_symbols"].add(sym)
                            if not at_max:
                                success = await try_open_position(exchange, cfg, sym, upper, "UPPER")
                                if success:
                                    current_open_count += 1
                            else:
                                write_log("BLOCKED", f"持倉已滿({current_open_count}/{cfg['max_symbols']})", symbol=sym)

                    # 離開上軌解鎖
                    if current_price < upper * 0.998 and sym in state["triggered_symbols"]:
                        if sym not in open_syms:
                            state["triggered_symbols"].discard(sym)



        except Exception as e:
            logger.error(f"主循環錯誤: {e}", exc_info=True)

        await asyncio.sleep(3)


def start_trading_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(trading_loop())
