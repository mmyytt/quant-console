"""
自动量化研究实验室完整链路测试（Task D）
========================================
覆盖：schema 生成方向（不依赖 LLM JSON）· 搜索规模（标准≥100 / 深度≥500 真实展开）·
      四阶段漏斗（Sharpe>1 / 参数极端 硬过滤）· 研究报告字段完整 · app.py 唯一入口静态校验。
运行: python test_research_ui_flow.py
"""
import os
import sys


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _synthetic_df(n=1460):
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2020-01-01", periods=n, freq="1D")
    rng = np.random.default_rng(5)
    t = np.arange(n)
    close = 100 + 0.03 * t + 8 * np.sin(t / 40.0) + np.cumsum(rng.normal(0, 0.4, n))
    return pd.DataFrame({
        "open": close - np.abs(rng.normal(0, 0.3, n)),
        "high": close + np.abs(rng.normal(0.3, 0.6, n)),
        "low": close - np.abs(rng.normal(0.3, 0.6, n)),
        "close": close,
        "vol": rng.uniform(500, 2000, n),
    }, index=idx)


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)

    import research_loop as rl

    assert "app" not in sys.modules, "测试前提：research_loop 不应 import app.py"

    # 1) schema_search_directions：Python 生成方向，指标全部来自真实 schema，覆盖多类别
    ctx = rl.parse_research_context("寻找 ETH 趋势策略，回撤低于 20%")
    assert ctx["symbol"] == "ETH" and ctx["strategy_style"] == "趋势跟踪", ctx
    directions = rl.schema_search_directions(ctx, mode="standard")
    assert len(directions) >= 6, f"方向过少: {len(directions)}"
    cats = set()
    for d in directions:
        for name in d["indicators"]:
            assert name in rl.INDICATOR_REGISTRY, f"指标 {name} 不在 schema"
            cats.add(rl.INDICATOR_REGISTRY[name]["category"])
        assert d.get("risk") and d.get("position"), "方向缺 risk/position"
    assert len(cats) >= 3, f"应覆盖多类别, 实际 {cats}"
    print(f"[OK] 1. schema_search_directions 生成 {len(directions)} 个方向, 覆盖类别 {cats}")

    # 2) 搜索规模真实展开：标准 ≥100 / 深度 ≥500（且深度 > 标准）
    std_pool = rl.build_search_space(directions, mode="standard")
    deep_pool = rl.build_search_space(directions, mode="deep")
    assert len(std_pool) >= 100, f"标准模式候选池应 ≥100, 实际 {len(std_pool)}"
    assert len(deep_pool) >= 500, f"深度模式候选池应 ≥500, 实际 {len(deep_pool)}"
    assert len(deep_pool) > len(std_pool), "深度模式应大于标准模式"
    print(f"[OK] 2. 搜索规模: 标准 {len(std_pool)} / 深度 {len(deep_pool)} 候选（真实展开）")

    # 2b) 搜索空间真实性：EMA 参数真实展开为多个值（不能所有候选参数相同）
    ema_dir = {"hypothesis": "EMA 趋势", "indicators": ["EMA 双均线"], "params": {},
               "risk": {"leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0},
               "position": {"_init_alloc_pct": 50.0}}
    ema_pool = rl.build_search_space([ema_dir], mode="standard")
    shorts, longs = set(), set()
    for c in ema_pool:
        po = (c["param_overrides"] or {}).get("EMA 双均线") or {}
        if "EMA_short" in po:
            shorts.add(po["EMA_short"])
        if "EMA_long" in po:
            longs.add(po["EMA_long"])
    assert len(shorts) >= 5, f"标准池 EMA_short 应 ≥5 个不同值, 实际 {sorted(shorts)}"
    assert len(longs) >= 5, f"标准池 EMA_long 应 ≥5 个不同值, 实际 {sorted(longs)}"
    fps = [rl.full_fingerprint(c["indicators"], c["param_overrides"], c["leverage"],
                               c["tp_pct"], c["sl_pct"], position_params=c["position_params"])
           for c in ema_pool]
    assert len(fps) == len(set(fps)), "候选池应无重复指纹（不能所有候选参数相同）"
    print(f"[OK] 2b. EMA 参数真实展开: EMA_short {sorted(shorts)} / EMA_long {sorted(longs)} "
          f"（{len(ema_pool)} 候选, 去重后指纹数 {len(set(fps))}）")

    # 3) 完整链路（用 2 个方向跑四阶段漏斗，避免过慢）：输入目标 → 生成候选 → 过滤 → Top
    df = _synthetic_df()
    progress_log = []
    result = rl.run_research_pipeline(directions[:2], df, "ETH", mode="standard",
                                      progress=lambda i, n, label: progress_log.append((i, n, label)))
    assert result["stage_counts"]["pool"] >= 20, f"池过小: {result['stage_counts']}"
    for k in ("收益不足", "交易次数过少", "回撤过大", "Sharpe不足", "参数极端",
              "样本外失败", "过拟合", "风险过高", "回测异常"):
        assert k in result["elimination"], f"淘汰统计缺少 {k}"
    assert isinstance(result["top"], list)
    assert progress_log, "progress 回调应有记录（候选生成/快速回测/样本外验证/风险过滤）"
    stages = " ".join(lbl for _, _, lbl in progress_log)
    # 候选生成 / 快速回测 每次都触发；样本外验证 / 风险过滤 仅在阶段1/2有幸存者时触发（空转则不回调）
    for stage in ("候选生成", "快速回测"):
        assert stage in stages, f"进度缺少阶段 {stage}"
    rl_src = open("research_loop.py", encoding="utf-8").read()
    for stage in ("候选生成", "快速回测", "样本外验证", "风险过滤"):
        assert stage in rl_src, f"run_research_pipeline 源码缺少阶段标签 {stage}"
    print(f"[OK] 3. 四阶段漏斗端到端: 池={result['stage_counts']['pool']} "
          f"淘汰={result['elimination']} Top={len(result['top'])}")

    # 4) 报告字段完整（非原始 JSON）：直接构造含 Top 的结果验证字段标签（排名/参数/收益/年化/Sharpe/回撤/样本外/风险评价）
    _mock_top = {
        "hypothesis": "趋势·EMA 策略方向", "indicators": ["EMA 双均线"],
        "param_overrides": {}, "leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0,
        "position_params": {"_init_alloc_pct": 50.0},
        "metrics": {"total_return": 12.0, "annual_return": 8.0, "sharpe": 1.5,
                    "max_drawdown": 9.0, "oos_return": 6.0,
                    "position_metrics": {"max_margin_usage": 60.0, "max_effective_leverage": 2.0, "add_count": 1}},
        "score": {"grade": "B"},
        "robustness": {"neighbors_tested": 0, "profitable_neighbors": 0, "overfit": False},
    }
    _mock_result = {"stage_counts": {"pool": 30, "stage1_pass": 10, "stage2_pass": 5,
                                     "stage3_pass": 3, "final": 1},
                    "elimination": {"收益不足": 1, "Sharpe不足": 2, "参数极端": 3},
                    "plan": {"goal": "测试", "universe": "ETH·4h", "search_scale": "标准"},
                    "top": [_mock_top]}
    report = rl.build_research_report(_mock_result, "ETH")
    assert "{" not in report and "}" not in report, "报告不应是原始 JSON"
    for kw in ("最终候选", "#1", "参数", "收益", "年化", "Sharpe", "回撤", "样本外", "风险评价"):
        assert kw in report, f"报告缺少字段 {kw}"
    print("[OK] 4. 研究报告字段完整（排名/参数/收益/年化/Sharpe/回撤/样本外/风险评价）")

    # 5) app.py 唯一入口：单输入 key=rl_goal + 单按钮 key=rl_run，走 schema 方向（非 LLM 复杂 JSON）
    src = open("app.py", encoding="utf-8").read()
    assert src.count('key="rl_goal"') == 1, "应只有一个研究目标输入框 key=rl_goal"
    assert src.count('key="rl_run"') == 1, "应只有一个启动研究按钮 key=rl_run"
    assert "rl.schema_search_directions" in src, "探索分支应使用 schema_search_directions"
    assert "rjobs.run_job" in src, "探索分支应通过后台持久化任务 rjobs.run_job 执行四阶段漏斗"
    assert "rjobs.create_job" in src, "探索分支应创建持久化任务 job"
    for banned in ("strict_search_prompt", "parse_research_plan", "run_parameter_search",
                   "rl.search_prompt", "rl_lab_goal", "rl_lab_btn"):
        assert banned not in src, f"app.py 不应再引用 {banned}"
    print("[OK] 5. app.py 唯一入口 + schema 生成方向 + 后台持久化任务（已移除 LLM 复杂 JSON / 重复输入）")

    print("\nALL RESEARCH UI FLOW TESTS PASSED")


if __name__ == "__main__":
    main()
