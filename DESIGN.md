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
| v4.1–v4.47 | 詳見舊 DESIGN 版本紀錄 | — |
| v4.57l | 三階停損簡化為單一止損（`place_sl`）；`next_funding_ms` 修正；篩選器診斷 log；`write_log("SCAN")` 移至 `run_scan()`；掃完立即同步候選池 | — |
| v4.58 | 修復開倉無法觸發；止損後清 hidden_grids+移除候選池；TP2 qty 改 REST 確認；reset_system symbol bug；allowed_keys 死碼移除 | — |
| v4.58-diag系列 | WS sleep(0)讓出CPU；掃描器改bb-scanner架構；config型別保護；background_scanner crash後重啟 | 診斷log尚未清除 |
| v4.58-diag16 | `_clear_symbol_state`統一清理：平倉即踢出候選池+清upper_1m_cache | — |
| v4.58-diag17 | extend_loss加`> 0`前置條件防負數誤判；config讀取加float()保護 | — |
| v4.58-diag19 | extend_loss重寫：用`realized_pnl+unrealized_pnl/total_margin`計算虧損率；邏輯改為超過基本上限才檢查虧損 | — |
| v4.58-diag20 | 日誌詳情按鈕修復：改用index傳參，不在HTML屬性塞JSON | — |
| v4.58-diag21 | **PnL double count修復**：`close_symbol`自算`pnl_from_market`+WS累積值導致損益翻倍；改為優先用WS累積值，0時才用價差估算 | — |
| v4.58-diag22 | paper mode止損後持倉未清零：精度差導致new_qty極小值>0，加0.01%容差判斷視為完全平倉 | — |
| v4.58-diag23 | **DB路徑固定bug**：同上，database.py import時Volume未掛載 | — |
| v4.58-diag24 | **DB路徑固定bug**：`database.py`在模組import時決定`DB_FILE`，若Railway Volume掛載晚於import則永遠指向容器本地路徑，重啟即清空；改為每次呼叫`_get_db_file()`動態決定。同修`api_download_db`用env var而非Volume路徑的問題 | — |

---

## 檔案結構

