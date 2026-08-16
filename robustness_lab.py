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

from i18n import t, set_lang

from engine_core import (
    BacktestEngineV2, DataEngine, PerformanceAnalyzer,
    MultiFactorRegime, StrategyBase,
)

from indicator_schema import INDICATOR_SCHEMA


# ================================================================
# 参数维度定义
# ================================================================

# Fibonacci 扫描值从 Schema 派生 (统一数据源, 消除硬编码上限)
_FIB_PV = INDICATOR_SCHEMA['fibonacci']['params']['FIB_lookback']
_FIB_SWEEP = list(range(_FIB_PV['min'], _FIB_PV['max'] + 1, _FIB_PV['step']))

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
        'format': lambda v: t('dim_format_atr_off') if not v['use_atr'] else f'ATR(14)*{int(v["atr_mult"])}',
        'category': 'hybrid',  # engine_kwargs + selected_indicators
    },
    'fibonacci': {
        'label': 'Fibonacci 回看',
        'values': _FIB_SWEEP,
        'format': lambda v: t('bars_unit', n=v),
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
    'volume_ma': {
        'label': '成交量均量周期',
        'values': [10, 20, 30, 50, 70, 100],
        'format': lambda v: t('bars_unit', n=v),
        'category': 'indicator',
        'indicator_name': '成交量突破',
    },
}


# 维度 key → i18n key 映射 (用于动态翻译维度名称)
_DIM_I18N_KEY = {
    'leverage': 'dim_leverage',
    'ema': 'dim_ema',
    'atr_stop': 'dim_atr_stop',
    'fibonacci': 'dim_fibonacci',
    'volume': 'dim_volume',
    'volume_ma': 'dim_volume_ma',
}


def _dim_label(dim: str) -> str:
    """获取维度的翻译后标签"""
    return t(_DIM_I18N_KEY.get(dim, dim))


# ================================================================
# 参数组合优化 — 网格定义
# ================================================================

