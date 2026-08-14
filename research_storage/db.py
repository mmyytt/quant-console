"""
研究数据持久化层（SQLite）
============================================================
本地开发用 SQLite 单文件，通过薄封装隔离，未来可换 Postgres/Turso 上云。
外挂模块：不 import app.py / engine_core.py，不触碰交易核心。

表：
  research_sessions     AI 研究会话
  research_messages     会话消息（角色/内容/时间）
  research_hypothesis   研究假设（new/testing/passed/failed/archived）
  strategy_experiments  每次回测实验
  strategy_library      策略库（含版本演进入口）
"""
import os
import sqlite3
from datetime import datetime

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_BASE_DIR, "research.db")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_time TEXT NOT NULL,
    model_provider TEXT,
    conversation_title TEXT,
    user_goal TEXT,
    status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS research_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES research_sessions(id)
);
CREATE TABLE IF NOT EXISTS research_hypothesis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_text TEXT NOT NULL,
    created_time TEXT NOT NULL,
    related_indicators TEXT,
    status TEXT DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS strategy_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT,
    indicator_combination TEXT,
    parameters TEXT,
    asset TEXT,
    timeframe TEXT,
    leverage REAL,
    backtest_time TEXT,
    total_return REAL,
    annual_return REAL,
    sharpe REAL,
    max_drawdown REAL,
    win_rate REAL,
    trade_count INTEGER,
    walk_forward_score REAL,
    monte_carlo_score REAL,
    final_rating TEXT
);
CREATE TABLE IF NOT EXISTS strategy_library (
    strategy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    logic_description TEXT,
    indicator_logic TEXT,
    parameters TEXT,
    risk_control TEXT,
    performance_summary TEXT,
    created_time TEXT NOT NULL,
    status TEXT DEFAULT 'draft'
);
"""


def init_db():
    """创建数据库与全部表（幂等）。"""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _rows(sql, params=()):
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _execute(sql, params=()):
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ------------------------------------------------------------
# sessions
# ------------------------------------------------------------
def create_session(model_provider=None, title="", user_goal="") -> int:
    return _execute(
        "INSERT INTO research_sessions (created_time, model_provider, conversation_title, user_goal, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (_now(), model_provider, title, user_goal),
    )


def list_sessions(limit=20):
    return _rows("SELECT * FROM research_sessions ORDER BY id DESC LIMIT ?", (limit,))


def get_session(session_id):
    rows = _rows("SELECT * FROM research_sessions WHERE id = ?", (session_id,))
    return rows[0] if rows else None


def update_session(session_id, **fields):
    if not fields:
        return
    allowed = {"conversation_title", "user_goal", "status", "model_provider"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    vals.append(session_id)
    _execute(f"UPDATE research_sessions SET {', '.join(sets)} WHERE id = ?", tuple(vals))


# ------------------------------------------------------------
# messages
# ------------------------------------------------------------
def add_message(session_id, role, content):
    return _execute(
        "INSERT INTO research_messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (session_id, role, content, _now()),
    )


def list_messages(session_id, limit=None):
    if limit:
        rows = _rows(
            "SELECT * FROM research_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return list(reversed(rows))
    return _rows("SELECT * FROM research_messages WHERE session_id = ? ORDER BY id ASC", (session_id,))


# ------------------------------------------------------------
# hypotheses
# ------------------------------------------------------------
def add_hypothesis(text, related_indicators=None, status="new") -> int:
    import json as _json
    rel = _json.dumps(related_indicators, ensure_ascii=False) if related_indicators else None
    return _execute(
        "INSERT INTO research_hypothesis (hypothesis_text, created_time, related_indicators, status) "
        "VALUES (?, ?, ?, ?)",
        (text, _now(), rel, status),
    )


def list_hypotheses(limit=100):
    return _rows("SELECT * FROM research_hypothesis ORDER BY id DESC LIMIT ?", (limit,))


def update_hypothesis_status(hyp_id, status):
    _execute("UPDATE research_hypothesis SET status = ? WHERE id = ?", (status, hyp_id))


def hypothesis_status_counts():
    rows = _rows("SELECT status, COUNT(*) AS n FROM research_hypothesis GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


# ------------------------------------------------------------
# experiments
# ------------------------------------------------------------
def add_experiment(strategy_name=None, indicator_combination=None, parameters=None,
                   asset=None, timeframe=None, leverage=None, backtest_time=None,
                   total_return=None, annual_return=None, sharpe=None, max_drawdown=None,
                   win_rate=None, trade_count=None, walk_forward_score=None,
                   monte_carlo_score=None, final_rating=None) -> int:
    import json as _json
    ic = _json.dumps(indicator_combination, ensure_ascii=False) if indicator_combination is not None else None
    pm = _json.dumps(parameters, ensure_ascii=False) if parameters is not None else None
    return _execute(
        "INSERT INTO strategy_experiments (strategy_name, indicator_combination, parameters, asset, "
        "timeframe, leverage, backtest_time, total_return, annual_return, sharpe, max_drawdown, "
        "win_rate, trade_count, walk_forward_score, monte_carlo_score, final_rating) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (strategy_name, ic, pm, asset, timeframe, leverage, backtest_time or _now(),
         total_return, annual_return, sharpe, max_drawdown, win_rate, trade_count,
         walk_forward_score, monte_carlo_score, final_rating),
    )


def list_experiments(limit=100):
    return _rows("SELECT * FROM strategy_experiments ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------
# strategy library
# ------------------------------------------------------------
def add_strategy(name, logic_description=None, indicator_logic=None, parameters=None,
                 risk_control=None, performance_summary=None, status="draft") -> int:
    import json as _json
    il = _json.dumps(indicator_logic, ensure_ascii=False) if indicator_logic is not None else None
    pm = _json.dumps(parameters, ensure_ascii=False) if parameters is not None else None
    rc = _json.dumps(risk_control, ensure_ascii=False) if risk_control is not None else None
    ps = _json.dumps(performance_summary, ensure_ascii=False) if performance_summary is not None else None
    return _execute(
        "INSERT INTO strategy_library (name, logic_description, indicator_logic, parameters, "
        "risk_control, performance_summary, created_time, status) VALUES (?,?,?,?,?,?,?,?)",
        (name, logic_description, il, pm, rc, ps, _now(), status),
    )


def list_strategies(limit=100):
    return _rows("SELECT * FROM strategy_library ORDER BY strategy_id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------
# memory summary（供 AI 研究记忆）
# ------------------------------------------------------------
def memory_summary() -> dict:
    """聚合研究记忆：最近会话、假设、实验、策略，供系统提示注入。"""
    sessions = list_sessions(5)
    hypotheses = list_hypotheses(30)
    experiments = list_experiments(20)
    strategies = list_strategies(10)
    return {
        "recent_sessions": sessions,
        "hypotheses": hypotheses,
        "experiments": experiments,
        "strategies": strategies,
        "hypothesis_counts": hypothesis_status_counts(),
    }