```
exchanges/
  __init__.py
  base.py          ← 抽象介面，定義所有方法簽名，新交易所繼承此類
  binance.py       ← 幣安期貨實作（testnet/live 切換）
  paper.py         ← Paper Trading 模擬實作
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
| 1分K上軌 | REST（每3秒批次並發） | `refresh_upper_1m()`，候選池+持倉幣 |
| TP2 持倉確認 | REST（tier1 成交後一次） | `get_position_rest()`，確保 TP2 qty 精確 |

### 3. 多交易所設計
- `exchanges/base.py` 定義所有方法簽名
- 新增交易所：在 `exchanges/` 下新增 `xxx.py`，繼承 `BaseExchange`，實作所有 abstract method
- `config.py` 加入 `"exchange": "bybit"` 即可切換，`trader.py` 不需修改

### 4. 隱形網格觸發時機
- **黑K確認後立刻建格**（以最高點為基準），不等第一張成交
- 任何 SELL 成交後重算（以實際成交價為基準），取代舊的
- 止損成交後立即清除 hidden_grids，不再加碼

### 5. 掃描 vs 開倉執行時間框架分離
- **掃描器**：用 15分K 布林通道過濾候選池
- **開倉執行**：用 1分K 布林通道判斷觸發
- 每輪主循環批次並發拉候選池+持倉幣的1分K

### 6. triggered_symbols 生命週期
- 加入：現價 >= 上軌 × 0.9995 時
- 解除（正常）：現價 < 上軌 × 0.998 且幣種未持倉
- 解除（止損）：止損成交後立即 discard
- 批量清理：候選池每次更新後，清除不在新池中且未持倉的幣種
- 目的：防止同一根上軌接觸重複開倉，同時確保離開後可重新觸發

---

## Config 欄位對照表

> ⚠️ 每次新增/修改/刪除欄位，必須同步更新此表。

### 開單設定
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `capital_per_order_pct` | 1.0 | `config.py: get_notional()` | 每單保證金佔帳戶餘額% |
| `leverage` | 30 | `trader.py: ensure_symbol_setup()` | 槓桿倍數 |
| `time_stop_minutes` | 100 | `trader.py: trading_loop()` | 第一槍開倉後X分鐘市價出清（0=停用） |

### 網格設定
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `grid_spacing_pct` | 0.2 | `trader.py: calc_hidden_grids()` | 隱形網格間距% |
| `grid_count` | 2 | `trader.py: update_hidden_grids()` | 每次成交後建立的隱形網格數量 |

### 止盈止損
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `tp_tier1_roi` | 30.0 | `trader.py: place_tp_sl()` | 第一段止盈：本金獲利達X%（SHORT 跌幅 = roi/leverage） |
| `tp_tier1_qty` | 50.0 | `trader.py: place_tp_sl()` | 第一段止盈：平倉X%倉位 |
| `tp_tier2_roi` | 40.0 | `trader.py: place_tp_sl()` | 第二段止盈：本金獲利達X%（剩餘全出） |
| `sl_loss_pct` | 40.0 | `trader.py: place_sl()` | 止損：本金虧損達X%，限價單全部平倉 |
| `sl_cooldown_minutes` | 30 | `trader.py: handle_user_event()` | 止損後冷卻分鐘數，期間不對該幣開新倉 |

### 持倉保護
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `pause_open_rise_pct` | 999.0 | `trader.py: check_position_protection()` | 現價比均入價上漲超過X%，停止該幣加碼（可恢復） |
| `force_close_capital_pct` | -90.0 | `trader.py: check_position_protection()` | ⚠️ config 有定義，未實作強制平倉邏輯 |
| `margin_usage_limit_pct` | 75.0 | `trader.py: try_open_position()` | 全帳戶保證金使用率上限，超過停止開新倉 |
| `funding_block_minutes` | 70 | `trader.py: try_open_position()` | 距資金費率結算X分鐘內不開新倉（0=停用） |

### 持倉管理
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `max_symbols` | 1 | `trader.py: try_open_position()` | 最多同時持倉幣種數 |
| `max_orders_per_symbol` | 20 | `trader.py: try_open_position()` / `check_and_place_hidden_grids()` | 每個幣種最多加碼次數 |
| `scale_up_after_order` | 10 | `trader.py: try_open_position()` | 第幾筆成交後開始放大每單保證金 |
| `scale_up_multiplier` | 1.0 | `trader.py: try_open_position()` | 放大倍數（1.0=停用） |
| `candidate_pool_size` | 10 | `app.py: run_scan()` | 候選監控池大小 |
| `pre_scan_size` | 50 | `app.py: run_scan()` | 從15分K篩選後取前N個 |
| `candidate_pool_refresh_min` | 3 | `trader.py: trading_loop()` | 候選池更新間隔（分鐘） |
| `extend_orders_max` | 20 | `trader.py: try_open_position()` | 放寬加碼上限（需配合 extend_loss_pct） |
| `extend_loss_pct` | 15.0 | `trader.py: try_open_position()` | 本金虧損在此%以內才適用放寬加碼 |

### 黑K濾網
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `black_k_require_below_upper` | true | `trader.py: check_black_k()` | 斜率過陡時高點須低於上軌才觸發 |
| `black_k_max_upper_slope_pct` | 0.03 | `trader.py: check_black_k()` | 上軌每根K最大漲幅% |
| `black_k_upper_slope_lookback` | 5 | `trader.py: check_black_k()` | 斜率計算回看根數 |

### 掃描篩選
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `min_volume_usdt` | 0 | `app.py: scan_symbol()` | 最低24H成交量（USDT），0=停用 |
| `max_dist_to_upper_pct` | 1.0 | `app.py: run_scan()` | 距15分K上軌最大距離% |
| `max_dist_1h_upper_pct` | 2.0 | `app.py: run_scan()` | 距1H上軌最大距離%（硬性過濾） |
| `min_band_width_pct` | 1.0 | `app.py: scan_symbol()` | BB帶寬最小值% |
| `max_band_width_pct` | 5.0 | `app.py: scan_symbol()` | BB帶寬最大值%，過濾已在暴漲的幣（0=停用） |
| `prev_high_min_excess_pct` | 1.0 | `app.py: run_scan()` | 前高保護：前5根中至少一根高點須超過現價X% |

### 黑名單
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `blacklist` | ["DYDX","DOGS"] | `app.py: run_scan()` | 排除不進候選池的幣種（不含USDT） |

---

## 止盈止損架構（v4.58 確認版）

### 止盈：兩段式限價單
```
第一段：avg_entry × (1 - tp1_roi / leverage / 100)，平 tp1_qty% 倉位
第二段：avg_entry × (1 - tp2_roi / leverage / 100)，平剩餘全部

