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
        'values': [50, 150, 300],
        'format': lambda v: f'{v}根',
        'indicator_name': '斐波那契回调',
    },
    'volume': {
        'label': '成交量倍数',
        'values': [1.5, 2.0, 3.0],
        'format': lambda v: f'{v}x',
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
                        'skip_reason': f'交易次数不足 ({is_metrics.get("total_trades", 0)} < {min_trades})',
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
            f'## 参数组合优化报告',
            f'',
            f'- 总组合数: {total_combos}',
            f'- 有效组合: {len(valid)}',
            f'- 跳过(交易不足): {len([r for r in results if r.get("skip")])}',
            f'- 错误: {len([r for r in results if r.get("error")])}',
            f'- OOS比例: {oos_ratio*100:.0f}%',
        ]

        if top10:
            summary_lines.append(f'')
            summary_lines.append(f'### Top 10 稳定组合')
            for i, t in enumerate(top10):
                rec = ' ⭐推荐实盘' if 'recommended' in t.get('flags', []) else ''
                summary_lines.append(
                    f'{i+1}. {t["label"]} | '
                    f'IS={t.get("is_return",0):+.1f}% OOS={t.get("oos_return",0):+.1f}% '
                    f'评分={t.get("composite_score",0):.1f}{rec}'
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
            return {'metrics': None, 'error': 'OOS数据不足(少于50根K线)'}

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

        评分规则:
        - 收益能力 (0-40分): IS收益 + OOS收益 标准化后加权
        - 风险控制 (0-30分): 最大回撤(低) + Sharpe(高) + Calmar(高)
        - 稳定性 (0-20分): IS/OOS收益一致性 + 胜率稳定性
        - 交易活跃度 (0-10分): 交易次数适中(非过拟合的极多或极少)
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
        n_oos_ret = normalize(oos_returns)
        n_dd = normalize(max_dds, invert=True)      # 回撤越小越好
        n_sharpe = normalize(sharpes)
        n_calmar = normalize(calmar_vals)
        n_trades = normalize(trade_counts)
        n_wr = normalize(win_rates)

        # OOS/IS 收益比 (接近1最好)
        oos_is_ratios = []
        for i in range(len(results)):
            is_r = is_returns[i]
            oos_r = oos_returns[i]
            if is_r > 0.5 and oos_r > 0.5:
                ratio = min(is_r, oos_r) / max(is_r, oos_r) if max(is_r, oos_r) > 0.01 else 0
            elif is_r > 0.5 and oos_r < -0.5:
                ratio = 0  # IS正OOS负 → 严重过拟合
            else:
                ratio = 0.3  # 中性
            oos_is_ratios.append(ratio)
        n_oos_is = normalize(oos_is_ratios)

        # 综合评分
        for i, r in enumerate(results):
            return_score = (n_is_ret[i] * 0.5 + n_oos_ret[i] * 0.5) * 40
            risk_score = (n_dd[i] * 0.35 + n_sharpe[i] * 0.35 + n_calmar[i] * 0.3) * 30
            stability_score = (n_oos_is[i] * 0.6 + n_wr[i] * 0.4) * 20
            trade_score = n_trades[i] * 10

            composite = return_score + risk_score + stability_score + trade_score

            r['is_return'] = is_returns[i]
            r['oos_return'] = oos_returns[i]
            r['composite_score'] = round(composite, 1)
            r['score_breakdown'] = {
                '收益能力': round(return_score, 1),
                '风险控制': round(risk_score, 1),
                '稳定性': round(stability_score, 1),
                '交易活跃度': round(trade_score, 1),
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
                '排名': i + 1,
                '标记': flag_icon,
                '参数组合': c['label'],
                '总分': c.get('composite_score', 0),
                'IS收益%': round(is_m.get('total_return', 0), 1),
                'OOS收益%': round(oos_m.get('total_return', 0), 1) if oos_m else '-',
                '年化%': round(is_m.get('annual_return', 0), 1),
                '最大回撤%': round(is_m.get('max_drawdown', 0), 1),
                'Sharpe': round(is_m.get('sharpe_ratio', 0), 2),
                'Calmar': round(
                    abs(is_m.get('annual_return', 0) / is_m.get('max_drawdown', 0.01))
                    if is_m.get('max_drawdown', 0) > 0.01 else 0, 2),
                '胜率%': round(is_m.get('win_rate', 0), 1),
                '交易数': is_m.get('total_trades', 0),
            })
        return pd.DataFrame(rows)

    @staticmethod
    def combo_generate_report(combo_result: Dict) -> str:
        """生成组合优化自然语言报告"""
        sc = combo_result.get('scores_detail', {})
        top10 = combo_result.get('top10', [])

        lines = [
            f'## 参数组合优化报告',
            f'',
            f'- 总组合数: {sc.get("total_combos", "?")}',
            f'- 有效组合: {sc.get("valid_count", "?")}',
            f'',
        ]

        if top10:
            lines.append('### Top 10 稳定组合')
            lines.append('')
            lines.append('| # | 标记 | 参数 | 总分 | IS% | OOS% | Sharpe | 回撤% |')
            lines.append('|---|------|------|------|-----|------|--------|-------|')
            for i, t in enumerate(top10):
                is_m = t.get('is_metrics', {})
                oos_m = t.get('oos_metrics') or {}
                flags = t.get('flags', [])
                icon = '⭐' if 'recommended' in flags else ('🔴' if 'overfit_severe' in flags else ('🟢' if 'stable' in flags else ''))
                lines.append(
                    f'| {i+1} | {icon} | {t["label"][:40]} | {t.get("composite_score",0):.0f} | '
                    f'{t.get("is_return",0):+.1f} | {t.get("oos_return",0):+.1f} | '
                    f'{is_m.get("sharpe_ratio",0):.2f} | {is_m.get("max_drawdown",0):.1f} |'
                )

            lines.append('')
            recs = [t for t in top10 if 'recommended' in t.get('flags', [])]
            if recs:
                lines.append(f'### ⭐ 推荐实盘参数 ({len(recs)}组)')
                for r in recs:
                    lines.append(f'- {r["label"]} → IS={r.get("is_return",0):+.1f}% OOS={r.get("oos_return",0):+.1f}%')
            else:
                stable = [t for t in top10 if 'stable' in t.get('flags', [])]
                if stable:
                    lines.append(f'### 🟢 稳定区域 ({len(stable)}组)')
                    for s in stable:
                        lines.append(f'- {s["label"]} → IS={s.get("is_return",0):+.1f}% OOS={s.get("oos_return",0):+.1f}%')

            overfit = [t for t in top10 if 'overfit_severe' in t.get('flags', []) or 'overfit_risk' in t.get('flags', [])]
            if overfit:
                lines.append(f'')
                lines.append(f'### 🔴 过拟合风险 ({len(overfit)}组)')
                lines.append(f'以下组合IS表现优异但OOS显著恶化，可能过拟合：')
                for o in overfit:
                    lines.append(f'- {o["label"]} → IS={o.get("is_return",0):+.1f}% OOS={o.get("oos_return",0):+.1f}%')

        lines.append('')
        lines.append('---')
        lines.append('*报告由 QuantCode 参数组合优化模块自动生成*')
        return '\n'.join(lines)
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
