import asyncio
import aiohttp
import json
import time
import math
import os
import threading
import logging
from flask import Flask, jsonify, render_template, request, Response
from datetime import datetime, timezone, timedelta
TZ_TAIPEI = timezone(timedelta(hours=8))
from config import load_config, save_config, get_notional
from database import (
    init_db, get_trade_history, get_daily_pnl, get_cumulative_pnl,
    add_capital_log, get_capital_log,
    get_logs, get_log_summary, write_log, export_logs_json, get_analytics
)
from trader import state, start_trading_loop, close_symbol, get_client, get_balance_cached

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
BINANCE_BASE = "https://fapi.binance.com"

# ===== Scanner Cache =====
scanner_cache = {
    "data": [],
    "last_updated": None,
    "is_scanning": False
}


async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()
    except Exception:
        return None


async def get_all_symbols(session):
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    if not data or "symbols" not in data:
        return []
    symbols = []
    for s in data["symbols"]:
        try:
            if (s.get("contractType") == "PERPETUAL" and
                    s.get("quoteAsset") == "USDT" and
                    s.get("status") == "TRADING" and
                    s.get("fundingIntervalHours", 8) != 1):
                symbols.append(s["symbol"])
        except Exception:
            continue
    return symbols


async def get_klines(session, symbol):
    return await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/klines", {
        "symbol": symbol, "interval": "15m", "limit": 25
    })


async def get_klines_1h(session, symbol):
    return await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/klines", {
        "symbol": symbol, "interval": "1h", "limit": 25
    })


async def get_all_tickers_24h(session):
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/ticker/24hr")
    if not data or not isinstance(data, list):
        return {}
    return {item["symbol"]: float(item.get("quoteVolume", 0)) for item in data}


async def get_funding_map(session):
    """拉取所有幣的下次資金費率結算時間，回傳 {symbol: nextFundingTime(ms)}"""
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/premiumIndex")
    if not data or not isinstance(data, list):
        return {}, {}
    next_funding = {item["symbol"]: int(item.get("nextFundingTime", 0)) for item in data}
    funding_rate = {item["symbol"]: float(item.get("lastFundingRate", 0)) for item in data}
    return next_funding, funding_rate


async def get_btc_1h_change(session):
    """取得 BTC 最近1H漲跌幅%"""
    try:
        klines = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/klines",
                                  {"symbol": "BTCUSDT", "interval": "1h", "limit": 2})
        if klines and len(klines) >= 2:
            prev_close = float(klines[-2][4])
            curr_close = float(klines[-1][4])
            if prev_close > 0:
                return round((curr_close - prev_close) / prev_close * 100, 4)
    except Exception:
        pass
    return None


def calc_bollinger(klines, period=20, std_mult=2.0):
    if not klines or len(klines) < period:
        return None
    closes = [float(k[4]) for k in klines]
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = mean + std_mult * std
    lower = mean - std_mult * std
    return {"price": closes[-1], "upper": upper, "middle": mean,
            "lower": lower, "std": std}


