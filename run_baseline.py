#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 0 — 建立基准 (Backtest Integrity Hardening)

对 ETH/BTC/SOL × 4H 跑当前(未修复)引擎, 保存基准指标到
baseline_before_integrity_fix.json。只读, 不修改任何交易逻辑。
"""
import sys, os, json, time
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine_core import run_backtest, PerformanceAnalyzer, MACrossStrategy

ASSETS = ["ETH", "BTC", "SOL"]
TIMEFRAME = "4h"

# 与 run_backtest 默认参数一致, 作为可复现基准
BASE_KWARGS = dict(
    timeframe=TIMEFRAME,
    initial_capital=10000.0,
    leverage=3,
    tp_pct=10.0,
    sl_pct=5.0,
    bull_alloc=1.0,
    range_alloc=0.5,
    bear_alloc=0.3,
    bear_ratio_limit=0.5,
    verbose=False,
    tp_mode="margin_pct",
    sl_mode="margin_pct",
    max_notional_pct=5.0,
)

METRIC_KEYS = [
    "total_return", "annual_return", "sharpe_ratio", "sortino_ratio",
    "calmar_ratio", "max_drawdown", "win_rate", "profit_factor",
    "total_trades", "total_pnl", "max_consecutive_losses", "recovery_factor",
    "buy_hold_return", "years",
]


def _clean(obj):
    """递归把 numpy/pandas 类型转成 json 可序列化类型。"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, np.datetime64)):
        return str(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def main():
    out = {
        "meta": {
            "purpose": "baseline_before_integrity_fix",
            "timeframe": TIMEFRAME,
            "assets": ASSETS,
            "engine": "BacktestEngineV2 (pre-fix)",
            "params": BASE_KWARGS,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "assets": {},
    }

    for coin in ASSETS:
        t0 = time.time()
        print(f"\n[{coin}] 基准回测中 ...")
        result, metrics = run_backtest(
            coins=coin, strategy=MACrossStrategy(), **BASE_KWARGS
        )
        elapsed = time.time() - t0

        m = {k: metrics.get(k) for k in METRIC_KEYS}
        entry = {
            "metrics": _clean(m),
            "data_start": result.get("data_start", ""),
            "data_end": result.get("data_end", ""),
            "data_bars": result.get("data_bars", 0),
            "final_equity": result.get("final_equity", 0),
            "elapsed_sec": round(elapsed, 1),
            "equity_curve": _clean(result.get("equity_curve", [])),
        }
        out["assets"][coin] = entry

        print(f"[{coin}] 完成 ({elapsed:.1f}s) | "
              f"total_return={m['total_return']}% | "
              f"mdd={m['max_drawdown']}% | "
              f"sharpe={m['sharpe_ratio']} | "
              f"trades={m['total_trades']} | "
              f"range={entry['data_start']} ~ {entry['data_end']}")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "baseline_before_integrity_fix.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] 基准已保存: {out_path}")


if __name__ == "__main__":
    main()
