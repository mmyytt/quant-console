"""
Phase B 代码级修复回归测试
===========================
覆盖:
  1. 固定加仓金额案例: 10000/3x/50%初始/50%加仓/max2 → 5000+2500+2500=10000
  2. P1-2: position_id 共享 + position_trades 聚合
  3. P1-1: 5m 真实数据 ≠ 4h 重采样数据 (K线数不同)
  4. P0-2: 仓位参数进入 full_fingerprint

运行: python test_phase_b_fixes.py
"""
import os
import sys


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _make_engine(leverage=3, trigger=4.0, add_pct=0.5, max_adds=2, equity=10000.0):
    from engine_core import BacktestEngineV2
    e = BacktestEngineV2(
        initial_capital=equity, leverage=leverage,
        bull_alloc=1.0, range_alloc=1.0, bear_alloc=1.0,
        max_notional_pct=1000.0,
        verbose=False,
    )
    e._enable_pyramiding = True
    e._pyr_trigger_pct = trigger
    e._pyr_add_pct = add_pct
    e._pyr_max = max_adds
    e._pyr_trail = False
    e._pos_mode = 'fixed_capital'
    return e


def _make_df(coin, open_prices):
    import pandas as pd
    idx = pd.to_datetime([
        "2024-01-01 0%d:00" % i for i in range(len(open_prices))
    ])
    return pd.DataFrame({
        'open': open_prices, 'high': open_prices, 'low': open_prices,
        'close': open_prices, 'vol': [1000.0] * len(open_prices),
    }, index=idx)


def test_fixed_pyramiding():
    """10000 / 3x / 50%初始 / 50%加仓 / max2 → 5000+2500+2500=10000"""
    e = _make_engine(leverage=3, trigger=4.0, add_pct=0.5, max_adds=2)
    df = _make_df('ETH', [100.0, 105.0, 105.0, 105.0, 105.0])
    dfs = {'ETH': df}
    ts = df.index

    # 初始 50% → 5000
    e._open('ETH', 'LONG', 100.0, 0.5, ts[0], 'range', df_row=df.loc[ts[0]])
    assert abs(e.positions[0]['init_margin'] - 5000.0) < 0.01, e.positions[0]['init_margin']
    assert abs(e.positions[0]['margin'] - 5000.0) < 0.01

    # 加仓1: 105 → 保证金收益 15% ≥ 4% → +2500
    e._check_pyramiding(ts[1], dfs, ['ETH'])
    assert len(e.positions) == 2
    assert abs(e.positions[1]['margin'] - 2500.0) < 0.01, e.positions[1]['margin']

    # 加仓2: count=1 < max2 → +2500
    e._check_pyramiding(ts[2], dfs, ['ETH'])
    assert len(e.positions) == 3
    assert abs(e.positions[2]['margin'] - 2500.0) < 0.01

    # count=2 == max2 → 不再加仓
    e._check_pyramiding(ts[3], dfs, ['ETH'])
    assert len(e.positions) == 3, "max2 后不应再加仓"

    total_margin = sum(p['margin'] for p in e.positions)
    assert abs(total_margin - 10000.0) < 0.01, f"总保证金应=10000, 实际 {total_margin}"

    # 平仓, 验证聚合
    for pos in list(e.positions):
        e._close(pos, 105.0, 'TP', ts[4])
    agg = e._aggregate_positions(e.trades)
    assert len(agg) == 1, "同一 position_id 应聚合成 1 个持仓"
    a = agg[0]
    assert abs(a['initial_margin'] - 5000.0) < 0.01, a
    assert abs(a['add_margin'] - 5000.0) < 0.01, a
    assert abs(a['total_margin'] - 10000.0) < 0.01, a
    assert a['pyramid_count'] == 2, a
    print("[OK] 1. 固定加仓金额: 5000+2500+2500=10000, position_trades 聚合正确")