async def scan_symbol(session, symbol, cfg=None, volume_map=None, funding_map=None, funding_rate_map=None, btc_change_1h=None):
    try:
        klines, klines_1h = await asyncio.gather(
            get_klines(session, symbol),
            get_klines_1h(session, symbol)
        )
    except Exception:
        return None
    if not klines:
        return None

    # 資金費率結算過濾：1小時內即將結算的幣排除
    if funding_map is not None:
        next_funding_ms = funding_map.get(symbol, 0)
        if next_funding_ms > 0:
            now_ms = int(time.time() * 1000)
            minutes_to_funding = (next_funding_ms - now_ms) / 1000 / 60
            if 0 < minutes_to_funding <= 90:
                return None

    volume_usdt = (volume_map or {}).get(symbol, 0)
    if cfg:
        min_vol = cfg.get("min_volume_usdt", 0)
        if min_vol > 0 and volume_usdt > 0 and volume_usdt < min_vol:
            return None

    bb = calc_bollinger(klines)
    if not bb:
        return None
    price = bb["price"]
    upper = bb["upper"]
    middle = bb["middle"]
    if price >= upper:
        return None

    band_width_pct = (upper - bb["lower"]) / middle * 100
    min_band = cfg.get("min_band_width_pct", 2.0) if cfg else 2.0
    if band_width_pct < min_band:
        return None

    dist_to_upper_pct = (upper - price) / upper * 100

    dist_1h_pct = None
    if klines_1h and not isinstance(klines_1h, Exception):
        bb1h = calc_bollinger(klines_1h)
        if bb1h and bb1h["upper"] > 0:
            dist_1h_pct = (bb1h["upper"] - price) / bb1h["upper"] * 100

    # 前高壓力評分
    # 邏輯：固定回看5根K棒，取所有高點中超過現價的最大幅度（%）
    # 一根高牆（超出幅度大）比多根矮牆更有意義
    # 候選池硬性門檻：max_excess >= prev_high_min_excess_pct（設定頁可調）
    prev_high_score = 0.0
    lookback = 5
    if len(klines) >= lookback + 1:
        recent_highs = [float(k[2]) for k in klines[-(lookback + 1):-1]]
        bars_above = [h for h in recent_highs if h > price]
        if bars_above:
            # 各根超出幅度%
            excesses = [(h - price) / price * 100 for h in bars_above]
            max_excess = max(excesses)
            avg_excess = sum(excesses) / len(excesses)
            count_score = len(bars_above) / lookback
            # score = 最大超出幅度（主要）+ 根數比例加權（次要）
            prev_high_score = round(max_excess + count_score * 0.2, 4)

    return {
        "symbol": symbol.replace("USDT", ""),
        "full_symbol": symbol,
        "price": price,
        "upper": upper,
        "middle": middle,
        "lower": bb["lower"],
        "dist_to_upper_pct": dist_to_upper_pct,
        "dist_1h_pct": dist_1h_pct,
        "band_width_pct": band_width_pct,
        "volume_usdt": volume_usdt,
        "prev_high_score": prev_high_score,
        "funding_rate": (funding_rate_map or {}).get(symbol),
        "btc_change_1h": btc_change_1h,
    }


async def run_scan():
    scanner_cache["is_scanning"] = True
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            symbols = await get_all_symbols(session)
            if not symbols:
                return
            cfg_scan = load_config()
            try:
                volume_map = await get_all_tickers_24h(session)
            except Exception:
                volume_map = {}
            try:
                funding_map, funding_rate_map = await get_funding_map(session)
            except Exception:
                funding_map, funding_rate_map = {}, {}
            try:
                btc_1h = await get_btc_1h_change(session)
            except Exception:
                btc_1h = None

            batch_size = 20
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                tasks = [scan_symbol(session, sym, cfg_scan, volume_map, funding_map, funding_rate_map, btc_1h) for sym in batch]
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in batch_results:
                    if r and not isinstance(r, Exception):
                        results.append(r)
                await asyncio.sleep(0.15)

        results.sort(key=lambda x: x["dist_to_upper_pct"])
        scanner_cache["data"] = results
        scanner_cache["last_updated"] = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        logger.error(f"掃描器錯誤: {e}", exc_info=True)
        return
    finally:
        scanner_cache["is_scanning"] = False

    logger.info(f"掃描完成，共 {len(results)} 個符合條件的幣種")

    # 同步給交易引擎（候選池）
    from trader import state as trader_state
    cfg = load_config()

    max_dist      = cfg.get("max_dist_to_upper_pct", 1.0)
    min_excess    = cfg.get("prev_high_min_excess_pct", 1.0)
    pre_scan_size = cfg.get("pre_scan_size", 30)
    pool_size     = cfg.get("candidate_pool_size", 10)

    # 步驟1：距15分K上軌 <= max_dist%，排除黑名單，取前 pre_scan_size 個
    blacklist = set(cfg.get("blacklist", []))
    filtered = [r for r in results
                if r.get("dist_to_upper_pct", 999) <= max_dist
                and r.get("symbol", "").replace("USDT", "") not in blacklist]
    top_15m = filtered[:pre_scan_size]

    # 步驟2：前高保護 >= min_excess%，依分數由高到低排序
    with_prev_high = [r for r in top_15m if r.get("prev_high_score", 0) >= min_excess]
    with_prev_high.sort(key=lambda x: x.get("prev_high_score", 0), reverse=True)

    # 步驟3：若數量 <= pool_size 直接放入；否則依距1H上軌由近到遠排序取前 pool_size 個
    if len(with_prev_high) <= pool_size:
        final_pool = with_prev_high
    else:
        with_prev_high.sort(key=lambda x: x.get("dist_1h_pct") if x.get("dist_1h_pct") is not None else 999)
        final_pool = with_prev_high[:pool_size]

    pool_count = len(final_pool)

    try:
        from trader import write_log
        write_log("SCAN", f"候選池{pool_count}個 15m<={max_dist}% 前高>={min_excess}%",
                  detail={"pool_count": pool_count, "dist_15m": max_dist, "prev_high": min_excess})
    except Exception:
        pass

    trader_state["scanner_latest_result"] = [
        {**r, "dist_to_upper": r.get("dist_to_upper_pct", 0)}
        for r in final_pool
    ]

    # 同步更新 candidate_pool，讓儀表板即時顯示，不等 trading_loop 週期
    try:
        from trader import scan_candidates
        candidates = await scan_candidates(cfg, scanner_data=trader_state["scanner_latest_result"])
        trader_state["candidate_pool"] = candidates
    except Exception as e:
        logger.error(f"候選池同步更新失敗: {e}")


