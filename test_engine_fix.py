#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Backtest Integrity Hardening — 单元测试 (随各 Phase 增量追加)

用法: python test_engine_fix.py
每个 Phase 独立函数, main 里依次跑, 任一失败返回非零。
"""
import sys, os
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine_core import StrategyBase

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def _synth_ohlcv(n=300, seed=7):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.01, n)
    close = 100 * np.exp(np.cumsum(ret))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    open_ = np.roll(close, 1); open_[0] = close[0]
    vol = rng.uniform(100, 1000, n)
    idx = pd.date_range("2020-01-01", periods=n, freq="4h")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "vol": vol}, index=idx)


def phase1_evaluate_shift():
    print("\n=== Phase 1: evaluate() shift + FutureLeakDetector ===")
    from engine_core import MultiFactorRegime, MACrossStrategy
    from future_leak_detector import FutureLeakDetector

    df = _synth_ohlcv()
    raw = df.copy()

    # 1) evaluate 内部 shift 后, 首行 regime 应为 range (无未来数据可用)
    mf = MultiFactorRegime()
    out = mf.evaluate(df.copy())
    check("evaluate 首行 regime=range (shift后无数据)", out['regime_mf'].iloc[0] == 'range',
          f"got={out['regime_mf'].iloc[0]}")

    # 2) 干净策略输出不应触发 detector
    s = MACrossStrategy()
    sig = s.generate_signals(df.copy())
    det = FutureLeakDetector()
    w = det.scan(sig, raw_df=raw)
    check("干净策略: detector 无 warning", len(w) == 0, f"warnings={w}")

    # 3) 注入未来函数: bad_close 用当前 close, bad_high 用未来 high
    leaky = sig.copy()
    leaky['bad_close'] = raw['close']
    leaky['bad_high'] = raw['high'].shift(-1)
    leaky['bad_vol'] = raw['vol'].shift(-1)
    w2 = det.scan(leaky, raw_df=raw)
    types = {x['column']: x['type'] for x in w2}
    check("注入当前close被识别", types.get('bad_close') == 'current_close', f"types={types}")
    check("注入未来high被识别", types.get('bad_high') == 'future_high', f"types={types}")
    check("注入未来volume被识别", types.get('bad_vol') == 'future_volume', f"types={types}")


class _SignalAtBar(StrategyBase):
    """在指定 bar 强制发出 LONG 信号 (仅测试用)。"""
    def __init__(self, bar_idx, name="signal_test"):
        super().__init__(name)
        self.bar_idx = bar_idx

    def generate_signals(self, df):
        df = df.copy()
        df['signal'] = 0
        df['regime'] = 'bull'
        df['br'] = 0.0
        df['score'] = 1
        df.iloc[self.bar_idx, df.columns.get_loc('signal')] = 1
        return df


def _run_entry_case(open_, high, low, close_, sig_bar=700):
    """构造一根信号K线(大幅波动), 跑引擎, 返回该 bar 开仓的交易。"""
    from engine_core import BacktestEngineV2
    n = 800  # 需超过引擎 warmup(600)
    idx = pd.date_range("2020-01-01", periods=n, freq="4h")
    base = np.full(n, 100.0)
    df = pd.DataFrame({
        "open": base.copy(), "high": base.copy(), "low": base.copy(),
        "close": base.copy(), "vol": np.full(n, 500.0),
    }, index=idx)
    df.iloc[sig_bar, df.columns.get_loc("open")] = open_
    df.iloc[sig_bar, df.columns.get_loc("high")] = high
    df.iloc[sig_bar, df.columns.get_loc("low")] = low
    df.iloc[sig_bar, df.columns.get_loc("close")] = close_

    engine = BacktestEngineV2(
        initial_capital=10000, leverage=3, tp_pct=10, sl_pct=5,
        max_positions=1, bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
        verbose=False, tp_mode="margin_pct", sl_mode="margin_pct",
        max_notional_pct=5.0,
    )
    engine._use_real_funding = False  # 测试用合成数据, 不拉取网络 funding
    result = engine.run({"ETH": df}, _SignalAtBar(sig_bar))
    ts_str = str(idx[sig_bar])
    return [t for t in result["trades"] if str(t["open_time"]) == ts_str]


def phase2_entry_candle():
    print("\n=== Phase 2: 入场K线 TP/SL 漏洞 ===")
    # 案例1: 开仓后最低价触发 SL
    trades = _run_entry_case(open_=100, high=101, low=97, close_=98)
    check("入场K线触发 SL", len(trades) == 1 and trades[0]["reason"] == "SL",
          f"trades={[(t['reason'], t['exit']) for t in trades]}")

    # 案例2: 开仓后最高价触发 TP
    trades = _run_entry_case(open_=100, high=105, low=99.5, close_=104)
    check("入场K线触发 TP", len(trades) == 1 and trades[0]["reason"] == "TP",
          f"trades={[(t['reason'], t['exit']) for t in trades]}")

    # 案例3: 同K线双触发 (TP+SL) → SL 优先
    trades = _run_entry_case(open_=100, high=105, low=97, close_=99)
    check("同K线双触发 SL 优先", len(trades) == 1 and trades[0]["reason"] == "SL",
          f"trades={[(t['reason'], t['exit']) for t in trades]}")


def phase3_fixed_risk():
    print("\n=== Phase 3: Fixed Risk 去重复缩放 ===")
    from engine_core import BacktestEngineV2

    def _open_fixed_risk(init_alloc):
        e = BacktestEngineV2(
            initial_capital=10000, leverage=3, tp_pct=10, sl_pct=5,
            max_positions=1, bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
            verbose=False, tp_mode="margin_pct", sl_mode="price_pct",
            max_notional_pct=100.0,
        )
        e._pos_mode = 'fixed_risk'
        e._risk_pct = 0.02
        e.equity = 10000.0
        e.positions = []
        e._open('ETH', 'LONG', price=100.0, alloc=init_alloc,
                ts='2020-01-01 00:00:00', regime='bull')
        return e.positions[0]

    pA = _open_fixed_risk(0.3)   # 建仓比例 30%
    pB = _open_fixed_risk(0.9)   # 建仓比例 90%

    # 1) init_alloc 不应再缩放 Fixed Risk: 30% vs 90% → 仓位一致
    check("init_alloc 不再缩放 Fixed Risk (30% vs 90% 仓位一致)",
          abs(pA['notional'] - pB['notional']) < 1e-6,
          f"notionalA={pA['notional']:.4f} notionalB={pB['notional']:.4f}")

    # 2) expected risk == actual risk (风险预算 = 数量 × 止损距离)
    risk_budget = 10000.0 * 0.02 * 1.0  # equity × risk_pct × bull_alloc(=1.0)
    actual_risk = (pA['notional'] / pA['entry']) * abs(pA['entry'] - pA['sl_price'])
    check("expected risk == actual risk (风险预算 = 数量×止损距离)",
          abs(actual_risk - risk_budget) < 1e-6,
          f"actual={actual_risk:.6f} expected={risk_budget:.6f}")


def phase4_multi_asset_equity():
    print("\n=== Phase 4: 多币种权益计算 ===")
    from engine_core import BacktestEngineV2

    e = BacktestEngineV2(
        initial_capital=10000, leverage=3, tp_pct=10, sl_pct=5,
        max_positions=5, bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
        verbose=False, tp_mode="margin_pct", sl_mode="margin_pct",
        max_notional_pct=100.0,
    )
    e.equity = 10000.0
    idx = pd.date_range("2020-01-01", periods=3, freq="4h")
    ts = idx[1]
    dfs = {
        "ETH": pd.DataFrame({"open": [100.0, 100.0, 100.0]}, index=idx),
        "BTC": pd.DataFrame({"open": [20000.0, 22000.0, 22000.0]}, index=idx),
    }
    # ETH 现价=entry(浮盈0), BTC 现价 22000 > entry 20000 (浮盈)
    e.positions = [
        {'coin': 'ETH', 'side': 'LONG', 'entry': 100.0, 'margin': 1000.0},
        {'coin': 'BTC', 'side': 'LONG', 'entry': 20000.0, 'margin': 1000.0},
    ]
    e._spot_leg = None
    e._short_leg = None

    exp_btc_pnl = 1000.0 * (22000.0 - 20000.0) / 20000.0 * 3  # = 300
    exp_total = 10000.0 + exp_btc_pnl
    got = e._calc_total_equity(dfs, ts)
    check("多币种各用自身价格计浮盈", abs(got - exp_total) < 1e-6,
          f"got={got:.4f} expected={exp_total:.4f}")


def phase5_pyramid_legs():
    print("\n=== Phase 5: 金字塔加仓 Leg 模型 ===")
    from engine_core import BacktestEngineV2

    e = BacktestEngineV2(
        initial_capital=10000, leverage=3, tp_pct=10, sl_pct=5,
        max_positions=5, bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
        verbose=False, tp_mode="margin_pct", sl_mode="margin_pct",
        max_notional_pct=100.0,
    )
    e._enable_pyramiding = True
    e._pyr_trigger_pct = 10.0
    e._pyr_add_pct = 0.5
    e._pyr_trail = False
    e.equity = 10000.0

    idx = pd.date_range("2020-01-01", periods=3, freq="4h")
    ts = idx[1]
    df = pd.DataFrame({
        "open": [100.0, 120.0, 120.0], "high": [100.0, 120.0, 120.0],
        "low": [100.0, 120.0, 120.0], "close": [100.0, 120.0, 120.0],
        "vol": [500.0, 500.0, 500.0],
    }, index=idx)
    dfs = {"ETH": df}

    e._open('ETH', 'LONG', 100.0, 0.3, ts, regime='bull')
    leg1 = e.positions[0]
    entry1, tp1, sl1 = leg1['entry'], leg1['tp_price'], leg1['sl_price']

    e._check_pyramiding(ts, dfs, ['ETH'])

    check("金字塔加仓后 leg 数 = 2", len(e.positions) == 2,
          f"n={len(e.positions)}")
    check("leg1 entry 未被覆盖", e.positions[0]['entry'] == entry1,
          f"{e.positions[0]['entry']} != {entry1}")
    check("leg1 tp 未被覆盖", e.positions[0]['tp_price'] == tp1,
          f"{e.positions[0]['tp_price']} != {tp1}")
    check("leg1 sl 未被覆盖", e.positions[0]['sl_price'] == sl1,
          f"{e.positions[0]['sl_price']} != {sl1}")
    check("leg2 独立 entry (≠ leg1)", e.positions[1]['entry'] != entry1,
          f"leg2 entry={e.positions[1]['entry']} leg1={entry1}")
    check("leg2 有独立 qty", e.positions[1].get('qty', 0) > 0,
          f"qty={e.positions[1].get('qty')}")


def phase6_combo_no_oos():
    print("\n=== Phase 6: combo_optimize 禁 OOS 选参 ===")
    from robustness_lab import RobustnessLab

    def _mk(is_ret, oos_ret):
        return {
            'is_metrics': {
                'total_return': is_ret, 'max_drawdown': 20.0,
                'sharpe_ratio': 1.0, 'win_rate': 50.0, 'total_trades': 100,
                'annual_return': 10.0,
            },
            'oos_metrics': {'total_return': oos_ret},
        }

    # 两个组合 IS 完全相同, OOS 一个 +50% 一个 -50%
    res = RobustnessLab._composite_score([_mk(30.0, 50.0), _mk(30.0, -50.0)])
    check("OOS 收益不参与选参排序 (IS相同→composite相同)",
          res[0]['composite_score'] == res[1]['composite_score'],
          f"sA={res[0]['composite_score']} sB={res[1]['composite_score']}")
    check("OOS 仍作独立字段保留",
          res[0]['oos_return'] == 50.0 and res[1]['oos_return'] == -50.0,
          f"oos={res[0]['oos_return']}, {res[1]['oos_return']}")


def phase7_p2_liquidation():
    print("\n=== Phase 7: P2-3 爆仓对齐维持保证金 ===")
    from engine_core import BacktestEngineV2
    e = BacktestEngineV2(initial_capital=10000, leverage=3, tp_pct=10, sl_pct=5,
                         max_positions=1, bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
                         verbose=False, tp_mode="margin_pct", sl_mode="margin_pct",
                         max_notional_pct=100.0)
    e.equity = 10000.0
    pos = {'coin': 'ETH', 'side': 'LONG', 'entry': 100.0, 'margin': 1000.0,
           'notional': 3000.0, 'mmr': 0.005, 'liq_price': 100.0 * (1 - 1/3 + 0.005),
           'open_time': '2020-01-01', 'regime': 'bull', 'resonance_score': 0,
           'tp_price': 110.0, 'sl_price': 95.0, 'highest_price': 100.0,
           'lowest_price': 100.0, 'trailing_activated': False}
    e.positions.append(pos)
    e._close(pos, 50.0, 'EOD', '2020-01-02')
    t = e.trades[-1]
    exp_pct = round((-1.0 + 0.005 * 3) * 100, 2)  # -98.5%
    check("爆仓保留维持保证金 (pnl_pct≈-98.5% 而非 -100%)",
          t['reason'] == 'LIQUIDATED' and abs(t['pnl_pct'] - exp_pct) < 0.01,
          f"reason={t['reason']} pnl_pct={t['pnl_pct']}")


def phase7_p2_portfolio_exposure():
    print("\n=== Phase 7: P2-5 组合级敞口控制 ===")
    from engine_core import BacktestEngineV2
    e = BacktestEngineV2(initial_capital=10000, leverage=3, tp_pct=10, sl_pct=5,
                         max_positions=5, bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
                         verbose=False, tp_mode="margin_pct", sl_mode="margin_pct",
                         max_notional_pct=0.5)  # 组合名义上限 = 5000
    e.equity = 10000.0
    e.positions = [{'coin': 'ETH', 'side': 'LONG', 'entry': 100.0, 'margin': 1500.0,
                    'notional': 4500.0, 'regime': 'bull', 'resonance_score': 0,
                    'open_time': '2020-01-01', 'tp_price': 110.0, 'sl_price': 95.0,
                    'liq_price': 0.0, 'mmr': 0.005, 'highest_price': 100.0,
                    'lowest_price': 100.0, 'trailing_activated': False}]
    e._open('BTC', 'LONG', 20000.0, 0.5, '2020-01-01 04:00:00', regime='bull')
    btc = e.positions[-1]
    check("组合级敞口: 新仓被 cap 到剩余额度 (≈500)",
          abs(btc['notional'] - 500.0) < 1.0, f"notional={btc['notional']}")


def phase7_p2_equity_final():
    print("\n=== Phase 7: P2-4 权益曲线期末结算点 ===")
    from engine_core import BacktestEngineV2
    n = 800
    idx = pd.date_range("2020-01-01", periods=n, freq="4h")
    base = np.full(n, 100.0)
    df = pd.DataFrame({"open": base, "high": base, "low": base, "close": base,
                       "vol": np.full(n, 500.0)}, index=idx)
    engine = BacktestEngineV2(initial_capital=10000, leverage=3, tp_pct=10, sl_pct=5,
                              max_positions=1, bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
                              verbose=False, tp_mode="margin_pct", sl_mode="margin_pct",
                              max_notional_pct=5.0)
    engine._use_real_funding = False  # 测试用合成数据, 不拉取网络 funding
    result = engine.run({"ETH": df}, _SignalAtBar(700))
    check("equity_curve[-1] == final_equity (期末已实现)",
          abs(result['equity_curve'][-1]['equity'] - result['final_equity']) < 1e-6,
          f"last={result['equity_curve'][-1]['equity']} final={result['final_equity']}")


def phase7_p2_funding_settlement():
    print("\n=== Phase 7: P2-1/P2-2 资金费率结算方向 ===")
    from engine_core import BacktestEngineV2

    def _mk_engine():
        e = BacktestEngineV2(initial_capital=10000, leverage=3, tp_pct=10, sl_pct=5,
                             max_positions=5, bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
                             verbose=False, tp_mode="margin_pct", sl_mode="margin_pct")
        e.equity = 10000.0
        e.positions = [
            {'coin': 'ETH', 'side': 'LONG', 'leg': 'SWAP', 'notional': 1000.0},
            {'coin': 'ETH', 'side': 'SHORT', 'leg': 'SWAP', 'notional': 1000.0},
            {'coin': 'ETH', 'side': 'LONG', 'leg': 'SPOT', 'notional': 1000.0},
        ]
        return e

    # 正费率: LONG 付, SHORT 收, SPOT 不收
    e = _mk_engine()
    e._funding_settle = {'ETH': {0: 0.001}}
    eq0 = e.equity
    e._settle_funding(0)
    # LONG -1.0, SHORT +1.0, SPOT 0 → net 0
    check("正费率: LONG付/SHORT收/SPOT不收 (净变化=0)",
          abs(e.equity - eq0) < 1e-9, f"d={e.equity - eq0}")

    # 负费率: 方向反转
    e = _mk_engine()
    e._funding_settle = {'ETH': {0: -0.001}}
    eq0 = e.equity
    e._settle_funding(0)
    check("负费率: LONG收/SHORT付 (净变化=0)",
          abs(e.equity - eq0) < 1e-9, f"d={e.equity - eq0}")

    # 纯单边 LONG 正费率 → 权益减少 notional×fr
    e = _mk_engine()
    e.positions = [{'coin': 'ETH', 'side': 'LONG', 'leg': 'SWAP', 'notional': 2000.0}]
    e._funding_settle = {'ETH': {0: 0.001}}
    eq0 = e.equity
    e._settle_funding(0)
    check("单边 LONG 正费率: 权益 -= notional×fr (=-2.0)",
          abs((e.equity - eq0) - (-2.0)) < 1e-9, f"d={e.equity - eq0}")

    # 现货模式 (leverage<=1) 不结算
    e = BacktestEngineV2(initial_capital=10000, leverage=1, tp_pct=10, sl_pct=5,
                         verbose=False)
    e.equity = 10000.0
    e.positions = [{'coin': 'ETH', 'side': 'LONG', 'leg': 'SWAP', 'notional': 2000.0}]
    e._funding_settle = {'ETH': {0: 0.001}}
    eq0 = e.equity
    e._settle_funding(0)
    check("现货模式 (leverage=1) 不结算", abs(e.equity - eq0) < 1e-9,
          f"d={e.equity - eq0}")


def phase7_p2_walk_forward_search():
    print("\n=== Phase 7: P2-7 Walk-Forward 真滚动优化 (IS-only 选参) ===")
    import walk_forward as wf

    class _WFStrat:
        def __init__(self, selected=None, use_and=True, mf_params=None):
            self.selected = selected or {}
        def generate_signals(self, df):
            return df.copy()

    calls = []  # 记录每个 trial 的 sl_pct

    class _FakeEngine:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.sl_pct = kwargs.get('sl_pct', 5)
        def run(self, dfs, strategy):
            calls.append(self.sl_pct)
            return {'initial_capital': 10000}

    class _FakeAnalyzer:
        @staticmethod
        def analyze(result):
            sl = calls[-1]
            # 确定性: Sharpe = 10 - sl → sl 越小越高, sl=3 应胜出
            return {'sharpe_ratio': float(10 - sl), 'total_return': 5.0,
                    'total_trades': 5, 'max_drawdown': 5.0, 'sortino_ratio': 1.0,
                    'calmar_ratio': 1.0, 'win_rate': 50.0, 'profit_factor': 1.2,
                    'annual_return': 3.0}

    _orig_engine = wf.BacktestEngineV2
    _orig_analyzer = wf.PerformanceAnalyzer
    try:
        wf.BacktestEngineV2 = _FakeEngine
        wf.PerformanceAnalyzer = _FakeAnalyzer

        engine_kwargs = {'sl_pct': 5, 'tp_pct': 10}
        best, grid = wf.WalkForwardAnalyzer._search_train_params(
            df_train=pd.DataFrame(), coin='ETH', strategy_config={},
            engine_kwargs=engine_kwargs, mf_params={}, use_and=True,
            strategy_class=_WFStrat, param_grid={'sl_pct': [3, 6]},
        )
        check("网格枚举: 2 个组合", len(grid) == 2, f"grid={len(grid)}")
        check("IS-only 选参: 选 Sharpe 最高 (sl=3)", best['sl_pct'] == 3,
              f"sl={best['sl_pct']}")
        check("未搜索参数保持原值 (tp=10)", best['tp_pct'] == 10,
              f"tp={best['tp_pct']}")

        # min_trades 过滤: 全部组合交易数不足 → 回退原参数
        class _NoTradeAnalyzer:
            @staticmethod
            def analyze(result):
                return {'sharpe_ratio': 5.0, 'total_return': 0.0, 'total_trades': 0,
                        'max_drawdown': 5.0, 'sortino_ratio': 0.0, 'calmar_ratio': 0.0,
                        'win_rate': 0.0, 'profit_factor': 0.0, 'annual_return': 0.0}
        wf.PerformanceAnalyzer = _NoTradeAnalyzer
        calls.clear()
        best2, _ = wf.WalkForwardAnalyzer._search_train_params(
            df_train=pd.DataFrame(), coin='ETH', strategy_config={},
            engine_kwargs=engine_kwargs, mf_params={}, use_and=True,
            strategy_class=_WFStrat, param_grid={'sl_pct': [3, 6]},
        )
        check("min_trades 过滤: 全部不足交易数 → 回退原参数 (sl=5)",
              best2['sl_pct'] == 5, f"sl={best2['sl_pct']}")
    finally:
        wf.BacktestEngineV2 = _orig_engine
        wf.PerformanceAnalyzer = _orig_analyzer


def phase7_p2_cagr_na():
    print("\n=== Phase 7: P2-6 CAGR 短周期 N/A ===")
    from engine_core import PerformanceAnalyzer
    result = {
        'initial_capital': 10000,
        'trades': [],
        'equity_array': np.array([10000.0, 10100.0, 10050.0, 10200.0]),
        'data_start': '2025-01-01', 'data_end': '2025-03-01',  # ~2个月 <1年
    }
    metrics = PerformanceAnalyzer.analyze(result)
    check("短周期 annual_return = None", metrics['annual_return'] is None,
          f"ar={metrics['annual_return']}")
    check("annual_return_label = N/A", metrics['annual_return_label'] == 'N/A',
          f"label={metrics['annual_return_label']}")


def phase8_p3_profit_factor():
    print("\n=== Phase 8: P3-4 真 Profit Factor 与 payoff_ratio 分离 ===")
    from engine_core import PerformanceAnalyzer

    # 3胜2亏: Σ赢=180, |Σ亏|=100 → PF=1.8; 均赢=60, 均亏=50 → 盈亏比=1.2
    result = {
        'initial_capital': 10000,
        'trades': [
            {'pnl': 100.0, 'reason': 'TP'},
            {'pnl': 50.0, 'reason': 'TP'},
            {'pnl': 30.0, 'reason': 'TP'},
            {'pnl': -50.0, 'reason': 'SL'},
            {'pnl': -50.0, 'reason': 'SL'},
        ],
        'equity_array': np.array([10000.0, 10180.0, 10200.0, 10180.0]),
        'data_start': '2025-01-01', 'data_end': '2026-01-01',
    }
    m = PerformanceAnalyzer.analyze(result)
    check("真 Profit Factor = Σ赢/|Σ亏| = 1.8", m.get('profit_factor') == 1.8,
          f"pf={m.get('profit_factor')}")
    check("payoff_ratio = 均赢/均亏 = 1.2", m.get('payoff_ratio') == 1.2,
          f"payoff={m.get('payoff_ratio')}")

    # 全胜: 无亏损 → PF=inf, payoff=inf
    result2 = {
        'initial_capital': 10000,
        'trades': [
            {'pnl': 100.0, 'reason': 'TP'},
            {'pnl': 50.0, 'reason': 'TP'},
        ],
        'equity_array': np.array([10000.0, 10150.0, 10150.0]),
        'data_start': '2025-01-01', 'data_end': '2026-01-01',
    }
    m2 = PerformanceAnalyzer.analyze(result2)
    check("全胜: profit_factor = 999.0 (哨兵值)", m2.get('profit_factor') == 999.0,
          f"pf={m2.get('profit_factor')}")
    check("全胜: payoff_ratio = 999.0 (哨兵值)", m2.get('payoff_ratio') == 999.0,
          f"payoff={m2.get('payoff_ratio')}")


def phase8_p3_walkforward_slice():
    print("\n=== Phase 8: P3-7/P3-8 Walk-Forward 窗口切片 ===")
    import walk_forward as wf

    # 数据: 2018-01-01 ~ 2026-12-31 (日线, 足够预热缓冲)
    idx = pd.date_range("2018-01-01", "2026-12-31", freq="D")
    df = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0,
                       "close": 100.0, "vol": 500.0}, index=idx)

    # P3-7: 训练窗前置 warmup 缓冲
    df_train, df_test, core, partial = wf.WalkForwardAnalyzer._slice_window(
        df, train_range=(2020, 2021), test_range=(2022, 2022))
    check("core_train_bars = 2020-2021 日数 (731)", core == 731, f"core={core}")
    check("df_train 起点早于训练期 (含 warmup 缓冲)",
          df_train.index[0] < pd.Timestamp("2020-01-01"),
          f"start={df_train.index[0]}")
    check("df_test 覆盖 2022 全年",
          df_test.index[0].year == 2022 and df_test.index[-1].year == 2022,
          f"{df_test.index[0]}~{df_test.index[-1]}")
    check("完整年度 → partial_year=False", partial is False, f"p={partial}")

    # P3-8: 数据截断到年中 → partial_year=True
    idx2 = pd.date_range("2018-01-01", "2026-06-30", freq="D")
    df2 = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0,
                        "close": 100.0, "vol": 500.0}, index=idx2)
    _, _, _, partial2 = wf.WalkForwardAnalyzer._slice_window(
        df2, train_range=(2024, 2025), test_range=(2026, 2026))
    check("数据年末前截断 → partial_year=True", partial2 is True, f"p={partial2}")


if __name__ == "__main__":
    phase1_evaluate_shift()
    phase2_entry_candle()
    phase3_fixed_risk()
    phase4_multi_asset_equity()
    phase5_pyramid_legs()
    phase6_combo_no_oos()
    phase7_p2_liquidation()
    phase7_p2_portfolio_exposure()
    phase7_p2_equity_final()
    phase7_p2_funding_settlement()
    phase7_p2_walk_forward_search()
    phase7_p2_cagr_na()
    phase8_p3_profit_factor()
    phase8_p3_walkforward_slice()
    print(f"\n{'='*50}\n  {PASS} passed, {FAIL} failed\n{'='*50}")
    sys.exit(1 if FAIL else 0)
