# BB Grid Trader — 設計文件

> **給下一個 Claude 對話串看的**
> 開始新對話前請先上傳此檔案，避免重複踩坑或覆蓋已確認的設計。
> 每次版本有任何改動，必須同步更新此文件。

---

## 版本紀錄

| 版本 | 主要改動 | 已知遺漏/問題 |
|------|---------|--------------|
| v1 | 基礎功能：BB掃描器、隱形網格、止盈止損、儀表板 | `reduce_only=False` bug；`pause_open_capital_pct` 有定義但未實作 |
| v2 | 修正 `reduce_only` 全部改 `True`；黑K確認後立刻建隱形網格 | 仍是 REST 輪詢架構，頻繁打 API 被封 IP |
| v3 | WebSocket 架構取代 REST 輪詢（價格/成交/餘額） | 無多交易所抽象；ML 資料結構不完整 |
| v4 | 多交易所抽象層（`exchanges/`）；ML補填+reward_score；漲幅暫停加碼（可恢復）；DB migrate | 見「已知設計遺漏」 |
| v4.1 | 修正 `prev_high_score` 計算邏輯（見下方說明）；Paper mode | — |

---

## 檔案結構

```
exchanges/
  __init__.py
  base.py          ← 抽象介面，定義所有方法簽名，新交易所繼承此類
  binance.py       ← 幣安期貨實作（testnet/live 切換）
trader.py          ← 主交易引擎，只用 BaseExchange 介面，不認識幣安細節
app.py             ← Flask API + BB掃描器背景執行緒
database.py        ← SQLite，只記已平倉歷史，含 ML 補填機制
config.py          ← 設定讀寫，環境變數優先於檔案
templates/
  index.html       ← 儀表板前端（單頁 SPA）
DESIGN.md          ← 本文件（不影響程式運作）
Procfile           ← Railway 啟動指令
requirements.txt
```

---

## 核心架構原則

### 1. 即時狀態以幣安為準，DB 只記歷史
- 即時持倉不存 DB，由 WebSocket `ACCOUNT_UPDATE` 事件維護 `state["_binance_positions_cache"]`
- 平倉後才寫入 `trade_history` 和 `trade_analytics`
- 重啟時用一次 REST 取得現有持倉初始化快取

### 2. WebSocket 優先，REST 最小化
| 資料類型 | 方式 | 說明 |
|---------|------|------|
| 所有幣種現價 | WS `!miniTicker@arr` | 取代輪詢 `get_all_prices()` |
| 成交事件 | WS `ORDER_TRADE_UPDATE` | 取代輪詢 `get_recent_fills()` |
| 餘額更新 | WS `ACCOUNT_UPDATE` | 取代輪詢 `get_balance()` |
| 持倉初始化 | REST（啟動時一次） | `get_all_binance_positions()` |
| 精度快取 | REST（啟動時一次，每小時更新） | `refresh_all_filters()` |
| 下單/取消 | REST（必須） | 無法用 WS 取代 |
| 黑K klines | REST（只在突破上軌時） | `check_black_k()` |

### 3. 多交易所設計
- `exchanges/base.py` 定義所有方法簽名
- 新增交易所：在 `exchanges/` 下新增 `xxx.py`，繼承 `BaseExchange`，實作所有 abstract method
- `config.py` 加入 `"exchange": "bybit"` 即可切換，`trader.py` 不需修改

### 4. 隱形網格觸發時機（v2 改動，重要）
- **黑K確認後立刻建4格**（以最高點為基準），不等第一張成交
- 原因：若最高點限價單未成交即反轉，後續下跌仍能靠隱形網格吃到
- 任何 SELL 成交後重算4格（以實際成交價為基準），取代舊的

---

## Config 欄位對照表

> ⚠️ 每次新增/修改/刪除欄位，必須同步更新此表。
> 驗證方式：每個欄位應能在程式碼中找到對應使用位置，找不到 = 設計遺漏。

### 開單設定
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `capital_per_order_pct` | 1.0 | `config.py: get_notional()` | 每單保證金佔帳戶餘額% |
| `leverage` | 30 | `trader.py: ensure_symbol_setup()` / `check_position_protection()` | 槓桿倍數 |

### 網格設定
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `grid_spacing_pct` | 0.15 | `trader.py: calc_hidden_grids()` | 隱形網格間距% |

### 止盈止損
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `take_profit_price_pct` | 1.0 | `trader.py: calc_tp_price()` | SHORT止盈：價格下跌X% |
| `force_close_price_pct` | 3.0 | `trader.py: calc_sl_price()` | SHORT止損：價格上漲X% |
| `tp_limit_pct` | 50 | `trader.py: place_tp_sl()` | 止盈止損拆單：限價單佔%，其餘為Stop-Market |

### 持倉保護
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `pause_open_rise_pct` | 2.0 | `trader.py: check_position_protection()` | 現價比均入價上漲超過X%，停止該幣加碼（回落自動恢復） |
| `force_close_capital_pct` | -90.0 | `trader.py: check_position_protection()` | 本金虧損超過X%強制平倉（負數） |
| `margin_usage_limit_pct` | 75.0 | `trader.py: try_open_position()` | 全帳戶保證金使用率上限，超過停止開新倉 |