def test_position_id_shared():
    """P1-2: 初始腿与加仓腿共享 position_id; 独立仓位的 id 递增"""
    e = _make_engine(leverage=3, trigger=4.0, add_pct=0.5, max_adds=2)
    df = _make_df('ETH', [100.0, 105.0])
    dfs = {'ETH': df}
    ts = df.index
    e._open('ETH', 'LONG', 100.0, 0.5, ts[0], 'range', df_row=df.loc[ts[0]])
    pid0 = e.positions[0]['position_id']
    assert pid0 >= 1
    e._check_pyramiding(ts[1], dfs, ['ETH'])
    pid1 = e.positions[1]['position_id']
    assert pid1 == pid0, "加仓腿应复用初始腿的 position_id"

    # 第二笔独立交易 → 新 id
    e._close(e.positions[1], 105.0, 'TP', ts[1])
    e._close(e.positions[0], 105.0, 'TP', ts[1])
    e._open('ETH', 'LONG', 100.0, 0.5, ts[0], 'range', df_row=df.loc[ts[0]])
    assert e.positions[0]['position_id'] != pid0, "独立仓位应分配新 position_id"
    print("[OK] 2. position_id: 加仓腿复用, 独立仓位递增")


def test_5m_vs_4h():
    """P1-1: ETH 5m 真实数据 K 线数 ≠ 4h 重采样 K 线数"""
    from engine_core import DataEngine
    de = DataEngine()
    all_tf = de.get_multi_timeframe('ETH')
    assert '5m' in all_tf, "ETH 应有 5m 数据 (本地 parquet 已存在)"
    n5 = len(all_tf['5m'])
    n4 = len(all_tf['4h'])
    n15 = len(all_tf['15m'])
    assert n5 > n15 > n4, f"K线数应 5m({n5}) > 15m({n15}) > 4h({n4})"
    print(f"[OK] 3. 5m≠4h: ETH 5m={n5} bars, 15m={n15}, 4h={n4}")


def test_fingerprint_position_params():
    """P0-2: 仓位参数进入 full_fingerprint, 不同仓位模型=不同指纹"""
    from research_loop import full_fingerprint
    base = full_fingerprint(['EMA', 'RSI'], position_params={
        '_init_alloc_pct': 30.0, '_enable_pyramiding': False})
    pyr = full_fingerprint(['EMA', 'RSI'], position_params={
        '_init_alloc_pct': 50.0, '_enable_pyramiding': True, '_pyr_max': 2})
    assert base != pyr, "仅仓位不同应产生不同指纹"
    assert '_INIT_ALLOC_PCT=30.0' in base
    assert '_ENABLE_PYRAMIDING=TRUE' in pyr
    assert '_PYR_MAX=2' in pyr
    print("[OK] 4. full_fingerprint: 仓位参数纳入指纹, 不同仓位模型可区分")


def test_position_param_surfacing():
    """P0-1 补全: AI 搜索空间暴露仓位参数 (context/prompt/提取)。"""
    from research_loop import _position_params_from, hypothesis_prompt, search_prompt
    from platform_context import format_context_text

    # 1. 嵌套 "position" 子对象可被提取
    pos = _position_params_from({"position": {
        "_init_alloc_pct": 50, "_enable_pyramiding": True, "_pyr_add_pct": 0.5,
        "_pyr_max": 2, "_bull_alloc": 100, "_range_alloc": 50, "_bear_alloc": 30}})
    assert pos and pos["_init_alloc_pct"] == 50 and pos["_enable_pyramiding"] is True

    # 2. 顶层键也可提取 (兼容旧 schema)
    assert _position_params_from({"_init_alloc_pct": 15})["_init_alloc_pct"] == 15

    # 3. context 与两个 prompt 均暴露 7 个仓位参数 key
    ctx = format_context_text()
    for k in ("_init_alloc_pct", "_enable_pyramiding", "_pyr_add_pct", "_pyr_max",
              "_bull_alloc", "_range_alloc", "_bear_alloc"):
        assert k in ctx, f"context 缺 {k}"
    hp = hypothesis_prompt("测试")
    sp = search_prompt("测试")
    for k in ("_init_alloc_pct", "_pyr_add_pct", "_bull_alloc"):
        assert k in hp and k in sp, f"prompt 缺 {k}"
    print("[OK] 5. 仓位参数已暴露给 AI (context + 双 prompt + 嵌套提取)")


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)
    test_fixed_pyramiding()
    test_position_id_shared()
    test_5m_vs_4h()
    test_fingerprint_position_params()
    test_position_param_surfacing()
    print("\nALL PHASE B FIX TESTS PASSED")


if __name__ == "__main__":
    main()
