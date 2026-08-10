#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""鲁棒性实验室验证脚本"""
import sys, os
sys.path.insert(0, r'C:\Users\myt\Desktop\量化交易')
from engine_core import (
    BacktestEngineV2, DataEngine, MACrossStrategy,
    MultiFactorRegime, PerformanceAnalyzer
)
from robustness_lab import RobustnessLab, SWEEP_DIMENSIONS, run_full_sweep

PASS = 0; FAIL = 0

def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [PASS] {label}")
    else:
        FAIL += 1; print(f"  [FAIL] {label}")

# ============================================================
# 准备基准数据
# ============================================================
print("=" * 60)
print("  Robustness Lab Verification")
print("=" * 60)

de = DataEngine()
dfs = de.get_multi_timeframe('ETH')
df_4h = dfs['4h']

# 限制数据范围加速测试 (只用最近2年)
df_test = df_4h.iloc[-2000:].copy()
print(f"\n  Test data: {len(df_test)} bars, {df_test.index[0]} ~ {df_test.index[-1]}")

# 基准配置
base_config = {
    'engine_kwargs': dict(
        initial_capital=10000, leverage=3, tp_pct=10, sl_pct=5,
        tp_mode='margin_pct', sl_mode='margin_pct',
        bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
        max_notional_pct=5.0, lock_streak=3, lock_bars=288, cooldown_bars=2,
        verbose=False, use_atr_sl=False, atr_period=14, atr_mult=2.0,
        max_positions=1, strategy_mode='classic',
    ),
    'selected_indicators': {
        'EMA 双均线': {'enabled': True, 'params': {'EMA_short': 7, 'EMA_long': 21}},
        '斐波那契回调': {'enabled': True, 'params': {'FIB_lookback': 50}},
        '成交量突破': {'enabled': True, 'params': {'VOL_ma': 20, 'VOL_mult': 1.5}},
        '_pos_mode': 'fixed_capital',
        '_risk_per_trade': 1.0,
        '_init_alloc_pct': 30,
        '_bull_alloc': 100.0, '_range_alloc': 50.0, '_bear_alloc': 30.0,
        '_tp_mode': 'margin_pct', '_sl_mode': 'margin_pct',
        '_use_atr_sl': False, '_atr_period': 14, '_atr_mult': 2.0,
        '_trade_mode': '双向', '_regime_filter': True,
    },
    'use_and': True,
    'mf_params': {'enabled': True, 'ema_w': 0.40, 'adx_w': 0.35, 'adx_th': 25, 'bull_th': 0.30},
    'coin': 'ETH',
    'df': df_test,
}

# ============================================================
# Test 1: 单维度扫描 (leverage)
# ============================================================
print("\n" + "=" * 60)
print("  Test 1: Leverage Sweep (5 runs)")
print("=" * 60)

sweeps = RobustnessLab.run_sweep(base_config, 'leverage')
check(len(sweeps) == 5, "5 leverage values tested")
check(all(s.get('metrics') is not None for s in sweeps), "All sweeps returned valid metrics")
check(not any(s.get('error') for s in sweeps), "No errors in any sweep")

# 检查不同杠杆产生不同结果
returns = [s['total_return'] for s in sweeps]
check(len(set(round(r, 4) for r in returns)) >= 2, "Different leverage -> different returns")

for s in sweeps:
    print(f"    {s['label']}: return={s['total_return']:+.2f}% dd={s['max_drawdown']:.2f}% "
          f"sharpe={s['sharpe_ratio']:.3f} trades={s['total_trades']} wr={s['win_rate']:.1f}%")

# ============================================================
# Test 2: EMA 维度扫描
# ============================================================
print("\n" + "=" * 60)
print("  Test 2: EMA Sweep (4 runs)")
print("=" * 60)

sweeps_ema = RobustnessLab.run_sweep(base_config, 'ema')
check(len(sweeps_ema) == 4, "4 EMA combos tested")
check(all(s.get('metrics') is not None for s in sweeps_ema), "All EMA sweeps valid")

for s in sweeps_ema:
    print(f"    {s['label']}: return={s['total_return']:+.2f}% trades={s['total_trades']}")

# ============================================================
# Test 3: ATR 维度扫描
# ============================================================
print("\n" + "=" * 60)
print("  Test 3: ATR Stop Sweep (4 runs)")
print("=" * 60)

