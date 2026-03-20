"""
交易所抽象介面

新增交易所只需：
1. 在 exchanges/ 下新增 xxx.py，繼承 BaseExchange
2. 實作所有 abstract method
3. trader.py 不需要任何修改

命名規範：
- symbol 格式統一為 "BTCUSDT"（各交易所自行轉換）
- side: "BUY" / "SELL"
- 餘額回傳格式統一（見 get_balance）
"""

from abc import ABC, abstractmethod


class BaseExchange(ABC):

    # ===== 帳戶 =====

    @abstractmethod
    async def get_balance(self) -> dict | None:
        """
        回傳:
        {
            "total": float,       # 錢包餘額
            "available": float,   # 可用保證金
            "unrealized_pnl": float,
            "margin_used": float,
            "margin_ratio": float,
        }
        """
        pass

    @abstractmethod
    async def get_positions(self, symbol: str = None) -> list:
        """
        回傳有持倉的列表，每筆格式：
        {
            "symbol": str,
            "positionAmt": str,   # 負數=空倉
            "entryPrice": str,
            "unRealizedProfit": str,
            "initialMargin": str,
        }
        """
        pass

    # ===== 市場資料 =====

    @abstractmethod
    async def get_exchange_info(self) -> dict:
        """回傳交易所規格資訊（含 symbols 精度）"""
        pass

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> dict | None:
        pass

    @abstractmethod
    async def get_price(self, symbol: str) -> float | None:
        pass

    @abstractmethod
    async def get_all_prices(self) -> dict:
        """回傳 {symbol: float}"""
        pass

    @abstractmethod
    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 10) -> list:
        """回傳 K 棒列表，每筆 [open_time, open, high, low, close, volume, ...]"""
        pass

    # ===== 幣種設定 =====

    @abstractmethod
    async def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> bool:
        """設定全倉/逐倉，已是目標模式時回傳 True"""
        pass

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        pass

    # ===== 下單 =====

    @abstractmethod
    async def place_limit_order(self, symbol: str, side: str, quantity: float,
                                price: float, reduce_only: bool = False) -> dict:
        """掛限價單，回傳含 orderId 的 dict"""
        pass

    @abstractmethod
    async def place_market_order(self, symbol: str, side: str, quantity: float,
                                 reduce_only: bool = False) -> dict:
        pass

    @abstractmethod
    async def place_stop_market_order(self, symbol: str, side: str, quantity: float,
                                      stop_price: float, reduce_only: bool = True) -> dict:
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        pass

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> dict:
        pass

    @abstractmethod
    async def get_open_orders(self, symbol: str = None) -> list:
        pass

    # ===== 精度工具 =====

    @abstractmethod
    async def get_symbol_filters(self, symbol: str) -> dict | None:
        """
        回傳:
        {
            "step_size": float,    # 數量精度
            "tick_size": float,    # 價格精度
            "min_notional": float,
        }
        """
        pass

    # ===== WebSocket Listen Key（只有需要的交易所實作）=====

    async def get_listen_key(self) -> str | None:
        """取得 User Data Stream listen key，不支援的交易所回傳 None"""
        return None

    async def keepalive_listen_key(self, listen_key: str) -> None:
        """延長 listen key 有效期"""
        pass

    # ===== 識別 =====

    @property
    @abstractmethod
    def name(self) -> str:
        """交易所名稱，例如 'binance', 'bybit'"""
        pass

    @property
    @abstractmethod
    def ws_base(self) -> str:
        """WebSocket base URL"""
        pass

    @property
    @abstractmethod
    def rest_base(self) -> str:
        """REST API base URL"""
        pass
