"""
幣安期貨交易所實作

繼承 BaseExchange，實作所有抽象方法
支援 testnet / live 切換
"""

import hmac
import hashlib
import time
import math
import aiohttp
import logging
from urllib.parse import urlencode
from exchanges.base import BaseExchange

logger = logging.getLogger(__name__)

TESTNET_REST = "https://testnet.binancefuture.com"
TESTNET_WS   = "wss://stream.binancefuture.com"
LIVE_REST    = "https://fapi.binance.com"
LIVE_WS      = "wss://fstream.binance.com"


class BinanceExchange(BaseExchange):

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet

    # ===== 識別 =====

    @property
    def name(self) -> str:
        return "binance"

    @property
    def ws_base(self) -> str:
        return TESTNET_WS if self.testnet else LIVE_WS

    @property
    def rest_base(self) -> str:
        return TESTNET_REST if self.testnet else LIVE_REST

    # ===== 內部工具 =====

    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        return hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()

    def _headers(self) -> dict:
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded"
        }

    async def _get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        if params is None:
            params = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
        url = self.rest_base + path
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, headers=self._headers(),
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def _post(self, path: str, params: dict = None) -> dict:
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        url = self.rest_base + path
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=params, headers=self._headers(),
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def _delete(self, path: str, params: dict = None) -> dict:
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        url = self.rest_base + path
        async with aiohttp.ClientSession() as s:
            async with s.delete(url, params=params, headers=self._headers(),
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def _put(self, path: str, params: dict = None) -> dict:
        if params is None:
            params = {}
        url = self.rest_base + path
        async with aiohttp.ClientSession() as s:
            async with s.put(url, params=params, headers=self._headers(),
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    # ===== 帳戶 =====

    async def get_balance(self) -> dict | None:
        try:
            data = await self._get("/fapi/v2/account", signed=True)
        except Exception as e:
            logger.error(f"get_balance 例外: {e}")
            return None
        if "assets" not in data:
            logger.error(f"get_balance 失敗: {str(data)[:200]}")
            return None
        for asset in data["assets"]:
            if asset["asset"] == "USDT":
                return {
                    "total": float(asset["walletBalance"]),
                    "available": float(asset["availableBalance"]),
                    "unrealized_pnl": float(asset["unrealizedProfit"]),
                    "margin_used": float(asset["initialMargin"]),
                    "margin_ratio": float(asset["maintMargin"]),
                }
        return None

    async def get_positions(self, symbol: str = None) -> list:
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = await self._get("/fapi/v2/positionRisk", params, signed=True)
        if isinstance(data, list):
            return [p for p in data if float(p.get("positionAmt", 0)) != 0]
        return []

    # ===== 市場資料 =====

    async def get_exchange_info(self) -> dict:
        return await self._get("/fapi/v1/exchangeInfo")

    async def get_symbol_info(self, symbol: str) -> dict | None:
        data = await self.get_exchange_info()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                return s
        return None

    async def get_price(self, symbol: str) -> float | None:
        data = await self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"]) if "price" in data else None

    async def get_all_prices(self) -> dict:
        data = await self._get("/fapi/v1/ticker/price")
        if isinstance(data, list):
            return {item["symbol"]: float(item["price"]) for item in data if "symbol" in item}
        return {}

    async def get_mark_price(self, symbol: str) -> float | None:
        """取得標記價格"""
        try:
            data = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
            if data and "markPrice" in data:
                return float(data["markPrice"])
        except Exception:
            pass
        return None

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 10) -> list:
        return await self._get("/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })

    # ===== 幣種設定 =====

    async def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> bool:
        result = await self._post("/fapi/v1/marginType", {
            "symbol": symbol, "marginType": margin_type
        })
        if result.get("code") == -4046:
            return True
        if result.get("code") and result.get("code") != 200:
            logger.warning(f"set_margin_type {symbol}: {result}")
            return False
        return True

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._post("/fapi/v1/leverage", {
            "symbol": symbol, "leverage": leverage
        })

    # ===== 下單 =====

    async def place_limit_order(self, symbol: str, side: str, quantity: float,
                                price: float, reduce_only: bool = False) -> dict:
        params = {
            "symbol": symbol, "side": side,
            "type": "LIMIT", "timeInForce": "GTC",
            "quantity": quantity, "price": price,
            "positionSide": "BOTH",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._post("/fapi/v1/order", params)

    async def place_market_order(self, symbol: str, side: str, quantity: float,
                                 reduce_only: bool = False) -> dict:
        params = {
            "symbol": symbol, "side": side,
            "type": "MARKET", "quantity": quantity,
            "positionSide": "BOTH",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._post("/fapi/v1/order", params)

    async def place_stop_market_order(self, symbol: str, side: str, quantity: float,
                                      stop_price: float, reduce_only: bool = True) -> dict:
        params = {
            "symbol": symbol, "side": side,
            "type": "STOP_MARKET", "quantity": quantity,
            "stopPrice": stop_price, "positionSide": "BOTH",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._post("/fapi/v1/order", params)

    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        return await self._delete("/fapi/v1/order", {
            "symbol": symbol, "orderId": order_id
        })

    async def cancel_all_orders(self, symbol: str) -> dict:
        return await self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})

    async def get_open_orders(self, symbol: str = None) -> list:
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._get("/fapi/v1/openOrders", params, signed=True)

    # ===== 精度工具 =====

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

    # ===== WebSocket Listen Key =====

    async def get_listen_key(self) -> str | None:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    self.rest_base + "/fapi/v1/listenKey",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    data = await r.json()
                    return data.get("listenKey")
        except Exception as e:
            logger.error(f"get_listen_key 失敗: {e}")
            return None

    async def keepalive_listen_key(self, listen_key: str) -> None:
        try:
            await self._put("/fapi/v1/listenKey", {"listenKey": listen_key})
        except Exception as e:
            logger.error(f"keepalive_listen_key 失敗: {e}")
