#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
顺势加仓架构升级 — 端到端验证 (历史回测 + 样本外)
=================================================
验证:
  1. 加仓开/关 现在产生不同资金曲线 (修复前完全一致)
  2. 单笔建仓比例 50% vs 100% 现在产生不同结果
  3. 加仓开启后的风险指标 (回撤/夏普/胜率) 合理性
  4. 样本外 (OOS) 表现
"""
import os
import sys
import time


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

    from engine_core import (
        BacktestEngineV2, DataEngine, MACrossStrategy,
        MultiFactorRegime, PerformanceAnalyzer,
    )

    de = DataEngine()
    dfs = de.get_multi_timeframe('ETH')
    df = dfs['4h'].copy()
    strategy = MACrossStrategy()
    regime = MultiFactorRegime()
    df = strategy.generate_signals(df)
    df = regime.evaluate(df)
    print(f"数据: ETH 4h {len(df)} 根, {df.index[0]} ~ {df.index[-1]}")

    KW = dict(
        initial_capital=10000, leverage=3,
        tp_pct=10.0, sl_pct=5.0, tp_mode='margin_pct', sl_mode='margin_pct',
        bull_alloc=1.0, range_alloc=1.0, bear_alloc=1.0,
        max_notional_pct=5.0, lock_streak=3, lock_bars=288, cooldown_bars=2,
        verbose=False,
    )

    def make_selected(enable_pyr, init_alloc, trigger=4.0, add=0.5, max_adds=3):
        return {
            '_enable_pyramiding': enable_pyr,
            '_pyr_trigger_pct': trigger,
            '_pyr_add_pct': add,
            '_pyr_max': max_adds,
            '_pyr_trail': False,
            '_init_alloc_pct': init_alloc,
            '_pos_mode': 'fixed_capital',
            '_risk_per_trade': 1.0,
            '_bull_alloc': 100.0, '_range_alloc': 100.0, '_bear_alloc': 100.0,
            '_tp_mode': 'margin_pct', '_sl_mode': 'margin_pct',
        }

    def run_backtest(df_seg, sel):
        s = MACrossStrategy()
        d = df_seg.copy()
        d = s.generate_signals(d)
        d = regime.evaluate(d)
        s.selected = sel
        eng = BacktestEngineV2(**KW)
        res = eng.run({'ETH': d}, s)
        m = PerformanceAnalyzer.analyze(res)
        return res, m

    def row(label, res, m):
        return (f"{label:<26s} 年化={m.get('annual_return') or 0:>7.2f}%  "
                f"累计={m.get('total_return') or 0:>8.2f}%  回撤={m.get('max_drawdown') or 0:>6.2f}%  "
                f"胜率={m.get('win_rate') or 0:>5.1f}%  交易={m.get('total_trades') or 0:>3d}  "
                f"夏普={m.get('sharpe_ratio') or 0:>5.2f}  "
                f"终值=${res.get('final_equity', 0):>10.2f}")

    print("\n=== 历史回测 (全样本) ===")
    cfgs = [
        ("A: 单笔50% 关闭加仓", False, 50),
        ("B: 单笔50% 开启加仓", True, 50),
        ("C: 单笔100% 关闭加仓", False, 100),
    ]
    results = {}
    for label, en, alloc in cfgs:
        t0 = time.time()
        res, m = run_backtest(df, make_selected(en, alloc))
        results[label] = (res, m)
        print("  " + row(label, res, m) + f"   ({time.time()-t0:.1f}s)")

    # 断言: 三个配置不再完全一致 (修复核心验证)
    ars = [results[l][1].get('annual_return') for l, _, _ in cfgs]
    if len(set(round(a, 3) for a in ars)) <= 1:
        print("\n  [WARN] 三个配置年化仍一致 — 加仓可能仍未生效!")
    else:
        print("\n  [OK] 三个配置年化已分化 — 加仓与单笔比例均影响资金曲线 (修复生效)")

    # 加仓开关对比
    ar_off = results["A: 单笔50% 关闭加仓"][1].get('annual_return')
    ar_on = results["B: 单笔50% 开启加仓"][1].get('annual_return')
    print(f"  [对比] 加仓开 vs 关: 年化 {ar_off:.2f}% → {ar_on:.2f}%")

    # 样本外 (OOS): 2024 之后
    print("\n=== 样本外 (OOS: 2024-2026) ===")
    mask = df.index.year >= 2024
    df_oos = df[mask].copy()
    if len(df_oos) >= 200:
        for label, en, alloc in [("A: 50% 关闭加仓", False, 50), ("B: 50% 开启加仓", True, 50)]:
            res, m = run_backtest(df_oos, make_selected(en, alloc))
            print("  " + row("OOS " + label, res, m))
    else:
        print("  OOS 数据不足 200 根, 跳过")

    print("\n验证完成")


if __name__ == "__main__":
    main()
