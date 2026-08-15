"""
策略搜索模式（V2）测试：parse_hypothesis_array + run_strategy_search
============================================================
覆盖：JSON 数组解析 · 真实 verify_hypothesis 链路（不 mock）· 排序 · 去重 · 无 import app
运行: python test_research_search.py
"""
import os
import sys
import tempfile


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
    import research_storage.db as db
    from indicator_schema import INDICATOR_REGISTRY

    assert "app" not in sys.modules, "测试前提：app 不应已加载"

    # 1) parse_hypothesis_array
    arr_text = '[{"indicators":["EMA 双均线"]},{"indicators":["RSI 相对强弱"]}]'
    arr = rl.parse_hypothesis_array(arr_text)
    assert len(arr) == 2 and arr[0]["indicators"] == ["EMA 双均线"], f"解析失败: {arr}"
    assert rl.parse_hypothesis_array("无 JSON 输出") == []
    assert rl.parse_hypothesis_array("") == []
    print(f"[OK] 1. parse_hypothesis_array：数组解析正确（{len(arr)} 个），非法/空输入返回 []")

    # 2) run_strategy_search（真实 verify_hypothesis 链路，不 mock）
    tmp = tempfile.mkdtemp()
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()

    names = list(INDICATOR_REGISTRY.keys())
    n1, n2 = names[0], names[1]

    # 合成 OHLCV（列名 vol，同 test_no_ui_import.py）
    n = 900
    idx = pd.date_range("2023-01-01", periods=n, freq="1h")
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0.03, 1.2, n))
    df = pd.DataFrame({
        "open": close - np.abs(rng.normal(0, 0.3, n)),
        "high": close + np.abs(rng.normal(0.3, 0.5, n)),
        "low": close - np.abs(rng.normal(0.3, 0.5, n)),
        "close": close,
        "vol": rng.uniform(500, 2000, n),
    }, index=idx)

    def _cand(tag, inds):
        return {"hypothesis": f"候选{tag}", "indicators": inds, "params": {},
                "asset": "ETH", "timeframe": "1h", "leverage": 2,
                "tp_pct": 8.0, "sl_pct": 4.0, "expected_logic": "测试",
                "expected_market_condition": "趋势", "failure_environment": "震荡",
                "risk_assumption": "回撤<20%"}

    candidates = [_cand("A", [n1]), _cand("B", [n2]), _cand("C", ["不存在的指标XYZ"])]

    progress_log = []
    results = rl.run_strategy_search(candidates, df, "ETH",
                                     progress=lambda i, n, label: progress_log.append((i, n, label)))
    assert "app" not in sys.modules, "run_strategy_search 意外 import 了 app.py！"
    assert len(results) == 3, f"预期 3 个结果，实际 {len(results)}"
    assert len(progress_log) == 3, f"progress 回调次数 {len(progress_log)}"

    # 候选C 无有效指标 → 跳过
    c_result = next(r for r in results if r["spec"] is candidates[2])
    assert c_result["skipped"] and c_result["reason"] == "无有效指标"

    # 候选A/B 跑真实 verify（不 mock），有完整 verdict
    for r in results:
        if not r["skipped"]:
            assert "verdict" in r and "metrics" in r["verdict"], "候选应返回完整 verdict"

    # 排序：跳过项 score 视为 -999 排最后
    scores = [(r.get("verdict") or {}).get("score", {}).get("total", -999.0) for r in results]
    assert scores == sorted(scores, reverse=True), f"未按综合分降序: {scores}"

    # 3) 失败记忆去重：相同指标组合第二次应被跳过
    fp = rl.fingerprint([n1])
    db.add_failure_memory(strategy_name="dup", indicator_combination=[n1], parameters={},
                          fingerprint=fp, failure_reason="测试失败", avoid=1)
    results2 = rl.run_strategy_search(candidates[:1], df, "ETH")
    dup = results2[0]
    assert dup["skipped"] and "历史失败" in dup["reason"], f"去重未生效: {dup}"

    print(f"[OK] 2. run_strategy_search：真实 verify 链路跑通（{len(progress_log)} 步回调），排序正确")
    print(f"[OK] 3. 失败记忆去重：相同指标组合第二次被跳过（{dup['reason']}）")

    # 4) V3 参数空间搜索：full_fingerprint / expand_parameter_grid / run_parameter_search
    fp1 = rl.full_fingerprint(["EMA 双均线"], {"EMA 双均线": {"EMA_short": 7}}, 2, 8.0, 4.0)
    fp2 = rl.full_fingerprint(["EMA 双均线"], {"EMA 双均线": {"EMA_short": 50}}, 2, 8.0, 4.0)
    fp3 = rl.full_fingerprint(["EMA 双均线"], {"EMA 双均线": {"EMA_short": 7}}, 5, 8.0, 4.0)
    assert fp1 != fp2, "不同参数应产生不同完整指纹"
    assert fp1 != fp3, "不同杠杆应产生不同完整指纹"
    assert fp1 == rl.full_fingerprint(["EMA 双均线"], {"EMA 双均线": {"EMA_short": 7}}, 2, 8.0, 4.0)
    print(f"[OK] 4. full_fingerprint：指标+参数+杠杆+TP/SL 全纳入，参数不同指纹不同")

    direction = {"indicators": ["EMA 双均线"], "params": {}, "leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0}
    combos = rl.expand_parameter_grid(direction, max_combos=20)
    labels = [c["label"] for c in combos]
    assert any("杠杆" in l for l in labels), "应包含杠杆扫描"
    assert any("TP" in l for l in labels), "应包含 TP/SL 扫描"
    assert any("EMA_short" in l for l in labels), "应包含指标主参数扫描"
    assert combos[0]["label"] == "基准参数", "第一个应为基准参数"
    assert len(combos) <= 20, "应受 max_combos 上限约束"
    # 去重：组合无完全重复
    fps = [rl.full_fingerprint(direction["indicators"], c["param_overrides"], c["leverage"], c["tp_pct"], c["sl_pct"]) for c in combos]
    assert len(fps) == len(set(fps)), "展开后不应有完全重复组合"
    print(f"[OK] 5. expand_parameter_grid：杠杆/TP·SL/主参数三轴展开（{len(combos)} 组合），有界且去重")

    _orig_bt = rl.run_hypothesis_backtest

    def _fake_backtest(df, coin, indicator_names, param_overrides=None,
                       leverage=2, tp_pct=8.0, sl_pct=4.0, strategy_factory=None):
        # 用参数产生可区分的 sharpe，验证排序
        s = 0.5 + (param_overrides or {}).get(indicator_names[0], {}).get("EMA_short", 7) / 100.0
        return {"total_return": 10.0, "annual_return": 15.0, "sharpe": s, "max_drawdown": 12.0,
                "win_rate": 0.5, "profit_factor": 1.4, "trade_count": 40, "max_consecutive_losses": 4,
                "leak_count": 0, "oos_return": 4.0, "oos_sharpe": 0.9, "oos_mdd": 8.0, "oos_trades": 10,
                "mc_p5": 1.0, "wf_avg_oos": 2.0, "wf_profit_ratio": 55.0, "wf_windows": 3, "wf_profitable": 2}

    try:
        rl.run_hypothesis_backtest = _fake_backtest
        directions = [
            {"hypothesis": "方向A", "indicators": ["EMA 双均线"], "params": {},
             "asset": "ETH", "timeframe": "1h", "leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0,
             "expected_logic": "趋势", "expected_market_condition": "趋势",
             "failure_environment": "震荡", "risk_assumption": "回撤<20%"},
            {"hypothesis": "坏方向", "indicators": ["不存在的指标XYZ"], "params": {}},
        ]
        results = rl.run_parameter_search(directions, df, "ETH")
        ok = [r for r in results if not r.get("skipped")]
        skipped = [r for r in results if r.get("skipped")]
        assert len(ok) >= 1, "至少一个有效方向应有实验"
        assert any(r["reason"] == "无有效指标" for r in skipped), "无效方向应跳过"
        # 排序降序
        scores = [r["verdict"]["score"]["total"] for r in ok]
        assert scores == sorted(scores, reverse=True), f"未按综合分降序: {scores}"
        # 每个有效实验都落库（含 tp_pct/sl_pct）
        exps = db.list_experiments(500)
        assert any(e.get("tp_pct") is not None for e in exps), "实验应记录 tp_pct"
        assert any(e.get("sl_pct") is not None for e in exps), "实验应记录 sl_pct"
        print(f"[OK] 6. run_parameter_search：方向展开 + 逐个回测 + 落库（tp/sl）+ 降序排名（{len(ok)} 实验）")
    finally:
        rl.run_hypothesis_backtest = _orig_bt

    print("\nALL SEARCH TESTS PASSED")


if __name__ == "__main__":
    main()
