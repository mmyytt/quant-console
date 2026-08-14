"""
Phase 2 测试:AI 量化研究闭环
覆盖:DB 迁移/新字段/报告表 · 指标解析 · 评分/等级 · 评审判定 · 去重 ·
      完整闭环(回测→判定→评分→报告→落库) · 旧回测不破坏
运行: python test_research_phase2.py
"""
import os
import sys
import json
import tempfile

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
    db.DB_PATH = os.path.join(tmp, "test_phase2.db")

    # 1) DB 迁移 + 新表 + 新字段
    db.init_db()
    conn = __import__("sqlite3").connect(db.DB_PATH)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    hyp_cols = {r[1] for r in conn.execute("PRAGMA table_info(research_hypothesis)")}
    exp_cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_experiments)")}
    lib_cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_library)")}
    conn.close()
    assert "research_reports" in tables, "缺 research_reports 表"
    for c in ("expected_logic", "expected_market_condition", "risk_assumption",
              "asset", "timeframe", "leverage", "parameters"):
        assert c in hyp_cols, f"research_hypothesis 缺列 {c}"
    for c in ("hypothesis_id", "oos_return", "research_score", "grade", "failure_reason"):
        assert c in exp_cols, f"strategy_experiments 缺列 {c}"
    for c in ("applicable_market", "failure_env", "research_score", "grade"):
        assert c in lib_cols, f"strategy_library 缺列 {c}"
    print("[OK] 1. DB 迁移 + research_reports 表 + 新字段")

    # 2) 假设 CRUD（含新字段）
    hid = db.add_hypothesis(
        "EMA趋势+量比确认提高突破成功率",
        related_indicators=["EMA 双均线", "量比 Volume Ratio"],
        user_goal="研究 ETH 4H 趋势突破", asset="ETH", timeframe="4h", leverage=2,
        parameters={"EMA 双均线": {"EMA_short": 15, "EMA_long": 60},
                    "量比 Volume Ratio": {"VR_period": 30, "VR_threshold": 1.7}},
        tp_pct=8.0, sl_pct=4.0,
        expected_logic="EMA判方向+量比过滤假突破",
        expected_market_condition="趋势行情", risk_assumption="回撤<30%",
    )
    h = db.get_hypothesis(hid)
    assert h["expected_logic"] == "EMA判方向+量比过滤假突破"
    assert h["asset"] == "ETH" and h["timeframe"] == "4h"
    print("[OK] 2. 假设 CRUD 含新字段 (id=%d)" % hid)

    # 3) 实验/策略/报告 CRUD（含新字段）
    eid = db.add_experiment(strategy_name="EMA+量比", indicator_combination=["EMA 双均线", "量比 Volume Ratio"],
                            hypothesis_id=hid, oos_return=12.5, research_score=72.0,
                            grade="A", failure_reason=None)
    e = db.get_experiment(eid)
    assert e["grade"] == "A" and e["research_score"] == 72.0 and e["hypothesis_id"] == hid
    db.add_strategy(name="ETH 趋势突破", applicable_market="趋势市", applicable_timeframe="4h",
                    core_indicators=["EMA 双均线", "量比 Volume Ratio"], failure_env="震荡市",
                    research_score=72.0, grade="A")
    db.add_report(experiment_id=eid, hypothesis_id=hid, grade="A", report_text="# 报告")
    assert len(db.list_reports(10)) == 1
    print("[OK] 3. 实验/策略/报告 CRUD 含新字段")

    # 4) 指标解析 + 评分/等级
    valid, invalid = rl.normalize_indicators(["EMA", "RSI 相对强弱", "量比", "不存在"])
    assert "EMA 双均线" in valid and "量比 Volume Ratio" in valid
    assert "不存在" in invalid
    assert rl.grade_from(70) == "A" and rl.grade_from(50) == "B"
    assert rl.grade_from(30) == "C" and rl.grade_from(20) == "D"
    m_ok = {"sharpe": 1.5, "max_drawdown": 20, "oos_return": 15, "mc_p5": 5,
            "trade_count": 80, "wf_profit_ratio": 60, "wf_windows": 5}
    sc = rl.research_score(m_ok)
    assert sc["grade"] in ("A", "B")
    assert 0 < sc["total"] <= 100
    print("[OK] 4. 指标解析 + 评分/等级 (score=%.1f grade=%s)" % (sc["total"], sc["grade"]))

    # 5) 评审判定
    passed, fails = rl.judge_pass(m_ok)
    assert passed, "示例指标应通过门禁"
    bad = dict(m_ok); bad["sharpe"] = 0.5; bad["trade_count"] = 18; bad["oos_return"] = -3
    passed2, fails2 = rl.judge_pass(bad)
    assert not passed2 and len(fails2) >= 3
    assert any("交易次数" in f for f in fails2) or any("trades" in f.lower() for f in fails2)
    print("[OK] 5. 评审判定 (通过=%s / 失败原因=%d 条)" % (passed, len(fails2)))

    # 6) 防重复研究
    dup = rl.check_duplicate(["EMA 双均线", "量比 Volume Ratio"])
    assert len(dup) >= 1, "应命中历史假设/实验"
    warn = rl.duplicate_warning(["EMA 双均线", "量比 Volume Ratio"])
    assert warn and "重合" in warn
    print("[OK] 6. 防重复研究 (命中 %d 条)" % len(dup))

    # 7) 完整闭环（回测→判定→评分→报告→落库，注入策略工厂，不 import app.py）
    df = _synthetic_df()
    hyp = {
        "id": hid,
        "hypothesis_text": "EMA趋势+量比确认提高突破成功率",
        "related_indicators": json.dumps(["EMA 双均线", "量比 Volume Ratio"], ensure_ascii=False),
        "parameters": json.dumps({"EMA 双均线": {"EMA_short": 15, "EMA_long": 60}}, ensure_ascii=False),
        "asset": "ETH", "timeframe": "4h", "leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0,
        "expected_logic": "EMA判方向", "expected_market_condition": "趋势",
        "risk_assumption": "回撤<30%",
    }
    # 静默引擎的 verbose 风险报告输出（只在控制台，不影响结果）
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        verdict = rl.verify_hypothesis(hyp, df, "ETH", strategy_factory=_strategy_factory)
    for k in ("passed", "failures", "score", "metrics", "report", "experiment_id"):
        assert k in verdict, f"verdict 缺 {k}"
    assert verdict["experiment_id"] is not None
    assert verdict["report"] and "研究报告" in verdict["report"]
    assert "使用因子" in verdict["report"]
    exp = db.get_experiment(verdict["experiment_id"])
    assert exp["hypothesis_id"] == hid and exp["grade"] in ("A", "B", "C", "D")
    assert db.get_hypothesis(hid)["status"] in ("passed", "failed")
    print("[OK] 7. 完整闭环 (passed=%s grade=%s score=%.1f exp#%d)"
          % (verdict["passed"], verdict["score"]["grade"], verdict["score"]["total"], verdict["experiment_id"]))

    # 8) 旧回测不破坏：抽取后的指标 compute 仍可运行
    d = _synthetic_df().iloc[:200]
    for fn, args in [(indicator_schema._ema_cross, (7, 21)),
                     (indicator_schema._donchian, (20,)),
                     (indicator_schema._volume_ratio, (20, 1.5))]:
        dd = d.copy()
        fn(dd, *args)
        assert "_long" in dd.columns and "_short" in dd.columns
    print("[OK] 8. 旧回测不破坏：指标 compute 正常")

    print("\nALL PHASE 2 TESTS PASSED")


if __name__ == "__main__":
    main()
