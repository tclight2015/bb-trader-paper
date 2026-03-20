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
    "black_k_targets": {},
    "black_k_last_k_time": {},
    "hidden_grids": {},
    "known_fills": {},
    "symbol_sell_count": {},    # symbol -> SELL 成交次數（開倉+加碼）
    "symbol_setup_done": set(),
    "symbol_filters_cache": {},

    "price_cache": {},
    "price_cache_time": 0,
    "balance_cache": None,
    "balance_cache_time": 0,
    "_binance_positions_cache": {},

    "triggered_symbols": set(),
    "symbol_open_paused": set(),   # 因價格上漲暫停加碼的幣種（可自動恢復）
    "ws_price_connected": False,
    "ws_user_connected": False,
}


def get_exchange(cfg) -> BaseExchange:
    """工廠函式：根據 config 建立對應交易所實例"""
    if cfg.get("paper_trading", False):
        from exchanges.paper import PaperExchange
        ex = PaperExchange()
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
            await handle_user_event(event, cfg, exchange)
            # 成交後立刻同步 paper 持倉到 state（不等下一輪主循環）
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
        for pos in update.get("P", []):
            sym = pos.get("s")
            amt = float(pos.get("pa", 0))
            if sym:
                if amt == 0:
                    state["_binance_positions_cache"].pop(sym, None)
                else:
                    state["_binance_positions_cache"][sym] = {
                        "qty": abs(amt),
                        "avg_entry": float(pos.get("ep", 0)),
                        "unrealized_pnl": float(pos.get("up", 0)),
                        "initial_margin": 0,
                    }

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
            # 累計開倉/加碼次數
            state["symbol_sell_count"][symbol] = state["symbol_sell_count"].get(symbol, 0) + 1
            update_hidden_grids(symbol, fill_price, cfg)
            await asyncio.sleep(0.5)
            await place_tp_sl(exchange, cfg, symbol)

        elif side == "BUY" and status == "FILLED" and realized_pnl != 0:
            await handle_close_fill(exchange, cfg, symbol, fill_price, fill_qty, realized_pnl)


async def handle_close_fill(exchange: BaseExchange, cfg: dict, symbol: str,
                             close_price: float, qty: float, realized_pnl: float):
    await asyncio.sleep(1)
    pos = await get_position_rest(exchange, symbol)
    if pos and pos["qty"] > 0:
        await place_tp_sl(exchange, cfg, symbol)
        return

    cached = state["_binance_positions_cache"].get(symbol, {})
    avg_entry = cached.get("avg_entry", close_price)
    margin = cached.get("initial_margin", 0)
    pnl = realized_pnl
    roe_pct = (pnl / margin * 100) if margin > 0 else 0
    order_count = state["symbol_sell_count"].get(symbol, 1)

    if avg_entry > 0 and close_price < avg_entry:
        close_reason = "TP_WS"
    elif avg_entry > 0 and close_price > avg_entry:
        close_reason = "SL_WS"
    else:
        close_reason = "CLOSE_WS"

    record_trade_close(
        symbol=symbol, avg_entry=avg_entry, close_price=close_price,
        total_qty=qty, total_margin=margin, total_pnl=pnl,
        roe_pct=roe_pct, close_reason=close_reason,
        exchange=exchange.name, order_count=order_count
    )

    candidate_info = next((c for c in state["candidate_pool"] if c["symbol"] == symbol), {})
    analytics_id = add_trade_analytics(
        symbol=symbol, avg_entry=avg_entry, close_price=close_price,
        total_qty=qty, total_margin=margin, total_pnl=pnl,
        roe_pct=roe_pct, close_reason=close_reason,
        market_snapshot=candidate_info, exchange=exchange.name
    )
    if analytics_id:
        asyncio.create_task(ml_fill_task(exchange, analytics_id, symbol))

    write_log("CLOSE", f"平倉完成(WS) PnL={pnl:.4f}", symbol=symbol,
              detail={"avg_entry": avg_entry, "close_price": close_price,
                      "pnl": round(pnl, 4), "roe_pct": round(roe_pct, 2)})
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
    grids = calc_hidden_grids(entry_price, cfg["grid_spacing_pct"], 4)
    state["hidden_grids"][symbol] = grids
    logger.info(f"隱形網格更新 {symbol}: {grids}")
    write_log("HIDDEN_GRID_UPDATE", f"隱形網格重算，基準價={entry_price}",
              symbol=symbol, detail={"entry_price": entry_price, "grids": grids})


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

    notional = get_notional(cfg, balance["total"])
    remaining = []

    for grid_price in grids:
        if current_price < grid_price:
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

