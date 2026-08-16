"""
自动量化研究实验室 (V3) 测试: strict schema → 解析 → 候选池 → 四阶段漏斗 → 报告
==================================================================================
覆盖: strict_search_prompt / parse_research_plan (risk_config→risk 归一化) /
      build_search_space (真实 schema 去重池) / _quick_backtest (参数进引擎) /
      run_research_pipeline (四阶段端到端, 报告/淘汰统计)。
运行: python test_research_pipeline.py
"""
import os
import sys


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)

    import research_loop as rl

    assert "app" not in sys.modules, "测试前提：research_loop 不应 import app.py"

    # 1) strict_search_prompt 输出严格 schema 字段
    p = rl.strict_search_prompt("寻找ETH趋势策略", mode="standard")
    for k in ("research_plan", "candidates", "risk_config", "position_config"):
        assert k in p, f"提示词缺少字段 {k}"
    assert "deep" not in p or "深度" in p
    print("[OK] 1. strict_search_prompt 输出严格 schema 字段")

    # 2) parse_research_plan: 空输入 → 空候选 + 诊断
    plan, cands, diag = rl.parse_research_plan("")
    assert cands == [] and diag.get("error"), "空输入应返回空候选 + 诊断"
    print("[OK] 2. 空输入 → 空候选 + 诊断")

    # 3) parse_research_plan: 严格 schema + risk_config/position_config 归一化
    text = ('{"research_plan": {"goal": "找趋势", "search_scale": "标准"}, '
            '"candidates": [{"hypothesis":"h1","indicators":["EMA 双均线"],"params":{},'
            '"risk_config":{"leverage":3,"tp_pct":9.0,"sl_pct":4.5},'
            '"position_config":{"_init_alloc_pct":50,"_enable_pyramiding":true,"_pyr_add_pct":0.5,"_pyr_max":2},'
            '"asset":"ETH","timeframe":"4h"}]}')
    plan, cands, diag = rl.parse_research_plan(text)
    assert plan.get("goal") == "找趋势", f"research_plan 解析失败: {plan}"
    assert len(cands) == 1, f"候选数量错误: {len(cands)}"
    c = cands[0]
    assert c.get("risk") == {"leverage": 3, "tp_pct": 9.0, "sl_pct": 4.5}, \
        f"risk_config 未归一化为 risk: {c.get('risk')}"
    assert c.get("position")["_init_alloc_pct"] == 50, "position_config 未归一化为 position"
    print("[OK] 3. 严格 schema 解析 + risk_config/position_config → risk/position 归一化")

    # 4) build_search_space: 从嵌套 risk 读杠杆/TP/SL, 去重池
    space = rl.build_search_space(cands, mode="standard")
    assert len(space) >= 20, f"标准模式候选池过小: {len(space)}"
    assert space[0]["leverage"] == 3 and space[0]["tp_pct"] == 9.0 and space[0]["sl_pct"] == 4.5, \
        "应从 risk_config 读取杠杆/TP/SL"
    assert space[0]["position_params"]["_init_alloc_pct"] == 50
    fps = [rl.full_fingerprint(x["indicators"], x["param_overrides"], x["leverage"],
                               x["tp_pct"], x["sl_pct"], position_params=x["position_params"])
           for x in space]
    assert len(fps) == len(set(fps)), "候选池应无重复指纹"
    # 所有指标参数 key 均来自真实 schema (无死参数)
    for x in space:
        for name, kv in x["param_overrides"].items():
            key = rl._NAME_TO_KEY.get(name)
            assert key, f"指标 {name} 不在 schema"
            valid_keys = set(rl.INDICATOR_SCHEMA[key]["params"].keys())
            for pk in kv:
                assert pk in valid_keys, f"死参数 {name}.{pk}"
    print(f"[OK] 4. build_search_space 生成 {len(space)} 组合, 从 risk_config 读风控, 去重且无死参数")

    # 4b) 多方向规模: 4 个方向 standard 应 ≥ 100 候选 (满足「标准 100~300」)
    from indicator_schema import INDICATOR_REGISTRY
    names = list(INDICATOR_REGISTRY.keys())
    multi = []
    for i in range(4):
        multi.append({"hypothesis": f"方向{i}", "indicators": [names[i % len(names)]],
                      "params": {}, "leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0,
                      "position_config": {"_init_alloc_pct": 50, "_enable_pyramiding": False}})
    std_pool = rl.build_search_space(multi, mode="standard")
    deep_pool = rl.build_search_space(multi, mode="deep")
    assert len(std_pool) >= 100, f"标准模式 4 方向池应 ≥ 100, 实际 {len(std_pool)}"
    assert len(deep_pool) > len(std_pool), "深度模式池应大于标准模式"
    print(f"[OK] 4b. 多方向规模: 标准 {len(std_pool)} / 深度 {len(deep_pool)} 候选")

    # 5) 合成趋势数据 (2021-2024 日线, 含 IS/OOS 切分)
    import numpy as np
    import pandas as pd
    n = 1460
    idx = pd.date_range("2020-01-01", periods=n, freq="1D")
    rng = np.random.default_rng(5)
    t = np.arange(n)
    close = 100 + 0.03 * t + 8 * np.sin(t / 40.0) + np.cumsum(rng.normal(0, 0.4, n))
    df = pd.DataFrame({
        "open": close - np.abs(rng.normal(0, 0.3, n)),
        "high": close + np.abs(rng.normal(0.3, 0.6, n)),
        "low": close - np.abs(rng.normal(0.3, 0.6, n)),
        "close": close,
        "vol": rng.uniform(500, 2000, n),
    }, index=idx)

    # 6) _quick_backtest: 参数真实进入引擎
    qm = rl._quick_backtest(df, "ETH", ["EMA 双均线"], {"EMA 双均线": {"EMA_short": 7}},
                            2, 8.0, 4.0, {"_init_alloc_pct": 30})
    for k in ("total_return", "sharpe", "max_drawdown", "trade_count", "position_metrics"):
        assert k in qm, f"_quick_backtest 缺少 {k}"
    print("[OK] 5. _quick_backtest 真实回测返回指标 + 仓位指标")

    # 7) 参数真实写入 strategy.selected (指标参数 17 → 引擎)
    sel = rl.build_selected(["EMA 双均线"], {"EMA 双均线": {"EMA_short": 17}})
    assert sel["EMA 双均线"]["params"]["EMA_short"] == 17, "指标参数未写入 selected"
    strat = rl._make_strategy(dict(sel), None)
    assert strat.selected["EMA 双均线"]["params"]["EMA_short"] == 17, "参数未到达 strategy"
    print("[OK] 6. 指标参数 17 真实写入 strategy.selected")

    # 8) run_research_pipeline 端到端 (1 方向 standard, 真实引擎)
    progress_log = []
    result = rl.run_research_pipeline([cands[0]], df, "ETH", mode="standard", plan=plan,
                                      progress=lambda i, n, label: progress_log.append((i, n, label)))
    assert "report" in result and result["report"], "应产出研究报告"
    assert result["stage_counts"]["pool"] >= 20, f"候选池大小异常: {result['stage_counts']}"
    for k in ("收益不足", "交易次数过少", "回撤过大", "样本外失败", "过拟合", "风险过高", "回测异常"):
        assert k in result["elimination"], f"淘汰统计缺少 {k}"
    assert isinstance(result["top"], list), "top 应为列表"
    assert len(progress_log) >= result["stage_counts"]["pool"], "progress 回调应覆盖阶段1所有候选"
    print(f"[OK] 7. run_research_pipeline 端到端: 池={result['stage_counts']['pool']} "
          f"淘汰={result['elimination']} 最终={len(result['top'])}")

    print("\nALL RESEARCH PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()
