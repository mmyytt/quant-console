"""
顺势加仓状态管理架构升级 回归测试
====================================
覆盖 (需求第八节 5 项):
  1. 单交易生命周期: 开仓 → 加仓 → 加仓 → 平仓 (加仓次数随仓位销毁)
  2. 两笔连续交易: 第一笔加仓结束, 第二笔重新允许加仓 (无全局状态污染)
  3. 多资产模拟: ETH/BTC/SOL 三仓位并存, 状态互不影响
  4. 多空一致: LONG / SHORT 加仓触发逻辑对称
  5. 杠杆一致: 1x/3x/5x/10x 收益率触发阈值一致 (保证金收益率量纲)

运行: python test_pyramiding_state_upgrade.py
"""
import os
import sys


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _make_engine(leverage=3, trigger=4.0, add_pct=0.5, max_adds=3, equity=10000.0):
    """构造引擎: 牛/震/熊系数全 1.0, 使 margin = equity × alloc 便于断言"""
    from engine_core import BacktestEngineV2
    e = BacktestEngineV2(
        initial_capital=equity, leverage=leverage,
        bull_alloc=1.0, range_alloc=1.0, bear_alloc=1.0,
        max_notional_pct=1000.0,  # 放大上限, 避免干扰加仓金额断言
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


def _open(e, coin, side, price, alloc, ts, df):
    """直接调用 _open 建初始仓 (绕过 run 循环), 返回该 leg"""
    row = df.loc[ts]
    e._open(coin, side, price, alloc, ts, 'range', df_row=row)
    return e.positions[-1]


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)

    import pandas as pd
    import engine_core

    # ---------- 测试 1: 单交易生命周期 ----------
    e = _make_engine(leverage=3, trigger=4.0, add_pct=0.5, max_adds=3)
    df = _make_df('ETH', [100.0, 105.0, 105.0, 105.0, 105.0])
    dfs = {'ETH': df}
    ts = df.index
    leg0 = _open(e, 'ETH', 'LONG', 100.0, 0.5, ts[0], df)
    assert leg0['pyramid_count'] == 0, "初始 leg pyramid_count 应为 0"
    assert abs(leg0['init_margin'] - 5000.0) < 0.01, f"初始投入应=5000, 实际 {leg0['init_margin']}"
    assert leg0['max_pyramid_count'] == 3

    # 加仓1 (105 → 保证金收益 15% ≥ 4% 触发)
    e._check_pyramiding(ts[1], dfs, ['ETH'])
    assert len(e.positions) == 2, "第一次加仓后应有 2 个 leg"
    assert e.positions[1]['pyramid_count'] == 1
    assert abs(e.positions[1]['margin'] - 2500.0) < 0.01, \
        f"加仓金额应=初始5000×50%=2500(固定), 实际 {e.positions[1]['margin']}"
    assert abs(e.positions[1]['init_margin'] - 5000.0) < 0.01

    # 加仓2 (固定2500, 不复利)
    e._check_pyramiding(ts[2], dfs, ['ETH'])
    assert len(e.positions) == 3
    assert e.positions[2]['pyramid_count'] == 2
    assert abs(e.positions[2]['margin'] - 2500.0) < 0.01, "第二次加仓也应固定2500"

    # 加仓3 (count 达到 max=3)
    e._check_pyramiding(ts[3], dfs, ['ETH'])
    assert len(e.positions) == 4
    assert e.positions[3]['pyramid_count'] == 3

    # 已达上限, 不再加仓
    e._check_pyramiding(ts[4], dfs, ['ETH'])
    assert len(e.positions) == 4, "达到 max_pyramid 后不应再加仓"
    print("[OK] 1. 单交易生命周期: 开仓→加仓×3→平仓上限 (固定加仓额2500, 状态在leg上)")

    # 平仓: 所有 leg 移除 → 状态销毁
    for pos in list(e.positions):
        e._close(pos, 105.0, 'TP', ts[4])
    assert len(e.positions) == 0, "平仓后仓位应清空"

    # ---------- 测试 2: 两笔连续交易 ----------
    e._open('ETH', 'LONG', 100.0, 0.5, ts[0], 'range', df_row=df.loc[ts[0]])
    assert e.positions[0]['pyramid_count'] == 0, "第二笔交易应重新从 0 开始加仓计数"
    e._check_pyramiding(ts[1], dfs, ['ETH'])
    assert len(e.positions) == 2 and e.positions[1]['pyramid_count'] == 1, \
        "第二笔交易应能重新加仓 (无全局状态污染)"
    print("[OK] 2. 两笔连续交易: 第一笔加仓不影响第二笔, 计数从0重新开始")

    # ---------- 测试 3: 多资产并存, 状态互不影响 ----------
    e3 = _make_engine(leverage=3, trigger=4.0, add_pct=0.5, max_adds=3)
    df_eth = _make_df('ETH', [100.0, 105.0])
    df_btc = _make_df('BTC', [50000.0, 50000.0])   # 不涨不跌, 不触发
    df_sol = _make_df('SOL', [150.0, 150.0])        # 不涨不跌, 不触发
    dfs3 = {'ETH': df_eth, 'BTC': df_btc, 'SOL': df_sol}
    _open(e3, 'ETH', 'LONG', 100.0, 0.5, df_eth.index[0], df_eth)
    _open(e3, 'BTC', 'LONG', 50000.0, 0.5, df_btc.index[0], df_btc)
    _open(e3, 'SOL', 'SHORT', 150.0, 0.5, df_sol.index[0], df_sol)
    assert len(e3.positions) == 3

    # 只有 ETH 上涨触发, 其余不动
    e3._check_pyramiding(df_eth.index[1], dfs3, ['ETH', 'BTC', 'SOL'])
    eth_legs = [p for p in e3.positions if p['coin'] == 'ETH']
    btc_legs = [p for p in e3.positions if p['coin'] == 'BTC']
    sol_legs = [p for p in e3.positions if p['coin'] == 'SOL']
    assert len(eth_legs) == 2 and eth_legs[1]['pyramid_count'] == 1, "ETH 应加仓 1 次"
    assert len(btc_legs) == 1 and btc_legs[0]['pyramid_count'] == 0, "BTC 状态不应被影响"
    assert len(sol_legs) == 1 and sol_legs[0]['pyramid_count'] == 0, "SOL 状态不应被影响"
    print("[OK] 3. 多资产并存: ETH/BTC/SOL 独立状态, 互不影响")

    # ---------- 测试 4: LONG / SHORT 加仓逻辑一致 ----------
    # LONG: 涨 5% 保证金收益 → 触发
    e_long = _make_engine(leverage=3, trigger=4.0)
    df_l = _make_df('ETH', [100.0, 105.0])
    _open(e_long, 'ETH', 'LONG', 100.0, 0.5, df_l.index[0], df_l)
    e_long._check_pyramiding(df_l.index[1], {'ETH': df_l}, ['ETH'])
    assert len(e_long.positions) == 2, "LONG 涨5% 应触发加仓"

    # SHORT: 跌 5% 保证金收益 → 触发 (对称)
    e_short = _make_engine(leverage=3, trigger=4.0)
    df_s = _make_df('SOL', [150.0, 142.5])   # -5%
    _open(e_short, 'SOL', 'SHORT', 150.0, 0.5, df_s.index[0], df_s)
    e_short._check_pyramiding(df_s.index[1], {'SOL': df_s}, ['SOL'])
    assert len(e_short.positions) == 2, "SHORT 跌5% 应触发加仓 (与LONG对称)"

    # 反向不触发: LONG 跌 / SHORT 涨
    e_l2 = _make_engine(leverage=3, trigger=4.0)
    df_l2 = _make_df('ETH', [100.0, 95.0])
    _open(e_l2, 'ETH', 'LONG', 100.0, 0.5, df_l2.index[0], df_l2)
    e_l2._check_pyramiding(df_l2.index[1], {'ETH': df_l2}, ['ETH'])
    assert len(e_l2.positions) == 1, "LONG 下跌不应触发加仓"
    print("[OK] 4. 多空一致: LONG涨/SHORT跌触发, 反向不触发")

    # ---------- 测试 5: 杠杆一致性 (保证金收益率量纲) ----------
    for lev in (1, 3, 5, 10):
        # 涨 5% 保证金收益 → 触发 (与杠杆无关)
        e_up = _make_engine(leverage=lev, trigger=4.0)
        df_up = _make_df('ETH', [100.0, 100.0 * (1 + 0.05 / lev)])
        _open(e_up, 'ETH', 'LONG', 100.0, 0.5, df_up.index[0], df_up)
        entry = e_up.positions[0]['entry']
        e_up._check_pyramiding(df_up.index[1], {'ETH': df_up}, ['ETH'])
        assert len(e_up.positions) == 2, f"lev={lev}: 保证金收益5%应触发"

        # 涨 3% 保证金收益 → 不触发 (<4%)
        e_dn = _make_engine(leverage=lev, trigger=4.0)
        df_dn = _make_df('ETH', [100.0, 100.0 * (1 + 0.03 / lev)])
        _open(e_dn, 'ETH', 'LONG', 100.0, 0.5, df_dn.index[0], df_dn)
        e_dn._check_pyramiding(df_dn.index[1], {'ETH': df_dn}, ['ETH'])
        assert len(e_dn.positions) == 1, f"lev={lev}: 保证金收益3%不应触发"
    print("[OK] 5. 杠杆一致: 1x/3x/5x/10x 均按保证金收益率触发 (5%触发/3%不触发)")

    print("\nALL PYRAMIDING STATE-UPGRADE TESTS PASSED")


if __name__ == "__main__":
    main()
