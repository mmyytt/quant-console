#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""收益质量审计模块验证"""
import sys, os
sys.path.insert(0, r'C:\Users\myt\Desktop\量化交易')
from engine_core import (
    BacktestEngineV2, DataEngine, MACrossStrategy,
    MultiFactorRegime, PerformanceAnalyzer
)

PASS = 0; FAIL = 0

def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [PASS] {label}")
    else:
        FAIL += 1; print(f"  [FAIL] {label}")

de = DataEngine()
dfs = de.get_multi_timeframe('ETH')
df_4h = dfs['4h']
strategy = MACrossStrategy()
regime_engine = MultiFactorRegime()
df_4h = strategy.generate_signals(df_4h)
df_4h = regime_engine.evaluate(df_4h)

BASE = dict(
    initial_capital=10000, leverage=4, tp_pct=10, sl_pct=5,
    tp_mode='margin_pct', sl_mode='margin_pct',
    bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
    max_notional_pct=5.0, lock_streak=3, lock_bars=288, cooldown_bars=2,
    verbose=False, use_atr_sl=False,
)

eng = BacktestEngineV2(**BASE)
s = MACrossStrategy()
df = df_4h.copy()
df = s.generate_signals(df)
df = regime_engine.evaluate(df)
s.selected = {
    '_pos_mode': 'fixed_capital',
    '_risk_per_trade': 1.0,
    '_init_alloc_pct': 30,
    '_bull_alloc': 100.0, '_range_alloc': 50.0, '_bear_alloc': 30.0,
    '_tp_mode': 'margin_pct', '_sl_mode': 'margin_pct',
    '_use_atr_sl': False, '_atr_period': 14, '_atr_mult': 2.0,
}
result = eng.run({'ETH': df}, s)
metrics = PerformanceAnalyzer.analyze(result)

print("=" * 60)
print("  Earnings Quality Audit Test")
print("=" * 60)
print(f"  Trades: {metrics.get('total_trades', 0)}")
print(f"  Return: {metrics.get('total_return', 0):.2f}%")

audit = PerformanceAnalyzer.quality_audit(result, metrics)

# Test 1: Annual table
annual = audit.get('annual_table', [])
print(f"\n  Annual table: {len(annual)} years")
for a in annual[:3]:
    print(f"    {a['year']}: return={a['return_pct']:+.2f}% dd={a['max_dd']:.2f}% trades={a['trades']} wr={a['win_rate']:.1f}%")

# Test 2: Contribution
c = audit.get('contribution', {})
print(f"\n  Contribution: top1={c.get('top1_pct',0):.1f}% top5={c.get('top5_pct',0):.1f}% top10={c.get('top10_pct',0):.1f}%")
print(f"  Level: {c.get('level', '?')} ({c.get('warning', '?')})")

# Test 3: Removal
rem = audit.get('extreme_removal', [])
print(f"\n  Removal tests: {len(rem)} scenarios")
for r in rem[:3]:
    print(f"    {r['label']}: new_return={r['new_return']:+.2f}% new_annual={r['new_annual']:+.2f}% new_maxdd={r['new_maxdd']:.2f}%")

# Test 4: Risk
risk = audit.get('risk_contrib', {})
print(f"\n  Risk: max_loss=${risk.get('max_single_loss',0):+,.0f} consecutive={risk.get('max_consecutive_losses',0)} period={risk.get('max_consecutive_period','?')}")

# Test 5: Stats
ts = audit.get('trade_stats', {})
print(f"\n  Stats: avg_win=${ts.get('avg_win',0):+,.0f} avg_loss=${ts.get('avg_loss',0):+,.0f}")
print(f"  max_win=${ts.get('max_win',0):+,.0f} max_loss=${ts.get('max_loss',0):+,.0f}")
print(f"  hold: avg={ts.get('avg_hold_hours',0):.1f}h max={ts.get('max_hold_hours',0):.1f}h")

# Verify all keys present
required = ['annual_table', 'contribution', 'extreme_removal', 'risk_contrib', 'trade_stats']
all_ok = all(k in audit and audit[k] is not None for k in required)
print(f"\n  All sections present: {'PASS' if all_ok else 'FAIL'}")

