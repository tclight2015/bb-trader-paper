"""
資料庫模組 v4

設計原則：DB只記已完成的歷史，不維護即時持倉狀態

ML 補填設計：
- 平倉後背景 task 在 1h / 4h 後補填當時價格
- reward_score 由 roe_pct + close_reason + hold_minutes + 出場品質 綜合計算
"""

import sqlite3
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
TZ_TAIPEI = timezone(timedelta(hours=8))
import os
_DB_VOLUME = "/data/trading.db"
_DB_LOCAL  = "trading.db"
DB_FILE = _DB_VOLUME if os.path.isdir("/data") else _DB_LOCAL


def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            exchange TEXT DEFAULT 'binance',
            open_time TEXT,
            close_time TEXT,
            avg_entry_price REAL,
            close_price REAL,
            total_quantity REAL,
            total_margin REAL,
            total_pnl REAL,
            roe_pct REAL,
            close_reason TEXT,
            order_count INTEGER DEFAULT 1,
            mark_price REAL,
            slippage_pct REAL,
            upper_15m REAL,
            dist_15m_pct REAL,
            dist_1h_pct REAL,
            prev_high_score REAL,
            volume_usdt REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            exchange TEXT DEFAULT 'binance',
            close_time TEXT,
            hold_minutes REAL,
            avg_entry_price REAL,
            close_price REAL,
            total_qty REAL,
            total_margin REAL,
            total_pnl REAL,
            roe_pct REAL,
            close_reason TEXT,
            upper_15m REAL,
            dist_15m_pct REAL,
            dist_1h_pct REAL,
            band_width_pct REAL,
            volume_usdt REAL,
            prev_high_score REAL,
            funding_rate REAL,
            btc_change_1h REAL,
            price_15m_after REAL,
            price_30m_after REAL,
            price_1h_after REAL,
            filled_15m INTEGER DEFAULT 0,
            filled_30m INTEGER DEFAULT 0,
            filled_1h INTEGER DEFAULT 0,
            reward_score REAL,
            extra_data TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS capital_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            type TEXT,
            amount REAL,
            note TEXT,
            balance_after REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS system_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            event_type TEXT,
            symbol TEXT,
            exchange TEXT DEFAULT 'binance',
            detail TEXT,
            note TEXT
        )
    """)

    _migrate(c)
    conn.commit()
    conn.close()


def _migrate(c):
    migrations = [
        ("trade_history",   "exchange",     "TEXT DEFAULT 'binance'"),
        ("trade_history",   "order_count",  "INTEGER DEFAULT 1"),
        ("trade_history",   "mark_price",   "REAL"),
        ("trade_history",   "slippage_pct", "REAL"),
        ("trade_history",   "upper_15m",    "REAL"),
        ("trade_history",   "dist_15m_pct", "REAL"),
        ("trade_history",   "dist_1h_pct",  "REAL"),
        ("trade_history",   "prev_high_score", "REAL"),
        ("trade_history",   "volume_usdt",  "REAL"),
        ("trade_analytics", "exchange",     "TEXT DEFAULT 'binance'"),
        ("trade_analytics", "hold_minutes", "REAL"),
        ("trade_analytics", "funding_rate", "REAL"),
        ("trade_analytics", "btc_change_1h", "REAL"),
        ("trade_analytics", "price_15m_after", "REAL"),
        ("trade_analytics", "price_30m_after", "REAL"),
        ("trade_analytics", "filled_15m",  "INTEGER DEFAULT 0"),
        ("trade_analytics", "filled_30m",  "INTEGER DEFAULT 0"),
        ("trade_analytics", "filled_1h",   "INTEGER DEFAULT 0"),
        ("trade_analytics", "reward_score", "REAL"),
        ("system_log",      "exchange",     "TEXT DEFAULT 'binance'"),
    ]
    for table, col, col_def in migrations:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except Exception:
            pass


# ===== 交易歷史 =====

def record_trade_close(symbol, avg_entry, close_price, total_qty,
                       total_margin, total_pnl, roe_pct, close_reason,
                       open_time=None, exchange="binance", order_count=1,
                       mark_price=None, slippage_pct=None, market_snapshot=None):
    conn = get_conn()
    now = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    snap = market_snapshot or {}
    conn.execute("""
        INSERT INTO trade_history
        (symbol, exchange, open_time, close_time, avg_entry_price, close_price,
         total_quantity, total_margin, total_pnl, roe_pct, close_reason, order_count,
         mark_price, slippage_pct, upper_15m, dist_15m_pct, dist_1h_pct,
         prev_high_score, volume_usdt)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, exchange, open_time or now, now,
          avg_entry, close_price, total_qty,
          total_margin, total_pnl, roe_pct, close_reason, order_count,
          mark_price, slippage_pct,
          snap.get("upper_15m"), snap.get("dist_15m_pct", snap.get("dist_15m")),
          snap.get("dist_1h_pct", snap.get("dist_1h")),
          snap.get("prev_high_score"), snap.get("volume_usdt")))
    conn.commit()
    conn.close()


