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
DB_FILE = "trading.db"


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
            close_reason TEXT
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
            price_1h_after REAL,
            price_4h_after REAL,
            filled_1h INTEGER DEFAULT 0,
            filled_4h INTEGER DEFAULT 0,
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
        ("trade_analytics", "exchange",     "TEXT DEFAULT 'binance'"),
        ("trade_analytics", "hold_minutes", "REAL"),
        ("trade_analytics", "filled_1h",    "INTEGER DEFAULT 0"),
        ("trade_analytics", "filled_4h",    "INTEGER DEFAULT 0"),
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
                       open_time=None, exchange="binance"):
    conn = get_conn()
    now = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO trade_history
        (symbol, exchange, open_time, close_time, avg_entry_price, close_price,
         total_quantity, total_margin, total_pnl, roe_pct, close_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, exchange, open_time or now, now,
          avg_entry, close_price, total_qty,
          total_margin, total_pnl, roe_pct, close_reason))
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
         volume_usdt, prev_high_score, extra_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, exchange, now, hold_minutes,
          avg_entry, close_price, total_qty,
          total_margin, total_pnl, roe_pct, close_reason,
          snap.get("upper_15m", 0),
          snap.get("dist_15m", snap.get("dist_15m_pct", 0)),
          snap.get("dist_1h", snap.get("dist_1h_pct", 0)),
          snap.get("band_width_pct", 0),
          snap.get("volume_usdt", 0),
          snap.get("prev_high_score", 0),
          json.dumps(snap, ensure_ascii=False))).lastrowid
    conn.commit()
    conn.close()
    return row_id


# ===== ML 補填 =====

def fill_price_after(analytics_id: int, hours: int, price: float):
    conn = get_conn()
    if hours == 1:
        conn.execute(
            "UPDATE trade_analytics SET price_1h_after=?, filled_1h=1 WHERE id=?",
            (price, analytics_id)
        )
    elif hours == 4:
        conn.execute(
            "UPDATE trade_analytics SET price_4h_after=?, filled_4h=1 WHERE id=?",
            (price, analytics_id)
        )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM trade_analytics WHERE id=?", (analytics_id,)
    ).fetchone()
    if row and row["filled_1h"] and row["filled_4h"]:
        score = _calc_reward(dict(row))
        conn.execute(
            "UPDATE trade_analytics SET reward_score=? WHERE id=?",
            (score, analytics_id)
        )
        conn.commit()
    conn.close()


def _calc_reward(row: dict) -> float:
    score = row.get("roe_pct", 0) or 0

    close_price = row.get("close_price", 0) or 0
    price_1h = row.get("price_1h_after", 0) or 0
    if close_price > 0 and price_1h > 0:
        missed_pct = (close_price - price_1h) / close_price * 100
        score -= missed_pct * 0.5

    hold = row.get("hold_minutes", 0) or 0
    if hold < 5:
        score -= 2.0
    elif hold > 240:
        score -= 1.0

    reason = row.get("close_reason", "")
    if reason == "FORCE_CLOSE":
        score -= 5.0
    elif reason == "MANUAL":
        score -= 1.0

    return round(score, 4)


async def ml_fill_task(exchange_client, analytics_id: int, symbol: str):
    for hours in [1, 4]:
        await asyncio.sleep(hours * 3600)
        try:
            price = await exchange_client.get_price(symbol)
            if price:
                fill_price_after(analytics_id, hours, price)
                logger.info(f"ML補填 {symbol} {hours}h @ {price} (id={analytics_id})")
        except Exception as e:
            logger.error(f"ML補填失敗 {symbol} {hours}h: {e}")


# ===== 查詢 =====

def get_trade_history(limit=100):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trade_history ORDER BY close_time DESC LIMIT ?", (limit,)
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
    where = "WHERE filled_1h=0 OR filled_4h=0" if unfilled_only else ""
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
