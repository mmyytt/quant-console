#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
策略鲁棒性分析实验室 (Parameter Robustness Lab)
==============================================
一键参数敏感性测试。所有测试调用真实 engine 回测流程。
禁止复制计算结果。保证 UI参数 -> engine参数链路一致。
"""
import copy, statistics, math, time
from typing import Dict, List, Optional
import pandas as pd
import numpy as np

from engine_core import (
    BacktestEngineV2, DataEngine, PerformanceAnalyzer,
    MultiFactorRegime, StrategyBase,
)


# ================================================================
# 参数维度定义
# ================================================================

SWEEP_DIMENSIONS = {
    'leverage': {
        'label': '杠杆倍数',
        'values': [1, 2, 3, 4, 5],
        'format': lambda v: f'{v}x',
        'category': 'engine',
    },
    'ema': {
        'label': 'EMA 双均线',
        'values': [(10, 50), (15, 60), (20, 80), (30, 100)],
        'format': lambda v: f'{v[0]}/{v[1]}',
        'category': 'indicator',
        'indicator_name': 'EMA 双均线',
    },
    'atr_stop': {
        'label': 'ATR 止损',
        'values': [
            {'use_atr': False, 'atr_mult': None},
            {'use_atr': True, 'atr_mult': 1.0},
            {'use_atr': True, 'atr_mult': 2.0},
            {'use_atr': True, 'atr_mult': 3.0},
        ],
        'format': lambda v: 'ATR关闭' if not v['use_atr'] else f'ATR(14)*{int(v["atr_mult"])}',
        'category': 'hybrid',  # engine_kwargs + selected_indicators
    },
    'fibonacci': {
        'label': 'Fibonacci 回看',
        'values': [100, 150, 200, 300],
        'format': lambda v: f'{v}根',
        'category': 'indicator',
        'indicator_name': '斐波那契回调',
    },
    'volume': {
        'label': '成交量倍数',
        'values': [1.5, 2.0, 2.5, 3.0],
        'format': lambda v: f'{v}x',
        'category': 'indicator',
        'indicator_name': '成交量突破',
    },
}


# ================================================================
# RobustnessLab 核心类
# ================================================================

class RobustnessLab:
    """参数鲁棒性分析实验室 — 所有方法为静态方法"""

    @staticmethod
    def run_sweep(base_config: Dict, dimension: str,
                  progress_callback=None, strategy_class=None) -> List[Dict]:
        """
        对单个维度的一组参数值，逐个运行完整回测。

        Args:
            base_config: {
                'engine_kwargs': dict,       # BacktestEngineV2 构造参数
                'selected_indicators': dict, # DynamicStrategy 的 selected
                'use_and': bool,
                'mf_params': dict,           # MultiFactorRegime 参数
                'coin': str,
                'df': pd.DataFrame,          # 原始K线数据
            }
            dimension: 'leverage'|'ema'|'atr_stop'|'fibonacci'|'volume'
            progress_callback: callable(current, total, label) 或 None
            strategy_class: DynamicStrategy 类引用，用于避免 Streamlit 环境下的自导入问题

        Returns:
            [{label, params, metrics, result_summary}, ...]
        """
        dim_def = SWEEP_DIMENSIONS[dimension]
        values = dim_def['values']
        results = []

        for i, val in enumerate(values):
            label = dim_def['format'](val)
            if progress_callback:
                progress_callback(i + 1, len(values), f'{dim_def["label"]}: {label}')

            try:
                cfg = RobustnessLab._build_config(base_config, dimension, val)
                sweep_result = RobustnessLab._run_single(cfg, strategy_class=strategy_class)
                sweep_result['label'] = label
                sweep_result['param_value'] = val
                results.append(sweep_result)
            except Exception as e:
                import traceback
                results.append({
                    'label': label, 'param_value': val,
                    'error': str(e), 'traceback': traceback.format_exc(),
                    'metrics': None,
                })

        return results

    @staticmethod
    def _build_config(base_config: Dict, dimension: str, value) -> Dict:
        """深拷贝并修改参数，构建本次回测配置"""
        cfg = {
            'engine_kwargs': copy.deepcopy(base_config['engine_kwargs']),
            'selected_indicators': copy.deepcopy(base_config['selected_indicators']),
            'use_and': base_config.get('use_and', True),
            'mf_params': copy.deepcopy(base_config.get('mf_params', {})),
            'coin': base_config['coin'],
            'df': base_config['df'].copy(),
        }

        dim_def = SWEEP_DIMENSIONS[dimension]
        cat = dim_def['category']
        sel = cfg['selected_indicators']
        ek = cfg['engine_kwargs']

        if dimension == 'leverage':
            ek['leverage'] = value

        elif dimension == 'ema':
            name = dim_def['indicator_name']
            fast, slow = value
            # 确保指标启用
            if name not in sel or not isinstance(sel.get(name), dict):
                sel[name] = {'enabled': True, 'params': {}}
            sel[name]['enabled'] = True
            sel[name]['params']['EMA_short'] = fast
            sel[name]['params']['EMA_long'] = slow

        elif dimension == 'atr_stop':
            use_atr = value['use_atr']
            atr_mult = value['atr_mult']
            ek['use_atr_sl'] = use_atr
            if atr_mult is not None:
                ek['atr_mult'] = atr_mult
            sel['_use_atr_sl'] = use_atr
            if atr_mult is not None:
                sel['_atr_mult'] = atr_mult

        elif dimension == 'fibonacci':
            name = dim_def['indicator_name']
            if name not in sel or not isinstance(sel.get(name), dict):
                sel[name] = {'enabled': True, 'params': {}}
            sel[name]['enabled'] = True
            sel[name]['params']['FIB_lookback'] = value

        elif dimension == 'volume':
            name = dim_def['indicator_name']
            if name not in sel or not isinstance(sel.get(name), dict):
                sel[name] = {'enabled': True, 'params': {}}
            sel[name]['enabled'] = True
            sel[name]['params']['VOL_mult'] = value

        return cfg

    @staticmethod
    def _run_single(cfg: Dict, strategy_class=None) -> Dict:
        """运行单次回测，返回指标摘要

        Args:
            cfg: 回测配置字典
            strategy_class: DynamicStrategy 类引用。在 Streamlit 环境(从app.py调用)
                           必须显式传入，避免 `from app import DynamicStrategy` 触发
                           自导入导致 ScriptRunContext 丢失。外部脚本可不传，自动导入。
        """
        # ── 日志: 引擎参数 ──
        ek = cfg['engine_kwargs']
        sel = cfg['selected_indicators']

        # ── 获取 DynamicStrategy 类 ──
        if strategy_class is not None:
            DynamicStrategyCls = strategy_class
        else:
            try:
                import sys
                # 如果 app 已在 sys.modules 中（Streamlit 环境），直接引用避免重导入
                if 'app' in sys.modules:
                    DynamicStrategyCls = sys.modules['app'].DynamicStrategy
                else:
                    from app import DynamicStrategy as DynamicStrategyCls
            except Exception as import_err:
                raise RuntimeError(
                    f"[鲁棒性测试] 无法导入 DynamicStrategy。"
                    f"请从 app.py 调用时传入 strategy_class 参数。"
                    f"原始错误: {import_err}"
                )

        # ── 日志: 关键测试参数 ──
        indicator_summary = {}
        for name, icfg in sel.items():
            if isinstance(icfg, dict) and icfg.get('enabled') and 'params' in icfg:
                indicator_summary[name] = icfg['params']

        # ── 创建引擎和策略 ──
        engine = BacktestEngineV2(**ek)
        strategy = DynamicStrategyCls(
            selected=sel,
            use_and=cfg['use_and'],
            mf_params=cfg['mf_params'],
        )

        # ── 运行回测 ──
        result = engine.run({cfg['coin']: cfg['df']}, strategy)
        metrics = PerformanceAnalyzer.analyze(result)

        return {
            'total_return': metrics.get('total_return', 0),
            'annual_return': metrics.get('annual_return', 0),
            'max_drawdown': metrics.get('max_drawdown', 0),
            'sharpe_ratio': metrics.get('sharpe_ratio', 0),
            'win_rate': metrics.get('win_rate', 0),
            'total_trades': metrics.get('total_trades', 0),
            'profit_factor': metrics.get('profit_factor', 0),
            'metrics': metrics,
            'result': result,
        }

    # ================================================================
    # 稳定性评分
    # ================================================================

    @staticmethod
    def stability_score(all_results: Dict) -> Dict:
        """
        分析参数敏感性，返回稳定性评估。

        Args:
            all_results: {dimension_name: [{label, metrics, ...}, ...]}

        Returns:
            {dim_scores: {dim: {cv, best, worst, range_pct, verdict}}, overall: str, summary: str}
        """
        dim_scores = {}
        for dim, sweeps in all_results.items():
            returns = [s.get('total_return', 0) for s in sweeps if s.get('metrics')]
            if len(returns) < 2:
                dim_scores[dim] = {'verdict': '数据不足', 'cv': 0, 'range_pct': 0}
                continue

            mean_r = statistics.mean(returns)
            std_r = statistics.stdev(returns) if len(returns) > 1 else 0
            cv = abs(std_r / mean_r) if abs(mean_r) > 0.01 else abs(std_r / 1.0)

            best_idx = returns.index(max(returns))
            worst_idx = returns.index(min(returns))
            range_pct = abs(max(returns) - min(returns))

            if cv > 0.5 and range_pct > 50:
                verdict = 'overfit'
            elif cv > 0.5:
                verdict = 'sensitive'
            elif cv < 0.3:
                verdict = 'robust'
            else:
                verdict = 'moderate'

            dim_scores[dim] = {
                'cv': round(cv, 3),
                'range_pct': round(range_pct, 1),
                'best': sweeps[best_idx]['label'] if best_idx < len(sweeps) else '?',
                'worst': sweeps[worst_idx]['label'] if worst_idx < len(sweeps) else '?',
                'best_return': round(returns[best_idx], 2),
                'worst_return': round(returns[worst_idx], 2),
                'verdict': verdict,
                'mean_return': round(mean_r, 2),
            }

        # 综合判断
        verdicts = [s['verdict'] for s in dim_scores.values()]
        if all(v == 'robust' for v in verdicts):
            overall = 'robust'
            summary = '策略对参数变化不敏感，多个参数区域均有效，具有良好鲁棒性。'
        elif any(v == 'overfit' for v in verdicts):
            overfit_dims = [SWEEP_DIMENSIONS[d]['label'] for d, s in dim_scores.items() if s['verdict'] == 'overfit']
            overall = 'overfit_risk'
            summary = f'参数小幅变化导致收益大幅变化，可能过拟合。敏感维度: {", ".join(overfit_dims)}。建议简化策略或增大样本量。'
        elif any(v == 'sensitive' for v in verdicts):
            sensitive_dims = [SWEEP_DIMENSIONS[d]['label'] for d, s in dim_scores.items() if s['verdict'] == 'sensitive']
            overall = 'sensitive'
            summary = f'策略对某些参数较敏感。敏感维度: {", ".join(sensitive_dims)}。建议在敏感维度上做更多验证。'
        else:
            overall = 'moderate'
            summary = '策略对参数变化中等敏感，部分维度需关注。'

        return {
            'dim_scores': dim_scores,
            'overall': overall,
            'summary': summary,
        }

    # ================================================================
    # 格式化输出
    # ================================================================

    @staticmethod
    def format_matrix(dimension: str, sweep_results: List[Dict]) -> pd.DataFrame:
        """将扫描结果格式化为矩阵表格"""
        rows = []
        for s in sweep_results:
            if s.get('error'):
                # 显示真实错误原因（截断到60字符）
                err_msg = s['error'][:60] + ('...' if len(s['error']) > 60 else '')
                rows.append({
                    '参数': s['label'],
                    '总收益%': f'ERR: {err_msg}',
                    '年化收益%': '-',
                    '最大回撤%': '-',
                    '夏普': '-',
                    '胜率%': '-',
                    '交易次数': '-',
                })
                continue
            rows.append({
                '参数': s['label'],
                '总收益%': round(s['total_return'], 2),
                '年化收益%': round(s['annual_return'], 2),
                '最大回撤%': round(s['max_drawdown'], 2),
                '夏普': round(s['sharpe_ratio'], 3),
                '胜率%': round(s['win_rate'], 1),
                '交易次数': s['total_trades'],
            })
        return pd.DataFrame(rows)

    @staticmethod
    def generate_report(all_results: Dict, stability: Dict) -> str:
        """生成自然语言鲁棒性报告"""
        lines = []
        lines.append(f'## 策略鲁棒性评估报告')
        lines.append(f'')
        lines.append(f'**综合评级**: {RobustnessLab._verdict_emoji(stability["overall"])} {stability["summary"]}')
        lines.append(f'')

        for dim, sweeps in all_results.items():
            dim_def = SWEEP_DIMENSIONS[dim]
            ds = stability['dim_scores'].get(dim, {})
            lines.append(f'### {dim_def["label"]}')
            lines.append(f'- 稳定性: {RobustnessLab._verdict_emoji(ds.get("verdict",""))} '
                         f'CV={ds.get("cv",0):.3f}, 收益波动范围={ds.get("range_pct",0):.1f}%')
            lines.append(f'- 最优: {ds.get("best","?")} (收益{ds.get("best_return",0):+.1f}%)')
            lines.append(f'- 最劣: {ds.get("worst","?")} (收益{ds.get("worst_return",0):+.1f}%)')
            lines.append(f'')

        lines.append(f'---')
        lines.append(f'*报告由 QuantCode 鲁棒性实验室自动生成*')
        return '\n'.join(lines)

    @staticmethod
    def _verdict_emoji(verdict: str) -> str:
        if verdict == 'robust': return '[ROBUST] 鲁棒'
        elif verdict == 'overfit_risk' or verdict == 'overfit': return '[OVERFIT] 过拟合风险'
        elif verdict == 'sensitive': return '[SENSITIVE] 敏感'
        elif verdict == 'moderate': return '[MODERATE] 中等敏感'
        return verdict


# ================================================================
# 便捷入口：全维度扫描
# ================================================================

def run_full_sweep(base_config: Dict,
                   dimensions: List[str] = None,
                   progress_callback=None,
                   strategy_class=None) -> Dict:
    """
    全维度参数扫描入口。

    Args:
        base_config: 基准配置
        dimensions: 要扫描的维度列表，None=全部
        progress_callback: callable(dim_name, current_dim_idx, total_dims, sweep_progress)

    Returns:
        {all_results, stability}
    """
    if dimensions is None:
        dimensions = list(SWEEP_DIMENSIONS.keys())

    all_results = {}
    for di, dim in enumerate(dimensions):
        if progress_callback:
            progress_callback(dim, di + 1, len(dimensions), None)

        def dim_progress(cur, total, label):
            if progress_callback:
                progress_callback(dim, di + 1, len(dimensions),
                                  {'current': cur, 'total': total, 'label': label})

        sweeps = RobustnessLab.run_sweep(base_config, dim,
                                          progress_callback=dim_progress,
                                          strategy_class=strategy_class)
        all_results[dim] = sweeps

    stability = RobustnessLab.stability_score(all_results)
    return {'all_results': all_results, 'stability': stability}
