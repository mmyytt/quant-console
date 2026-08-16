"""
AI 研究舱 V2 搜索闭环真实路径回归测试
====================================
覆盖：
  1. app.py 调用的所有 rl.* 方法在 research_loop 中全部存在（防 AttributeError）
  2. 四个核心接口（search_prompt/parse_hypothesis_array/run_strategy_search/verify_hypothesis）
     名称 + 参数一致
  3. AppTest 真实模拟：打开 AI 研究舱 → 填 API Key → 输入研究目标 → 点击「开始搜索」
     → 断言无 AttributeError / NameError / ImportError / StreamlitAPIException
运行: python test_ui_search_real_path.py
"""
import os
import re
import sys
import json
import tempfile
import inspect


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
    import engine_core
    import llm_client

    # 1) 接口一致性：app.py 里所有 rl.<方法> 调用，research_loop 必须全部存在
    src = open("app.py", encoding="utf-8").read()
    rl_calls = sorted(set(re.findall(r'\brl\.([a-zA-Z_]+)', src)))
    missing = [c for c in rl_calls if not hasattr(rl, c)]
    assert not missing, f"research_loop 缺失方法/常量: {missing}"
    print(f"[OK] 1. app.py 调用 rl.{', rl.'.join(rl_calls)} 全部存在")

    # 2) 核心接口签名一致（名称 + 首参）
    sig = {
        "search_prompt": list(inspect.signature(rl.search_prompt).parameters),
        "parse_hypothesis_array": list(inspect.signature(rl.parse_hypothesis_array).parameters),
        "run_strategy_search": list(inspect.signature(rl.run_strategy_search).parameters),
        "run_parameter_search": list(inspect.signature(rl.run_parameter_search).parameters),
        "verify_hypothesis": list(inspect.signature(rl.verify_hypothesis).parameters),
    }
    assert sig["search_prompt"][0] == "goal"
    assert sig["parse_hypothesis_array"][0] == "text"
    assert sig["run_strategy_search"][:3] == ["candidates", "df", "coin"]
    assert sig["run_parameter_search"][:3] == ["directions", "df", "coin"]
    assert sig["verify_hypothesis"][:3] == ["hyp", "df", "coin"]
    print("[OK] 2. 核心接口签名一致（search_prompt/parse_hypothesis_array/run_strategy_search/run_parameter_search/verify_hypothesis）")

    # 3) AppTest 真实点击路径：登录 → 填 Key → 输入目标 → 点击搜索
    _orig_path = db.DB_PATH
    _orig_bt = rl.run_hypothesis_backtest
    _orig_mtf = engine_core.DataEngine.get_multi_timeframe
    _orig_call = llm_client.call_unified_api
    try:
        tmp = tempfile.mkdtemp()
        db.DB_PATH = os.path.join(tmp, "search.db")
        db.init_db()

        # mock 网络 LLM：返回 1 个有效方向 + 1 个无效指标方向（走真实 run_parameter_search 编排）
        fake_candidates = json.dumps([
            {"hypothesis": "EMA 趋势策略", "indicators": ["EMA 双均线"], "params": {},
             "asset": "ETH", "timeframe": "1h", "leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0,
             "strategy_style": "趋势跟踪", "entry_rules": ["EMA 金叉"], "exit_rules": ["EMA 死叉"],
             "expected_logic": "趋势", "expected_market_condition": "趋势",
             "failure_environment": "震荡", "risk_assumption": "回撤<20%"},
            {"hypothesis": "坏方向", "indicators": ["不存在的指标XYZ"], "params": {}},
        ])
        def _fake_call(messages, api_key, model_name, trading_notes):
            # 第一次调用是意图判断（intent_prompt 输出 JSON），第二次才是探索（search_prompt）
            last_user = messages[-1]["content"] if messages else ""
            if "只输出一个 JSON 对象" in last_user:
                return {"success": True, "content": '{"intent": "explore", "goal": "test"}'}
            return {"success": True, "content": fake_candidates}
        llm_client.call_unified_api = _fake_call

        # mock 回测（不跑重计算）：返回完整 metrics，让真实 run_parameter_search 编排执行
        def _fake_backtest(df, coin, indicator_names, param_overrides=None,
                           leverage=2, tp_pct=8.0, sl_pct=4.0, strategy_factory=None):
            return {
                "total_return": 12.0, "annual_return": 18.0, "sharpe": 1.5, "max_drawdown": 9.0,
                "win_rate": 0.52, "profit_factor": 1.8, "trade_count": 35, "max_consecutive_losses": 3,
                "leak_count": 0, "oos_return": 6.0, "oos_sharpe": 1.1, "oos_mdd": 7.0, "oos_trades": 12,
                "mc_p5": 2.5, "wf_avg_oos": 3.0, "wf_profit_ratio": 66.0, "wf_windows": 3, "wf_profitable": 2,
            }
        rl.run_hypothesis_backtest = _fake_backtest

        # mock 数据加载：DatetimeIndex + 必需列
        idx = pd.to_datetime(["2024-01-01 00:00", "2024-01-01 04:00"])
        fake_df = pd.DataFrame({"open": [100.0, 101.0], "high": [102.0, 103.0],
                                "low": [99.0, 98.0], "close": [101.0, 102.0],
                                "vol": [1000.0, 1100.0]}, index=idx)
        engine_core.DataEngine.get_multi_timeframe = lambda self, asset: {tf: fake_df for tf in ("15m", "1h", "4h", "1d")}

        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file("app.py", default_timeout=60)
        at.session_state["logged_in"] = True
        at.session_state["active_tab"] = "AI 对话舱"
        at.run()
        assert not list(getattr(at, "exception", []) or []), "AI 研究舱启动异常"

        # 填 API Key（启用研究按钮）+ 研究目标
        at.text_input(key="ai_main_key").set_value("sk-test-search")
        at.text_input(key="rl_goal").set_value("寻找 ETH 趋势策略，回撤低于20%")
        at.run()
        assert not list(getattr(at, "exception", []) or []), "填参后渲染异常"

        # 点击「开始研究」按钮（AI 判意图后自动路由到探索分支）
        btn = [b for b in at.button if str(getattr(b, "key", "")) == "rl_run"]
        assert btn, "未找到研究按钮 key=rl_run"
        btn[0].click()
        at.run()

        errs = list(getattr(at, "exception", []) or [])
        # 仅容忍 Rerun 类（st.rerun 正常控制流），其余一律视为失败
        real_errs = [e for e in errs if not ("Rerun" in str(type(e).__name__) or "rerun" in str(e).lower())]
        assert not real_errs, f"点击搜索触发异常: {real_errs}"
        print("[OK] 3. 打开AI研究舱 → 填Key → 输入目标 → 点击搜索（参数空间搜索）：无 AttributeError/NameError/ImportError/StreamlitAPIException")
    finally:
        db.DB_PATH = _orig_path
        rl.run_hypothesis_backtest = _orig_bt
        engine_core.DataEngine.get_multi_timeframe = _orig_mtf
        llm_client.call_unified_api = _orig_call

    print("\nALL UI SEARCH REAL-PATH TESTS PASSED")


if __name__ == "__main__":
    main()