第二段 qty 計算原則：
  - tier1 未成交：total_qty - align(t1_qty)
  - tier1 已成交：REST 向幣安確認實際剩餘數量（不用本地計算）

第二段保底追蹤：
  - 若價格反彈回第一段目標價 → 取消第二段限價，市價全出
```

### 止損：單一限價單
```
sl_price = avg_entry × (1 + sl_loss_pct / leverage / 100)
數量 = 持倉全部（align 後）

止損成交後：
  1. cancel_all_orders（清所有掛單含隱形網格）
  2. hidden_grids.pop（不再嘗試加碼）
  3. candidate_pool 移除該幣種
  4. triggered_symbols.discard
  5. sl_cooldown_until = now + sl_cooldown_minutes
  6. symbol_sl_triggered.add（等待完全平倉，期間不重掛任何單）
```

---

## 已解決的 Bug

| Bug | 修復版本 | 說明 |
|-----|---------|------|
| `reduce_only=False` | v2 | 止盈止損 BUY 單必須 reduce_only=True |
| IP被封後仍持續打API | v3 | 改 WebSocket |
| 停損無限循環 | v4.38 | 改為只有止損單實際成交後才標記 triggered |
| 分批平倉 PnL 只記最後一張 | v4.4 | symbol_realized_pnl 累積所有批次 |
| `next_funding_ms` 永遠為0 | v4.57d | get_funding_map 回傳值不再丟棄 |
| 篩選池一直空/日誌空 | v4.57d | 三重過濾太嚴+write_log 位置錯誤 |
| **開倉無法觸發** | **v4.58** | **triggered_symbols 未清除：幣種曾觸過上軌後永遠被鎖，候選池更新後不清除** |
| **reset_system 只清最後一個 symbol** | **v4.58** | **for loop 結束後 symbol 變數殘留，.discard/.pop 只清了最後一個** |
| **TP2 qty 精度尾巴** | **v4.58** | **tier1 成交後改用 REST 確認實際剩餘數量** |
| **止損後 hidden_grids 殘留** | **v4.58** | **止損成交後立即 pop hidden_grids，不再加碼** |
| 平倉後掛單/網格未清除 | diag15-16 | paper mode BUY成交後立即取消同幣SELL單；`_clear_symbol_state`統一清理 |
| 完全平倉路徑無冷卻期 | diag15 | 完全平倉前判斷止損並設冷卻期，在`_clear_symbol_state`前執行 |
| extend_loss誤判（負數/來源不準） | diag19 | 改用realized+unrealized PnL/total_margin計算，不依賴avg_entry |
| 日誌詳情按鈕無法點開 | diag20 | HTML屬性塞JSON遇特殊字元爆掉，改用index傳參 |
| PnL double count | diag21 | `close_symbol`自算+WS累積值相加導致翻倍，改為優先用WS累積值 |
| paper mode止損後持倉未清零 | diag22 | 精度差導致new_qty極小值>0，加0.01%容差判斷視為完全平倉 |
| **儀表板/日誌停在部署前舊資料** | **diag23** | **`database.py` import時Volume未掛載導致DB_FILE固定為容器本地路徑；改用`_get_db_file()`每次動態決定** |
| **設定頁參數儲存後不生效（抓預設值）** | **diag24** | **`config.py`同樣在import時固定路徑，Volume晚掛則`CONFIG_FILE`指向本地，儲存的設定讀不到；改用`_get_config_file()`** |
| 正式版止損跳空未成交 | diag25 | 限價止損在極端行情可能無對手方；加掛Stop-Market保底單（限價價再+0.5%）確保一定成交 |

---

## 已知設計遺漏（待處理）

| 項目 | 位置 | 說明 |
|------|------|------|
| `force_close_capital_pct` 未實作 | `trader.py: check_position_protection()` | config 有定義（-90%），但函數裡只有漲幅暫停加碼，沒有強制平倉邏輯 |
| `ACCOUNT_UPDATE` margin_used 更新 | `trader.py: handle_user_event()` | 已用 `iw` 欄位更新，但 paper mode 沒有此欄位，paper 的 margin_used 可能不準 |
| 診斷log未清除 | `trader.py: trading_loop()` | 主循環輪數log（每10輪）和POOL_DIAG（每30輪）仍在，正式版前需移除 |

---

## 待驗證問題（下一個對話串優先處理）

| 問題 | 版本 | 狀態 |
|------|------|------|
| PnL double count | diag21修 | 未驗證：`close_symbol`自算+WS累積值相加翻倍，改為優先用WS累積值 |
| 持倉清零精度 | diag22修 | 未驗證：止損單qty與持倉qty浮點差導致持倉殘留，加0.01%容差判斷 |
| extend_loss濾網 | diag19修 | 未驗證：改用realized+unrealized PnL計算虧損率 |
| 掃描器間歇停止 | 未修 | 偶爾掃描器停止後不再寫SCAN log，原因不明，background_scanner有保護但可能不夠 |
| 日誌頁不自動更新 | 未修 | 進入日誌頁後需手動按篩選，沒有自動輪詢機制 |
| ROE計算準確性 | diag21修後 | 未驗證：需要一筆完整交易（開倉→止盈/止損）確認ROE正確 |

---

## State 欄位說明

```python
state = {
    "running": bool,
    "paused": bool,
    "last_pool_scan": float,
    "margin_pause": bool,
    "candidate_pool": list,
    "scanner_latest_result": list,
    "tp_sl_orders": dict,            # symbol -> {tp1_limit, tp2_limit}
    "symbol_sl_order": dict,         # symbol -> order_id（止損限價單）
    "symbol_sl_stop_order": dict,    # symbol -> order_id（止損 Stop-Market 保底單，正式版專用）
    "symbol_sl_triggered": set,      # 止損單已觸發，等待完全平倉，不重掛任何單
    "sl_cooldown_until": dict,       # symbol -> timestamp，止損後冷卻期
    "tp_tier1_done": set,            # 第一段止盈已成交的幣種
    "tp_tier2_guard": dict,          # symbol -> 第二段追蹤保底價（=第一段目標價）
    "black_k_targets": dict,         # symbol -> target_price
    "black_k_last_k_time": dict,     # symbol -> k_open_time（防重複）
    "hidden_grids": dict,            # symbol -> [price1, price2, ...]
    "known_fills": dict,             # symbol -> set of fill_keys（防重複處理）
    "symbol_setup_done": set,        # 已完成全倉+槓桿設定的幣種
    "symbol_filters_cache": dict,    # symbol -> {step_size, tick_size}
    "symbol_sell_count": dict,       # symbol -> SELL 成交次數
    "symbol_total_margin": dict,     # symbol -> 累積總保證金
    "symbol_avg_entry": dict,        # symbol -> 最新均入價
    "symbol_realized_pnl": dict,     # symbol -> 累積已實現損益
    "symbol_open_time": dict,        # symbol -> 第一筆開倉時間（ISO string）
    "symbol_open_paused": set,       # 因漲幅超限暫停加碼的幣種
    "symbol_last_close_time": dict,  # symbol -> 上次平倉時間戳
    "pending_open": set,             # 已掛開倉單但尚未成交（防超過 max_symbols）
    "closing_symbols": set,          # 正在 close_symbol 處理中（防 handle_close_fill 重複寫DB）
    "triggered_symbols": set,        # 已觸碰上軌（防同一接觸重複開倉）
    "price_cache": dict,             # symbol -> float（WS 維護）
    "_binance_positions_cache": dict,# symbol -> {qty, avg_entry, ...}（WS 維護）
    "balance_cache": dict,           # {total, available, margin_used, ...}（WS 維護）
    "upper_1m_cache": dict,          # symbol -> {upper, middle, ts, history}
    "ws_price_connected": bool,
    "ws_user_connected": bool,
}
```

---

## WebSocket 事件處理流程

```
ACCOUNT_UPDATE
  → 更新 balance_cache（total, available）
  → 更新 _binance_positions_cache（qty, avg_entry, initial_margin）
  → 重算 margin_used（所有持倉 initial_margin 加總）

