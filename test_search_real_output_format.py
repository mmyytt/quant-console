"""
AI 研究舱 V4 搜索链路真实输出格式回归测试
============================================
覆盖：
  1. parse_hypothesis_array_diag 容错：纯 JSON 数组 / markdown JSON / 中文说明+JSON / 多候选
  2. 解析失败时返回完整 diag（raw_len/preview/extracted/error），不静默返回 []
  3. 搜索链路：parse → run_parameter_search（mock 回测）全流程跑通
  4. app.py 统一入口调用链：rl_run → intent_prompt/parse_intent → 探索(search_prompt/run_parameter_search) / 验证(hypothesis_prompt/verify_hypothesis)
运行: python test_search_real_output_format.py
"""
import os
import sys
import json
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

    import research_loop as rl
    import research_storage.db as db

    tmp = tempfile.mkdtemp()
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()

    cand = {
        "hypothesis": "EMA 趋势策略", "indicators": ["EMA 双均线"], "params": {},
        "asset": "ETH", "timeframe": "1h", "leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0,
        "strategy_style": "趋势跟踪", "entry_rules": ["EMA 金叉"], "exit_rules": ["EMA 死叉"],
        "expected_logic": "趋势", "expected_market_condition": "趋势",
        "failure_environment": "震荡", "risk_assumption": "回撤<20%",
    }
    cand2 = dict(cand, hypothesis="RSI 均值回归策略", indicators=["RSI 相对强弱"])

    # 1) 四种 LLM 返回格式，全部应解析出 2 个候选
    formats = {
        "纯JSON数组": json.dumps([cand, cand2], ensure_ascii=False),
        "markdown JSON": "```json\n" + json.dumps([cand, cand2], ensure_ascii=False) + "\n```",
        "中文说明+JSON": "以下是策略方向，覆盖趋势与均值回归：\n" + json.dumps([cand, cand2], ensure_ascii=False),
        "多候选策略": "```\n" + json.dumps([cand, cand2], ensure_ascii=False) + "\n```\n以上共 2 个方向。",
    }
    for name, text in formats.items():
        arr, diag = rl.parse_hypothesis_array_diag(text)
        assert len(arr) == 2, f"[{name}] 应解析出 2 个候选，实际 {len(arr)}：{diag}"
        assert diag["error"] is None, f"[{name}] 不应有解析错误：{diag['error']}"
        print(f"[OK] 1.{name}：解析出 {len(arr)} 个候选")

    # 2) 解析失败诊断：纯文字 / 空输入，返回完整 diag 而非静默 []
    raw_text = "这是纯文字，没有 JSON"
    arr, diag = rl.parse_hypothesis_array_diag(raw_text)
    assert arr == [] and diag["error"], "纯文字应返回 [] 且带 error"
    assert diag["raw_len"] == len(raw_text), "diag 应含 raw_len"
    assert diag["preview"] == raw_text, "diag 应含前 500 字符 preview"
    assert diag["extracted"] is None, "未提取到 JSON 时 extracted 应为 None"
    arr2, diag2 = rl.parse_hypothesis_array_diag("")
    assert arr2 == [] and diag2["error"] == "空输入"
    print("[OK] 2. 解析失败返回完整 diag（raw_len/preview/extracted/error），不静默返回 []")

    # 3) 搜索链路：parse → run_parameter_search（mock 回测）全流程
    import numpy as np
    import pandas as pd
    _orig_bt = rl.run_hypothesis_backtest

    def _fake_backtest(df, coin, indicator_names, param_overrides=None,
                       leverage=2, tp_pct=8.0, sl_pct=4.0, strategy_factory=None):
        return {"total_return": 10.0, "annual_return": 15.0, "sharpe": 1.5, "max_drawdown": 12.0,
                "win_rate": 0.5, "profit_factor": 1.4, "trade_count": 40, "max_consecutive_losses": 4,
                "leak_count": 0, "oos_return": 4.0, "oos_sharpe": 0.9, "oos_mdd": 8.0, "oos_trades": 10,
                "mc_p5": 1.0, "wf_avg_oos": 2.0, "wf_profit_ratio": 55.0, "wf_windows": 3, "wf_profitable": 2}

    try:
        rl.run_hypothesis_backtest = _fake_backtest
        candidates = rl.parse_hypothesis_array(formats["markdown JSON"])
        idx = pd.date_range("2024-01-01", periods=900, freq="1h")
        rng = np.random.default_rng(1)
        close = 100 + np.cumsum(rng.normal(0.03, 1.2, 900))
        df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                           "vol": rng.uniform(500, 2000, 900)}, index=idx)
        results = rl.run_parameter_search(candidates, df, "ETH")
        assert len(results) >= 2, f"搜索应返回 >= 2 个结果，实际 {len(results)}"
        print(f"[OK] 3. 搜索链路 parse→run_parameter_search 跑通（{len(results)} 个实验）")
    finally:
        rl.run_hypothesis_backtest = _orig_bt

    # 4) app.py 统一入口调用链：rl_run → intent_prompt/parse_intent → 探索(参数搜索) / 验证(单次验证)
    src = open("app.py", encoding="utf-8").read()
    block = src[src.index('key="rl_run"'):src.index('rl_search_results')]
    assert "rl.intent_prompt" in block, "统一入口应先调用 intent_prompt 判断意图"
    assert "rl.parse_intent" in block, "统一入口应调用 parse_intent 路由"
    assert "rl.search_prompt" in block and "rl.run_parameter_search" in block, "探索分支应走参数空间搜索"
    assert "rl.hypothesis_prompt" in block and "rl.verify_hypothesis" in block, "验证分支应走单次验证"
    print("[OK] 4. app.py 统一入口：rl_run → intent_prompt/parse_intent → 探索(search_prompt→run_parameter_search) / 验证(hypothesis_prompt→verify_hypothesis)")

    print("\nALL SEARCH REAL-OUTPUT-FORMAT TESTS PASSED")


if __name__ == "__main__":
    main()