def calc_tp_price(avg_entry, cfg):
    return avg_entry * (1 - cfg.get("take_profit_price_pct", 1.0) / 100)


def calc_sl_price(avg_entry, cfg):
    return avg_entry * (1 + cfg.get("force_close_price_pct", 3.0) / 100)


async def place_tp_sl(exchange: BaseExchange, cfg: dict, symbol: str):
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
    tp_price_raw = calc_tp_price(avg_entry, cfg)
    sl_price_raw = calc_sl_price(avg_entry, cfg)

    if tp_price_raw >= avg_entry or sl_price_raw <= avg_entry:
        write_log("ERROR", f"止盈止損價格方向錯誤，跳過", symbol=symbol)
        return

    filters = await get_filters_cached(exchange, symbol)
    if not filters:
        return

    tp_price = align_price(tp_price_raw, filters["tick_size"])
    sl_price = align_price(sl_price_raw, filters["tick_size"])

    # 取消所有掛單（確保殘留的舊止盈止損單都被清掉）
    try:
        await exchange.cancel_all_orders(symbol)
    except Exception:
        pass
    old = state["tp_sl_orders"].get(symbol, {})
    for order_id in old.values():
        try:
            await exchange.cancel_order(symbol, order_id)
        except Exception:
            pass
    state["tp_sl_orders"][symbol] = {}

    tp_limit_pct = cfg.get("tp_limit_pct", 50)
    limit_qty = align_qty(total_qty * (tp_limit_pct / 100), filters["step_size"])
    stop_qty = align_qty(total_qty - limit_qty, filters["step_size"])
    new_orders = {}
    tp_pct = round((avg_entry - tp_price) / avg_entry * 100, 3)
    sl_pct = round((sl_price - avg_entry) / avg_entry * 100, 3)

    if limit_qty > 0:
        r = await exchange.place_limit_order(symbol, "BUY", limit_qty, tp_price, reduce_only=True)
        if "orderId" in r:
            new_orders["tp_limit"] = str(r["orderId"])
            logger.info(f"✅ 止盈限價 {symbol} @ {tp_price} (-{tp_pct}%)")
        else:
            write_log("ERROR", f"止盈限價單失敗: {r.get('msg','')}", symbol=symbol, detail={"resp": r})

    if stop_qty > 0:
        r = await exchange.place_stop_market_order(symbol, "BUY", stop_qty, tp_price, reduce_only=True)
        if "orderId" in r:
            new_orders["tp_stop"] = str(r["orderId"])
        else:
            write_log("ERROR", f"止盈Stop單失敗: {r.get('msg','')}", symbol=symbol, detail={"resp": r})

    if limit_qty > 0:
        r = await exchange.place_limit_order(symbol, "BUY", limit_qty, sl_price, reduce_only=True)
        if "orderId" in r:
            new_orders["sl_limit"] = str(r["orderId"])
            logger.info(f"✅ 止損限價 {symbol} @ {sl_price} (+{sl_pct}%)")
        else:
            write_log("ERROR", f"止損限價單失敗: {r.get('msg','')}", symbol=symbol, detail={"resp": r})

    if stop_qty > 0:
        r = await exchange.place_stop_market_order(symbol, "BUY", stop_qty, sl_price, reduce_only=True)
        if "orderId" in r:
            new_orders["sl_stop"] = str(r["orderId"])
        else:
            write_log("ERROR", f"止損Stop單失敗: {r.get('msg','')}", symbol=symbol, detail={"resp": r})

    state["tp_sl_orders"][symbol] = new_orders
    write_log("TP_SL_ORDER",
              f"止盈止損更新 avg={avg_entry:.6f} tp={tp_price}(-{tp_pct}%) sl={sl_price}(+{sl_pct}%)",
              symbol=symbol, detail={"avg_entry": avg_entry, "tp_price": tp_price,
                                     "sl_price": sl_price, "orders": new_orders})