ORDER_TRADE_UPDATE（status=FILLED）
  SELL 成交（開空/加碼）
    → symbol_sell_count += 1
    → update_hidden_grids()（以成交價重算網格）
    → place_tp_sl()（重掛兩段止盈）
    → place_sl()（重掛止損）
  BUY 成交（平倉）且 realized_pnl != 0
    → 累積 symbol_realized_pnl
    → 若持倉已清空（fully_closed）：
        handle_close_fill() → record DB → _clear_symbol_state()
    → 若部分平倉：
        → 若是止損單成交：
            symbol_sl_triggered.add
            hidden_grids.pop
            cancel_all_orders
            candidate_pool 移除
            triggered_symbols.discard
            sl_cooldown_until 設定
            return（不重掛任何單）
        → 若是止盈 tier1：
            tp_tier1_done.add
            place_tp_sl()（重掛 tier2，qty 用 REST 確認）
            place_sl()
        → 其他部分平倉：
            place_tp_sl() + place_sl()
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

---

## Paper Trading 模式

### 啟用方式
- `config.py` 預設 `"paper_trading": true`
- **不需要 API 金鑰**

### 與正式版差異
| 項目 | 正式版 | Paper 版 |
|------|--------|---------|
| 價格來源 | 真實 WS `!miniTicker@arr` | 同左 |
| 掃描器 | 真實幣安公開端點 | 同左 |
| 下單 | 幣安 REST | 本地 state 模擬 |
| 成交偵測 | WS `ORDER_TRADE_UPDATE` | 主循環每輪檢查 |
| 餘額 | WS `ACCOUNT_UPDATE` | 本地 `_recalc_balance()` |
| 初始資金 | 真實帳戶 | 預設 10,000 USDT |

