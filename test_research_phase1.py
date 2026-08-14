"""
Phase 1 测试:Quant Research Agent 记忆基础
覆盖:SQLite 创建 / 消息持久化 / 指标读取 / 旧回测不破坏 / 假设·实验·策略 CRUD
运行: python test_research_phase1.py
"""
import os
import sqlite3
import tempfile

import pandas as pd
import numpy as np

import research_storage.db as db
import indicator_schema
import platform_context as pc
import research_agent as ra


def main():
    # 用临时库，不污染真实 research.db
    tmp = tempfile.mkdtemp()
    db.DB_PATH = os.path.join(tmp, "test_research.db")

    # 1) SQLite 正常创建 + 5 表
    db.init_db()
    conn = sqlite3.connect(db.DB_PATH)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    expected = {"research_sessions", "research_messages", "research_hypothesis",
                "strategy_experiments", "strategy_library"}
    assert expected.issubset(tables), f"缺表: {expected - tables}"
    print("[OK] 1. SQLite 创建 + 5 表:", sorted(expected))

    # 2) 消息持久化（模拟退出页面重进）
    sid = db.create_session(model_provider="DeepSeek-V3", title="t", user_goal="研究 ETH 突破")
    db.add_message(sid, "user", "研究 ETH 短线突破策略")
    db.add_message(sid, "assistant", "好的，先查历史研究记忆，避免重复研究…")
    msgs = db.list_messages(sid)  # 模拟重进:从 DB 重新读取
    assert len(msgs) == 2 and msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant"
    print("[OK] 2. 消息持久化: 重进后仍能读到", len(msgs), "条")

    # 3) AI 可读取指标列表
    ctx = pc.build_platform_context()
    names = {i["name"] for i in ctx["indicators"]}
    for n in ["EMA 双均线", "RSI 相对强弱", "MACD 异同均线", "ADX/DMI 趋势强度",
              "布林带 Bollinger", "Donchian 通道", "量比 Volume Ratio", "SuperTrend 超级趋势"]:
        assert n in names, f"缺指标: {n}"
    assert len(ctx["indicators"]) >= 20
    print("[OK] 3. AI 可读取指标:", len(ctx["indicators"]), "个, 能力地图键:",
          list(ctx.keys()))

    # 4) 旧回测不破坏:抽取后的指标 compute 函数仍可运行
    n = 120
    df = pd.DataFrame({
        "open": np.linspace(100, 120, n), "high": np.linspace(101, 122, n),
        "low": np.linspace(99, 118, n), "close": np.linspace(100, 121, n),
        "vol": np.linspace(1000, 2000, n),
    })
    for fn, args in [(indicator_schema._ema_cross, (7, 21)),
                     (indicator_schema._rsi_signal, (14, 30, 70)),
                     (indicator_schema._donchian, (20,)),
                     (indicator_schema._macd_signal, (12, 26, 9))]:
        d = df.copy()
        fn(d, *args)
        assert "_long" in d.columns and "_short" in d.columns
    # 注册表 compute 均可调用
    assert len(indicator_schema.INDICATOR_REGISTRY) == len(indicator_schema.INDICATOR_SCHEMA)
    for name, info in indicator_schema.INDICATOR_REGISTRY.items():
        assert callable(info["compute"])
    print("[OK] 4. 旧回测不破坏:", len(indicator_schema.INDICATOR_SCHEMA), "个指标 compute 正常")

    # 5) 假设 / 实验 / 策略 CRUD + 记忆
    h = db.add_hypothesis("EMA趋势过滤+成交量突破可能提高趋势策略胜率",
                          related_indicators=["EMA 双均线", "量比 Volume Ratio"])
    db.update_hypothesis_status(h, "testing")
    assert db.hypothesis_status_counts()["testing"] == 1

    db.add_experiment(strategy_name="EMA+量比", indicator_combination=["EMA 双均线", "量比 Volume Ratio"],
                      asset="ETH", timeframe="4h", leverage=2,
                      total_return=15.2, sharpe=0.6, max_drawdown=8.1, win_rate=39.0, trade_count=120)
    assert db.list_experiments(1)[0]["strategy_name"] == "EMA+量比"

    db.add_strategy(name="ETH 突破", logic_description="Donchian 通道突破")
    assert db.list_strategies(1)[0]["name"] == "ETH 突破"

    mem = db.memory_summary()
    stats = ra.memory_stats(mem)
    assert stats["hypotheses"] == 1 and stats["experiments"] == 1 and stats["strategies"] == 1
    prompt = ra.build_system_prompt(ctx, mem, "")
    assert "EMA 双均线" in prompt and "EMA趋势过滤" in prompt and "研究记忆" in prompt
    print("[OK] 5. 假设/实验/策略 CRUD + 记忆注入:", stats)

    print("\nALL PHASE 1 TESTS PASSED")


if __name__ == "__main__":
    main()