PARAM_COMBO_GRID = {
    'leverage': {
        'label': '杠杆倍数',
        'values': [2, 3, 5],
        'format': lambda v: f'{v}x',
    },
    'ema': {
        'label': 'EMA双均线',
        'values': [(7, 21), (15, 60), (30, 100)],
        'format': lambda v: f'{v[0]}/{v[1]}',
        'indicator_name': 'EMA 双均线',
    },
    'fibonacci': {
        'label': 'Fibonacci',
        'values': [50, 200, 300, 500],
        'format': lambda v: t('bars_unit', n=v),
        'indicator_name': '斐波那契回调',
    },
    'volume': {
        'label': '成交量倍数',
        'values': [1.5, 2.0, 3.0],
        'format': lambda v: f'{v}x',
        'indicator_name': '成交量突破',
    },
    'volume_ma': {
        'label': '成交量均量周期',
        'values': [20, 50, 100],
        'format': lambda v: t('bars_unit', n=v),
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
                progress_callback(i + 1, len(values), label)

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

        elif dimension == 'volume_ma':
            name = dim_def['indicator_name']
            if name not in sel or not isinstance(sel.get(name), dict):
                sel[name] = {'enabled': True, 'params': {}}
            sel[name]['enabled'] = True
            sel[name]['params']['VOL_ma'] = value

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
                    from strategy_models import DynamicStrategy as DynamicStrategyCls
            except Exception as import_err:
                raise RuntimeError(
                    t('err_import_strategy')
                    + ' ' + t('err_orig_fmt', err=import_err)
                )

        # ── 日志: 关键测试参数 ──
        indicator_summary = {}
        for name, icfg in sel.items():
            if isinstance(icfg, dict) and icfg.get('enabled') and 'params' in icfg:
                indicator_summary[name] = icfg['params']
        # 运行日志: 显示本次实际使用的指标参数（如 Fibonacci FIB_lookback / 成交量 VOL_ma）
        print(f"[RobustnessLab] 实际指标参数: {indicator_summary}", flush=True)

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
            {dim_scores: {dim: {cv, best, worst, range_pct, verdict}},
             overall: str, summary: str}
            — overall 字段始终存在，不会缺失。
        """
        # 防御: 空或非字典输入
        if not isinstance(all_results, dict) or not all_results:
            return {
                'dim_scores': {},
                'overall': 'insufficient_data',
                'summary': t('overall_no_data'),
            }

        dim_scores = {}
        for dim, sweeps in all_results.items():
            if not isinstance(sweeps, list):
                dim_scores[dim] = {'verdict': t('verdict_data_error'), 'cv': 0, 'range_pct': 0}
                continue

            returns = [s.get('total_return', 0) for s in sweeps
                      if isinstance(s, dict) and s.get('metrics')]
            if len(returns) < 2:
                dim_scores[dim] = {'verdict': t('verdict_insufficient'), 'cv': 0, 'range_pct': 0}
                continue

            try:
                mean_r = statistics.mean(returns)
                std_r = statistics.stdev(returns) if len(returns) > 1 else 0
                cv = abs(std_r / mean_r) if abs(mean_r) > 0.01 else abs(std_r / 1.0)
            except Exception:
                dim_scores[dim] = {'verdict': t('verdict_calc_error'), 'cv': 0, 'range_pct': 0}
                continue

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
                'best': sweeps[best_idx].get('label', '?') if best_idx < len(sweeps) else '?',
                'worst': sweeps[worst_idx].get('label', '?') if worst_idx < len(sweeps) else '?',
                'best_return': round(returns[best_idx], 2),
                'worst_return': round(returns[worst_idx], 2),
                'verdict': verdict,
                'mean_return': round(mean_r, 2),
            }

        # 综合判断 — overall 字段始终存在
        verdicts = [s.get('verdict', t('unknown')) for s in dim_scores.values()]
        summary_dims = []
        if not verdicts:
            overall = 'insufficient_data'
        elif all(v == 'robust' for v in verdicts):
            overall = 'robust'
        elif any(v == 'overfit' for v in verdicts):
            summary_dims = [d for d, s in dim_scores.items() if s.get('verdict') == 'overfit']
            overall = 'overfit_risk'
        elif any(v == 'sensitive' for v in verdicts):
            summary_dims = [d for d, s in dim_scores.items() if s.get('verdict') == 'sensitive']
            overall = 'sensitive'
        else:
            overall = 'moderate'

        return {
            'dim_scores': dim_scores,
            'overall': overall,
            'summary': RobustnessLab._build_summary(overall, summary_dims),
            'summary_dims': summary_dims,
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
                    t('param_label'): s['label'],
                    f'{t("total_return")}%': f'ERR: {err_msg}',
                    f'{t("annual_return")}%': '-',
                    f'{t("max_drawdown")}%': '-',
                    t('sharpe'): '-',
                    f'{t("win_rate")}%': '-',
                    t('trade_count'): '-',
                })
                continue
            rows.append({
                t('param_label'): s['label'],
                f'{t("total_return")}%': round(s['total_return'], 2),
                f'{t("annual_return")}%': round(s['annual_return'], 2),
                f'{t("max_drawdown")}%': round(s['max_drawdown'], 2),
                t('sharpe'): round(s['sharpe_ratio'], 3),
                f'{t("win_rate")}%': round(s['win_rate'], 1),
                t('trade_count'): s['total_trades'],
            })
        return pd.DataFrame(rows)

    @staticmethod
    def generate_report(all_results: Dict, stability: Dict, lang: str = None) -> str:
        """生成自然语言鲁棒性报告

        全面防御：任一维度失败不影响其他维度报告生成。

        Args:
            all_results: 所有维度的扫描结果
            stability: 稳定性评分
            lang: 可选语言参数 (zh/en)，若不传则使用当前全局语言
        """
        if lang:
            set_lang(lang)

        lines = []
        lines.append(f'## {t("robustness_report_title")}')
        lines.append(f'')

        # ── 安全读取 stability 字段 ──
        if not isinstance(stability, dict):
            lines.append(t('report_stability_error', type=str(type(stability))))
            lines.append(f'')
            lines.append(f'---')
            lines.append(f'*{t("report_generation_error")}*')
            return '\n'.join(lines)

        overall = stability.get('overall', 'unknown')
        # 动态生成 summary（避免缓存文本语言固化）
        summary = RobustnessLab._build_summary(overall, stability.get('summary_dims', []))
        lines.append(t('report_overall_rating',
                       verdict=RobustnessLab._verdict_emoji(overall),
                       summary=summary))
        lines.append(f'')

        # ── 安全遍历各维度 ──
        if not isinstance(all_results, dict):
            lines.append(f'**{t("report_generation_error")}**')
            return '\n'.join(lines)

        for dim, sweeps in all_results.items():
            # 维度标题使用翻译后的标签
            lines.append(f'### {_dim_label(dim)}')

            # 维度评分安全查找
            dim_scores = stability.get('dim_scores', {})
            if not isinstance(dim_scores, dict):
                lines.append(f'- {t("report_generation_error")}')
                lines.append(f'')
                continue

            ds = dim_scores.get(dim, {})
            if not isinstance(ds, dict):
                ds = {}

            verdict = ds.get('verdict', t('unknown'))
            cv = ds.get('cv', 0)
            range_pct = ds.get('range_pct', 0)
            best = ds.get('best', '?')
            best_return = ds.get('best_return', 0)
            worst = ds.get('worst', '?')
            worst_return = ds.get('worst_return', 0)

            lines.append(t('report_stability_detail',
                           verdict=RobustnessLab._verdict_emoji(verdict),
                           cv=cv, range=range_pct))
            lines.append(t('report_best', best=best, ret=best_return))
            lines.append(t('report_worst', worst=worst, ret=worst_return))

            # 错误详情
            errors_in_dim = [s for s in sweeps if isinstance(s, dict) and s.get('error')]
            if errors_in_dim:
                err_labels = [s.get('label', '?') for s in errors_in_dim]
                lines.append(t('report_dim_errors', count=len(errors_in_dim),
                              labels=', '.join(err_labels[:3])))

            lines.append(f'')

        lines.append(f'---')
        lines.append(f'*{t("report_footer")}*')
        return '\n'.join(lines)

    @staticmethod
    def _verdict_emoji(verdict: str) -> str:
        """将 verdict 字段转为可读标记（支持中英文）"""
        if not verdict or not isinstance(verdict, str):
            return t('verdict_unknown')
        v = verdict.lower()
        if v == 'robust':
            return t('verdict_robust')
        elif v in ('overfit_risk', 'overfit'):
            return t('verdict_overfit')
        elif v == 'sensitive':
            return t('verdict_sensitive')
        elif v == 'moderate':
            return t('verdict_moderate')
        elif '不足' in verdict or 'insufficient' in v:
            return t('verdict_insufficient')
        return f'[{verdict}]'

    @staticmethod
    def _build_summary(overall: str, summary_dims: list = None) -> str:
        """根据 overall 评级动态生成 summary 文本（用当前全局语言）

        这样缓存的结构化数据 (overall + summary_dims) 与语言解耦，
        切换语言后重新调用即可得到对应语言的摘要。
        summary_dims 存的是维度 key（如 'leverage'），翻译时动态映射。
        """
        dims = ', '.join(_dim_label(d) for d in summary_dims) if summary_dims else ''
        if overall == 'robust':
            return t('overall_robust')
        elif overall == 'overfit_risk':
            return t('overall_overfit', dims=dims)
        elif overall == 'sensitive':
            return t('overall_sensitive', dims=dims)
        elif overall == 'moderate':
            return t('overall_moderate')
        elif overall == 'insufficient_data':
            return t('overall_insufficient')
        return t('overall_unknown')

    # ================================================================
    # 参数组合优化 (Combo Optimization)
    # ================================================================

    @staticmethod
    def combo_optimize(base_config: Dict,
                       param_grid: Dict = None,
                       oos_ratio: float = 0.3,
                       min_trades: int = 5,
                       progress_callback=None,
                       strategy_class=None) -> Dict:
        """
        多参数组合网格扫描 + OOS验证。

        Args:
            base_config: 基准配置
            param_grid: {dim: {label, values, format, indicator_name?}}
                       默认使用 PARAM_COMBO_GRID
            oos_ratio: OOS 数据占比 (0.0~0.5)
            min_trades: 最少交易次数阈值
            progress_callback: callable(current, total, label)
            strategy_class: DynamicStrategy类引用

        Returns:
            {'combinations': [...], 'top10': [...], 'summary': str,
             'scores_detail': {...}, 'total_combos': int}
        """
        if param_grid is None:
            param_grid = PARAM_COMBO_GRID

        import itertools

        # 生成所有参数组合
        dims = list(param_grid.keys())
        value_lists = [param_grid[d]['values'] for d in dims]
        all_combos = list(itertools.product(*value_lists))
        total_combos = len(all_combos)

        # OOS 数据分割
        df = base_config['df']
        split_idx = int(len(df) * (1.0 - oos_ratio))
        df_is = df.iloc[:split_idx].copy()
        df_oos = df.iloc[split_idx:].copy()

        results = []
        for ci, combo_values in enumerate(all_combos):
            # 构造参数描述标签
            labels_parts = []
            for di, dim in enumerate(dims):
                labels_parts.append(param_grid[dim]['format'](combo_values[di]))
            combo_label = ' | '.join(labels_parts)

            if progress_callback:
                progress_callback(ci + 1, total_combos, combo_label)

            try:
                # 构建本次组合的配置
                cfg = copy.deepcopy(base_config)
                cfg['df'] = df_is  # IS数据
                sel = cfg['selected_indicators']
                ek = cfg['engine_kwargs']

                for di, dim in enumerate(dims):
                    val = combo_values[di]
                    if dim == 'leverage':
                        ek['leverage'] = val
                    elif dim == 'ema':
                        name = param_grid[dim]['indicator_name']
                        fast, slow = val
                        if name not in sel or not isinstance(sel.get(name), dict):
                            sel[name] = {'enabled': True, 'params': {}}
                        sel[name]['enabled'] = True
                        sel[name]['params']['EMA_short'] = fast
                        sel[name]['params']['EMA_long'] = slow
                    elif dim == 'fibonacci':
                        name = param_grid[dim]['indicator_name']
                        if name not in sel or not isinstance(sel.get(name), dict):
                            sel[name] = {'enabled': True, 'params': {}}
                        sel[name]['enabled'] = True
                        sel[name]['params']['FIB_lookback'] = val
                    elif dim == 'volume':
                        name = param_grid[dim]['indicator_name']
                        if name not in sel or not isinstance(sel.get(name), dict):
                            sel[name] = {'enabled': True, 'params': {}}
                        sel[name]['enabled'] = True
                        sel[name]['params']['VOL_mult'] = val
                    elif dim == 'volume_ma':
                        name = param_grid[dim]['indicator_name']
                        if name not in sel or not isinstance(sel.get(name), dict):
                            sel[name] = {'enabled': True, 'params': {}}
                        sel[name]['enabled'] = True
                        sel[name]['params']['VOL_ma'] = val

                # IS 回测
                is_result = RobustnessLab._run_single(cfg, strategy_class=strategy_class)
                is_metrics = is_result['metrics']

                # OOS 回测 (使用相同参数 + OOS数据)
                oos_result = RobustnessLab._run_single_oos(cfg, df_oos, strategy_class=strategy_class)
                oos_metrics = oos_result['metrics'] if oos_result.get('metrics') else None

                if is_metrics and is_metrics.get('total_trades', 0) >= min_trades:
                    combo_entry = {
                        'label': combo_label,
                        'params': {dim: combo_values[di] for di, dim in enumerate(dims)},
                        'is_metrics': is_metrics,
                        'oos_metrics': oos_metrics,
                    }
                    results.append(combo_entry)
                else:
                    # 交易次数不足
                    results.append({
                        'label': combo_label,
                        'params': {dim: combo_values[di] for di, dim in enumerate(dims)},
                        'is_metrics': is_metrics,
                        'oos_metrics': oos_metrics,
                        'skip': True,
                        'skip_reason': t('err_trades_insufficient',
                                        actual=is_metrics.get('total_trades', 0),
                                        min=min_trades),
                    })

            except Exception as e:
                import traceback
                results.append({
                    'label': combo_label,
                    'params': {dim: combo_values[di] for di, dim in enumerate(dims)},
                    'error': str(e),
                    'traceback': traceback.format_exc(),
                })

        # 综合评分
        valid = [r for r in results if not r.get('error') and not r.get('skip')]
        if valid:
            scored = RobustnessLab._composite_score(valid)
            # 标记
            for s in scored:
                s['flags'] = RobustnessLab._flag_combination(s)
            # 按综合评分排序
            scored.sort(key=lambda x: x.get('composite_score', 0), reverse=True)
            top10 = scored[:10]
        else:
            scored = valid
            top10 = []

        # 生成摘要
        summary_lines = [
            f'## {t("combo_report_title")}',
            f'',
            f'- {t("combo_report_total")}: {total_combos}',
            f'- {t("combo_report_valid")}: {len(valid)}',
            f'- {t("combo_report_skipped")}: {len([r for r in results if r.get("skip")])}',
            f'- {t("combo_report_errors")}: {len([r for r in results if r.get("error")])}',
            f'- {t("combo_report_oos_ratio")}: {oos_ratio*100:.0f}%',
        ]

        if top10:
            summary_lines.append(f'')
            summary_lines.append(f'### {t("combo_report_top10_header")}')
            for i, t_entry in enumerate(top10):
                rec = f' {t("flag_recommended")}' if 'recommended' in t_entry.get('flags', []) else ''
                summary_lines.append(
                    f'{i+1}. {t_entry["label"]} | '
                    f'IS={t_entry.get("is_return",0):+.1f}% OOS={t_entry.get("oos_return",0):+.1f}% '
                    f'{t("combo_score")}={t_entry.get("composite_score",0):.1f}{rec}'
                )

        return {
            'combinations': scored,
            'top10': top10,
            'summary': '\n'.join(summary_lines),
            'scores_detail': {
                'total_combos': total_combos,
                'valid_count': len(valid),
                'all_results': results,
            },
            'total_combos': total_combos,
        }

    @staticmethod
    def _run_single_oos(cfg: Dict, df_oos, strategy_class=None) -> Dict:
        """使用OOS数据运行回测（参数与IS相同）"""
        if len(df_oos) < 50:
            return {'metrics': None, 'error': t('err_oos_insufficient')}

        cfg_oos = copy.deepcopy(cfg)
        cfg_oos['df'] = df_oos

        try:
            return RobustnessLab._run_single(cfg_oos, strategy_class=strategy_class)
        except Exception as e:
            return {'metrics': None, 'error': str(e)}

    @staticmethod
    def _composite_score(results: List[Dict]) -> List[Dict]:
        """
        综合评分: 收益40% + 风险30% + 稳定性20% + 交易10%

        评分规则 (P1-6: 全部 IS-only, 禁止用 OOS 结果选参):
        - 收益能力 (0-40分): IS收益 标准化
        - 风险控制 (0-30分): 最大回撤(低) + Sharpe(高) + Calmar(高) (均IS)
        - 稳定性 (0-20分): IS胜率
        - 交易活跃度 (0-10分): 交易次数适中(非过拟合的极多或极少)
        OOS 指标仅作独立展示字段与 overfit 标记, 不参与排序。
        """
        if not results:
            return results

        # 提取所有指标
        is_returns = [r['is_metrics'].get('total_return', 0) for r in results]
        oos_returns = []
        for r in results:
            oos_m = r.get('oos_metrics')
            oos_returns.append(oos_m.get('total_return', 0) if oos_m else 0)
        max_dds = [r['is_metrics'].get('max_drawdown', 100) for r in results]
        sharpes = [r['is_metrics'].get('sharpe_ratio', 0) for r in results]
        win_rates = [r['is_metrics'].get('win_rate', 0) for r in results]
        trade_counts = [r['is_metrics'].get('total_trades', 0) for r in results]
        calmar_vals = []
        for i, r in enumerate(results):
            ann_ret = r['is_metrics'].get('annual_return', is_returns[i])
            dd = max_dds[i]
            calmar_vals.append(abs(ann_ret / dd) if dd > 0.01 else 0)

        # 标准化函数 (min-max, 处理全相等情况)
        def normalize(vals, invert=False):
            vmin, vmax = min(vals), max(vals)
            if vmax - vmin < 0.001:
                return [0.5 for _ in vals]
            normed = [(v - vmin) / (vmax - vmin) for v in vals]
            return [1.0 - n if invert else n for n in normed]

        n_is_ret = normalize(is_returns)
        n_dd = normalize(max_dds, invert=True)      # 回撤越小越好
        n_sharpe = normalize(sharpes)
        n_calmar = normalize(calmar_vals)
        n_trades = normalize(trade_counts)
        n_wr = normalize(win_rates)

        # 综合评分 (P1-6: IS-only, OOS 不参与选参排序)
        for i, r in enumerate(results):
            return_score = n_is_ret[i] * 40
            risk_score = (n_dd[i] * 0.35 + n_sharpe[i] * 0.35 + n_calmar[i] * 0.3) * 30
            stability_score = n_wr[i] * 20
            trade_score = n_trades[i] * 10

            composite = return_score + risk_score + stability_score + trade_score

            r['is_return'] = is_returns[i]
            r['oos_return'] = oos_returns[i]
            r['composite_score'] = round(composite, 1)
            r['score_breakdown'] = {
                t('score_return'): round(return_score, 1),
                t('score_risk'): round(risk_score, 1),
                t('score_stability'): round(stability_score, 1),
                t('score_trade_activity'): round(trade_score, 1),
            }

        return results

    @staticmethod
    def _flag_combination(entry: Dict) -> List[str]:
        """标记组合特征"""
        flags = []
        is_r = entry.get('is_return', 0)
        oos_r = entry.get('oos_return', 0)
        score = entry.get('composite_score', 0)

        # 过拟合检测: IS远好于OOS
        if is_r > 0 and oos_r < 0:
            flags.append('overfit_severe')    # 🔴 IS正OOS负
        elif is_r > 0 and oos_r > 0 and is_r > oos_r * 3:
            flags.append('overfit_risk')      # 🟠 IS超OOS 3倍
        elif is_r > 0 and oos_r > 0 and is_r > oos_r * 2:
            flags.append('overfit_suspect')   # 🟡 IS超OOS 2倍

        # 稳定区域: IS和OOS都为正且接近
        if is_r > 0 and oos_r > 0 and abs(is_r - oos_r) < max(abs(is_r), abs(oos_r)) * 0.3:
            flags.append('stable')

        # 高收益
        if is_r > 15 and oos_r > 5:
            flags.append('high_return')

        # 推荐实盘: 综合评分≥70 且 在稳定区域
        if score >= 70 and 'stable' in flags and oos_r > 0:
            flags.append('recommended')

        return flags

    @staticmethod
    def combo_format_table(combinations: List[Dict], top_n: int = 10) -> pd.DataFrame:
        """格式化组合优化结果为表格"""
        rows = []
        for i, c in enumerate(combinations[:top_n]):
            is_m = c.get('is_metrics', {})
            oos_m = c.get('oos_metrics') or {}
            flags = c.get('flags', [])
            flag_icon = ''
            if 'recommended' in flags:
                flag_icon = '⭐'
            elif 'overfit_severe' in flags:
                flag_icon = '🔴'
            elif 'overfit_risk' in flags:
                flag_icon = '🟠'
            elif 'stable' in flags:
                flag_icon = '🟢'

            rows.append({
                t('combo_rank'): i + 1,
                t('combo_flag'): flag_icon,
                t('combo_params'): c['label'],
                t('combo_score'): c.get('composite_score', 0),
                t('combo_is_return'): round(is_m.get('total_return', 0), 1),
                t('combo_oos_return'): round(oos_m.get('total_return', 0), 1) if oos_m else '-',
                f'{t("annual_return")}%': round(is_m.get('annual_return', 0), 1),
                f'{t("max_drawdown")}%': round(is_m.get('max_drawdown', 0), 1),
                'Sharpe': round(is_m.get('sharpe_ratio', 0), 2),
                t('calmar'): round(
                    abs(is_m.get('annual_return', 0) / is_m.get('max_drawdown', 0.01))
                    if is_m.get('max_drawdown', 0) > 0.01 else 0, 2),
                f'{t("win_rate")}%': round(is_m.get('win_rate', 0), 1),
                t('trade_count'): is_m.get('total_trades', 0),
            })
        return pd.DataFrame(rows)

    @staticmethod
    def combo_generate_report(combo_result: Dict, lang: str = None) -> str:
        """生成组合优化自然语言报告

        Args:
            combo_result: 组合优化结果字典
            lang: 可选语言参数 (zh/en)，若不传则使用当前全局语言
        """
        if lang:
            set_lang(lang)

        sc = combo_result.get('scores_detail', {})
        top10 = combo_result.get('top10', [])

        lines = [
            f'## {t("combo_report_title")}',
            f'',
            f'- {t("combo_report_total")}: {sc.get("total_combos", "?")}',
            f'- {t("combo_report_valid")}: {sc.get("valid_count", "?")}',
            f'',
        ]

        if top10:
            lines.append(f'### {t("combo_report_top10_header")}')
            lines.append('')
            lines.append(t('combo_report_table_header'))
            lines.append(t('combo_report_separator'))
            for i, ct in enumerate(top10):
                is_m = ct.get('is_metrics', {})
                oos_m = ct.get('oos_metrics') or {}
                flags = ct.get('flags', [])
                icon = '⭐' if 'recommended' in flags else ('🔴' if 'overfit_severe' in flags else ('🟢' if 'stable' in flags else ''))
                lines.append(
                    f'| {i+1} | {icon} | {ct["label"][:40]} | {ct.get("composite_score",0):.0f} | '
                    f'{ct.get("is_return",0):+.1f} | {ct.get("oos_return",0):+.1f} | '
                    f'{is_m.get("sharpe_ratio",0):.2f} | {is_m.get("max_drawdown",0):.1f} |'
                )

            lines.append('')
            recs = [ct for ct in top10 if 'recommended' in ct.get('flags', [])]
            if recs:
                lines.append(t('combo_report_recommended', count=len(recs)))
                for r in recs:
                    lines.append(f'- {r["label"]} → IS={r.get("is_return",0):+.1f}% OOS={r.get("oos_return",0):+.1f}%')
            else:
                stable = [ct for ct in top10 if 'stable' in ct.get('flags', [])]
                if stable:
                    lines.append(t('combo_report_stable', count=len(stable)))
                    for s in stable:
                        lines.append(f'- {s["label"]} → IS={s.get("is_return",0):+.1f}% OOS={s.get("oos_return",0):+.1f}%')

            overfit = [ct for ct in top10 if 'overfit_severe' in ct.get('flags', []) or 'overfit_risk' in ct.get('flags', [])]
            if overfit:
                lines.append(f'')
                lines.append(t('combo_report_overfit', count=len(overfit)))
                lines.append(t('combo_report_overfit_desc'))
                for o in overfit:
                    lines.append(f'- {o["label"]} → IS={o.get("is_return",0):+.1f}% OOS={o.get("oos_return",0):+.1f}%')

        lines.append('')
        lines.append('---')
        lines.append(f'*{t("combo_report_generated_by")}*')
        return '\n'.join(lines)


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


def run_combo_optimize(base_config: Dict,
                       param_grid: Dict = None,
                       oos_ratio: float = 0.3,
                       progress_callback=None,
                       strategy_class=None) -> Dict:
    """参数组合优化便捷入口"""
    return RobustnessLab.combo_optimize(
        base_config=base_config,
        param_grid=param_grid,
        oos_ratio=oos_ratio,
        progress_callback=progress_callback,
        strategy_class=strategy_class,
    )
