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
| v4.1 | 修正 `prev_high_score` 計算邏輯；Paper mode 新增 | Paper mode 止盈止損方向 bug |
| v4.2 | Paper mode 大量 bug 修正（見已解決 Bug）；儀表板新增功能 | 見「已知設計遺漏」 |
| v4.3 | 修正 roe_pct 用累積總保證金計算；修正 avg_entry 平倉時可能為空；Paper mode 槓桿從 config 讀取 | — |
| v4.4 | 修正分批平倉 PnL 只記最後一張；累積 `symbol_realized_pnl` 確保全部批次都加總 | — |
| v4.5 | 新增 `max_orders_per_symbol`（每幣最大加碼次數，預設20）；開倉和隱形網格都檢查 | — |
| v4.6 | 隱形網格數量 `grid_count` 改為可調整（預設4）；不再寫死 | — |
| v4.7 | 修正持倉數超過 max_symbols：加入 `pending_open` 追蹤已掛單但未成交的幣種；強制平倉記錄修正 | — |
| v4.8 | 黑K邏輯限制為已持倉才觸發；paper_fill_handler 持倉同步順序修正（先同步再handle_user_event）；`close_symbol` pnl 變數名稱 bug；`max_symbols` 預設改為 1 | — |
| v4.9 | 開倉觸發改用1分K布林上軌（掃描器仍用15分K過濾候選池）；每輪主循環批次並發拉候選池+持倉幣的1分K，fallback到15分K上軌 | — |
| v4.10 | 主循環 sleep 10秒→3秒（提升觸價精準度）；`tp_limit_pct` 預設改為100（全限價止盈） | — |
| v4.11 | 修正黑K偵測條件：移除「現價>上軌」限制，改為已持倉就持續偵測（黑K確認後才啟動，跟現價位置無關）；`pause_open_rise_pct` 預設改為999（實際停用） | — |
| v4.12 | 新增加碼遞增邏輯：`scale_up_after_order`（第幾筆成交後放大）、`scale_up_multiplier`（放大倍數）；以已成交筆數判斷，掛單時才套用，預設1.0停用 | — |
| v4.13 | 黑K濾網：新增 `black_k_min_body_pct`（實體最小幅度%）和 `black_k_require_below_upper`（高點須低於1分K上軌）；防止強勢上漲型態誤觸黑K開空 | — |
| v4.14 | 修正加碼超標bug（掛單前檢查已成交次數，防止隱形網格繞過上限）；新增加碼放寬條件（`extend_orders_max`/`extend_loss_pct`）；黑K斜率濾網（`black_k_max_upper_slope_pct`/`black_k_upper_slope_lookback`）；設定頁UI重寫（分色區塊分類，checkbox支援bool欄位）；止損預設改2.5% | — |
| v4.15 | 修正平倉後隱形網格仍成交問題：`handle_close_fill` 平倉後逐一取消殘留 SELL 掛單，若已成交則補市價 BUY 平倉；`close_symbol`（手動/強制）維持取消全部掛單再平倉的原始邏輯 | — |
| v4.16 | 修正黑K無限循環bug：開倉成功後不再 pop `black_k_last_k_time`，保留防重複機制，防止同一根K棒反覆觸發黑K開倉 | — |
| v4.17 | 設定頁儲存時若 `grid_spacing_pct` 或 `grid_count` 有變更，自動對所有現有持倉取消舊網格掛單並以新設定重算 | — |
| v4.18 | 黑K濾網2+3合併：斜率過陡 AND 高點在上軌上方才擋；斜率過陡但高點在上軌下方放行；斜率不陡無論高點在哪都放行 | — |
| v4.56 | 重構候選池篩選邏輯：移除動態調整機制，改用固定config值；新篩選流程：步驟1距15分K上軌過濾→步驟2前高保護排序過濾→步驟3數量不足直接入池/過多則依1H距離排序取前N個；移除 `max_dist_1h_upper_pct` 設定欄位（1H距離改為背後自動排序）；設定頁整合「掃描與候選池」區塊 | — |
| v4.55 | market_snapshot 新增 `funding_rate`（開倉時的資金費率）和 `btc_change_1h`（BTC最近1H漲跌幅）；掃描時從 `premiumIndex` 批次取得，寫入 `trade_analytics` 供ML訓練使用 | — |
| v4.55b | 動態篩選放寬上限調整：15m=2%、1H=5%、前高=0（完全不過濾）；確保行情平靜時能持續放寬到上限 | — |
| v4.55c | 修正候選池儀表板顯示問題：掃描完成後直接 await scan_candidates 更新 candidate_pool，不再等 trading_loop 的3分鐘週期 | — |
| v4.49 | 黑名單預設新增 DOGS | — |
| v4.46 | 新增黑名單功能：設定頁「黑名單」區塊可輸入幣種（逗號分隔，不含USDT），掃描器自動排除；預設加入 DYDX | — |
| v4.45 | 新增重開倉冷卻期（`reopen_cooldown_minutes`，預設10分鐘）：平倉後N分鐘內不重複開同一幣，防止震盪市場反覆開倉；設定頁「開單設定」可調；`min_volume_usdt` 預設改為0 | — |
| v4.44 | 止盈第二段改用持倉快取實際數量（不留尾巴）；更新預設值：時間停損100分、一階停損40%全出、二三階停損門檻60%/70%比例0、放寬加碼上限20、預掃描50 | — |
| v4.43 | 精度殘留自動清除：持倉迴圈每輪檢查持倉數量是否小於最小交易單位（step_size），是則自動市價清除，防止尾巴占名額 | — |
| v4.42 | 報表改回只記最後一筆（完全平倉時才寫DB）；儀表板持倉卡片新增「已實現 ±XXU」標籤，即時顯示部分止盈/止損累積金額 | — |
| v4.41 | 報表頁調整：每日績效移至歷史交易紀錄上方；歷史紀錄只顯示最近3天（DB資料仍完整保留，供下載分析用） | — |
| v4.40 | 黑K偵測加回：v4.33 誤刪，現恢復持倉迴圈的黑K偵測（偵測到黑K→在目標價掛限價空單+建隱形網格）；設定頁加回「黑K濾網」區塊（黑K高點須低於上軌/上軌最大斜率/回看根數）；config 加回對應欄位 | — |
| v4.39 | ML追蹤時間調整：移除 `price_4h_after`，新增 `price_15m_after`/`price_30m_after`；補填時間從1h/4h改為15m/30m/60m；更符合1分K短倉策略的追蹤需求 | — |
| v4.38 | 修正停損重掛無限循環：舊邏輯「有停損單且虧損<門檻就重掛」導致每3秒觸發；改為只有停損單實際成交後才標記 `symbol_sl_triggered`，虧損回落時才重掛，且重掛後清除 flag 防重複 | — |
| v4.37 | 修正止盈精度殘留：第二段止盈數量改為總量減去第一段對齊後的數量，確保兩段加總等於總持倉，不留尾巴 | — |
| v4.36 | 三項改動：(1)停損殘留修復：最後一階停損改為平倉全部剩餘數量，不按比例；(2)ML資料補齊：trade_history加入開倉快照欄位（upper_15m/dist_15m_pct/dist_1h_pct/prev_high_score/volume_usdt），部分平倉和完全平倉都記market_snapshot和觸發ml_fill_task；(3)日誌頁新增「下載資料庫(DB)」按鈕 | — |
| v4.35 | 修正分階停損循環bug：停損單成交後不再重掛停損單（避免成交→重掛→再成交的無限循環）；停損重掛只在虧損回落時由 check_position_protection 觸發；日誌下拉新增 SL_TIER/TP_TIER/TIME_STOP/SLIPPAGE 選項 | — |
| v4.34 | 部分平倉即時寫DB（方案B累積PnL）：每次BUY成交都記一筆，PnL為累積到當下的總損益；完全平倉走原有流程；報表可即時看到每階停損/止盈紀錄 | — |
| v4.33 | 大幅簡化架構：移除黑K整套邏輯（check_black_k函數/state/config欄位/設定頁區塊）；移除未實作欄位（min_band_width_pct/volume_spike_multiplier/single_candle_max_rise_pct）；掃描器頁面簡化為純結果顯示（只留重新掃描按鈕）；設定頁掃描篩選區整合所有篩選條件（成交量/15分K距上軌/1H距上軌/前高保護） | — |
| v4.32 | 正式版滑點偵測：`close_symbol` 下市價單前抓標記價格，成交後計算滑點%；存入 DB `slippage_pct`/`mark_price` 欄位；報表平倉清單最後一欄顯示燈號（🟢<0.1% / 🟡<0.3% / 🟠<0.5% / 🔴≥0.5%），滑點可供調整倉位大小參考 | — |
| v4.31 | 新增時間停損（`time_stop_minutes`，預設0=停用）：第一槍開倉後超過X分鐘未止盈，市價出清並記DB；各幣獨立計時；設定頁「開單設定」區塊可調 | — |
| v4.30 | 儀表板持倉卡片加入即時計時器，顯示開倉至今已過時間（每秒更新，格式：X時XX分 / XX分XX秒）；`/api/positions` 新增 `open_time` 欄位 | — |
| v4.29 | 修正開倉時間顯示bug（open_time 未傳入 DB 導致開倉=平倉時間）；state 加入 `symbol_open_time` 記錄第一筆開倉時間，平倉時傳入 DB；報表新增「持倉時間」欄位（分/時顯示） | — |
| v4.28 | 分階停損改為預掛模式：SELL成交後立即預掛三張停損限價單；虧損回落到第一階門檻以下時取消並重掛（持倉重新計算比例）；移除 `symbol_sl_tiers_done`，改用 `symbol_sl_orders` 記錄三張單的 order_id；`tp_tier1_roi`/`tp_tier2_roi` 預設改為30%/40% | — |
| v4.27 | 分段止盈取代原有止盈：移除 `take_profit_price_pct`/`tp_limit_pct`；新增 `tp_tier1_roi`/`tp_tier1_qty`/`tp_tier2_roi`；第一段限價單平X%倉位，第二段限價單平剩餘；第二段啟動後追蹤保底：若價格反彈回第一段目標價則取消第二段改市價全出；止盈均以本金獲利率計算 | — |
| v4.26 | 掃描器頁新增三個欄位（15分K距上軌%/1H距上軌%/黑K最小實體%），直接在掃描器頁調整並自動儲存到config；`max_dist_1h_upper_pct` 從排序改為硬性過濾；`max_dist_to_upper_pct` 預設0.3%、`max_dist_1h_upper_pct` 預設0.5%；`black_k_min_body_pct` 從設定頁移至掃描器頁 | — |
| v4.25 | 分階停損取代原有止損：移除 `force_close_price_pct`/`force_close_capital_pct`；新增三階停損（`sl_tier1~3_loss_pct`/`close_pct`）；每階觸發時掛限價單（現價×1.001）平對應比例倉位；三階全觸發後取消所有掛單；`place_tp_sl` 移除止損掛單只保留止盈；設定頁新增「分階停損」區塊 | — |
| v4.24 | 黑K偵測移至持倉迴圈：原本在候選池迴圈偵測，持倉幣掉出候選池後就不再偵測黑K加碼；改為在持倉迴圈（第1步）對所有持倉幣執行，候選池只負責第一次觸碰上軌開倉 | — |
| v4.23 | 前高保護邏輯重寫：固定回看5根K棒，改用最大超出幅度%為主指標（`prev_high_score ≈ max_excess + count*0.2`）；候選池門檻改為 `prev_high_min_excess_pct`（預設1.0%），至少一根高點須超過現價1%才進池；移除 `prev_high_lookback` 設定欄位；設定頁同步更新 | — |
| v4.22 | 修正平倉重複寫DB：止損單WS成交後 handle_close_fill 與 close_symbol（FORCE_CLOSE）同時觸發導致報表出現兩筆；加入 `closing_symbols` 標記，close_symbol 執行中時 handle_close_fill 自動跳過 | — |
| v4.21 | 隱形網格四項改動：(1)基準價改為 max(成交價, 均入價)，避免成本下移；(2)網格價格低於1分K中軌暫緩掛出；(3)每輪主循環重試暫緩網格，回到中軌以上才掛；(4)`grid_spacing_pct` 預設0.15→0.2，`grid_count` 預設4→2 | — |
| v4.20 | 儀表板開倉筆數改為直接顯示實際成交次數（`symbol_sell_count`），不再用保證金反推 | — |
| v4.19 | 候選池篩選邏輯重寫：前高壓力改為硬性條件（`prev_high_score > 0` 才進候選池）；`pre_scan_size` 預設改30；篩選順序：15分K距離過濾 → 有前高壓力才留（按壓力大到小）→ 1H上軌距離由近到遠排序 → 取前 `candidate_pool_size` 個 | — |