# ===== 開倉 =====

async def try_open_position(exchange: BaseExchange, cfg: dict, symbol: str,
                             entry_price: float, trigger_type: str = "UPPER") -> bool:
    if state["paused"] or state["margin_pause"]:
        return False

    # 該幣種因漲幅超限暫停加碼
    if symbol in state["symbol_open_paused"]:
        return False

    open_syms = set(state["_binance_positions_cache"].keys())
    if symbol not in open_syms and len(open_syms) >= cfg["max_symbols"]:
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
                      }})
    return True


# ===== 平倉（手動/強制）=====

async def close_symbol(exchange: BaseExchange, cfg: dict, symbol: str, reason: str = "TP"):
    logger.info(f"平倉 {symbol} reason={reason}")
    pos = await get_position_rest(exchange, symbol)

    if pos:
        total_qty = pos["qty"]
        avg_entry = pos["avg_entry"]
        margin = pos["initial_margin"]

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

        pnl = (avg_entry - actual_close_price) * total_qty
        roe_pct = (pnl / margin * 100) if margin > 0 else 0
        order_count = state["symbol_sell_count"].get(symbol, 1)

        record_trade_close(
            symbol=symbol, avg_entry=avg_entry, close_price=actual_close_price,
            total_qty=total_qty, total_margin=margin, total_pnl=pnl,
            roe_pct=roe_pct, close_reason=reason, exchange=exchange.name,
            order_count=order_count
        )

        candidate_info = next((c for c in state["candidate_pool"] if c["symbol"] == symbol), {})
        analytics_id = add_trade_analytics(
            symbol=symbol, avg_entry=avg_entry, close_price=actual_close_price,
            total_qty=total_qty, total_margin=margin, total_pnl=pnl,
            roe_pct=roe_pct, close_reason=reason,
            market_snapshot=candidate_info, exchange=exchange.name
        )
        if analytics_id:
            asyncio.create_task(ml_fill_task(exchange, analytics_id, symbol))

        write_log("CLOSE", f"平倉完成 PnL={pnl:.4f} ROE={roe_pct:.2f}%",
                  symbol=symbol, detail={"avg_entry": avg_entry,
                                         "close_price": actual_close_price,
                                         "total_pnl": round(pnl, 4),
                                         "roe_pct": round(roe_pct, 2),
                                         "reason": reason})

    try:
        await exchange.cancel_all_orders(symbol)
    except Exception as e:
        logger.error(f"取消掛單失敗 {symbol}: {e}")

    _clear_symbol_state(symbol)


def _clear_symbol_state(symbol: str):
    state["tp_sl_orders"].pop(symbol, None)
    state["black_k_targets"].pop(symbol, None)
    state["black_k_last_k_time"].pop(symbol, None)
    state["hidden_grids"].pop(symbol, None)
    state["known_fills"].pop(symbol, None)
    state["_binance_positions_cache"].pop(symbol, None)
    state["triggered_symbols"].discard(symbol)
    state["symbol_open_paused"].discard(symbol)
    state["symbol_sell_count"].pop(symbol, None)
    state["margin_pause"] = False


# ===== 黑K偵測 =====

async def check_black_k(exchange: BaseExchange, symbol: str) -> float | None:
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
    volume = float(last_k[5])

    if state["black_k_last_k_time"].get(symbol) == k_open_time:
        return None
    if close_p >= open_p:
        return None

    state["black_k_last_k_time"][symbol] = k_open_time
    three_ks = klines[-4:-1]
    highest = max(float(k[2]) for k in three_ks)

    body_pct = round((open_p - close_p) / open_p * 100, 3)
    logger.info(f"🖤 黑K {symbol} body={body_pct}% 目標={highest}")
    write_log("BLACK_K", f"黑K確認，目標={highest}", symbol=symbol,
              detail={"open": open_p, "close": close_p, "high": high_p,
                      "body_pct": body_pct, "volume": volume,
                      "highest": highest, "k_open_time": k_open_time})
    return highest