### 持倉管理
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `max_symbols` | 3 | `trader.py: try_open_position()` / 主循環 | 最多同時持倉幣種數 |
| `candidate_pool_size` | 10 | `app.py: run_scan()` | 候選監控池大小 |
| `pre_scan_size` | 20 | `app.py: run_scan()` | 從15分K篩選後取前N個進候選池 |
| `candidate_pool_refresh_min` | 3 | `trader.py: trading_loop()` | 候選池更新間隔（分鐘） |

### 掃描篩選
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `min_volume_usdt` | 5,000,000 | `app.py: scan_symbol()` | 最低24H成交量（USDT） |
| `max_dist_to_upper_pct` | 0.5 | `app.py: run_scan()` | 距15分K上軌最大距離% |
| `max_dist_1h_upper_pct` | 1.0 | `app.py: run_scan()` ⚠️ 只排序，未硬過濾 | 距1H上軌最大距離% |
| `min_band_width_pct` | 1.0 | `app.py: scan_symbol()` | 最低BB帶寬% |
| `prev_high_lookback` | 5 | `app.py: scan_symbol()` | 前高壓力評分回看K棒數（見下方詳細說明） |
| `volume_spike_multiplier` | 3.0 | ⚠️ config 有定義，未實作 | 成交量異常倍數（超過均量N倍跳過） |
| `single_candle_max_rise_pct` | 1.0 | ⚠️ config 有定義，未實作 | 單K最大漲幅%（超過不開倉） |

### prev_high_score 計算邏輯詳解

**目標型態（高分）：** 價格從下方靠近上軌，前幾根K棒的高點壓在現價上方，形成歷史壓力區，做空勝率高。

**反例型態（低分）：** 價格一路下跌靠近上軌，前高都在現價之下，上方無壓力，容易繼續突破。

**計算方式（`app.py: scan_symbol()`）：**
```
lookback_highs = 前N根K棒的最高點列表
bars_above = 高點 > 現價 的K棒

count_score = len(bars_above) / lookback        # 比例分（0~1）
avg_excess  = 這些高點平均超過現價的%           # 幅度分
prev_high_score = count_score + avg_excess * 0.1
```

**注意：** 前高無須靠近BB上軌，超過現價即可，超過越多（壓力越強）得分越高。此分數影響候選池排序，不影響開倉觸發條件。

---

## 已解決的 Bug

| Bug | 影響版本 | 修復版本 | 說明 |
|-----|---------|---------|------|
| `reduce_only=False` | v1 | v2 | 止盈止損和平倉的 BUY 單必須 `reduce_only=True`，否則幣安視為開多倉 |
| 黑K後網格未預建 | v1 | v2 | 黑K確認後不等成交就建隱形網格，讓反轉下跌也能吃到 |
| IP被封後仍持續打API | v1-v2 | v3 | 改 WebSocket，REST 調用量大幅減少 |
| `pause_open_capital_pct` 未實作 | v1-v3 | v4 | config 有定義但 trader.py 完全沒有對應邏輯；v4 改為漲幅控制（`pause_open_rise_pct`）並實作 |

---

## 已知設計遺漏（待處理）

| 項目 | 位置 | 說明 |
|------|------|------|
| `single_candle_max_rise_pct` 未實作 | `app.py: scan_symbol()` | config 有定義，掃描器沒有用到此條件過濾 |
| `volume_spike_multiplier` 未實作 | `app.py: scan_symbol()` | config 有定義，掃描器沒有成交量異常偵測邏輯 |
| `max_dist_1h_upper_pct` 只排序不過濾 | `app.py: run_scan()` | 目前只影響候選池排序，沒有做硬性距離過濾 |
| `ACCOUNT_UPDATE` 的 `margin_used` 未更新 | `trader.py: handle_user_event()` | WS 推送沒有 `initialMargin`，`balance_cache["margin_used"]` 只在啟動時 REST 取得，之後不更新，影響保證金使用率計算準確度 |
| 部分平倉 PnL 漏記 | `trader.py: handle_close_fill()` | BUY 成交後若仍有持倉視為部分平倉，只重掛止盈止損，不記錄歷史。分批止盈時，只有最後一筆平倉才會記錄，中間批次 PnL 漏記 |

---

## State 欄位說明

