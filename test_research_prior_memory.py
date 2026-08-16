"""
研究经验记忆层（Research Prior Memory）测试 (Task E)
====================================================
覆盖：
  (a) EMA 参数池同时包含经验优先参数 与 扩展探索参数
  (b) 经验优先比例约 70%（不 100% 关闭探索）
  (c) 极端边界参数不占主导
  (d) 候选/结果记录 parameter_prior_score（报告含「参数合理性」星级 + 「研究经验」计数）
  (e) engine_core.py / 回测 / 过滤逻辑未被改动（静态边界检查）
运行: python test_research_prior_memory.py
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

    import research_memory as rmem
    import research_loop as rl

    assert "app" not in sys.modules, "测试前提：research_memory/research_loop 不应 import app.py"

    # (a) EMA 参数池同时包含经验优先 + 扩展探索参数
    grid = rmem.prior_grid("ema", "EMA_short", n=10)
    pref = set(rmem.INDICATOR_PRIORS["ema"]["EMA_short"]["preferred"])
    ext = set(rmem.INDICATOR_PRIORS["ema"]["EMA_short"]["extended"])
    assert grid and set(grid) & pref, f"EMA_short 网格应含经验优先参数, 实际 {grid}"
    assert set(grid) & ext, f"EMA_short 网格应含扩展探索参数（不关闭探索）, 实际 {grid}"
    print(f"[OK] a. EMA_short 网格同时含经验优先+探索: {grid}")

    # (b) 经验优先比例约 70%（n=10 → 7 经验 / 3 探索）
    n_pref = len([v for v in grid if v in pref])
    n_ext = len([v for v in grid if v in ext])
    ratio = n_pref / len(grid)
    assert 0.6 <= ratio <= 0.8, f"经验优先比例应约 70%, 实际 {ratio:.2f} ({n_pref}/{len(grid)})"
    assert n_ext >= 1, "探索参数不应为 0（不能 100% 关闭探索）"
    print(f"[OK] b. 经验优先比例约 {ratio:.2f}（经验 {n_pref} / 探索 {n_ext}）")

    # (c) 极端边界参数不占主导
    lo, hi = 3, 50
    boundary = len([v for v in grid if v in (lo, hi)])
    assert boundary <= len(grid) // 2, f"极端边界参数不应占主导: {grid}"
    print(f"[OK] c. 极端边界参数占比 {boundary}/{len(grid)}（不占主导）")

    # (d) parameter_prior_score 记录：候选 + 报告字段
    assert rmem.param_prior_score("ema", "EMA_short", 8) == 1.0   # preferred
    assert rmem.param_prior_score("ema", "EMA_short", 3) == 0.5   # extended
    assert rmem.param_prior_score("ema", "EMA_short", 50, is_extreme=True) == 0.1  # 极端
    ema_dir = {"hypothesis": "EMA 趋势", "indicators": ["EMA 双均线"], "params": {},
               "risk": {"leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0},
               "position": {"_init_alloc_pct": 50.0},
               "timeframe": "1h", "strategy_style": "趋势跟踪"}
    pool = rl.build_search_space([ema_dir], mode="standard")
    assert pool and all("prior_score" in c for c in pool), "每个候选应记录 prior_score"
    mock_top = {"hypothesis": "EMA 趋势", "indicators": ["EMA 双均线"], "param_overrides": {},
                "leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0,
                "position_params": {"_init_alloc_pct": 50.0},
                "metrics": {"total_return": 10.0, "annual_return": 8.0, "sharpe": 1.5,
                            "max_drawdown": 9.0, "oos_return": 6.0,
                            "position_metrics": {"max_margin_usage": 60.0, "max_effective_leverage": 2.0, "add_count": 0}},
                "score": {"grade": "B"},
                "robustness": {"neighbors_tested": 0, "profitable_neighbors": 0, "overfit": False},
                "prior_score": 1.0}
    result = {"stage_counts": {"pool": 8, "stage1_pass": 4, "stage2_pass": 2, "stage3_pass": 1, "final": 1},
              "elimination": {}, "plan": {"goal": "测试", "universe": "ETH·1h", "search_scale": "标准"},
              "prior_stats": {"preferred": 5, "exploration": 3, "total": 8}, "top": [mock_top]}
    report = rl.build_research_report(result, "ETH")
    assert "参数合理性" in report, "报告应含「参数合理性」星级"
    assert "研究经验" in report, "报告应含「研究经验」区块"
    assert "经验参数：**5** 个" in report, "报告应显示经验参数计数"
    assert "探索参数：3 个" in report, "报告应显示探索参数计数"
    print("[OK] d. prior_score 记录 + 报告含「参数合理性」星级与「研究经验」计数")

    # (e) engine_core.py / 回测 / 过滤逻辑未被改动（静态边界检查）
    import re
    rmem_src = open("research_memory.py", encoding="utf-8").read()
    assert not re.search(r"^\s*(import|from)\s+(app|engine_core)\b", rmem_src, re.M), \
        "research_memory 不应 import app.py / engine_core.py"
    for banned in ("def run_single", "def compute_pnl", "place_order", "def _execute"):
        assert banned not in rmem_src, f"research_memory 不应含 {banned}"
    print("[OK] e. research_memory 不 import engine_core/app，无撮合/PnL/风控执行逻辑")

    print("\nALL RESEARCH PRIOR MEMORY TESTS PASSED")


if __name__ == "__main__":
    main()
