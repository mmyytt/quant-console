"""
Phase 3B 测试：AI 量化研究能力增强计划
覆盖：research_tasks 表 + failure_category 列 · 因子探索引擎(逻辑约束组合) ·
      组合→假设 · 失败原因分类 · 评分(20/20/20/20/10/10) · IS-only 参数搜索 ·
      研究任务模式(批量自主研究) · 旧回测不破坏
运行: python test_research_phase3b.py
"""
import os
import sys
import json
import tempfile
import io
import contextlib

import numpy as np
import pandas as pd

import research_storage.db as db
import research_loop as rl
import indicator_schema


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _synthetic_df():
    n = 365 * 10
    idx = pd.date_range("2017-01-01", periods=n, freq="D")
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.5, n))
    vol = np.abs(rng.normal(1000, 200, n))
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "vol": vol}, index=idx)


def _strategy_factory(selected):
    from engine_core import StrategyBase

    class _Trend(StrategyBase):
        def __init__(self):
            super().__init__("test_trend")
            self.selected = selected

        def generate_signals(self, df):
            df = df.copy()
            c = df["close"].shift(1)
            ma = c.rolling(50).mean()
            df["signal"] = 0
            df.loc[c > ma, "signal"] = 1
            df.loc[c < ma, "signal"] = -1
            df["regime"] = "range"
            df["br"] = 0.0
            return df

    return _Trend()


