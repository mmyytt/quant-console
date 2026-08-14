"""
Phase 3 测试:AI 主动研究能力增强
覆盖:失败记忆表/新字段 · 失败策略进入记忆 · 重复策略识别(指纹+相似度) ·
      参数敏感性分析 · 评分(新权重)+过拟合风险 · 旧回测不破坏
运行: python test_research_phase3.py
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


def _strategy_factory(selected, dead=False):
    """注入策略工厂（不 import app.py）。dead=True → 永不交易（保证判定失败）。"""
    from engine_core import StrategyBase

    class _Trend(StrategyBase):
        def __init__(self):
            super().__init__("test_trend")
            self.selected = selected

        def generate_signals(self, df):
            df = df.copy()
            if dead:
                df["signal"] = 0
            else:
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
    db.DB_PATH = os.path.join(tmp, "test_phase3.db")

    # 1) DB 迁移：failure_memory 表 + Phase 3 新字段
    db.init_db()
    conn = __import__("sqlite3").connect(db.DB_PATH)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    hyp_cols = {r[1] for r in conn.execute("PRAGMA table_info(research_hypothesis)")}
    exp_cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_experiments)")}
    lib_cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_library)")}
    fm_cols = {r[1] for r in conn.execute("PRAGMA table_info(research_failure_memory)")}
    conn.close()
    assert "research_failure_memory" in tables, "缺 research_failure_memory 表"
    assert "failure_environment" in hyp_cols, "research_hypothesis 缺 failure_environment"
    for c in ("fingerprint", "overfitting_risk", "param_stability"):
        assert c in exp_cols, f"strategy_experiments 缺 {c}"
    for c in ("indicator_roles", "param_stable_range", "overfitting_risk", "validation_count"):
        assert c in lib_cols, f"strategy_library 缺 {c}"
    for c in ("fingerprint", "failure_reason", "failure_env", "avoid"):
        assert c in fm_cols, f"research_failure_memory 缺 {c}"
    print("[OK] 1. DB 迁移 + failure_memory 表 + Phase 3 新字段")

    # 2) 指纹 + 相似度 + 重复识别
    assert rl.fingerprint(["EMA 双均线", "量比 Volume Ratio"]) == "EMA_VOLUME_RATIO", \
        f"指纹错误: {rl.fingerprint(['EMA 双均线', '量比 Volume Ratio'])}"
    sim = rl.strategy_similarity(["EMA 双均线", "量比 Volume Ratio"],
                                 {"EMA 双均线": {"EMA_short": 15, "EMA_long": 60}},
                                 ["EMA 双均线", "量比 Volume Ratio"],
                                 {"EMA 双均线": {"EMA_short": 20, "EMA_long": 50}})
    assert sim >= 0.8, f"同指标不同参数应判定高度相似, 实际 {sim}"
    hid = db.add_hypothesis("EMA趋势+量比确认", related_indicators=["EMA 双均线", "量比 Volume Ratio"])
    hits = rl.check_duplicate(["EMA 双均线", "量比 Volume Ratio"])
    assert len(hits) >= 1, "应识别历史重复假设"
    roles = rl.indicator_roles(["EMA 双均线", "量比 Volume Ratio"])
    assert "EMA 双均线" in roles and "量比 Volume Ratio" in roles
    print("[OK] 2. 指纹 + 相似度 + 重复识别 (sim=%.2f 命中%d)" % (sim, len(hits)))

    # 3) 参数敏感性分析 + 过拟合风险
    df = _synthetic_df()
    indicators = ["EMA 双均线", "量比 Volume Ratio"]
    params = {"EMA 双均线": {"EMA_short": 15, "EMA_long": 60},
              "量比 Volume Ratio": {"VR_period": 30, "VR_threshold": 1.7}}
    with contextlib.redirect_stdout(io.StringIO()):
        sen = rl.sensitivity_analysis(df, "ETH", indicators, params, 2, 8.0, 4.0,
                                      strategy_factory=_strategy_factory)
    for k in ("stable_ranges", "param_viability", "stability", "overfitting",
              "points_tested", "points_viable", "base_metrics", "grid"):
        assert k in sen, f"sensitivity 缺 {k}"
    assert 0 <= sen["stability"] <= 100
    assert sen["points_tested"] >= len(params), "应至少逐参数各测 1 点"
    assert rl.overfitting_risk(80) == "Low"
    assert rl.overfitting_risk(50) == "Medium"
    assert rl.overfitting_risk(20) == "High"
    print("[OK] 3. 参数敏感性 (stability=%.1f overfit=%s 测点%d)"
          % (sen["stability"], sen["overfitting"], sen["points_tested"]))

    # 4) 评分（新权重 20/20/20/20/10/10）+ 等级
    m_ok = {"total_return": 150, "annual_return": 30, "sharpe": 1.5, "max_drawdown": 20,
            "win_rate": 55, "profit_factor": 1.8, "trade_count": 80,
            "oos_return": 15, "oos_sharpe": 1.2, "oos_mdd": 18, "wf_profit_ratio": 60,
            "wf_windows": 5, "wf_profitable": 3, "mc_p5": 5, "max_consecutive_losses": 4}
    sc = rl.research_score(m_ok)
    for k in ("return", "sharpe", "mdd", "oos", "param_stability", "monte_carlo"):
        assert k in sc, f"评分缺分量 {k}"
    assert 0 < sc["total"] <= 100 and sc["grade"] in ("A", "B", "C", "D")
    sc_stable = rl.research_score(m_ok, param_stability=90.0)
    assert sc_stable["param_stability"] == 90.0, "参数稳定性分量应精确反映"
    # 收益分量上限 100：150% 收益 / 200 = 0.75 → 75 分
    assert 0 <= sc["return"] <= 100, f"收益分量异常 {sc['return']}"
    print("[OK] 4. 评分新权重 (total=%.1f grade=%s return=%.1f param_stability=%.1f)"
          % (sc["total"], sc["grade"], sc["return"], sc["param_stability"]))

    # 5) 失败策略进入记忆 + 完整闭环（死策略 → 判定失败 → 写入 failure_memory）
    hyp = {
        "id": hid, "hypothesis_text": "EMA趋势+量比确认提高突破成功率",
        "related_indicators": json.dumps(indicators, ensure_ascii=False),
        "parameters": json.dumps(params, ensure_ascii=False),
        "asset": "ETH", "timeframe": "4h", "leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0,
        "expected_logic": "EMA判方向", "expected_market_condition": "趋势",
        "failure_environment": "震荡市", "risk_assumption": "回撤<30%",
    }
    with contextlib.redirect_stdout(io.StringIO()):
        verdict = rl.verify_hypothesis(hyp, df, "ETH",
                                       strategy_factory=lambda s: _strategy_factory(s, dead=True))
    assert verdict["passed"] is False, "死策略应判定失败"
    assert verdict["fingerprint"] == "EMA_VOLUME_RATIO"
    fm = db.list_failure_memory(10)
    assert len(fm) >= 1, "失败策略应进入失败记忆"
    assert fm[0]["fingerprint"] == "EMA_VOLUME_RATIO"
    assert fm[0]["avoid"] == 1
    # 报告 14 段
    for sec in ("## 研究目标", "## 策略假设", "## 为什么认为有效", "## 使用因子解释",
                "## 参数", "## 历史表现", "## 样本外表现", "## Walk Forward",
                "## Monte Carlo", "## 参数稳定性", "## 风险", "## 适用环境",
                "## 不适用环境", "## 最终建议"):
        assert sec in verdict["report"], f"报告缺章节 {sec}"
    # 失败记忆可被重复识别命中
    dup_fm = rl.check_duplicate(indicators)
    assert any(d["type"] == "failure_memory" for d in dup_fm), "失败记忆应被相似度识别"
    print("[OK] 5. 失败记忆 + 完整闭环 (passed=%s fm=%d 报告14段)"
          % (verdict["passed"], len(fm)))

    # 6) 旧回测不破坏：指标 compute 仍可运行
    d = _synthetic_df().iloc[:200]
    for fn, args in [(indicator_schema._ema_cross, (7, 21)),
                     (indicator_schema._donchian, (20,)),
                     (indicator_schema._volume_ratio, (20, 1.5))]:
        dd = d.copy()
        fn(dd, *args)
        assert "_long" in dd.columns and "_short" in dd.columns
    print("[OK] 6. 旧回测不破坏：指标 compute 正常")

    print("\nALL PHASE 3 TESTS PASSED")


if __name__ == "__main__":
    main()
