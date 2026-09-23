# -*- coding: utf-8 -*-
"""本地 SQLite 存储：ETF 元数据 + 每日份额快照。"""
import sqlite3
import threading
from datetime import datetime

from config import DB_PATH

_local = threading.local()


def conn():
    """每个线程一个连接（sqlite3 连接不能跨线程复用）。"""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


def init_db():
    c = conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS etf_meta (
            code         TEXT PRIMARY KEY,
            name         TEXT,
            index_name   TEXT,
            index_code   TEXT,
            f10_updated  TEXT
        );
        CREATE TABLE IF NOT EXISTS shares_daily (
            code   TEXT NOT NULL,
            date   TEXT NOT NULL,
            shares REAL NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (code, date)
        );
        CREATE INDEX IF NOT EXISTS idx_shares_date ON shares_daily(date);
        CREATE TABLE IF NOT EXISTS run_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # 迁移：记录指数代码的来源，便于区分「交易所官方映射」与「名称解析结果」
    cols = {r[1] for r in c.execute("PRAGMA table_info(etf_meta)")}
    if "index_source" not in cols:
        c.execute("ALTER TABLE etf_meta ADD COLUMN index_source TEXT")
    c.commit()


def upsert_meta(code, name, index_name=None, index_code=None, f10_updated=None,
                index_source=None):
    c = conn()
    c.execute(
        """INSERT INTO etf_meta(code, name, index_name, index_code, f10_updated, index_source)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(code) DO UPDATE SET
             name=COALESCE(excluded.name, name),
             index_name=COALESCE(excluded.index_name, index_name),
             index_code=COALESCE(excluded.index_code, index_code),
             f10_updated=COALESCE(excluded.f10_updated, f10_updated),
             index_source=CASE WHEN excluded.index_code IS NOT NULL
                               THEN excluded.index_source ELSE index_source END""",
        (code, name, index_name, index_code, f10_updated, index_source),
    )
    c.commit()


def get_meta():
    """返回 {code: {name, index_name, index_code, index_source, f10_updated}}"""
    c = conn()
    rows = c.execute(
        "SELECT code,name,index_name,index_code,f10_updated,index_source FROM etf_meta").fetchall()
    return {r[0]: {"name": r[1], "index_name": r[2], "index_code": r[3],
                   "f10_updated": r[4], "index_source": r[5]} for r in rows}


def upsert_share(code, date, shares, source):
    c = conn()
    c.execute(
        """INSERT INTO shares_daily(code, date, shares, source) VALUES(?,?,?,?)
           ON CONFLICT(code, date) DO UPDATE SET shares=excluded.shares, source=excluded.source""",
        (code, date, shares, source),
    )
    c.commit()


def upsert_shares_many(rows):
    c = conn()
    c.executemany(
        """INSERT INTO shares_daily(code, date, shares, source) VALUES(?,?,?,?)
           ON CONFLICT(code, date) DO UPDATE SET shares=excluded.shares, source=excluded.source""",
        rows,
    )
    c.commit()


def get_share_dates(code):
    """该基金本地已有的份额数据日期集合。"""
    c = conn()
    return {r[0] for r in c.execute("SELECT date FROM shares_daily WHERE code=?", (code,))}


def get_all_shares():
    """返回 {code: [(date, shares), ...]} 按日期升序。

    只返回已纳入 etf_meta 的代码——交易所份额报表里还包含货币型等
    未被 ETF 名单收录的品种，避免它们污染汇总结果。
    """
    c = conn()
    out = {}
    rows = c.execute(
        "SELECT s.date, s.code, s.shares FROM shares_daily s "
        "JOIN etf_meta m ON m.code = s.code ORDER BY s.date").fetchall()
    for d, code, s in rows:
        out.setdefault(code, []).append((d, s))
    return out


def latest_share_date():
    """本地份额数据中最新的一条日期（无数据时返回 None）。"""
    c = conn()
    row = c.execute("SELECT MAX(date) FROM shares_daily").fetchone()
    return row[0] if row else None


# ---- 运行状态（记录上次成功抓取/生成报告的时间与数据版本，用于避免重复抓取）----

def set_states(mapping):
    c = conn()
    c.executemany(
        """INSERT INTO run_state(key, value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        list(mapping.items()),
    )
    c.commit()


def set_state(key, value):
    set_states({key: value})


def get_states():
    """返回 {key: value}（表不存在时返回空字典）。"""
    c = conn()
    try:
        return {k: v for k, v in c.execute("SELECT key, value FROM run_state")}
    except sqlite3.OperationalError:
        return {}


def today_str():
    return datetime.now().strftime("%Y-%m-%d")
