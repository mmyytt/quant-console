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
    print("\nALL SEARCH TESTS PASSED")


if __name__ == "__main__":
    main()
