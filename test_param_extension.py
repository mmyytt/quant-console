"""
参数空间扩展回归测试：Fibonacci 回看周期 + 成交量均量周期
==========================================================
覆盖:
  1. 指标 schema 边界: FIB_lookback 50~500 step50; VOL_ma 10~100
  2. 整型参数识别/类型保持: step50 仍按整型采样, _coerce 不把整型转 float
  3. AI 参数采样: FIB_lookback / VOL_ma 采样值落在新边界内且为整型
  4. full_fingerprint: FIB 200 vs 300 指纹不同
  5. 真实回测: Fibonacci 200 vs 300 结果不同 (参数真正生效)
  6. 真实回测: VOL_ma 20 vs 50 结果不同
  7. 鲁棒性实验室: volume_ma 维度已定义并正确写 VOL_ma

运行: python test_param_extension.py
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


def _run_fib(lookback, df):
    from research_loop import build_selected, _make_strategy
    from research_phase1 import make_engine_kwargs, run_single
    sel = build_selected(['斐波那契回调'], {'斐波那契回调': {'FIB_lookback': lookback}})
    strat = _make_strategy(dict(sel), None)
    kw = make_engine_kwargs(2, 8.0, 4.0)
    _, m = run_single(df, 'ETH', strat, kw)
    return m


def _run_volma(period, df):
    from research_loop import build_selected, _make_strategy
    from research_phase1 import make_engine_kwargs, run_single
    sel = build_selected(['成交量突破'], {'成交量突破': {'VOL_ma': period, 'VOL_mult': 1.5}})
    strat = _make_strategy(dict(sel), None)
    kw = make_engine_kwargs(2, 8.0, 4.0)
    _, m = run_single(df, 'ETH', strat, kw)
    return m


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)

    # ── 1. schema 边界 ──
    from indicator_schema import INDICATOR_SCHEMA
    fib = INDICATOR_SCHEMA['fibonacci']['params']['FIB_lookback']
    check(fib['min'] == 50 and fib['max'] == 500 and fib.get('step') == 50,
          f"1a. FIB_lookback 边界 50~500 step50 (实际 {fib['min']}~{fib['max']} step{fib.get('step')})")
    vol = INDICATOR_SCHEMA['vol_break']['params']['VOL_ma']
    check(vol['min'] == 10 and vol['max'] == 100,
          f"1b. VOL_ma 边界 10~100 (实际 {vol['min']}~{vol['max']})")

    # ── 2. 整型识别 / 类型保持 ──
    from research_loop import _param_type_int, _coerce
    check(_param_type_int(fib) is True, "2a. FIB_lookback step50 仍识别为整型参数")
    check(_coerce(300, fib['default']) == 300 and isinstance(_coerce(300, fib['default']), int),
          "2b. _coerce(300) 保持整型 (不转 float 300.0)")
    check(_coerce(2.0, vol['default']) == 2 and isinstance(_coerce(2.0, vol['default']), int),
          "2c. _coerce 整型参数取整")

    # ── 3. AI 参数采样 ──
    from research_loop import _param_sample_values
    fib_vals = dict(_param_sample_values('fibonacci'))['FIB_lookback']
    check(all(isinstance(v, int) for v in fib_vals) and min(fib_vals) >= 50 and max(fib_vals) <= 500,
          f"3a. FIB_lookback 采样为整型且落 50~500 ({min(fib_vals)}~{max(fib_vals)})")
    vol_vals = dict(_param_sample_values('vol_break'))['VOL_ma']
    check(all(isinstance(v, int) for v in vol_vals) and min(vol_vals) >= 10 and max(vol_vals) <= 100,
          f"3b. VOL_ma 采样为整型且落 10~100 ({min(vol_vals)}~{max(vol_vals)})")

    # ── 4. full_fingerprint 区分 ──
    from research_loop import full_fingerprint
    fp200 = full_fingerprint(['斐波那契回调'], {'斐波那契回调': {'FIB_lookback': 200}})
    fp300 = full_fingerprint(['斐波那契回调'], {'斐波那契回调': {'FIB_lookback': 300}})
    check(fp200 != fp300, f"4a. FIB 200 vs 300 指纹不同 ({fp200} != {fp300})")
    check('FIB_LOOKBACK=200' in fp200 and 'FIB_LOOKBACK=300' in fp300,
          "4b. 指纹含 FIB_lookback 参数值")

    # ── 5/6. 真实回测差异 ──
    from engine_core import DataEngine
    de = DataEngine()
    df = de.get_multi_timeframe('ETH')['1h']
    m200 = _run_fib(200, df)
    m300 = _run_fib(300, df)
    diff_fib = (m200.get('total_return'), m300.get('total_return'),
                m200.get('trade_count'), m300.get('trade_count'))
    check(m200.get('trade_count') != m300.get('trade_count')
          or m200.get('total_return') != m300.get('total_return'),
          f"5. Fibonacci 200 vs 300 结果不同 (ret {m200.get('total_return')}%/{m300.get('total_return')}%, "
          f"trades {m200.get('trade_count')}/{m300.get('trade_count')})")

    m20 = _run_volma(20, df)
    m50 = _run_volma(50, df)
    check(m20.get('trade_count') != m50.get('trade_count')
          or m20.get('total_return') != m50.get('total_return'),
          f"6. VOL_ma 20 vs 50 结果不同 (ret {m20.get('total_return')}%/{m50.get('total_return')}%, "
          f"trades {m20.get('trade_count')}/{m50.get('trade_count')})")

    # ── 7. 鲁棒性实验室 volume_ma 维度 ──
    from robustness_lab import SWEEP_DIMENSIONS, PARAM_COMBO_GRID, RobustnessLab
    check('volume_ma' in SWEEP_DIMENSIONS and 'volume_ma' in PARAM_COMBO_GRID,
          "7a. volume_ma 维度已加入 SWEEP_DIMENSIONS / PARAM_COMBO_GRID")
    check(SWEEP_DIMENSIONS['fibonacci']['values'] == [50, 100, 150, 200, 250, 300, 350, 400, 450, 500],
          "7b. fibonacci sweep 覆盖 50~500")
    cfg = RobustnessLab._build_config(
        {'engine_kwargs': {}, 'selected_indicators': {}, 'coin': 'ETH', 'df': df},
        'volume_ma', 70)
    check(cfg['selected_indicators'].get('成交量突破', {}).get('params', {}).get('VOL_ma') == 70,
          "7c. volume_ma 扫描正确写入 VOL_ma=70")

    print("\n" + "=" * 60)
    print(f"  RESULT: {PASS} PASS, {FAIL} FAIL")
    print("=" * 60)
    if FAIL == 0:
        print("  >>> ALL PARAM EXTENSION TESTS PASSED")
    else:
        print(f"  >>> {FAIL} TEST(S) FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
