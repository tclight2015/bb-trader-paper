import json
import os

CONFIG_FILE = "trading_config.json"

DEFAULT_CONFIG = {
    # === 帳戶設定（從環境變數讀取）===
    "api_key": os.environ.get("BINANCE_API_KEY", ""),
    "api_secret": os.environ.get("BINANCE_API_SECRET", ""),
    "testnet": os.environ.get("BINANCE_TESTNET", "true").lower() == "true",

    # === 開單設定 ===
    "capital_per_order_pct": 1.0,
    "time_stop_minutes": 0,            # 時間停損：第一槍後X分鐘市價出清（0=停用）
    "leverage": 30,                     # 槓桿倍數
    # notional = 帳戶餘額 * capital_per_order_pct% * leverage

    # === 網格設定 ===
    "grid_spacing_pct": 0.2,           # 網格間距%（隱形網格間距）
    "grid_count": 2,                   # 每次重算後建立的隱形網格數量

    # === 持倉管理 ===
    "max_symbols": 1,                   # 最多同時持倉幣種數
    "max_orders_per_symbol": 20,        # 每個幣種最多加碼次數（含首次開倉）
    "scale_up_after_order": 10,         # 第幾筆成交後開始放大每單保證金
    "scale_up_multiplier": 1.0,         # 放大倍數（1.0=停用）
    "candidate_pool_size": 10,          # 候選監控池大小
    "pre_scan_size": 20,               # 從15分K取前N個再篩候選池

    # === 分段止盈（基於本金獲利率%）===
    "tp_tier1_roi": 30.0,              # 第一段止盈：本金獲利達X%
    "tp_tier1_qty": 50.0,              # 第一段止盈：平倉X%倉位
    "tp_tier2_roi": 40.0,              # 第二段止盈：本金獲利達X%（剩餘全出，未到則以第一段保底）

    # === 開倉保護（基於本金%）===
    "pause_open_rise_pct": 999.0,      # 暫停對該幣加碼：現價比均入價上漲X%（可恢復）

    # === 分階停損（基於本金虧損率%）===
    "sl_tier1_loss_pct": 60.0,         # 第一階停損：本金虧損達X%
    "sl_tier1_close_pct": 33.0,        # 第一階停損：平倉X%倉位
    "sl_tier2_loss_pct": 75.0,         # 第二階停損：本金虧損達X%
    "sl_tier2_close_pct": 33.0,        # 第二階停損：平倉X%倉位
    "sl_tier3_loss_pct": 90.0,         # 第三階停損：本金虧損達X%
    "sl_tier3_close_pct": 34.0,        # 第三階停損：平倉X%倉位（剩餘全部）

    # === 保證金水位保護 ===
    "margin_usage_limit_pct": 75.0,    # 全帳戶保證金使用率上限%

    # === 掃描篩選條件 ===
    "min_volume_usdt": 5_000_000,      # 最低24H成交量（USDT），0=無限制
    "max_dist_to_upper_pct": 0.3,      # 距15分K上軌最大距離%（硬性條件）
    "max_dist_1h_upper_pct": 0.5,      # 距1H上軌最大距離%（硬性條件）
    "prev_high_min_excess_pct": 1.0,   # 前高保護：前5根中至少一根高點須超過現價X%

    # === 異常偵測 ===

    # === 黑K濾網 ===

    # === 加碼放寬 ===
    "extend_orders_max": 25,           # 虧損未達門檻時，可放寬加碼到幾筆
    "extend_loss_pct": 15.0,           # 本金虧損在此%以內才適用放寬（正數，例如15=虧15%內）

    # === 系統設定 ===
    "system_running": True,
    "candidate_pool_refresh_min": 3,   # 候選池更新間隔（分鐘）
    "paper_trading": True,             # Paper mode：本地模擬下單，不打幣安
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        cfg = DEFAULT_CONFIG.copy()
        cfg.update(saved)
        # 環境變數優先
        env_key = os.environ.get("BINANCE_API_KEY", "")
        env_secret = os.environ.get("BINANCE_API_SECRET", "")
        env_testnet = os.environ.get("BINANCE_TESTNET", "")
        if env_key:
            cfg["api_key"] = env_key
        if env_secret:
            cfg["api_secret"] = env_secret
        if env_testnet:
            cfg["testnet"] = env_testnet.lower() == "true"
        return cfg
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    # 不存 api_key/secret 到檔案（從環境變數讀）
    save_data = {k: v for k, v in cfg.items()
                 if k not in ["api_key", "api_secret"]}
    with open(CONFIG_FILE, "w") as f:
        json.dump(save_data, f, indent=2, ensure_ascii=False)


def get_notional(cfg, account_balance):
    """計算每單名義價值"""
    margin_per_order = account_balance * (cfg["capital_per_order_pct"] / 100)
    return margin_per_order * cfg["leverage"]
