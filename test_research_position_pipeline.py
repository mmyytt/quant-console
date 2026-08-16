"""
Phase 6 端到端链路测试: AI 输出 → parser → parameter builder → engine → backtest
===============================================================================
验证仓位参数 (init_alloc/pyramiding/add/max_add/move_stop) 全链路一致到达引擎,
且保证金占用率 ≤ 权益 (仓位真实性护栏)。
运行: python test_research_position_pipeline.py
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

    import numpy as np
    import pandas as pd
    import research_loop as rl
    from research_phase1 import make_engine_kwargs, run_single

    # AI 输出的策略 spec (指标 + 仓位 + 风控联合研究)
    spec = {
        "hypothesis": "EMA+斐波那契+量比 趋势回踩, 半仓建仓+两次加仓+移动止损",
        "indicators": ["EMA 双均线", "斐波那契回调", "量比 Volume Ratio"],
        "params": {},
        "asset": "ETH", "timeframe": "1h",
        "leverage": 3, "tp_pct": 8.0, "sl_pct": 4.0,
        "move_stop": True,
        "position": {"_init_alloc_pct": 50, "_enable_pyramiding": True,
                     "_pyr_add_pct": 0.5, "_pyr_max": 2},
    }

    indicators, _inv = rl.normalize_indicators(spec["indicators"])
    assert len(indicators) == 3, f"指标归一化失败: {indicators}"

    hyp = rl._spec_to_hyp(spec, indicators, "ETH")
    pos = rl._position_params_from(spec)

    # Phase 5 校验: 仓位参数存在 + 无死参数
    ok, violations = rl.validate_research_strategy(spec, indicators, pos)
    assert ok, f"仓位参数校验失败: {violations}"

    # 合成强趋势 OHLCV (列名 vol)
    n = 1200
    idx = pd.date_range("2023-01-01", periods=n, freq="1h")
    rng = np.random.default_rng(11)
    close = 100 + np.linspace(0, 60, n) + np.cumsum(rng.normal(0, 1.0, n))
    df = pd.DataFrame({
        "open": close - np.abs(rng.normal(0, 0.3, n)),
        "high": close + np.abs(rng.normal(0.3, 0.6, n)),
        "low": close - np.abs(rng.normal(0.3, 0.6, n)),
        "close": close,
        "vol": rng.uniform(500, 2000, n),
    }, index=idx)

    # 全链路: build_selected → make_engine_kwargs → _make_strategy → run_single → engine
    base_selected = rl.build_selected(indicators, hyp["parameters"])
    kw = make_engine_kwargs(hyp["leverage"], hyp["tp_pct"], hyp["sl_pct"], **pos)
    strategy = rl._make_strategy(dict(base_selected), None)
    res, m = run_single(df, "ETH", strategy, kw)

    # 仓位参数真实到达引擎 (run_single 注入 strategy.selected)
    sel = strategy.selected
    assert sel["_pos_mode"] == "fixed_capital", "研究路径应使用 fixed_capital 使 init_alloc 生效"
    assert sel["_init_alloc_pct"] == 50, f"_init_alloc_pct 未到达引擎: {sel['_init_alloc_pct']}"
    assert sel["_enable_pyramiding"] is True, "_enable_pyramiding 未到达引擎"
    assert sel["_pyr_add_pct"] == 0.5, f"_pyr_add_pct 未到达引擎: {sel['_pyr_add_pct']}"
    assert sel["_pyr_max"] == 2, f"_pyr_max 未到达引擎: {sel['_pyr_max']}"
    assert sel["_pyr_trail"] is True, "move_stop 别名未映射到 _pyr_trail"
    print(f"[OK] 仓位参数到达引擎: init_alloc={sel['_init_alloc_pct']}% "
          f"pyramiding={sel['_enable_pyramiding']} add={sel['_pyr_add_pct']} "
          f"max_add={sel['_pyr_max']} move_stop={sel['_pyr_trail']}")

    # 仓位真实性: 保证金占用率 ≤ 权益 (引擎护栏 + 事件回放复验)
    pm = rl._position_metrics(res, hyp["leverage"])
    assert pm["max_margin_usage"] <= 100.0, f"保证金占用率超权益: {pm['max_margin_usage']}%"
    assert set(pm) >= {"max_margin_usage", "avg_margin_usage", "max_effective_leverage",
                       "avg_position_ratio", "add_count", "positions_with_add", "total_trades"}
    print(f"[OK] 仓位真实性: 最大保证金占用率 {pm['max_margin_usage']}% ≤ 100%, "
          f"交易 {pm['total_trades']} 笔, 加仓 {pm['add_count']} 次")

    # 生产路径 run_hypothesis_backtest 也产出 position_metrics (端到端闭环)
    out = rl.run_hypothesis_backtest(df, "ETH", indicators, hyp["parameters"],
                                     hyp["leverage"], hyp["tp_pct"], hyp["sl_pct"],
                                     position_params=pos)
    assert "position_metrics" in out, "run_hypothesis_backtest 应产出 position_metrics"
    assert out["position_metrics"]["max_margin_usage"] <= 100.0
    print(f"[OK] 生产路径 run_hypothesis_backtest 产出 position_metrics, "
          f"最大保证金占用率 {out['position_metrics']['max_margin_usage']}% ≤ 100%")

    print("\nALL POSITION PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()