### Paper mode 掛單觸發邏輯
- **SELL 限價**：掛單時現價 >= 掛單價 → 立即成交
- **BUY 限價（intent=tp）**：現價 <= 掛單價 → 成交
- **BUY 限價（intent=sl）**：現價 >= 掛單價 → 成交
- **BUY Stop-Market（intent=tp）**：現價 <= stopPrice → 成交
- **BUY Stop-Market（intent=sl）**：現價 >= stopPrice → 成交

---

## 部署注意事項

- 環境變數：`BINANCE_API_KEY`、`BINANCE_API_SECRET`、`BINANCE_TESTNET`（true/false）
- Railway Volume 掛載 `/data`：config (`trading_config.json`) 和 DB (`trading.db`) 持久化
- Paper mode 重新部署：持倉/餘額歸零，歷史紀錄保留在 Railway Volume

---

## 新增交易所步驟

1. 新增 `exchanges/xxx.py`，繼承 `BaseExchange`，實作所有 `@abstractmethod`
2. 在 `trader.py: get_exchange()` 加入對應 `elif`
3. `config.py` 設定 `"exchange": "xxx"` 即可切換
ws_price_stream需要`await asyncio.sleep(0)`讓出CPU否則trading_loop會被餓死
- Wipe Volume後config重建，需重新在設定頁儲存所有參數
- 正式版切換：`paper_trading: false` + 填入真實API key

---

## 新增交易所步驟

1. 新增`exchanges/xxx.py`，繼承`BaseExchange`
2. 在`trader.py: get_exchange()`加入對應elif
3. `config.py`設定`"exchange": "xxx"`即可切換