sweeps_atr = RobustnessLab.run_sweep(base_config, 'atr_stop')
check(len(sweeps_atr) == 4, "4 ATR configs tested")

for s in sweeps_atr:
    print(f"    {s['label']}: return={s['total_return']:+.2f}% trades={s['total_trades']}")

# ============================================================
# Test 4: 稳定性评分
# ============================================================
print("\n" + "=" * 60)
print("  Test 4: Stability Scoring")
print("=" * 60)

all_results = {
    'leverage': sweeps,
    'ema': sweeps_ema,
    'atr_stop': sweeps_atr,
}
stability = RobustnessLab.stability_score(all_results)

check('dim_scores' in stability, "stability has dim_scores")
check('overall' in stability, "stability has overall verdict")
check('summary' in stability, "stability has summary text")
check(len(stability['summary']) > 0, "Summary is non-empty")

for dim, ds in stability['dim_scores'].items():
    print(f"    {dim}: CV={ds['cv']:.3f} range={ds['range_pct']:.1f}% "
          f"best={ds['best']}({ds['best_return']:+.1f}%) verdict={ds['verdict']}")
print(f"  Overall: {stability['overall']} — {stability['summary']}")

# ============================================================
# Test 5: 矩阵格式化
# ============================================================
print("\n" + "=" * 60)
print("  Test 5: Matrix Formatting")
print("=" * 60)

mat = RobustnessLab.format_matrix('leverage', sweeps)
check(len(mat) == 5, "Matrix has 5 rows")
check('参数' in mat.columns, "Matrix has param column")
check('总收益%' in mat.columns, "Matrix has return column")
check('夏普' in mat.columns, "Matrix has sharpe column")
print("  Matrix columns:", list(mat.columns))

# ============================================================
# Test 6: 报告生成
# ============================================================
print("\n" + "=" * 60)
print("  Test 6: Report Generation")
print("=" * 60)

report = RobustnessLab.generate_report(all_results, stability)
check('策略鲁棒性评估报告' in report, "Report has title")
check('综合评级' in report, "Report has overall rating")
check('杠杆' in report, "Report mentions leverage")
check('EMA' in report, "Report mentions EMA")
check('ATR' in report, "Report mentions ATR")
print("  Report preview:")
for line in report.split('\n')[:5]:
    print(f"    {line}")

# ============================================================
# Test 7: SWEEP_DIMENSIONS 定义完整性
# ============================================================
print("\n" + "=" * 60)
print("  Test 7: Dimension Definitions")
print("=" * 60)

expected_dims = ['leverage', 'ema', 'atr_stop', 'fibonacci', 'volume']
for d in expected_dims:
    check(d in SWEEP_DIMENSIONS, f"Dimension '{d}' defined")
    check('label' in SWEEP_DIMENSIONS[d], f"  {d}: has label")
    check('values' in SWEEP_DIMENSIONS[d], f"  {d}: has values")
    check('format' in SWEEP_DIMENSIONS[d], f"  {d}: has format function")

# ============================================================
# Test 8: 参数隔离 (基准配置不应被修改)
# ============================================================
print("\n" + "=" * 60)
print("  Test 8: Base Config Isolation")
print("=" * 60)

# 保存原始值
orig_leverage = base_config['engine_kwargs']['leverage']
orig_ema_params = dict(base_config['selected_indicators']['EMA 双均线']['params'])

# 运行扫描
_ = RobustnessLab.run_sweep(base_config, 'leverage')
_ = RobustnessLab.run_sweep(base_config, 'ema')

# 验证原始值未变
check(base_config['engine_kwargs']['leverage'] == orig_leverage,
      f"Leverage unchanged: {base_config['engine_kwargs']['leverage']} == {orig_leverage}")
check(base_config['selected_indicators']['EMA 双均线']['params']['EMA_short'] == orig_ema_params['EMA_short'],
      f"EMA_short unchanged")

# ============================================================
# Summary
# ============================================================
print("\n" + "=" * 60)
print(f"  VERIFICATION COMPLETE: {PASS} PASS, {FAIL} FAIL")
print("=" * 60)

if FAIL == 0:
    print("\n  >>> ALL TESTS PASSED. Robustness Lab verified correct.")
else:
    print(f"\n  >>> {FAIL} TEST(S) FAILED. Review above.")