# ===== 持倉保護（漲幅暫停 + 強制平倉）=====

async def check_position_protection(exchange: BaseExchange, cfg: dict, symbol: str):
    """
    兩層保護，每輪都跑：
    1. 現價上漲超過 pause_open_rise_pct% → 停止對該幣加碼（可恢復）
       現價回落到觸發線以下 → 自動解除
    2. 本金虧損超過 force_close_capital_pct% → 強制平倉
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
    rise_pct = (current_price - avg_entry) / avg_entry * 100  # SHORT：上漲為正=不利

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

    # === 層2：本金虧損強制平倉 ===
    total_qty = cached_pos["qty"]
    margin = cached_pos.get("initial_margin", 0)
    if margin <= 0:
        return

    unrealized_pnl = (avg_entry - current_price) * total_qty
    roe_pct = unrealized_pnl / margin * 100
    capital_return_pct = roe_pct / cfg["leverage"]

    if capital_return_pct <= cfg["force_close_capital_pct"]:
        write_log("ROE_FORCE", f"本金虧損{capital_return_pct:.1f}%，強制平倉", symbol=symbol)
        await close_symbol(exchange, cfg, symbol, reason="FORCE_CLOSE")


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
    state["black_k_targets"].clear()
    state["black_k_last_k_time"].clear()
    state["hidden_grids"].clear()
    state["known_fills"].clear()
    state["triggered_symbols"].clear()
    state["symbol_setup_done"].clear()
    state["symbol_filters_cache"].clear()
    state["balance_cache"] = None
    state["margin_pause"] = False
    state["_binance_positions_cache"] = all_positions

    for symbol in all_positions.keys():
        await place_tp_sl(exchange, cfg, symbol)

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
                    await check_and_place_hidden_grids(exchange, cfg, symbol)
                    await check_position_protection(exchange, cfg, symbol)
                except Exception as e:
                    logger.error(f"監控失敗 {symbol}: {e}")

            # 2. 更新候選池
            pool_refresh_sec = cfg.get("candidate_pool_refresh_min", 3) * 60
            if (time.time() - state["last_pool_scan"]) >= pool_refresh_sec and not state["paused"]:
                scanner_data = state.get("scanner_latest_result", [])
                candidates = await scan_candidates(cfg, scanner_data=scanner_data)
                state["candidate_pool"] = candidates
                state["last_pool_scan"] = time.time()

            # 3. 候選池開倉監控
            if not state["paused"] and not state["margin_pause"]:
                current_open_count = len(open_syms)

                for candidate in state["candidate_pool"]:
                    sym = candidate["symbol"]
                    current_price = get_cached_price(sym)
                    if not current_price:
                        continue

                    upper = candidate["upper_15m"]
                    already_has_position = sym in open_syms
                    at_max = not already_has_position and current_open_count >= cfg["max_symbols"]

                    # 觸碰上軌開倉
                    if current_price >= upper * 0.9995:
                        if sym not in state["triggered_symbols"]:
                            write_log("TRIGGER", f"觸碰上軌 price={current_price} upper={upper}", symbol=sym)
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

                    # 突破上軌：黑K偵測
                    if current_price > upper and sym not in state["black_k_targets"]:
                        target = await check_black_k(exchange, sym)
                        if target:
                            state["black_k_targets"][sym] = target
                            update_hidden_grids(sym, target, cfg)

                    # 黑K目標觸價
                    if sym in state["black_k_targets"]:
                        target_price = state["black_k_targets"][sym]
                        if current_price >= target_price * 0.9995:
                            at_max = not already_has_position and current_open_count >= cfg["max_symbols"]
                            if not at_max:
                                success = await try_open_position(exchange, cfg, sym, target_price, "BLACK_K")
                                if success:
                                    current_open_count += 1
                                    state["black_k_targets"].pop(sym, None)
                                    state["black_k_last_k_time"].pop(sym, None)
                            else:
                                write_log("BLOCKED", f"持倉已滿({current_open_count}/{cfg['max_symbols']})", symbol=sym)

        except Exception as e:
            logger.error(f"主循環錯誤: {e}", exc_info=True)

        await asyncio.sleep(10)


def start_trading_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(trading_loop())