# Verify data from trades, not equity curve
closed = PerformanceAnalyzer._get_closed_trades(result)
if closed:
    # Verify top1 actually matches
    sorted_pnl = sorted([t['pnl'] for t in closed], reverse=True)
    calc_top1 = sorted_pnl[0] if sorted_pnl else 0
    print(f"  Top1 verification: audit={c.get('top1_amount', 0):.2f} vs calc={calc_top1:.2f} {'PASS' if abs(c.get('top1_amount',0)-calc_top1)<0.1 else 'FAIL'}")

    # Verify holding time
    holds_ok = ts.get('avg_hold_hours', -1) >= 0
    print(f"  Holding time valid: {'PASS' if holds_ok else 'FAIL'}")

# ============================================================
# New Methods Test (2026-08-11)
# ============================================================
print("\n" + "=" * 60)
print("  New Analysis Methods Test")
print("=" * 60)

# Test 1: Trading Frequency
freq = PerformanceAnalyzer.trading_frequency(result)
print(f"\n  [Frequency] {freq['total_trades']} trades, {freq['avg_per_year']:.1f}/yr, {freq['avg_per_month']:.1f}/mo")
print(f"  Level: {freq['level']} | Period: {freq['period']}")
check(freq['total_trades'] == metrics.get('total_trades', 0), "Frequency total matches metrics")
check(freq['avg_per_year'] > 0, "Avg trades/year > 0")
check(len(freq['level']) > 0, "Frequency level assigned")

# Test 2: Market Attribution
attr = PerformanceAnalyzer.market_attribution(result)
print(f"\n  [Market Attr] Bull: ${attr['bull_pnl']:+.0f} ({attr['bull_trades']}t, WR={attr['bull_wr']:.0f}%)")
print(f"  Range: ${attr['range_pnl']:+.0f} ({attr['range_trades']}t, WR={attr['range_wr']:.0f}%)")
print(f"  Bear: ${attr['bear_pnl']:+.0f} ({attr['bear_trades']}t, WR={attr['bear_wr']:.0f}%)")
print(f"  Conclusion: {attr['conclusion']}")
check(abs(attr['bull_pct'] + attr['range_pct'] + attr['bear_pct'] - 100.0) < 1.0,
      "Market pct sums to 100%")
check(len(attr['conclusion']) > 0, "Conclusion generated")

# Test 3: Strategy Summary
summary = PerformanceAnalyzer.generate_strategy_summary(result, metrics, audit)
print(f"\n  [Strategy Summary]")
for line in summary.split('\n'):
    print(f"    {line}")
check('策略类型' in summary, "Summary includes strategy type")
check('收益特征' in summary, "Summary includes return characteristics")
check('交易频率' in summary, "Summary includes frequency")
check('优化建议' in summary, "Summary includes suggestions")

# Test 4: Parameter Audit
p_report = PerformanceAnalyzer.param_audit_report(result, metrics)
print(f"\n  [Param Audit] {p_report['total_params']} UI params, {len(p_report['engine_params'])} engine-confirmed")
if p_report['anomalies']:
    print(f"  Anomalies: {p_report['anomalies']}")
else:
    print(f"  No anomalies detected")
check(p_report['total_params'] >= 10, "At least 10 UI params tracked")
check('ui_params' in p_report, "UI params list present")
check('engine_params' in p_report, "Engine params list present")

# Verify all new methods return valid data
new_methods = {
    'trading_frequency': freq,
    'market_attribution': attr,
    'generate_strategy_summary': summary,
    'param_audit_report': p_report,
}
for name, data in new_methods.items():
    check(data is not None and len(str(data)) > 0, f"{name}() returns valid data")

print("\n" + "=" * 60)
print(f"  ALL TESTS: {PASS} PASS, {FAIL} FAIL")
print("=" * 60)

if FAIL == 0:
    print("\n  >>> ALL TESTS PASSED. New analysis methods verified correct.")
else:
    print(f"\n  >>> {FAIL} TEST(S) FAILED. Review above [FAIL] items.")
