"""
Fibonacci 回看周期全链路验证：200 / 300 / 400 / 500 均可运行
==========================================================
覆盖用户报告的「页面仍限制 200」遗漏点，验证：
  1. schema / registry 的 FIB_lookback 上限 = 500（非 200）
  2. _fibonacci 计算函数在 lookback=200/300/400/500 下真实计算，产生信号（非静默跳过）
  3. 不同 lookback 产生不同信号
  4. 真实回测 FIB=200/300/400/500 全部运行且结果不同（参数真实生效）
  5. 鲁棒性实验室 fibonacci 扫描值从 schema 派生（统一数据源）

运行: python test_fib_range.py
"""
import os
import sys


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


def _synthetic_df(n=800):
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + np.abs(rng.normal(0, 2, n))
    low = close - np.abs(rng.normal(0, 2, n))
    open_ = np.roll(close, 1); open_[0] = close[0]
    vol = rng.uniform(100, 1000, n)
    df = pd.DataFrame({
        'open': open_, 'high': high, 'low': low,
        'close': close, 'volume': vol, 'vol': vol,
    })
    df.index = pd.date_range('2024-01-01', periods=n, freq='1h')
    return df


def _run_fib(lookback, df):
    from research_loop import build_selected, _make_strategy
    from research_phase1 import make_engine_kwargs, run_single
    sel = build_selected(['斐波那契回调'], {'斐波那契回调': {'FIB_lookback': lookback}})
    strat = _make_strategy(dict(sel), None)
    kw = make_engine_kwargs(2, 8.0, 4.0)
    _, m = run_single(df, 'ETH', strat, kw)
    return m


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)

    # ── 1. schema / registry 上限 = 500 ──
    import indicator_schema as sch
    fib = sch.INDICATOR_SCHEMA['fibonacci']['params']['FIB_lookback']
    check(fib['max'] == 500, f"1a. schema FIB_lookback.max == 500 (实际 {fib['max']})")
    reg = sch.INDICATOR_REGISTRY['斐波那契回调']['params']['FIB_lookback']
    check(reg['max'] == 500, f"1b. registry FIB_lookback.max == 500 (实际 {reg['max']})")

    # ── 2. _fibonacci 计算: 200/300/400/500 真实产生信号 ──
    df_syn = _synthetic_df()
    sig_counts = {}
    for lb in (200, 300, 400, 500):
        d = df_syn.copy()
        sch._fibonacci(d, lb)  # 整型 lookback，真实计算
        has_l = '_long' in d.columns and bool(d['_long'].sum())
        has_s = '_short' in d.columns and bool(d['_short'].sum())
        n_sig = int(d['_long'].sum() + d['_short'].sum())
        sig_counts[lb] = n_sig
        check(has_l and has_s and n_sig > 0,
              f"2. lookback={lb} 真实计算, 信号数={n_sig} (非静默跳过)")

    # ── 3. 不同 lookback 产生不同信号 ──
    uniq = len(set(sig_counts.values()))
    check(uniq >= 2, f"3. 不同 lookback 信号数不同 (signal_counts={sig_counts})")

    # ── 4. 真实回测 FIB=200/300/400/500 全部运行且结果不同 ──
    from engine_core import DataEngine
    df_real = DataEngine().get_multi_timeframe('ETH')['1h']
    rets = {}
    for lb in (200, 300, 400, 500):
        m = _run_fib(lb, df_real)
        rets[lb] = m.get('total_return')
        check(m.get('total_return') is not None, f"4a. FIB={lb} 回测成功 (ret={m.get('total_return')}%)")
    check(len(set(round(r, 4) for r in rets.values() if r is not None)) >= 2,
          f"4b. FIB 200/300/400/500 结果不同 (rets={ {k: round(v,2) for k,v in rets.items()} })")

    # ── 5. 鲁棒性实验室 fibonacci 扫描从 schema 派生 ──
    from robustness_lab import SWEEP_DIMENSIONS, _FIB_SWEEP, _FIB_PV
    expected = list(range(_FIB_PV['min'], _FIB_PV['max'] + 1, _FIB_PV['step']))
    check(SWEEP_DIMENSIONS['fibonacci']['values'] == expected,
          f"5a. fibonacci sweep 派生自 schema (values={SWEEP_DIMENSIONS['fibonacci']['values']})")
    check(max(SWEEP_DIMENSIONS['fibonacci']['values']) == 500,
          f"5b. sweep 最大值为 500 (实际 {max(SWEEP_DIMENSIONS['fibonacci']['values'])})")

    print("\n" + "=" * 60)
    print(f"  RESULT: {PASS} PASS, {FAIL} FAIL")
    print("=" * 60)
    if FAIL == 0:
        print("  >>> ALL FIB RANGE TESTS PASSED")
    else:
        print(f"  >>> {FAIL} TEST(S) FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
