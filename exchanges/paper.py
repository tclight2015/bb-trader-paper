"""
Paper Trading 交易所實作

繼承 BaseExchange，所有下單操作在本地模擬，不打幣安 API
價格來源：真實市場 WebSocket（!miniTicker@arr），不需要 API 金鑰

模擬成交邏輯：
- 限價單：掛單後，下一輪價格到達即視為成交
- 市價單：立即以當前快取價格成交
- Stop-Market：價格觸碰 stopPrice 即視為成交
"""

import time
import math
import logging
import asyncio
from exchanges.base import BaseExchange

logger = logging.getLogger(__name__)

LIVE_REST = "https://fapi.binance.com"
LIVE_WS   = "wss://fstream.binance.com"


class PaperExchange(BaseExchange):

    def __init__(self, leverage: int = 30):
        # Paper mode 不需要 API 金鑰
        self._order_id_counter = 10000
        self._orders = {}
        self._positions = {}
        self._balance = {
            "total": 10000.0,
            "available": 10000.0,
            "unrealized_pnl": 0.0,
            "margin_used": 0.0,
            "margin_ratio": 0.0,
        }
        self._price_ref = None
        self._default_leverage = leverage
        self._leverage_map = {}
        self._fill_callbacks = []

    def set_price_ref(self, price_cache: dict):
        """注入 price_cache 引用，讓 paper exchange 能讀到最新價格"""
        self._price_ref = price_cache

    def register_fill_callback(self, callback):
        """註冊成交回調，模擬 WS ORDER_TRADE_UPDATE 事件"""
        self._fill_callbacks.append(callback)

    def _get_price(self, symbol: str) -> float | None:
        if self._price_ref:
            return self._price_ref.get(symbol)
        return None

    def _next_order_id(self) -> int:
        self._order_id_counter += 1
        return self._order_id_counter

    # ===== 識別 =====

    @property
    def name(self) -> str:
        return "paper"

    @property
    def ws_base(self) -> str:
        return LIVE_WS  # 用真實價格 WS

    @property
    def rest_base(self) -> str:
        return LIVE_REST  # klines 等公開端點用真實的

    # ===== 帳戶 =====

    async def get_balance(self) -> dict | None:
        self._recalc_balance()
        return self._balance.copy()

    def _recalc_balance(self):
        """重新計算浮動損益和保證金使用"""
        unrealized = 0.0
        margin_used = 0.0
        for sym, pos in self._positions.items():
            qty = abs(float(pos["positionAmt"]))
            entry = float(pos["entryPrice"])
            lev = self._leverage_map.get(sym, self._default_leverage)
            current = self._get_price(sym) or entry
            # SHORT 倉：入場均價 - 現價 = 損益方向
            pnl = (entry - current) * qty
            margin = (qty * entry) / lev
            unrealized += pnl
            margin_used += margin
            pos["unRealizedProfit"] = str(round(pnl, 4))
            pos["initialMargin"] = str(round(margin, 4))

        self._balance["unrealized_pnl"] = round(unrealized, 4)
        self._balance["margin_used"] = round(margin_used, 4)
        total = self._balance["total"]
        self._balance["margin_ratio"] = round(margin_used / total * 100, 2) if total > 0 else 0
        self._balance["available"] = round(total - margin_used, 4)

    async def get_positions(self, symbol: str = None) -> list:
        self._recalc_balance()
        if symbol:
            pos = self._positions.get(symbol)
            if pos and abs(float(pos["positionAmt"])) > 0:
                return [pos]
            return []
        return [p for p in self._positions.values()
                if abs(float(p.get("positionAmt", 0))) > 0]

    # ===== 市場資料（公開端點，打真實幣安）=====

    async def get_exchange_info(self) -> dict:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{LIVE_REST}/fapi/v1/exchangeInfo",
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def get_symbol_info(self, symbol: str) -> dict | None:
        data = await self.get_exchange_info()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                return s
        return None

    async def get_price(self, symbol: str) -> float | None:
        # 優先用快取
        if self._price_ref:
            p = self._price_ref.get(symbol)
            if p:
                return p
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{LIVE_REST}/fapi/v1/ticker/price",
                             params={"symbol": symbol},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                return float(data["price"]) if "price" in data else None

    async def get_all_prices(self) -> dict:
        if self._price_ref:
            return dict(self._price_ref)
        return {}

    async def get_mark_price(self, symbol: str) -> float | None:
        """Paper mode 無真實標記價格，回傳 None"""
        return None

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 10) -> list:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{LIVE_REST}/fapi/v1/klines",
                             params={"symbol": symbol, "interval": interval, "limit": limit},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    # ===== 幣種設定（模擬）=====

    async def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> bool:
        return True  # paper mode 直接通過

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        self._leverage_map[symbol] = leverage
        logger.info(f"[PAPER] 設定槓桿 {symbol} x{leverage}")
        return {"symbol": symbol, "leverage": leverage}

    # ===== 下單（本地模擬）=====

    async def place_limit_order(self, symbol: str, side: str, quantity: float,
                                price: float, reduce_only: bool = False,
                                intent: str = "") -> dict:
        """
        intent: 'tp'=止盈（BUY掛在現價下方，跌到成交）
                'sl'=止損（BUY掛在現價上方，漲到成交）
                ''=自動判斷（SELL一律為開倉）
        """
        order_id = self._next_order_id()
        current_price = self._get_price(symbol) or price

        # 自動推斷 intent，加 0.1% 容差避免邊界誤判
        # stop_price 明顯低於現價（差距 > 0.1%）才算止盈，否則算止損
        if not intent and side == "BUY":
            threshold = current_price * 0.001
            intent = "tp" if (current_price - price) > threshold else "sl"

        order = {
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "price": price,
            "origQty": quantity,
            "executedQty": 0,
            "status": "NEW",
            "reduceOnly": reduce_only,
            "stopPrice": None,
            "intent": intent,
            "time": int(time.time() * 1000),
        }
        self._orders[order_id] = order
        logger.info(f"[PAPER] 限價單 #{order_id} {symbol} {side} @ {price} qty={quantity} intent={intent}")

        # 只對 SELL 單做立即成交檢查（開倉觸碰上軌）
        if side == "SELL" and current_price >= price:
            await self._simulate_fill(order_id, symbol, side, quantity, price, reduce_only)
            return {
                "orderId": order_id, "symbol": symbol,
                "status": "FILLED", "avgPrice": str(price),
            }

        return {"orderId": order_id, "symbol": symbol, "status": "NEW"}

    async def place_market_order(self, symbol: str, side: str, quantity: float,
                                 reduce_only: bool = False) -> dict:
        current_price = self._get_price(symbol)
        if not current_price:
            return {"code": -1, "msg": "paper: 無法取得價格"}

        order_id = self._next_order_id()
        logger.info(f"[PAPER] 市價單 #{order_id} {symbol} {side} @ {current_price} qty={quantity}")

        # 立即模擬成交
        await self._simulate_fill(order_id, symbol, side, quantity, current_price, reduce_only)

        return {
            "orderId": order_id,
            "symbol": symbol,
            "status": "FILLED",
            "avgPrice": str(current_price),
            "executedQty": str(quantity),
        }

    async def place_stop_market_order(self, symbol: str, side: str, quantity: float,
                                      stop_price: float, reduce_only: bool = True,
                                      intent: str = "") -> dict:
        order_id = self._next_order_id()
        current_price = self._get_price(symbol) or stop_price

        # 自動推斷 intent，加 0.1% 容差
        if not intent and side == "BUY":
            threshold = current_price * 0.001
            intent = "tp" if (current_price - stop_price) > threshold else "sl"

        order = {
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "price": stop_price,
            "stopPrice": stop_price,
            "origQty": quantity,
            "executedQty": 0,
            "status": "NEW",
            "reduceOnly": reduce_only,
            "intent": intent,
            "time": int(time.time() * 1000),
        }
        self._orders[order_id] = order
        logger.info(f"[PAPER] Stop-Market #{order_id} {symbol} {side} stopPrice={stop_price} qty={quantity} intent={intent}")

        return {"orderId": order_id, "symbol": symbol, "status": "NEW"}

    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        oid = int(order_id)
        if oid in self._orders:
            self._orders[oid]["status"] = "CANCELED"
            logger.info(f"[PAPER] 取消單 #{order_id} {symbol}")
        return {"orderId": order_id, "status": "CANCELED"}

    async def cancel_all_orders(self, symbol: str) -> dict:
        canceled = 0
        for oid, order in self._orders.items():
            if order["symbol"] == symbol and order["status"] == "NEW":
                order["status"] = "CANCELED"
                canceled += 1
        logger.info(f"[PAPER] 取消所有掛單 {symbol}，共 {canceled} 筆")
        return {"symbol": symbol}

    async def get_open_orders(self, symbol: str = None) -> list:
        orders = [o for o in self._orders.values() if o["status"] == "NEW"]
        if symbol:
            orders = [o for o in orders if o["symbol"] == symbol]
        return orders

    # ===== 精度工具（打真實幣安公開端點）=====

    async def get_symbol_filters(self, symbol: str) -> dict | None:
        info = await self.get_symbol_info(symbol)
        if not info:
            return None
        step_size, tick_size, min_notional = 0.001, 0.0001, 5.0
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
            elif f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
            elif f["filterType"] == "MIN_NOTIONAL":
                min_notional = float(f.get("notional", 5.0))
        return {"step_size": step_size, "tick_size": tick_size, "min_notional": min_notional}

    # ===== Listen Key（paper mode 不用）=====

    async def get_listen_key(self) -> str | None:
        return None  # paper mode 用本地模擬，不需要 listen key

    # ===== 模擬成交引擎 =====

    async def _simulate_fill(self, order_id: int, symbol: str, side: str,
                              quantity: float, fill_price: float, reduce_only: bool):
        """更新本地持倉，並觸發成交回調模擬 WS 事件"""
        order = self._orders.get(order_id)
        if order:
            order["status"] = "FILLED"
            order["executedQty"] = quantity

        lev = self._leverage_map.get(symbol, self._default_leverage)
        realized_pnl = 0.0

        if side == "SELL":
            # 開空或加碼
            pos = self._positions.get(symbol)
            if pos:
                old_qty = abs(float(pos["positionAmt"]))
                old_entry = float(pos["entryPrice"])
                new_qty = old_qty + quantity
                new_entry = (old_entry * old_qty + fill_price * quantity) / new_qty
                pos["positionAmt"] = str(-new_qty)
                pos["entryPrice"] = str(round(new_entry, 8))
            else:
                self._positions[symbol] = {
                    "symbol": symbol,
                    "positionAmt": str(-quantity),
                    "entryPrice": str(fill_price),
                    "unRealizedProfit": "0",
                    "initialMargin": str(round(quantity * fill_price / lev, 4)),
                    "leverage": str(lev),
                }

        elif side == "BUY":
            # 平倉（reduce_only）
            pos = self._positions.get(symbol)
            if pos:
                old_qty = abs(float(pos["positionAmt"]))
                old_entry = float(pos["entryPrice"])
                close_qty = min(quantity, old_qty)
                realized_pnl = (old_entry - fill_price) * close_qty
                new_qty = old_qty - close_qty

                self._balance["total"] = round(self._balance["total"] + realized_pnl, 4)

                # 精度容差：殘留量極小（< 1 個最小單位）視為完全平倉
                if new_qty <= 0 or new_qty < 0.0001 * old_qty:
                    self._positions.pop(symbol, None)
                else:
                    pos["positionAmt"] = str(-new_qty)

        self._recalc_balance()
        logger.info(f"[PAPER] 成交 {symbol} {side} @ {fill_price} qty={quantity} pnl={realized_pnl:.4f}")

        # 觸發成交回調（模擬 WS ORDER_TRADE_UPDATE）
        event = {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "s": symbol,
                "S": side,
                "i": order_id,
                "X": "FILLED",
                "ap": str(fill_price),
                "z": str(quantity),
                "rp": str(round(realized_pnl, 4)),
                "L": str(fill_price),
            }
        }
        for cb in self._fill_callbacks:
            try:
                await cb(event)
            except Exception as e:
                logger.error(f"[PAPER] 成交回調錯誤: {e}")

    async def check_and_trigger_orders(self, cfg: dict):
        """
        每輪主循環呼叫，檢查掛單是否應該成交
        限價單：現價穿越 → 成交
        Stop-Market：現價觸碰 stopPrice → 成交
        """
        for order_id, order in list(self._orders.items()):
            if order["status"] != "NEW":
                continue

            symbol = order["symbol"]
            current_price = self._get_price(symbol)
            if not current_price:
                continue

            side = order["side"]
            order_price = float(order["price"])
            qty = float(order["origQty"])
            order_type = order["type"]

            should_fill = False

            if order_type == "LIMIT":
                if side == "SELL":
                    # 開空：現價漲到掛單價
                    if current_price >= order_price:
                        should_fill = True
                elif side == "BUY":
                    intent = order.get("intent", "tp")
                    if intent == "tp":
                        # 止盈：現價跌到掛單價（下方）
                        if current_price <= order_price:
                            should_fill = True
                    else:
                        # 止損：現價漲到掛單價（上方）
                        if current_price >= order_price:
                            should_fill = True

            elif order_type == "STOP_MARKET":
                stop = float(order.get("stopPrice", order_price))
                intent = order.get("intent", "sl")
                if side == "BUY":
                    if intent == "tp":
                        # 止盈保底：現價跌到 stopPrice（SHORT倉跌到止盈價）
                        if current_price <= stop:
                            should_fill = True
                    else:
                        # 止損保底：現價漲到 stopPrice（SHORT倉漲到止損價）
                        if current_price >= stop:
                            should_fill = True

            if should_fill:
                await self._simulate_fill(
                    order_id, symbol, side, qty, current_price,
                    order.get("reduceOnly", False)
                )
                # 任何 BUY 平倉單成交後，立即取消同幣種所有 SELL 掛單
                # 防止同一輪隱形網格繼續成交造成重複開倉
                if side == "BUY" and order.get("reduceOnly", False):
                    for oid in list(self._orders.keys()):
                        o = self._orders.get(oid)
                        if o and o["symbol"] == symbol and o["status"] == "NEW" and o["side"] == "SELL":
                            o["status"] = "CANCELED"
                # 止損單成交後跳出本輪
                intent = order.get("intent", "tp")
                if side == "BUY" and intent == "sl":
                    break