def add_trade_analytics(symbol, avg_entry, close_price, total_qty,
                        total_margin, total_pnl, roe_pct, close_reason,
                        market_snapshot=None, open_time=None, exchange="binance"):
    conn = get_conn()
    snap = market_snapshot or {}
    now = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")

    hold_minutes = None
    if open_time:
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            t_open = datetime.strptime(open_time, fmt)
            t_close = datetime.strptime(now, fmt)
            hold_minutes = (t_close - t_open).total_seconds() / 60
        except Exception:
            pass

    row_id = conn.execute("""
        INSERT INTO trade_analytics
        (symbol, exchange, close_time, hold_minutes,
         avg_entry_price, close_price, total_qty,
         total_margin, total_pnl, roe_pct, close_reason,
         upper_15m, dist_15m_pct, dist_1h_pct, band_width_pct,
         volume_usdt, prev_high_score, funding_rate, btc_change_1h, extra_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, exchange, now, hold_minutes,
          avg_entry, close_price, total_qty,
          total_margin, total_pnl, roe_pct, close_reason,
          snap.get("upper_15m", 0),
          snap.get("dist_15m", snap.get("dist_15m_pct", 0)),
          snap.get("dist_1h", snap.get("dist_1h_pct", 0)),
          snap.get("band_width_pct", 0),
          snap.get("volume_usdt", 0),
          snap.get("prev_high_score", 0),
          snap.get("funding_rate"),
          snap.get("btc_change_1h"),
          json.dumps(snap, ensure_ascii=False))).lastrowid
    conn.commit()
    conn.close()
    return row_id


# ===== ML 補填 =====

def fill_price_after(analytics_id: int, minutes: int, price: float):
    conn = get_conn()
    if minutes == 15:
        conn.execute(
            "UPDATE trade_analytics SET price_15m_after=?, filled_15m=1 WHERE id=?",
            (price, analytics_id)
        )
    elif minutes == 30:
        conn.execute(
            "UPDATE trade_analytics SET price_30m_after=?, filled_30m=1 WHERE id=?",
            (price, analytics_id)
        )
    elif minutes == 60:
        conn.execute(
            "UPDATE trade_analytics SET price_1h_after=?, filled_1h=1 WHERE id=?",
            (price, analytics_id)
        )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM trade_analytics WHERE id=?", (analytics_id,)
    ).fetchone()
    if row and row["filled_1h"]:
        score = _calc_reward(dict(row))
        conn.execute(
            "UPDATE trade_analytics SET reward_score=? WHERE id=?",
            (score, analytics_id)
        )
        conn.commit()
    conn.close()


def _calc_reward(row: dict) -> float:
    """
    reward_score 設計原則：
    - 目標是虧損筆數少、報酬率平滑
    - 不鼓勵「撐久吃更多」，鼓勵「快速乾淨獲利」
    - 止損本身不重罰，但強制平倉和長時間被套要扣
    """
    roe = row.get("roe_pct", 0) or 0
    hold = row.get("hold_minutes", 0) or 0
    reason = row.get("close_reason", "") or ""
    close_price = row.get("close_price", 0) or 0
    price_1h = row.get("price_1h_after", 0) or 0

    score = roe

    # 1. 快速獲利加分（30分鐘內獲利出場，效率高）
    if roe > 0 and hold <= 30:
        score += 1.0

    # 2. 持倉過長扣分（超過2小時代表策略失效或被套）
    if hold > 120:
        score -= 1.5
    elif hold > 60:
        score -= 0.5

    # 3. 極短持倉且虧損：可能是誤觸，扣分
    if hold < 5 and roe < 0:
        score -= 2.0

    # 4. 強制平倉重罰（表示風控失守）
    if reason == "FORCE_CLOSE":
        score -= 5.0

    # 5. 手動平倉輕罰（非系統正常流程）
    elif reason == "MANUAL":
        score -= 0.5

    # 6. 出場後價格繼續下跌：不加分也不扣分
    #    （快出是好的，不應懲罰「出得太早」）

    # 7. 出場後價格反彈回來（做空方向反轉）：小扣分
    #    代表這筆開倉時機選得不好，不是趨勢空
    if close_price > 0 and price_1h > 0:
        rebound_pct = (price_1h - close_price) / close_price * 100
        if rebound_pct > 1.0:  # 平倉後1h漲回超過1%
            score -= rebound_pct * 0.3

    return round(score, 4)


async def ml_fill_task(exchange_client, analytics_id: int, symbol: str):
    for minutes in [15, 30, 60]:
        await asyncio.sleep(minutes * 60)
        try:
            price = await exchange_client.get_price(symbol)
            if price:
                fill_price_after(analytics_id, minutes, price)
                logger.info(f"ML補填 {symbol} {minutes}m @ {price} (id={analytics_id})")
        except Exception as e:
            logger.error(f"ML補填失敗 {symbol} {minutes}m: {e}")


# ===== 查詢 =====

def get_trade_history(limit=100):
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM trade_history
           WHERE close_time >= datetime('now', '-3 days', 'localtime')
           ORDER BY close_time DESC LIMIT ?""", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_pnl():
    conn = get_conn()
    rows = conn.execute("""
        SELECT DATE(close_time) as date, COUNT(*) as trades,
               SUM(CASE WHEN total_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(total_pnl) as pnl
        FROM trade_history
        GROUP BY DATE(close_time)
        ORDER BY date DESC LIMIT 30
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_cumulative_pnl():
    conn = get_conn()
    rows = conn.execute("""
        SELECT close_time, total_pnl, symbol
        FROM trade_history ORDER BY close_time ASC
    """).fetchall()
    conn.close()
    data = [dict(r) for r in rows]
    cumulative = 0
    for d in data:
        cumulative += d["total_pnl"]
        d["cumulative_pnl"] = round(cumulative, 4)
    return data


def get_analytics(limit=200, unfilled_only=False):
    conn = get_conn()
    where = "WHERE filled_15m=0 OR filled_30m=0 OR filled_1h=0" if unfilled_only else ""
    rows = conn.execute(
        f"SELECT * FROM trade_analytics {where} ORDER BY close_time DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===== 出入金 =====

def add_capital_log(type_, amount, note, balance_after):
    conn = get_conn()
    conn.execute("""
        INSERT INTO capital_log (time, type, amount, note, balance_after)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
          type_, amount, note, balance_after))
    conn.commit()
    conn.close()


def get_capital_log():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM capital_log ORDER BY time DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===== 系統日誌 =====

def write_log(event_type, note, symbol=None, detail=None, exchange="binance"):
    conn = get_conn()
    conn.execute("""
        INSERT INTO system_log (time, event_type, symbol, exchange, detail, note)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
          event_type, symbol, exchange,
          json.dumps(detail, ensure_ascii=False) if detail else None,
          note))
    conn.commit()
    conn.close()


def get_logs(event_type=None, symbol=None, limit=200):
    conn = get_conn()
    conditions, params = [], []
    if event_type:
        conditions.append("event_type=?")
        params.append(event_type)
    if symbol:
        conditions.append("symbol=?")
        params.append(symbol)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM system_log {where} ORDER BY time DESC LIMIT ?", params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_log_summary():
    conn = get_conn()
    rows = conn.execute("""
        SELECT event_type, COUNT(*) as count
        FROM system_log GROUP BY event_type ORDER BY count DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def export_logs_json(limit=200):
    logs = get_logs(limit=limit)
    for log in logs:
        if log.get("detail") and isinstance(log["detail"], str):
            try:
                log["detail"] = json.loads(log["detail"])
            except Exception:
                pass
    return json.dumps(logs, ensure_ascii=False, indent=2)
