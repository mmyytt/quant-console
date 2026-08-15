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
  research_reports      研究报告（Phase 2 自动生成）
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
CREATE TABLE IF NOT EXISTS research_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER,
    hypothesis_id INTEGER,
    grade TEXT,
    report_text TEXT,
    created_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_failure_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT,
    indicator_combination TEXT,
    parameters TEXT,
    fingerprint TEXT,
    failure_reason TEXT,
    failure_env TEXT,
    metrics TEXT,
    avoid INTEGER DEFAULT 1,
    created_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal TEXT NOT NULL,
    asset TEXT,
    timeframe TEXT,
    status TEXT DEFAULT 'pending',
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    current TEXT,
    result TEXT,
    created_time TEXT NOT NULL
);
"""


# Phase 2 新增字段（对已存在的旧库做 ALTER TABLE 增量迁移）
_COLUMN_ADDITIONS = {
    "research_sessions": {
        "research_context": "TEXT",
    },
    "research_hypothesis": {
        "user_goal": "TEXT",
        "asset": "TEXT",
        "timeframe": "TEXT",
        "leverage": "REAL",
        "parameters": "TEXT",
        "tp_pct": "REAL",
        "sl_pct": "REAL",
        "expected_logic": "TEXT",
        "expected_market_condition": "TEXT",
        "risk_assumption": "TEXT",
        "failure_environment": "TEXT",
        "strategy_config": "TEXT",
    },
    "strategy_experiments": {
        "hypothesis_id": "INTEGER",
        "oos_return": "REAL",
        "research_score": "REAL",
        "grade": "TEXT",
        "failure_reason": "TEXT",
        "fingerprint": "TEXT",
        "overfitting_risk": "TEXT",
        "param_stability": "REAL",
    },
    "strategy_library": {
        "applicable_market": "TEXT",
        "applicable_timeframe": "TEXT",
        "core_indicators": "TEXT",
        "failure_env": "TEXT",
        "research_score": "REAL",
        "grade": "TEXT",
        "indicator_roles": "TEXT",
        "param_stable_range": "TEXT",
        "overfitting_risk": "TEXT",
        "validation_count": "INTEGER",
    },
    "research_failure_memory": {
        "failure_category": "TEXT",
    },
}


def init_db():
    """创建数据库与全部表（幂等）+ 增量迁移新增列。"""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _migrate()


def _columns(table):
    conn = _connect()
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _migrate():
    """对已存在表补充缺失列（幂等，CREATE TABLE IF NOT EXISTS 不会加列）。"""
    for table, cols in _COLUMN_ADDITIONS.items():
        existing = _columns(table)
        for col, ddl in cols.items():
            if col not in existing:
                _execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


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


def _dumps(v):
    import json as _json
    return _json.dumps(v, ensure_ascii=False) if v is not None else None


# ------------------------------------------------------------
# sessions
# ------------------------------------------------------------
def create_session(model_provider=None, title="", user_goal="", research_context=None) -> int:
    return _execute(
        "INSERT INTO research_sessions (created_time, model_provider, conversation_title, user_goal, status, research_context) "
        "VALUES (?, ?, ?, ?, 'active', ?)",
        (_now(), model_provider, title, user_goal, _dumps(research_context)),
    )


def list_sessions(limit=20):
    return _rows("SELECT * FROM research_sessions ORDER BY id DESC LIMIT ?", (limit,))


def get_session(session_id):
    rows = _rows("SELECT * FROM research_sessions WHERE id = ?", (session_id,))
    return rows[0] if rows else None


def update_session(session_id, **fields):
    if not fields:
        return
    allowed = {"conversation_title", "user_goal", "status", "model_provider", "research_context"}
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
def add_hypothesis(text, related_indicators=None, status="new",
                   user_goal=None, asset=None, timeframe=None, leverage=None,
                   parameters=None, tp_pct=None, sl_pct=None,
                   expected_logic=None, expected_market_condition=None,
                   risk_assumption=None, failure_environment=None,
                   strategy_config=None) -> int:
    return _execute(
        "INSERT INTO research_hypothesis (hypothesis_text, created_time, related_indicators, status, "
        "user_goal, asset, timeframe, leverage, parameters, tp_pct, sl_pct, "
        "expected_logic, expected_market_condition, risk_assumption, failure_environment, "
        "strategy_config) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (text, _now(), _dumps(related_indicators), status,
         user_goal, asset, timeframe, leverage, _dumps(parameters), tp_pct, sl_pct,
         expected_logic, expected_market_condition, risk_assumption, failure_environment,
         _dumps(strategy_config)),
    )


def list_hypotheses(limit=100):
    return _rows("SELECT * FROM research_hypothesis ORDER BY id DESC LIMIT ?", (limit,))


def get_hypothesis(hyp_id):
    rows = _rows("SELECT * FROM research_hypothesis WHERE id = ?", (hyp_id,))
    return rows[0] if rows else None


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
                   monte_carlo_score=None, final_rating=None,
                   hypothesis_id=None, oos_return=None, research_score=None,
                   grade=None, failure_reason=None, fingerprint=None,
                   overfitting_risk=None, param_stability=None) -> int:
    return _execute(
        "INSERT INTO strategy_experiments (strategy_name, indicator_combination, parameters, asset, "
        "timeframe, leverage, backtest_time, total_return, annual_return, sharpe, max_drawdown, "
        "win_rate, trade_count, walk_forward_score, monte_carlo_score, final_rating, "
        "hypothesis_id, oos_return, research_score, grade, failure_reason, fingerprint, "
        "overfitting_risk, param_stability) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (strategy_name, _dumps(indicator_combination), _dumps(parameters), asset,
         timeframe, leverage, backtest_time or _now(),
         total_return, annual_return, sharpe, max_drawdown, win_rate, trade_count,
         walk_forward_score, monte_carlo_score, final_rating,
         hypothesis_id, oos_return, research_score, grade, failure_reason, fingerprint,
         overfitting_risk, param_stability),
    )


def list_experiments(limit=100):
    return _rows("SELECT * FROM strategy_experiments ORDER BY id DESC LIMIT ?", (limit,))


def get_experiment(exp_id):
    rows = _rows("SELECT * FROM strategy_experiments WHERE id = ?", (exp_id,))
    return rows[0] if rows else None


def update_experiment(exp_id, **fields):
    if not fields:
        return
    allowed = {"final_rating", "grade", "failure_reason", "research_score", "status",
               "overfitting_risk", "param_stability"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    vals.append(exp_id)
    _execute(f"UPDATE strategy_experiments SET {', '.join(sets)} WHERE id = ?", tuple(vals))


# ------------------------------------------------------------
# strategy library
# ------------------------------------------------------------
def add_strategy(name, logic_description=None, indicator_logic=None, parameters=None,
                 risk_control=None, performance_summary=None, status="draft",
                 applicable_market=None, applicable_timeframe=None,
                 core_indicators=None, failure_env=None,
                 research_score=None, grade=None,
                 indicator_roles=None, param_stable_range=None,
                 overfitting_risk=None, validation_count=None) -> int:
    return _execute(
        "INSERT INTO strategy_library (name, logic_description, indicator_logic, parameters, "
        "risk_control, performance_summary, created_time, status, "
        "applicable_market, applicable_timeframe, core_indicators, failure_env, "
        "research_score, grade, indicator_roles, param_stable_range, overfitting_risk, "
        "validation_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, logic_description, _dumps(indicator_logic), _dumps(parameters),
         _dumps(risk_control), _dumps(performance_summary), _now(), status,
         applicable_market, applicable_timeframe, _dumps(core_indicators), failure_env,
         research_score, grade, _dumps(indicator_roles), _dumps(param_stable_range),
         overfitting_risk, validation_count),
    )


def list_strategies(limit=100):
    return _rows("SELECT * FROM strategy_library ORDER BY strategy_id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------
# research reports (Phase 2)
# ------------------------------------------------------------
def add_report(experiment_id=None, hypothesis_id=None, grade=None, report_text="") -> int:
    return _execute(
        "INSERT INTO research_reports (experiment_id, hypothesis_id, grade, report_text, created_time) "
        "VALUES (?,?,?,?,?)",
        (experiment_id, hypothesis_id, grade, report_text, _now()),
    )


def list_reports(limit=100):
    return _rows("SELECT * FROM research_reports ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------
# failure memory (Phase 3：失败研究记忆，避免重复验证已失败策略)
# ------------------------------------------------------------
def add_failure_memory(strategy_name=None, indicator_combination=None, parameters=None,
                       fingerprint=None, failure_reason=None, failure_env=None,
                       metrics=None, failure_category=None, avoid=1) -> int:
    return _execute(
        "INSERT INTO research_failure_memory (strategy_name, indicator_combination, parameters, "
        "fingerprint, failure_reason, failure_env, metrics, failure_category, avoid, created_time) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (strategy_name, _dumps(indicator_combination), _dumps(parameters), fingerprint,
         failure_reason, failure_env, _dumps(metrics), _dumps(failure_category), avoid, _now()),
    )


def list_failure_memory(limit=100):
    return _rows("SELECT * FROM research_failure_memory ORDER BY id DESC LIMIT ?", (limit,))


def search_failure_memory(fingerprint=None, indicator_combination=None, limit=100):
    """按指纹或指标组合检索失败记忆。返回命中列表（用于相似度提醒）。"""
    rows = _rows("SELECT * FROM research_failure_memory ORDER BY id DESC LIMIT ?", (limit,))
    hits = []
    for r in rows:
        fp = r.get("fingerprint")
        ic = r.get("indicator_combination")
        if fingerprint and fp and fp == fingerprint:
            hits.append(r)
            continue
        if indicator_combination and ic:
            try:
                existing = set(json.loads(ic))
                if existing & set(indicator_combination):
                    hits.append(r)
            except Exception:
                pass
    return hits


# ------------------------------------------------------------
# research tasks（Phase 3B：研究任务模式，后台批量研究进度）
# ------------------------------------------------------------
def create_task(goal, asset=None, timeframe=None) -> int:
    return _execute(
        "INSERT INTO research_tasks (goal, asset, timeframe, status, total, done, created_time) "
        "VALUES (?,?,?,?,0,0,?)",
        (goal, asset, timeframe, "pending", _now()),
    )


def update_task(task_id, **fields):
    if not fields:
        return
    allowed = {"status", "total", "done", "current", "result"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    vals.append(task_id)
    _execute(f"UPDATE research_tasks SET {', '.join(sets)} WHERE id = ?", tuple(vals))


def get_task(task_id):
    rows = _rows("SELECT * FROM research_tasks WHERE id = ?", (task_id,))
    return rows[0] if rows else None


def list_tasks(limit=20):
    return _rows("SELECT * FROM research_tasks ORDER BY id DESC LIMIT ?", (limit,))


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
        "failure_memory": list_failure_memory(15),
        "hypothesis_counts": hypothesis_status_counts(),
    }