---

## 檔案結構

```
exchanges/
  __init__.py
  base.py          ← 抽象介面，定義所有方法簽名，新交易所繼承此類
  binance.py       ← 幣安期貨實作（testnet/live 切換）
  paper.py         ← Paper Trading 模擬實作（僅 paper repo 有此檔）
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
| 1分K上軌 | REST（每10秒批次並發） | `refresh_upper_1m()`，候選池+持倉幣 |

### 3. 多交易所設計
- `exchanges/base.py` 定義所有方法簽名
- 新增交易所：在 `exchanges/` 下新增 `xxx.py`，繼承 `BaseExchange`，實作所有 abstract method
- `config.py` 加入 `"exchange": "bybit"` 即可切換，`trader.py` 不需修改

### 4. 隱形網格觸發時機（v2 改動，重要）
- **黑K確認後立刻建4格**（以最高點為基準），不等第一張成交
- 原因：若最高點限價單未成交即反轉，後續下跌仍能靠隱形網格吃到
- 任何 SELL 成交後重算4格（以實際成交價為基準），取代舊的

### 5. 掃描 vs 開倉執行時間框架分離（v4.9）
- **掃描器**：用 15分K 布林通道過濾候選池（過濾噪音，找大趨勢靠近上軌的幣）
- **開倉執行**：用 1分K 布林通道判斷觸發（精確捕捉實際突破時機）
- 每輪主循環（10秒）批次並發拉候選池+持倉幣的1分K，`asyncio.gather` 並發，不串行
- fallback：若 1分K 上軌取得失敗，退回使用 15分K 上軌

---

## Config 欄位對照表

> ⚠️ 每次新增/修改/刪除欄位，必須同步更新此表。
> 驗證方式：每個欄位應能在程式碼中找到對應使用位置，找不到 = 設計遺漏。

### 開單設定
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `capital_per_order_pct` | 1.0 | `config.py: get_notional()` / `app.py: /api/account` | 每單保證金佔帳戶餘額% |
| `leverage` | 30 | `trader.py: ensure_symbol_setup()` / `check_position_protection()` | 槓桿倍數 |

### 網格設定
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `grid_spacing_pct` | 0.15 | `trader.py: calc_hidden_grids()` | 隱形網格間距% |
| `grid_count` | 4 | `trader.py: update_hidden_grids()` | 每次成交後建立的隱形網格數量 |

### 止盈止損
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `tp_tier1_roi` | 10.0 | `trader.py: place_tp_sl()` | 第一段止盈：本金獲利達X%觸發 |
| `tp_tier1_qty` | 50.0 | `trader.py: place_tp_sl()` | 第一段止盈平倉X%倉位 |
| `tp_tier2_roi` | 20.0 | `trader.py: place_tp_sl()` | 第二段止盈：本金獲利達X%觸發（未到則以第一段價格保底市價出場） |
| `sl_loss_pct` | 40.0 | `trader.py: place_sl()` | 止損：本金虧損達X%，掛限價單全部平倉 |

### 持倉保護
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `pause_open_rise_pct` | 2.0 | `trader.py: check_position_protection()` | 現價比均入價上漲超過X%，停止該幣加碼（回落自動恢復） |
| `force_close_capital_pct` | -90.0 | `trader.py: check_position_protection()` | 本金虧損超過X%強制平倉（負數） |
| `margin_usage_limit_pct` | 75.0 | `trader.py: try_open_position()` | 全帳戶保證金使用率上限，超過停止開新倉 |

### 持倉管理
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `max_symbols` | 1 | `trader.py: try_open_position()` / 主循環 | 最多同時持倉幣種數 |
| `max_orders_per_symbol` | 20 | `trader.py: try_open_position()` / `check_and_place_hidden_grids()` | 每個幣種最多加碼次數（含首次開倉），達上限停止加碼 |
| `scale_up_after_order` | 10 | `trader.py: try_open_position()` / `check_and_place_hidden_grids()` | 第幾筆成交後開始放大每單保證金 |
| `scale_up_multiplier` | 1.0 | `trader.py: try_open_position()` / `check_and_place_hidden_grids()` | 放大倍數（1.0=停用） |
| `candidate_pool_size` | 10 | `app.py: run_scan()` | 候選監控池大小 |
| `pre_scan_size` | 20 | `app.py: run_scan()` | 從15分K篩選後取前N個進候選池 |
| `candidate_pool_refresh_min` | 3 | `trader.py: trading_loop()` | 候選池更新間隔（分鐘） |

### 掃描篩選
| 欄位 | 預設值 | 實作位置 | 說明 |
|------|--------|---------|------|
| `min_volume_usdt` | 5,000,000 | `app.py: scan_symbol()` | 最低24H成交量（USDT） |
| `max_dist_to_upper_pct` | 0.5 | `app.py: run_scan()` | 距15分K上軌最大距離% |
| `min_band_width_pct` | 1.0 | `app.py: scan_symbol()` | 最低BB帶寬% |
| `prev_high_min_excess_pct` | 1.0 | `app.py: run_scan()` | 前高保護門檻：前5根中至少一根高點須超過現價X%（固定5根，不再可調） |
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
| `margin_used` 啟動後不更新 | v1-v3 | v4 | `ACCOUNT_UPDATE` 的 `P` 陣列有 `iw`（initialMargin）欄位，每次持倉變動重算所有持倉的 margin_used 並寫回 balance_cache |
| 黑K後網格未預建 | v1 | v2 | 黑K確認後不等成交就建隱形網格，讓反轉下跌也能吃到 |
| IP被封後仍持續打API | v1-v2 | v3 | 改 WebSocket，REST 調用量大幅減少 |
| `pause_open_capital_pct` 未實作 | v1-v3 | v4 | config 有定義但 trader.py 完全沒有對應邏輯；v4 改為漲幅控制（`pause_open_rise_pct`）並實作 |
| Paper mode 持倉不顯示 | paper v1 | paper v2 | PaperExchange 每輪重建導致持倉消失；改為主循環重用同一實例 |
| Paper mode 止盈止損秒觸發 | paper v1-v2 | paper v3 | BUY 限價單和 Stop-Market 觸發方向全部搞反；加入 `intent`（tp/sl）區分方向，並加 0.1% 容差避免邊界誤判 |
| Paper mode 重複平倉紀錄 | paper v1 | paper v2 | `cancel_all_orders` 後重新掛止盈止損，舊單未清除導致多組同時觸發 |
| 報表筆數顯示合約數量 | v4.1 | v4.2 | `position_count` 欄位不存在顯示 undefined；改為新增 `order_count` 記錄 SELL 成交次數 |
| `close_reason` 全寫 `TP_WS` | paper v1 | paper v2 | 改為根據平倉價格判斷：低於均入價=TP_WS，高於=SL_WS |
| 掃描器時間顯示 UTC | v4.1 | v4.2 | `app.py` 的 `datetime.now()` 未帶時區；改為 `datetime.now(TZ_TAIPEI)` |
| `roe_pct` 計算只用單筆保證金 | v1-v4.2 | v4.3 | 開多槍時 roe_pct 膨脹（5槍卻只除以1槍保證金）；改用 `symbol_total_margin` 累積所有槍保證金 |
| `avg_entry` 平倉時可能為空 | v1-v4.2 | v4.3 | 平倉時 `_binance_positions_cache` 已清空，`avg_entry` 退回 `close_price`，PnL 顯示 0；改用 `symbol_avg_entry` 在每次 SELL 成交時存下均入價 |
| Paper mode 預設槓桿寫死 20 | v4.1-v4.2 | v4.3 | `PaperExchange` 預設槓桿 20 但 config 是 30，保證金計算偏高；改為從 `cfg["leverage"]` 傳入 |
| 分批平倉 PnL 只記最後一張 | v1-v4.3 | v4.4 | `tp_limit_pct=50` 時止盈分兩張，限價單那張的 PnL 沒有被記入；改用 `symbol_realized_pnl` 累積所有批次，完全平倉後才寫DB |
| 黑K邏輯在無持倉時觸發 | v1-v4.7 | v4.8 | 候選池循環中黑K偵測未檢查是否已持倉，可能以黑K當第一單；加入 `and sym in open_syms` 限制只在已持倉時追蹤 |
| Paper mode 平倉不記DB | v4.1-v4.7 | v4.8 | `paper_fill_handler` 先呼叫 `handle_user_event` 後才同步持倉，導致 `handle_close_fill` 誤判持倉未清空，跳去重掛止盈止損；改為先同步持倉再呼叫 `handle_user_event` |
| `close_symbol` 中 `pnl` 未定義 | v1-v4.7 | v4.8 | `write_log` 中誤用 `pnl` 變數，應為 `total_pnl`；修正變數名稱 |
| 黑K濾網2+3獨立導致好空點錯過 | v4.13-v4.17 | v4.18 | 舊邏輯：高點超上軌一律擋、斜率過陡一律擋；新邏輯：斜率過陡 AND 高點在上軌上方才擋，斜率過陡但高點已低於上軌放行，斜率不陡無論高點在哪都放行 |

---

## 已知設計遺漏（待處理）

| 項目 | 位置 | 說明 |
|------|------|------|
| `single_candle_max_rise_pct` 未實作 | `app.py: scan_symbol()` | config 有定義，掃描器沒有用到此條件過濾 |
| `volume_spike_multiplier` 未實作 | `app.py: scan_symbol()` | config 有定義，掃描器沒有成交量異常偵測邏輯 |
| 部分平倉 PnL 漏記 | `trader.py: handle_close_fill()` | 分批止盈時只有最後一筆平倉才記錄，中間批次 PnL 漏記 |

---

## State 欄位說明

```python
state = {
    "running": bool,                    # 引擎是否運行
    "paused": bool,                     # 手動暫停（不開新倉，保留現有止盈止損）
    "margin_pause": bool,               # 全帳戶保證金超限暫停
    "symbol_open_paused": set,          # 因漲幅超限暫停加碼的幣種（可自動恢復）
    "symbol_sell_count": dict,          # symbol -> SELL 成交次數（開倉+加碼，平倉後清除）
    "symbol_total_margin": dict,         # symbol -> 累積總保證金（每次SELL成交後累加，平倉後清除）
    "symbol_avg_entry": dict,            # symbol -> 最新均入價（每次SELL成交後從持倉快取更新，平倉後清除）
    "symbol_realized_pnl": dict,         # symbol -> 累積已實現損益（每次BUY成交後累加，完全平倉記DB後清除）
    "pending_open": set,                  # 已掛開倉單但尚未成交的幣種（計入持倉數防止超過 max_symbols）
    "candidate_pool": list,             # 當前候選監控池
    "scanner_latest_result": list,      # 掃描器最新結果（供 trader 讀取）
    "tp_sl_orders": dict,               # symbol -> {tp1_limit, tp2_limit}
    "symbol_sl_order": dict,            # symbol -> order_id（單一止損限價單）
    "tp_tier1_done": set,               # 第一段止盈已成交的幣種
    "tp_tier2_guard": dict,             # symbol -> 第二段追蹤保底價（=第一段目標價）
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

## 儀表板功能清單

### 儀表板首頁
- 帳戶統計：總餘額、可用保證金、浮動損益、持倉幣種數、每單合約價值
- 保證金使用率進度條
- **已實現盈虧卡片**：下拉選今日/7天/30天/90天，顯示損益/交易次數/勝率（台北時間）
- 持倉狀態卡片：均入價、保證金、**開倉筆數**、**浮動損益（含本金%）**、隱形網格列表
- 系統控制按鈕、候選監控池
- 出入金紀錄

### 開倉筆數計算
```
開倉筆數 = 初始保證金 / (帳戶餘額 × capital_per_order_pct%)
```
從 `state["symbol_sell_count"]` 計數，平倉後清除。

---

## DB 表結構重要欄位

### trade_history
| 欄位 | 說明 |
|------|------|
| `order_count` | SELL 成交次數（開倉+加碼筆數），預設 1 |
| `close_reason` | TP_WS/SL_WS/CLOSE_WS/MANUAL/FORCE_CLOSE |
| `exchange` | 交易所名稱（binance/paper） |

### trade_analytics
| 欄位 | 說明 |
|------|------|
| `filled_1h` / `filled_4h` | 補填完成標記 |
| `reward_score` | 兩個補填都完成後自動計算 |

---

## WebSocket 事件處理流程

```
ACCOUNT_UPDATE
  → 更新 balance_cache（total, available）
  → 更新 _binance_positions_cache（qty, avg_entry）

ORDER_TRADE_UPDATE（status=FILLED）
  SELL 成交（開空/加碼）
    → symbol_sell_count[symbol] += 1
    → update_hidden_grids()（以成交價重算4格）
    → place_tp_sl()（重掛止盈止損）
  BUY 成交（平倉）且 realized_pnl != 0
    → 確認幣安持倉是否清空（REST）
    → 若清空：record_trade_close(order_count) + add_trade_analytics() + ml_fill_task()
    → 若未清空（部分平倉）：重掛止盈止損
```

---

## ML 資料補填設計

### reward_score 計算邏輯（`database.py: _calc_reward()`）
```
base = roe_pct
- 30分鐘內獲利出場：+1.0（快速乾淨獲利）
- 持倉 > 60 分鐘：-0.5
- 持倉 > 120 分鐘：-1.5（策略失效或被套）
- 持倉 < 5 分鐘且虧損：-2.0（可能誤觸）
- close_reason == FORCE_CLOSE：-5.0（風控失守）
- close_reason == MANUAL：-0.5
- 平倉後1h價格反彈超過1%（做空出場後漲回）：-rebound_pct × 0.3（開倉時機差）
- 平倉後繼續下跌：不扣分（快出是好的，不懲罰出得太早）
```

---

## Paper Trading 模式

### 啟用方式
- `config.py` 預設 `"paper_trading": true`
- **不需要 API 金鑰**，Railway 環境變數可留空

### 與正式版差異
| 項目 | 正式版 | Paper 版 |
|------|--------|---------|
| 價格來源 | 真實 WS `!miniTicker@arr` | 同左（相同） |
| 掃描器 | 真實幣安公開端點 | 同左（相同） |
| 下單 | 打幣安 REST | 本地 state 模擬 |
| 成交偵測 | WS `ORDER_TRADE_UPDATE` | 主循環每輪檢查 + 掛單立即成交 |
| 餘額 | WS `ACCOUNT_UPDATE` | 本地 `_recalc_balance()` |
| 初始資金 | 真實帳戶 | 預設 10,000 USDT |
| API 金鑰 | 必須 | 不需要 |

### Paper mode 掛單觸發邏輯（`exchanges/paper.py`）
- **SELL 限價**：掛單時現價已 >= 掛單價 → 立即成交（模擬開倉觸碰上軌）
- **BUY 限價（止盈 intent=tp）**：現價 <= 掛單價 → 成交（跌到止盈價）
- **BUY 限價（止損 intent=sl）**：現價 >= 掛單價 → 成交（漲到止損價）
- **BUY Stop-Market（intent=tp）**：現價 <= stopPrice → 成交
- **BUY Stop-Market（intent=sl）**：現價 >= stopPrice → 成交
- `intent` 在掛單時自動推斷：掛單價比現價低超過 0.1% → tp，否則 → sl

### 已知模擬限制
- 不考慮滑價、流動性
- 成交時機最快是「下一輪主循環（10秒）」，SELL 開倉為立即成交

### 儀表板識別
- nav badge 紫色「模擬版」（正式版橘色「幣安版」）
- 持倉卡片有紫色「模擬」標籤

---

## 部署注意事項

- 環境變數：`BINANCE_API_KEY`、`BINANCE_API_SECRET`、`BINANCE_TESTNET`（true/false）
- testnet 時連 `testnet.binancefuture.com`，WS 連 `stream.binancefuture.com`
- 啟動時有3次 REST 初始化（精度快取、持倉、餘額），IP 封鎖期間會失敗但不影響後續 WS 運作
- `DESIGN.md` 不影響程式運作，Railway 部署包含與否皆可
- Paper mode 重新部署：持倉/餘額歸零，歷史紀錄保留在 Railway Volume

---

## 新增交易所步驟

1. 新增 `exchanges/xxx.py`，繼承 `BaseExchange`，實作所有 `@abstractmethod`
2. 在 `trader.py: get_exchange()` 加入對應 `elif`
3. `config.py` 預設或用戶設定 `"exchange": "xxx"` 即切換
4. `trader.py` / `app.py` / `database.py` 不需要修改

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
| `tp_tier1_roi` | 10.0 | `trader.py: place_tp_sl()` | 第一段止盈：本金獲利達X%觸發 |
| `tp_tier1_qty` | 50.0 | `trader.py: place_tp_sl()` | 第一段止盈平倉X%倉位 |
| `tp_tier2_roi` | 20.0 | `trader.py: place_tp_sl()` | 第二段止盈：本金獲利達X%觸發（未到則以第一段價格保底市價出場） |
| `sl_loss_pct` | 40.0 | `trader.py: place_sl()` | 止損：本金虧損達X%，掛限價單全部平倉 |

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
| `min_band_width_pct` | 1.0 | `app.py: scan_symbol()` | 最低BB帶寬% |
| `prev_high_min_excess_pct` | 1.0 | `app.py: run_scan()` | 前高保護門檻：前5根中至少一根高點須超過現價X%（固定5根，不再可調） |
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
| `margin_used` 啟動後不更新 | v1-v3 | v4 | `ACCOUNT_UPDATE` 的 `P` 陣列有 `iw`（initialMargin）欄位，每次持倉變動重算所有持倉的 margin_used 並寫回 balance_cache |
| 黑K後網格未預建 | v1 | v2 | 黑K確認後不等成交就建隱形網格，讓反轉下跌也能吃到 |
| IP被封後仍持續打API | v1-v2 | v3 | 改 WebSocket，REST 調用量大幅減少 |
| `pause_open_capital_pct` 未實作 | v1-v3 | v4 | config 有定義但 trader.py 完全沒有對應邏輯；v4 改為漲幅控制（`pause_open_rise_pct`）並實作 |

---

## 已知設計遺漏（待處理）

| 項目 | 位置 | 說明 |
|------|------|------|
| `single_candle_max_rise_pct` 未實作 | `app.py: scan_symbol()` | config 有定義，掃描器沒有用到此條件過濾 |
| `volume_spike_multiplier` 未實作 | `app.py: scan_symbol()` | config 有定義，掃描器沒有成交量異常偵測邏輯 |

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
    "tp_sl_orders": dict,               # symbol -> {tp1_limit, tp2_limit}
    "symbol_sl_order": dict,            # symbol -> order_id（單一止損限價單）
    "tp_tier1_done": set,               # 第一段止盈已成交的幣種
    "tp_tier2_guard": dict,             # symbol -> 第二段追蹤保底價（=第一段目標價）
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
- 30分鐘內獲利出場：+1.0（快速乾淨獲利）
- 持倉 > 60 分鐘：-0.5
- 持倉 > 120 分鐘：-1.5（策略失效或被套）
- 持倉 < 5 分鐘且虧損：-2.0（可能誤觸）
- close_reason == FORCE_CLOSE：-5.0（風控失守）
- close_reason == MANUAL：-0.5
- 平倉後1h價格反彈超過1%（做空出場後漲回）：-rebound_pct × 0.3（開倉時機差）
- 平倉後繼續下跌：不扣分（快出是好的，不懲罰出得太早）
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