def run_scan_sync():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_scan())
    loop.close()


def background_scanner():
    while True:
        if not scanner_cache["is_scanning"]:
            run_scan_sync()
        time.sleep(180)  # 3分鐘掃一次，避免API限速


def get_account_sync():
    """直接從 WS 維護的快取取餘額，不打 REST"""
    return get_balance_cached()


# ===== Flask Routes =====

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scanner/data")
def api_scanner_data():
    return jsonify({
        "data": scanner_cache["data"],
        "last_updated": scanner_cache["last_updated"],
        "is_scanning": scanner_cache["is_scanning"],
        "count": len(scanner_cache["data"])
    })


@app.route("/api/scanner/refresh", methods=["POST"])
def api_scanner_refresh():
    if not scanner_cache["is_scanning"]:
        t = threading.Thread(target=run_scan_sync)
        t.daemon = True
        t.start()
    return jsonify({"status": "started"})


@app.route("/api/account")
def api_account():
    balance = get_account_sync()
    cfg = load_config()
    if balance:
        total = balance["total"]
        margin_used = balance["margin_used"]
        margin_ratio = (margin_used / total * 100) if total > 0 else 0
        notional_per_order = get_notional(cfg, total)
        balance["margin_ratio"] = round(margin_ratio, 2)
        balance["notional_per_order"] = round(notional_per_order, 2)
        balance["margin_limit_pct"] = cfg["margin_usage_limit_pct"]

    return jsonify({
        "balance": balance,
        "system_running": cfg.get("system_running", True),
        "paused": state["paused"],
        "margin_pause": state["margin_pause"],
        "candidate_pool": state["candidate_pool"],
        "max_symbols": cfg.get("max_symbols", 3),
        "capital_per_order_pct": cfg.get("capital_per_order_pct", 1.0),
    })


@app.route("/api/positions")
def api_positions():
    """從 WS 維護的持倉快取取資料，不打 REST"""
    result = {}
    for sym, pos in state["_binance_positions_cache"].items():
        result[sym] = {
            **pos,
            "hidden_grids": state["hidden_grids"].get(sym, []),
            "tp_sl_orders": state["tp_sl_orders"].get(sym, {}),
            "sell_count": state["symbol_sell_count"].get(sym, 0),
            "open_time": state["symbol_open_time"].get(sym),
            "realized_pnl": state["symbol_realized_pnl"].get(sym, 0),
        }
    return jsonify({"positions": result, "symbols": list(result.keys())})


