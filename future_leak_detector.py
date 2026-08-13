#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FutureLeakDetector — 未来函数 / 数据泄露静态扫描器
===================================================

对策略生成的指标列做启发式检测, 判断是否偷用了未来或当前K线数据:
  - 使用当前 close (未 shift)
  - 使用未来 high / low / volume / close (shift(-1))

非阻塞: 命中时 print warning 并返回列表, 不改变任何交易逻辑。

检测规则 (只针对 float 数值列, 跳过 signal/regime/score 等标签列):
  1. trailing NaN (列末为 NaN)        → 疑似 shift(-1) 使用未来数据
  2. 与 close 逐元素相等 (>99%)        → 使用当前 close 未 shift
  3. 与 high/low/vol/close.shift(-1) 相等 → 使用未来 high/low/volume/close
  4. 无 leading warmup NaN 且与 close 高度相关 → 疑似未 shift (软启发)
"""
import numpy as np
import pandas as pd

# 引擎内部 / 标签列, 不作为指标列扫描
_IGNORE_COLS = {
    'open', 'high', 'low', 'close', 'vol',
    'signal', 'score', 'regime', 'br', 'resonance_score',
    '_atr_14', '_gap',
}

_EPS = 1e-12


def _nan_equal_ratio(a: pd.Series, b: pd.Series) -> float:
    """两列在共同非 NaN 位置逐元素相等的比例 (忽略双 NaN)。"""
    both = a.notna() & b.notna()
    if both.sum() == 0:
        return 0.0
    return float((a[both] == b[both]).mean())


def _leading_nan(s: pd.Series) -> int:
    """前导 NaN 数量。"""
    mask = s.isna()
    if not mask.any():
        return 0
    return int(mask.argmax()) if not mask.iloc[0] else int((~mask).argmax())


class FutureLeakDetector:
    """扫描策略输出 df, 报告未来函数嫌疑。"""

    def __init__(self, threshold: float = 0.99, corr_threshold: float = 0.999):
        self.threshold = threshold
        self.corr_threshold = corr_threshold

    def scan(self, strategy_df: pd.DataFrame,
             raw_df: pd.DataFrame = None) -> list:
        """
        Args:
            strategy_df: 策略 generate_signals 输出 (含指标列)
            raw_df: 原始 OHLCV (若为 None, 用 strategy_df 自身的 OHLCV 列)
        Returns:
            [{'column', 'type', 'message'}, ...]
        """
        warnings = []
        base = raw_df if raw_df is not None else strategy_df

        close = base['close']
        high = base['high']; low = base['low']; vol = base.get('vol')

        future_refs = {
            'future_high': high.shift(-1),
            'future_low': low.shift(-1),
            'future_close': close.shift(-1),
        }
        if vol is not None:
            future_refs['future_volume'] = vol.shift(-1)

        for col in strategy_df.columns:
            if col in _IGNORE_COLS:
                continue
            s = strategy_df[col]
            if not pd.api.types.is_float_dtype(s) and not pd.api.types.is_numeric_dtype(s):
                continue
            # 非数值或全 NaN 跳过
            if s.notna().sum() == 0:
                continue

            # 规则 1: 与当前 close 相等 → 未 shift (specific 优先)
            if _nan_equal_ratio(s, close) >= self.threshold:
                warnings.append({
                    'column': col, 'type': 'current_close',
                    'message': f"列 {col} 与当前 close 高度一致, 疑似使用当前K线 close 未 shift",
                })
                continue

            # 规则 2: 与未来 high/low/close/volume 相等 (specific)
            matched = False
            for ftype, fser in future_refs.items():
                if fser is not None and _nan_equal_ratio(s, fser) >= self.threshold:
                    warnings.append({
                        'column': col, 'type': ftype,
                        'message': f"列 {col} 与 {ftype} 高度一致, 疑似使用未来数据",
                    })
                    matched = True
                    break
            if matched:
                continue

            # 规则 3: trailing NaN → 未来数据 (generic fallback)
            if len(s) > 1 and pd.isna(s.iloc[-1]) and not pd.isna(s.iloc[0]):
                warnings.append({
                    'column': col, 'type': 'future_data',
                    'message': f"列 {col} 尾部含 NaN, 疑似使用 shift(-1) 偷看未来数据",
                })
                continue

            # 规则 4: 无 leading warmup 且与 close 高度相关 → 软启发
            if _leading_nan(s) == 0 and s.dtype.kind == 'f':
                valid = s.notna() & close.notna()
                if valid.sum() > 10:
                    corr = s[valid].corr(close[valid])
                    if corr is not None and not np.isnan(corr) and abs(corr) >= self.corr_threshold:
                        warnings.append({
                            'column': col, 'type': 'no_shift_suspect',
                            'message': f"列 {col} 无前导 warmup NaN 且与 close 高度相关, 疑似未 shift",
                        })

        return warnings

    @staticmethod
    def report(warnings: list) -> str:
        if not warnings:
            return "[FutureLeakDetector] OK: 未发现未来函数嫌疑"
        lines = ["[FutureLeakDetector] WARNING: 发现未来函数嫌疑:"]
        for w in warnings:
            lines.append(f"  - [{w['type']}] {w['message']}")
        return "\n".join(lines)