def main():
    _fix_encoding()
    tmp = tempfile.mkdtemp()
    db.DB_PATH = os.path.join(tmp, "test_phase3b.db")

    # 1) DB 迁移：research_tasks 表 + failure_category 列
    db.init_db()
    conn = __import__("sqlite3").connect(db.DB_PATH)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fm_cols = {r[1] for r in conn.execute("PRAGMA table_info(research_failure_memory)")}
    tk_cols = {r[1] for r in conn.execute("PRAGMA table_info(research_tasks)")}
    conn.close()
    assert "research_tasks" in tables, "缺 research_tasks 表"
    assert "failure_category" in fm_cols, "research_failure_memory 缺 failure_category"
    for c in ("goal", "status", "total", "done", "result", "created_time"):
        assert c in tk_cols, f"research_tasks 缺列 {c}"
    tid = db.create_task("测试研究任务", "ETH", "4h")
    db.update_task(tid, status="running", total=3, done=1, current="EMA+RSI")
    t = db.get_task(tid)
    assert t["status"] == "running" and t["done"] == 1 and t["current"] == "EMA+RSI"
    print("[OK] 1. DB 迁移 + research_tasks 表 + failure_category 列 (task#%d)" % tid)

    # 2) 因子探索引擎：逻辑约束组合
    pool = rl.factor_pool()
    assert len(pool) == 17, f"因子池应有 17 个核心因子，实际 {len(pool)}"
    names = {p["name"] for p in pool}
    assert "EMA 双均线" in names and "量比 Volume Ratio" in names
    two = rl.generate_factor_combos(2, 2)
    assert len(two) == 108, f"2 因子组合应 108 个，实际 {len(two)}"
    combos = rl.generate_factor_combos(2, 4)
    for c in combos:
        cats = [rl._class_of(n) for n in c]
        assert len(set(cats)) == len(cats), f"同类冗余因子堆叠: {c}"
        assert any(cat in rl._PRIMARY_CLASSES for cat in cats), f"缺少主信号: {c}"
        assert 2 <= len(c) <= 4, f"因子数越界: {c}"
    print("[OK] 2. 因子探索引擎 (因子池 17 · 2因子 %d · 全组合 %d)" % (len(two), len(combos)))

    # 3) 组合 → 假设（含每指标作用/环境/风险）
    hyp = rl.combo_to_hypothesis(["EMA 双均线", "量比 Volume Ratio"], "寻找 ETH 趋势策略")
    for k in ("hypothesis_text", "related_indicators", "parameters", "asset", "timeframe",
              "leverage", "tp_pct", "sl_pct", "expected_logic",
              "expected_market_condition", "failure_environment", "risk_assumption"):
        assert k in hyp, f"假设缺字段 {k}"
    assert hyp["related_indicators"] == ["EMA 双均线", "量比 Volume Ratio"]
    assert "EMA_short" in hyp["parameters"]["EMA 双均线"]
    assert "VR_threshold" in hyp["parameters"]["量比 Volume Ratio"]
    print("[OK] 3. 组合→假设 (%s · %s)" % (hyp["hypothesis_text"], hyp["expected_market_condition"]))

    # 4) 失败原因结构化分类（5 类）
    bad = {"oos_return": -5, "max_drawdown": 40, "trade_count": 10,
           "total_return": 20, "wf_profit_ratio": 30}
    tags = rl.classify_failure(bad)
    for tag in ("OOS亏损", "MDD过高", "交易次数不足", "市场迁移失败", "参数敏感"):
        assert tag in tags, f"缺失败分类 {tag}"
    assert rl.classify_failure({"oos_return": 15, "max_drawdown": 10, "trade_count": 80,
                                "total_return": 30, "wf_profit_ratio": 70}) == []
    print("[OK] 4. 失败原因分类 (5 类齐全)")

    # 5) 评分新权重 20/20/20/20/10/10
    m_ok = {"total_return": 150, "sharpe": 1.5, "max_drawdown": 20, "oos_return": 15,
            "mc_p5": 5, "wf_profit_ratio": 60}
    sc = rl.research_score(m_ok)
    assert sc["return"] == 75.0 and sc["sharpe"] == 75.0 and sc["monte_carlo"] == 50.0
    assert sc["param_stability"] == 60.0
    assert abs(sc["total"] - 62.7) < 0.2, f"加权总分异常 {sc['total']}"
    assert "return" in sc and "monte_carlo" in sc
    print("[OK] 5. 评分 20/20/20/20/10/10 (total=%.1f return=%.1f mc=%.1f)"
          % (sc["total"], sc["return"], sc["monte_carlo"]))

    # 6) IS-only 参数搜索（禁止偷看 OOS）
    df = _synthetic_df()
    indicators = ["EMA 双均线", "量比 Volume Ratio"]
    params = {"EMA 双均线": {"EMA_short": 15, "EMA_long": 60},
              "量比 Volume Ratio": {"VR_period": 30, "VR_threshold": 1.7}}
    with contextlib.redirect_stdout(io.StringIO()):
        ps = rl.parameter_search(df, "ETH", indicators, params, 2, 8.0, 4.0,
                                 strategy_factory=_strategy_factory)
    assert "best_params" in ps and "history" in ps
    assert len(ps["history"]) >= 4, "参数搜索应至少测多个候选"
    assert ps["is_end_year"] == rl.IS_END_YEAR
    print("[OK] 6. IS-only 参数搜索 (候选点 %d · IS 截止 %s)"
          % (len(ps["history"]), ps["is_end_year"]))

    # 7) 研究任务模式（批量自主研究：组合→假设→回测→排名）
    events = []
    with contextlib.redirect_stdout(io.StringIO()):
        res = rl.run_research_task("寻找 ETH 趋势策略", df, "ETH", timeframe="4h",
                                   strategy_factory=_strategy_factory,
                                   max_hypotheses=5, max_factors=2,
                                   progress=lambda d, t, l: events.append((d, t, l)))
    assert res["ranked"], "研究任务应有排名结果"
    assert res["summary"] and res["goal"] == "寻找 ETH 趋势策略"
    assert len(events) == 5, f"进度回调应 5 次，实际 {len(events)}"
    totals = [r["score"].get("total", 0) for r in res["ranked"]]
    assert totals == sorted(totals, reverse=True), "排名应按综合分降序"
    assert db.list_hypotheses(50), "假设应已落库"
    print("[OK] 7. 研究任务模式 (假设 %d 个 · 排名 Top1=%s · %s)"
          % (len(res["ranked"]), " + ".join(res["ranked"][0]["combo"]), res["summary"][:40]))

    # 8) 旧回测不破坏：指标 compute 仍可运行
    d = _synthetic_df().iloc[:200]
    for fn, args in [(indicator_schema._ema_cross, (7, 21)),
                     (indicator_schema._donchian, (20,)),
                     (indicator_schema._volume_ratio, (20, 1.5))]:
        dd = d.copy()
        fn(dd, *args)
        assert "_long" in dd.columns and "_short" in dd.columns
    print("[OK] 8. 旧回测不破坏：指标 compute 正常")

    print("\nALL PHASE 3B TESTS PASSED")


if __name__ == "__main__":
    main()