@app.route("/api/close/<symbol>", methods=["POST"])
def api_close_symbol(symbol):
    cfg = load_config()
    # Paper mode 必須用主循環的同一個實例，不能重建（重建會是空持倉）
    client = state.get("_paper_exchange") or get_client(cfg)

    async def do_close():
        await close_symbol(client, cfg, symbol, reason="MANUAL")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(do_close())
    loop.close()
    return jsonify({"status": "ok", "symbol": symbol})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    from trader import reset_system
    cfg = load_config()
    client = state.get("_paper_exchange") or get_client(cfg)

    async def do_reset():
        return await reset_system(client, cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(do_reset())
    loop.close()
    return jsonify(result)

@app.route("/api/control", methods=["POST"])
def api_control():
    data = request.json
    action = data.get("action")
    cfg = load_config()

    if action == "pause":
        state["paused"] = True
        client = get_client(cfg)
        from trader import handle_pause
        async def do_pause():
            await handle_pause(client, cfg)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(do_pause())
        loop.close()
        write_log("PAUSE", "系統暫停")
        return jsonify({"status": "ok", "action": action, "system_running": True, "paused": True})

    elif action == "resume":
        state["paused"] = False
        state["margin_pause"] = False
        write_log("RESUME", "系統恢復")
        return jsonify({"status": "ok", "action": action, "system_running": True, "paused": False})

    elif action == "stop":
        cfg["system_running"] = False
        save_config(cfg)
        write_log("STOP", "系統停止")
        return jsonify({"status": "ok", "action": action, "system_running": False, "paused": False})

    elif action == "start":
        cfg["system_running"] = True
        save_config(cfg)
        state["paused"] = False
        write_log("START", "系統啟動")
        return jsonify({"status": "ok", "action": action, "system_running": True, "paused": False})

    return jsonify({"status": "error", "message": "unknown action"})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = load_config()
    safe_cfg = {k: v for k, v in cfg.items()
                if k not in ["api_key", "api_secret", "capital_transactions"]}
    return jsonify(safe_cfg)


@app.route("/api/config", methods=["POST"])
def api_config_set():
    cfg = load_config()
    data = request.json
    allowed_keys = [
        "capital_per_order_pct", "leverage", "grid_spacing_pct", "grid_count", "time_stop_minutes",
        "max_symbols", "max_orders_per_symbol", "scale_up_after_order", "scale_up_multiplier",
        "candidate_pool_size", "pre_scan_size",
        "take_profit_price_pct", "force_close_price_pct",
        "tp_limit_pct",
        "pause_open_rise_pct", "force_close_capital_pct",
        "margin_usage_limit_pct", "min_volume_usdt",
        "candidate_pool_refresh_min",
        "max_dist_to_upper_pct", "prev_high_min_excess_pct",
        "tp_tier1_roi", "tp_tier1_qty", "tp_tier2_roi",
        "sl_loss_pct",
        "black_k_require_below_upper", "black_k_max_upper_slope_pct", "black_k_upper_slope_lookback",
        "blacklist",
        "black_k_max_upper_slope_pct", "black_k_upper_slope_lookback",
        "extend_orders_max", "extend_loss_pct",
        "system_running"
    ]
    grid_changed = any(k in data for k in ["grid_spacing_pct", "grid_count"])
    for k in allowed_keys:
        if k in data:
            cfg[k] = data[k]
    save_config(cfg)

    # 若網格相關設定有變更，對所有現有持倉重算網格
    if grid_changed:
        from trader import update_hidden_grids, get_exchange, get_client
        open_positions = state.get("_binance_positions_cache", {})
        if open_positions:
            exchange = state.get("_paper_exchange") or get_client(cfg)

            async def refresh_grids():
                for symbol, pos in open_positions.items():
                    try:
                        # 取消現有未成交的隱形網格掛單
                        open_orders = await exchange.get_open_orders(symbol)
                        sell_orders = [o for o in open_orders
                                       if o.get("side") == "SELL"
                                       and str(o.get("orderId")) not in
                                       list((state.get("tp_sl_orders", {}).get(symbol, {})).values())]
                        for order in sell_orders:
                            try:
                                await exchange.cancel_order(symbol, str(order["orderId"]))
                            except Exception:
                                pass
                        # 用最新均入價重算網格
                        avg_entry = pos.get("avg_entry", 0)
                        if avg_entry > 0:
                            update_hidden_grids(symbol, avg_entry, cfg)
                            write_log("HIDDEN_GRID_UPDATE", f"設定變更後重算網格，基準價={avg_entry}",
                                      symbol=symbol)
                    except Exception as e:
                        logger.error(f"重算網格失敗 {symbol}: {e}")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(refresh_grids())
            loop.close()

    return jsonify({"status": "ok"})


@app.route("/api/reports/pnl_summary")
def api_pnl_summary():
    """台北時間為基準，回傳今日/7天/30天/90天的已實現盈虧"""
    from database import get_conn
    conn = get_conn()
    now_taipei = datetime.now(TZ_TAIPEI)

    def query_pnl(days):
        if days == 0:
            # 今日：台北時間當天 00:00 起
            since = now_taipei.strftime("%Y-%m-%d") + " 00:00:00"
        else:
            from datetime import timedelta
            since = (now_taipei - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute("""
            SELECT
                COUNT(*) as trades,
                SUM(CASE WHEN total_pnl > 0 THEN 1 ELSE 0 END) as wins,
                COALESCE(SUM(total_pnl), 0) as pnl
            FROM trade_history
            WHERE close_time >= ?
        """, (since,)).fetchone()
        return dict(row) if row else {"trades": 0, "wins": 0, "pnl": 0}

    result = {
        "today":   query_pnl(0),
        "7d":      query_pnl(7),
        "30d":     query_pnl(30),
        "90d":     query_pnl(90),
    }
    conn.close()
    return jsonify(result)


@app.route("/api/download/db")
def api_download_db():
    """下載完整 SQLite 資料庫"""
    import os
    db_path = os.environ.get("DB_PATH", "trading.db")
    if not os.path.exists(db_path):
        return jsonify({"error": "DB not found"}), 404
    from flask import send_file
    return send_file(db_path, as_attachment=True,
                     download_name="bb_grid_trading.db",
                     mimetype="application/octet-stream")


@app.route("/api/reports/history")
def api_history():
    rows = get_trade_history(100)
    for r in rows:
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            t_open = datetime.strptime(r["open_time"], fmt) if r.get("open_time") else None
            t_close = datetime.strptime(r["close_time"], fmt) if r.get("close_time") else None
            if t_open and t_close and t_close > t_open:
                r["hold_minutes"] = round((t_close - t_open).total_seconds() / 60, 1)
            else:
                r["hold_minutes"] = None
        except Exception:
            r["hold_minutes"] = None
    return jsonify(rows)


@app.route("/api/reports/daily")
def api_daily():
    return jsonify(get_daily_pnl())


@app.route("/api/reports/pnl_curve")
def api_pnl_curve():
    return jsonify(get_cumulative_pnl())


@app.route("/api/capital_log", methods=["GET"])
def api_capital_log_get():
    return jsonify(get_capital_log())


@app.route("/api/capital_log", methods=["POST"])
def api_capital_log_post():
    data = request.json
    balance = get_account_sync()
    balance_after = balance["total"] if balance else 0
    add_capital_log(
        data.get("type", "DEPOSIT"),
        float(data.get("amount", 0)),
        data.get("note", ""),
        balance_after
    )
    return jsonify({"status": "ok"})

@app.route("/api/logs")
def api_logs():
    event_type = request.args.get("event_type")
    symbol = request.args.get("symbol")
    limit = int(request.args.get("limit", 200))
    return jsonify(get_logs(event_type, symbol, limit))


@app.route("/api/logs/summary")
def api_logs_summary():
    return jsonify(get_log_summary())


@app.route("/api/reports/analytics")
def api_analytics():
    unfilled = request.args.get("unfilled", "false").lower() == "true"
    return jsonify(get_analytics(limit=200, unfilled_only=unfilled))


@app.route("/api/logs/export")
def api_logs_export():
    """下載最近200筆日誌為JSON檔，供分析用"""
    limit = int(request.args.get("limit", 200))
    json_str = export_logs_json(limit=limit)
    filename = f"bb_grid_logs_{datetime.now(TZ_TAIPEI).strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        json_str,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


init_db()

_scanner_thread = threading.Thread(target=background_scanner)
_scanner_thread.daemon = True
_scanner_thread.start()

_trader_thread = threading.Thread(target=start_trading_loop)
_trader_thread.daemon = True
_trader_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