```python
state = {
    "running": bool,                    # 引擎是否運行
    "paused": bool,                     # 手動暫停（不開新倉，保留現有止盈止損）
    "margin_pause": bool,               # 全帳戶保證金超限暫停
    "symbol_open_paused": set,          # 因漲幅超限暫停加碼的幣種（可自動恢復）
    "candidate_pool": list,             # 當前候選監控池
    "scanner_latest_result": list,      # 掃描器最新結果（供 trader 讀取）
    "tp_sl_orders": dict,               # symbol -> {tp_limit, tp_stop, sl_limit, sl_stop}
    "black_k_targets": dict,            # symbol -> target_price（黑K目標）
    "black_k_last_k_time": dict,        # symbol -> k_open_time（防重複）
    "hidden_grids": dict,               # symbol -> [price1, price2, ...]（隱形網格）
    "known_fills": dict,                # symbol -> set of fill_keys（防重複處理）
    "symbol_setup_done": set,           # 已完成全倉+槓桿設定的幣種
    "symbol_filters_cache": dict,       # symbol -> {step_size, tick_size}
    "price_cache": dict,                # symbol -> float（由 WS 維護）
    "_binance_positions_cache": dict,   # symbol -> {qty, avg_entry, ...}（由 WS 維護）
    "balance_cache": dict,              # {total, available, ...}（由 WS 維護）
    "ws_price_connected": bool,         # 價格 WS 連線狀態
    "ws_user_connected": bool,          # User Data WS 連線狀態
}
```

---

## WebSocket 事件處理流程

```
ACCOUNT_UPDATE
  → 更新 balance_cache（total, available）
  → 更新 _binance_positions_cache（qty, avg_entry）
  ⚠️ margin_used 不在此事件中，啟動後不更新

ORDER_TRADE_UPDATE（status=FILLED）
  SELL 成交（開空/加碼）
    → update_hidden_grids()（以成交價重算4格）
    → place_tp_sl()（重掛止盈止損）
  BUY 成交（平倉）且 realized_pnl != 0
    → 確認幣安持倉是否清空（REST）
    → 若清空：record_trade_close() + add_trade_analytics() + ml_fill_task()
    → 若未清空（部分平倉）：重掛止盈止損
```

---

## ML 資料補填設計

### reward_score 計算邏輯（`database.py: _calc_reward()`）
```
base = roe_pct
- 若平倉後1h價格繼續下跌（做空方向），代表出得太早，扣分（× 0.5）
- 持倉 < 5 分鐘：-2.0（可能誤觸）
- 持倉 > 240 分鐘：-1.0（持倉過長）
- close_reason == FORCE_CLOSE：-5.0
- close_reason == MANUAL：-1.0
```

### trade_analytics 補填時機
| 欄位 | 填入時機 |
|------|---------|
| 開倉快照（`upper_15m` 等） | 平倉時從 `candidate_pool` 取 |
| `hold_minutes` | 平倉時計算 |
| `price_1h_after` | 平倉後1小時 `ml_fill_task()` 補填 |
| `price_4h_after` | 平倉後4小時 `ml_fill_task()` 補填 |
| `reward_score` | 兩個補填都完成後自動計算 |

---

## 部署注意事項

- 環境變數：`BINANCE_API_KEY`、`BINANCE_API_SECRET`、`BINANCE_TESTNET`（true/false）
- testnet 時連 `testnet.binancefuture.com`，WS 連 `stream.binancefuture.com`
- 啟動時有3次 REST 初始化（精度快取、持倉、餘額），IP 封鎖期間會失敗但不影響後續 WS 運作
- `DESIGN.md` 不影響程式運作，Railway 部署包含與否皆可

---

## 新增交易所步驟

1. 新增 `exchanges/xxx.py`，繼承 `BaseExchange`，實作所有 `@abstractmethod`
2. 在 `trader.py: get_exchange()` 加入對應 `elif`
3. `config.py` 預設或用戶設定 `"exchange": "xxx"` 即切換
4. `trader.py` / `app.py` / `database.py` 不需要修改

---

## Paper Trading 模式

### 啟用方式
- `config.py` 預設 `"paper_trading": true`
- 或環境變數（未來可加 `PAPER_TRADING=true`）
- **不需要 API 金鑰**，Railway 環境變數可留空

### 與正式版差異
| 項目 | 正式版 | Paper 版 |
|------|--------|---------|
| 價格來源 | 真實 WS `!miniTicker@arr` | 同左（相同） |
| 掃描器 | 真實幣安公開端點 | 同左（相同） |
| 下單 | 打幣安 REST | 本地 state 模擬 |
| 成交偵測 | WS `ORDER_TRADE_UPDATE` | 主循環每輪檢查價格觸發 |
| 餘額 | WS `ACCOUNT_UPDATE` | 本地計算 |
| 初始資金 | 真實帳戶 | 預設 10,000 USDT（`PaperExchange.__init__`） |
| API 金鑰 | 必須 | 不需要 |

### 模擬成交邏輯（`exchanges/paper.py: check_and_trigger_orders()`）
- SELL 限價單：現價 >= 掛單價 → 成交
- BUY 限價單：現價 <= 掛單價 → 成交
- BUY Stop-Market：現價 >= stopPrice → 成交

### 已知模擬限制
- 不考慮滑價
- 不考慮流動性（任何數量都能成交）
- 成交時機是「下一輪主循環（10秒）」，不是精確的觸價時刻

### 儀表板識別
- nav badge 為紫色「模擬版」（正式版為橘色「幣安版」）
- 持倉卡片有紫色「模擬」標籤
- title 為「策略A｜模擬版」
