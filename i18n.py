#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
QuantCode i18n 国际化语言管理模块
===================================
统一管理所有 UI 文案、报告文本的中英文翻译。
"""

import streamlit as st

# ================================================================
# 当前语言 (模块级变量，由 app.py 在每次渲染时设置)
# ================================================================
current_lang = "zh"


def set_lang(lang: str):
    """设置当前语言"""
    global current_lang
    current_lang = lang if lang in ("zh", "en") else "zh"


def get_lang() -> str:
    """获取当前语言"""
    return current_lang


def t(key: str, **kwargs) -> str:
    """
    获取当前语言的翻译文本。

    用法:
        t("total_return") -> "总收益"  (中文) / "Total Return" (English)
        t("score_value", val=85) -> "得分: 85" / "Score: 85"
    """
    lang = current_lang
    text = _TRANSLATIONS.get(lang, _TRANSLATIONS["zh"]).get(key)
    if text is None:
        # 回退: 返回 key 本身 (开发阶段可发现遗漏)
        text = key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError):
            return text
    return text


# ================================================================
# 完整翻译映射表
# ================================================================

_TRANSLATIONS = {
    "zh": {
        # ── 导航与页面标题 ──
        "app_title": "QuantCode 量化回测平台",
        "nav_backtest": "📈 回测看板",
        "nav_ai_chat": "🤖 翔哥 AI 对话舱",
        "nav_robustness": "🔬 鲁棒性实验室",
        "version_label": "QuantCode {version} | Commit: `{commit}` | Build: {build}",

        # ── 通用指标名 ──
        "total_return": "总收益",
        "total_return_pct": "总收益%",
        "annual_return": "年化收益",
        "annual_return_pct": "年化收益%",
        "max_drawdown": "最大回撤",
        "max_drawdown_pct": "最大回撤%",
        "sharpe_ratio": "夏普比率",
        "sharpe": "夏普",
        "calmar": "Calmar",
        "win_rate": "胜率",
        "win_rate_pct": "胜率%",
        "trade_count": "交易次数",
        "profit_factor": "盈亏比",
        "profit_factor_label": "盈亏因子",
        "avg_win": "平均盈利",
        "avg_loss": "平均亏损",
        "initial_capital": "初始资金",
        "final_equity": "最终权益",
        "total_trades": "总交易",
        "closed_trades": "已平仓",
        "long_trades": "做多",
        "short_trades": "做空",
        "avg_hold_bars": "平均持仓(K线)",
        "max_consecutive_loss": "最大连续亏损",

        # ── 回测表单 ──
        "sidebar_title": "🎛️ 策略参数配置",
        "coin_select": "币种",
        "timeframe_select": "周期",
        "leverage_label": "杠杆",
        "leverage_unit": "x",
        "capital_label": "初始资金",
        "tp_pct_label": "止盈%",
        "sl_pct_label": "止损%",
        "tp_mode_label": "止盈模式",
        "sl_mode_label": "止损模式",
        "bull_alloc_label": "牛市仓位",
        "range_alloc_label": "震荡仓位",
        "bear_alloc_label": "熊市仓位",
        "strategy_mode_label": "策略模式",
        "max_positions_label": "最大持仓数",
        "trailing_pct_label": "移动止损%",
        "lock_streak_label": "锁仓连损数",
        "lock_bars_label": "锁仓K线数",
        "hedge_ratio_label": "对冲比例",
        "max_pyramid_label": "最大加仓次数",
        "pyramid_step_label": "加仓步长",
        "unlock_pct_label": "解锁回调%",
        "spot_tp_label": "现货止盈%",
        "spot_sl_label": "现货止损%",
        "short_sl_label": "空单止损%",
        "atr_sl_toggle": "ATR入场止损",
        "atr_period_label": "ATR周期",
        "atr_mult_label": "ATR止损倍数",
        "regime_filter_toggle": "市场状态过滤",
        "ema_w_label": "EMA权重",
        "adx_w_label": "ADX权重",
        "adx_th_label": "ADX阈值",
        "bull_th_label": "牛市阈值",
        "date_range_label": "日期范围",
        "selected_indicators_label": "已选指标",

        # ── 按钮 ──
        "btn_run_backtest": "确认参数并运行回测",
        "btn_running": "运行中...",
        "btn_start_robustness": "🔬 开始鲁棒性测试",
        "btn_start_combo": "🧬 开始组合优化扫描",
        "btn_switch_robustness": "🔬 鲁棒性实验室",
        "btn_clear": "清除",
        "btn_confirm": "确认",
        "btn_cancel": "取消",
        "btn_reset": "重置",
        "btn_export": "导出报告",

        # ── 状态提示 ──
        "warning_no_backtest": "⚠️ 尚未运行回测。请先在【回测看板】中运行一次回测，再进入本实验室。",
        "hint_goto_backtest": "操作步骤: 切换到【📈 回测看板】→ 配置参数 → 点击【确认参数并运行回测】→ 回到本页",
        "loading_data": "加载数据...",
        "scanning_progress": "正在测试: **{label}** ({current}/{total})",
        "scan_complete": "✅ 扫描完成！",
        "combo_scanning": "🔬 {current}/{total}: {label}",
        "combo_complete": "✅ 组合优化完成！",
        "hint_robustness_tip": "💡 测试期间请勿切换页面",
        "hint_select_dims": "👆 选择测试维度后，点击上方按钮开始鲁棒性分析。",
        "info_cached_result": "📋 显示上次测试结果（缓存在内存中，刷新页面会丢失）",
        "hint_rerun_combo": "💡 点击上方「开始组合优化扫描」可重新运行",
        "warning_date_filter_failed": "日期过滤失败: {error}，使用全部数据",
        "error_data_empty": "数据加载为空，请检查日期范围。",
        "success_backtest_done": "✅ 回测完成！",
        "success_robustness_done": "🎉 鲁棒性测试完成！可通过上方折叠面板查看各维度详细结果。",
        "error_robustness_type": "❌ 鲁棒性测试失败!",

        # ── 指标名称映射 ──
        "ind_ema_dual": "EMA 双均线",
        "ind_fibonacci": "斐波那契回调",
        "ind_volume_break": "成交量突破",
        "ind_macd": "MACD",
        "ind_rsi": "RSI",
        "ind_bollinger": "布林带",
        "ind_atr": "ATR",
        "ind_ma_trend": "均线趋势",
        "ind_support_resistance": "支撑阻力",

        # ── 鲁棒性实验室 ──
        "robustness_title": "🔬 策略鲁棒性分析实验室",
        "robustness_subtitle": "一键参数敏感性测试 — 所有测试调用真实 engine 回测流程，保证 UI参数→engine参数链路一致。",
        "robustness_baseline_params": "📋 基准策略参数",
        "robustness_dim_select": "📐 测试维度选择",
        "robustness_dim_desc": "选择需要扫描的参数维度。每维度独立测试，保持其他参数为基准值。全选共约 21 次回测。",
        "robustness_est_time": "预计运行 **{count}** 次完整回测，约需 **{min}~{max}** 秒",
        "robustness_scan_progress": "🔄 扫描进度",
        "robustness_result_analysis": "📊 结果分析",
        "robustness_stability_cv": "变异系数(CV)",
        "robustness_stability_range": "收益波动范围",
        "robustness_rating": "评级",
        "robustness_full_report": "📝 完整鲁棒性报告",
        "robustness_report_title": "策略鲁棒性评估报告",
        "robustness_dimension_label": "📐 {label}",
        "robustness_stability_label": "稳定性",
        "robustness_best_label": "最优",
        "robustness_worst_label": "最劣",

        # 维度标签
        "dim_leverage": "杠杆倍数",
        "dim_ema": "EMA 双均线",
        "dim_atr_stop": "ATR 止损",
        "dim_fibonacci": "Fibonacci 回看",
        "dim_volume": "成交量倍数",

        # 维度参数格式
        "dim_format_leverage": "{v}x",
        "dim_format_atr_off": "ATR关闭",
        "dim_format_atr_on": "ATR(14)*{v}",
        "dim_format_fib": "{v}根",
        "dim_format_vol": "{v}x",

        # ── 组合优化 ──
        "combo_title": "🧬 参数组合优化",
        "combo_subtitle": "单参数最优组合后收益可能下降，说明参数存在**交互影响**。本模块对 EMA、Fibonacci、成交量、杠杆进行多参数组合网格扫描，通过 IS/OOS 双段验证寻找长期可复制的稳定参数区域。",
        "combo_oos_ratio": "OOS 数据比例",
        "combo_oos_help": "最后N%的数据作为样本外验证集",
        "combo_min_trades": "最少交易次数",
        "combo_min_trades_help": "低于此交易次数的组合将被跳过",
        "combo_total_combos": "组合总数",
        "combo_est_time": "约{min}~{max}秒",
        "combo_result_analysis": "📊 组合优化结果",
        "combo_scanned": "扫描组合",
        "combo_valid": "有效组合",
        "combo_recommended": "⭐推荐实盘",
        "combo_stable_region": "🟢稳定区域",
        "combo_top10_title": "🏆 Top 10 稳定组合",
        "combo_scoring_rules": "📐 评分规则说明",
        "combo_scoring_table": """
| 维度 | 权重 | 说明 |
|------|------|------|
| 收益能力 | 40% | IS收益 + OOS收益 标准化 |
| 风险控制 | 30% | 最大回撤(低) + Sharpe(高) + Calmar(高) |
| 稳定性 | 20% | IS/OOS收益一致性 + 胜率稳定性 |
| 交易活跃度 | 10% | 交易次数适中(避免过拟合) |
""",
        "combo_flag_legend": "🏷️ 标记说明",
        "combo_flag_legend_text": """
- ⭐ **推荐实盘**: 综合评分≥70 + 稳定区域 + OOS为正
- 🟢 **稳定区域**: IS和OOS均为正且差异<30%
- 🟠 **过拟合嫌疑**: IS收益 > OOS收益 × 2
- 🔴 **严重过拟合**: IS为正但OOS为负
""",
        "combo_is_oos_chart": "📈 IS vs OOS 收益对比 (Top10)",
        "combo_full_report": "📝 完整组合优化报告",
        "combo_no_valid": "⚠️ 没有找到有效的参数组合。请检查: 1) 数据是否足够 2) 交易次数阈值是否过高",
        "combo_error": "❌ 组合优化异常",

        "combo_rank": "排名",
        "combo_flag": "标记",
        "combo_params": "参数组合",
        "combo_score": "总分",
        "combo_is_return": "IS收益%",
        "combo_oos_return": "OOS收益%",

        # ── 稳定性评级 ──
        "verdict_robust": "[ROBUST] 鲁棒",
        "verdict_overfit": "[OVERFIT] 过拟合风险",
        "verdict_sensitive": "[SENSITIVE] 敏感",
        "verdict_moderate": "[MODERATE] 中等敏感",
        "verdict_insufficient": "[INSUFFICIENT] 数据不足",
        "verdict_unknown": "[?] 未知",
        "verdict_data_error": "[ERROR] 数据异常",
        "verdict_calc_error": "[ERROR] 计算异常",

        "overall_robust": "策略对参数变化不敏感，多个参数区域均有效，具有良好鲁棒性。",
        "overall_overfit": "参数小幅变化导致收益大幅变化，可能过拟合。敏感维度: {dims}。建议简化策略或增大样本量。",
        "overall_sensitive": "策略对某些参数较敏感。敏感维度: {dims}。建议在敏感维度上做更多验证。",
        "overall_moderate": "策略对参数变化中等敏感，部分维度需关注。",
        "overall_insufficient": "所有维度数据不足，无法生成综合评级。",
        "overall_no_data": "无有效回测数据，无法生成稳定性评估。请先运行回测。",
        "overall_unknown": "综合评级无法确定。",

        # ── 报告生成 ──
        "report_overall_rating": "**综合评级**: {verdict} {summary}",
        "report_stability_detail": "- 稳定性: {verdict} CV={cv:.3f}, 收益波动范围={range:.1f}%",
        "report_best": "- 最优: {best} (收益{ret:+.1f}%)",
        "report_worst": "- 最劣: {worst} (收益{ret:+.1f}%)",
        "report_dim_errors": "- ⚠️ 该维度 {count} 组测试失败: {labels}",
        "report_footer": "报告由 QuantCode 鲁棒性实验室自动生成",
        "report_generation_error": "报告生成异常 — stability 结构错误",
        "report_stability_error": "[ERROR] stability 对象非字典类型: {type}",

        # ── 组合优化报告 ──
        "combo_report_title": "参数组合优化报告",
        "combo_report_total": "总组合数",
        "combo_report_valid": "有效组合",
        "combo_report_skipped": "跳过(交易不足)",
        "combo_report_errors": "错误",
        "combo_report_oos_ratio": "OOS比例",
        "combo_report_top10_header": "Top 10 稳定组合",
        "combo_report_table_header": "| # | 标记 | 参数 | 总分 | IS% | OOS% | Sharpe | 回撤% |",
        "combo_report_separator": "|---|------|------|------|-----|------|--------|-------|",
        "combo_report_recommended": "⭐ 推荐实盘参数 ({count}组)",
        "combo_report_stable": "🟢 稳定区域 ({count}组)",
        "combo_report_overfit": "🔴 过拟合风险 ({count}组)",
        "combo_report_overfit_desc": "以下组合IS表现优异但OOS显著恶化，可能过拟合：",
        "combo_report_generated_by": "报告由 QuantCode 参数组合优化模块自动生成",

        # ── 评分维度 ──
        "score_return": "收益能力",
        "score_risk": "风险控制",
        "score_stability": "稳定性",
        "score_trade_activity": "交易活跃度",

        # ── 标记 ──
        "flag_recommended": "推荐实盘",
        "flag_stable": "稳定区域",
        "flag_overfit_suspect": "过拟合嫌疑",
        "flag_overfit_severe": "严重过拟合",
        "flag_high_return": "高收益",

        # ── 回测看板 ──
        "dashboard_metrics": "📊 核心指标",
        "dashboard_equity_curve": "📈 权益曲线",
        "dashboard_trade_list": "📋 交易明细",
        "dashboard_monthly_heatmap": "📅 月度收益热力图",
        "dashboard_drawdown": "📉 回撤曲线",
        "dashboard_strategy_summary": "策略评价报告",
        "dashboard_market_attr": "市场状态归因",
        "dashboard_trade_freq": "交易频率分析",
        "dashboard_param_audit": "参数一致性审计",
        "dashboard_oos_test": "样本外测试",
        "dashboard_walk_forward": "Walk Forward 分析",

        # ── 市场状态 ──
        "market_bull": "牛市",
        "market_range": "震荡",
        "market_bear": "熊市",
        "market_attribution_title": "市场状态归因分析",

        # ── 风险报告 ──
        "risk_report_title": "[Risk Report] 风险配置报告",
        "risk_position_mode": "仓位模式",
        "risk_active_stop": "活跃止损",
        "risk_ignored_params": "被忽略参数",
        "risk_formula": "风险公式",
        "risk_regime_multipliers": "市场状态乘数",
        "risk_leverage": "杠杆",
        "risk_max_notional": "最大名义仓位",
        "risk_lock": "锁仓",
        "risk_initial_equity": "初始权益",

        # ── 策略模式 ──
        "mode_classic": "经典模式",
        "mode_delta_neutral": "Delta中性对冲",
        "mode_spot_only": "仅现货",

        # ── 仓位模式 ──
        "pos_mode_fixed_capital": "固定保证金 × 仓位",
        "pos_mode_fixed_pct": "固定权益百分比",
        "pos_mode_full_equity": "全仓",

        # ── 语言切换 ──
        "lang_selector": "🌐 Language / 语言",
        "lang_zh": "中文",
        "lang_en": "English",

        # ── 调试 ──
        "debug_info": "🔧 调试信息",
        "debug_stability_keys": "stability.keys() = {keys}",
        "debug_signature": "run_sweep 签名: {sig}",
        "debug_dynamic_type": "DynamicStrategy 类型: {type}",
        "debug_git_commit": "Git commit: {hash}",

        # ── 参数名 ──
        "param_label": "参数",

        # ── 错误/异常 ──
        "err_import_strategy": "无法导入 DynamicStrategy。请从 app.py 调用时传入 strategy_class 参数。",
        "err_oos_insufficient": "OOS数据不足(少于50根K线)",
        "err_trades_insufficient": "交易次数不足 ({actual} < {min})",
        "err_unexpected": "❌ 异常: {type}: {msg}",
        "err_type_error_call": "❌ TypeError 调用 run_sweep 失败!",

        # ── 通用 ──
        "close": "关闭",
        "expand": "展开",
        "collapse": "折叠",
        "yes": "是",
        "no": "否",
        "unknown": "未知",
        "none": "无",
        "is_test": "样本内测试",
        "oos_test": "样本外测试",
        "stop_test": "停止测试",
        "start_backtest": "开始回测",
        "risk_of_overfitting": "过拟合风险",
        "insufficient_data": "数据不足",
    },

    "en": {
        # ── Navigation & Page Titles ──
        "app_title": "QuantCode Backtesting Platform",
        "nav_backtest": "📈 Backtest Dashboard",
        "nav_ai_chat": "🤖 Xiang AI Chat",
        "nav_robustness": "🔬 Robustness Lab",
        "version_label": "QuantCode {version} | Commit: `{commit}` | Build: {build}",

        # ── Common Metric Names ──
        "total_return": "Total Return",
        "total_return_pct": "Total Return%",
        "annual_return": "Annual Return",
        "annual_return_pct": "Annual Return%",
        "max_drawdown": "Max Drawdown",
        "max_drawdown_pct": "Max Drawdown%",
        "sharpe_ratio": "Sharpe Ratio",
        "sharpe": "Sharpe",
        "calmar": "Calmar",
        "win_rate": "Win Rate",
        "win_rate_pct": "Win Rate%",
        "trade_count": "Trade Count",
        "profit_factor": "Profit Factor",
        "profit_factor_label": "Profit Factor",
        "avg_win": "Avg Win",
        "avg_loss": "Avg Loss",
        "initial_capital": "Initial Capital",
        "final_equity": "Final Equity",
        "total_trades": "Total Trades",
        "closed_trades": "Closed Trades",
        "long_trades": "Long",
        "short_trades": "Short",
        "avg_hold_bars": "Avg Hold (bars)",
        "max_consecutive_loss": "Max Consecutive Loss",

        # ── Backtest Form ──
        "sidebar_title": "🎛️ Strategy Parameters",
        "coin_select": "Coin",
        "timeframe_select": "Timeframe",
        "leverage_label": "Leverage",
        "leverage_unit": "x",
        "capital_label": "Initial Capital",
        "tp_pct_label": "Take Profit%",
        "sl_pct_label": "Stop Loss%",
        "tp_mode_label": "TP Mode",
        "sl_mode_label": "SL Mode",
        "bull_alloc_label": "Bull Allocation",
        "range_alloc_label": "Range Allocation",
        "bear_alloc_label": "Bear Allocation",
        "strategy_mode_label": "Strategy Mode",
        "max_positions_label": "Max Positions",
        "trailing_pct_label": "Trailing Stop%",
        "lock_streak_label": "Lock Streak Count",
        "lock_bars_label": "Lock Bars",
        "hedge_ratio_label": "Hedge Ratio",
        "max_pyramid_label": "Max Pyramid",
        "pyramid_step_label": "Pyramid Step",
        "unlock_pct_label": "Unlock Pullback%",
        "spot_tp_label": "Spot TP%",
        "spot_sl_label": "Spot SL%",
        "short_sl_label": "Short SL%",
        "atr_sl_toggle": "ATR Entry Stop",
        "atr_period_label": "ATR Period",
        "atr_mult_label": "ATR Multiplier",
        "regime_filter_toggle": "Regime Filter",
        "ema_w_label": "EMA Weight",
        "adx_w_label": "ADX Weight",
        "adx_th_label": "ADX Threshold",
        "bull_th_label": "Bull Threshold",
        "date_range_label": "Date Range",
        "selected_indicators_label": "Selected Indicators",

        # ── Buttons ──
        "btn_run_backtest": "Confirm & Run Backtest",
        "btn_running": "Running...",
        "btn_start_robustness": "🔬 Start Robustness Test",
        "btn_start_combo": "🧬 Start Combo Optimization",
        "btn_switch_robustness": "🔬 Robustness Lab",
        "btn_clear": "Clear",
        "btn_confirm": "Confirm",
        "btn_cancel": "Cancel",
        "btn_reset": "Reset",
        "btn_export": "Export Report",

        # ── Status Messages ──
        "warning_no_backtest": "⚠️ No backtest found. Please run a backtest first in the Backtest Dashboard.",
        "hint_goto_backtest": "Steps: Switch to [📈 Backtest Dashboard] → Configure parameters → Click [Confirm & Run Backtest] → Return here",
        "loading_data": "Loading data...",
        "scanning_progress": "Testing: **{label}** ({current}/{total})",
        "scan_complete": "✅ Scan complete!",
        "combo_scanning": "🔬 {current}/{total}: {label}",
        "combo_complete": "✅ Combo optimization complete!",
        "hint_robustness_tip": "💡 Do not switch pages during testing",
        "hint_select_dims": "👆 Select test dimensions above, then click the start button.",
        "info_cached_result": "📋 Showing cached results (in-memory, lost on page refresh)",
        "hint_rerun_combo": "💡 Click 'Start Combo Optimization' above to re-run",
        "warning_date_filter_failed": "Date filter failed: {error}, using all data",
        "error_data_empty": "Data is empty. Please check date range.",
        "success_backtest_done": "✅ Backtest complete!",
        "success_robustness_done": "🎉 Robustness test complete! Expand panels above for detailed results.",
        "error_robustness_type": "❌ Robustness test failed!",

        # ── Indicator Names ──
        "ind_ema_dual": "EMA Dual",
        "ind_fibonacci": "Fibonacci Retracement",
        "ind_volume_break": "Volume Breakout",
        "ind_macd": "MACD",
        "ind_rsi": "RSI",
        "ind_bollinger": "Bollinger Bands",
        "ind_atr": "ATR",
        "ind_ma_trend": "MA Trend",
        "ind_support_resistance": "Support/Resistance",

        # ── Robustness Lab ──
        "robustness_title": "🔬 Strategy Robustness Lab",
        "robustness_subtitle": "One-click parameter sensitivity test — all tests use real engine backtesting workflow.",
        "robustness_baseline_params": "📋 Baseline Parameters",
        "robustness_dim_select": "📐 Test Dimension Selection",
        "robustness_dim_desc": "Select dimensions to scan. Each dimension is tested independently. Full selection = ~21 backtests.",
        "robustness_est_time": "Estimated **{count}** backtests, ~**{min}~{max}** seconds",
        "robustness_scan_progress": "🔄 Scan Progress",
        "robustness_result_analysis": "📊 Result Analysis",
        "robustness_stability_cv": "CV (Coefficient of Variation)",
        "robustness_stability_range": "Return Volatility Range",
        "robustness_rating": "Rating",
        "robustness_full_report": "📝 Full Robustness Report",
        "robustness_report_title": "Strategy Robustness Assessment Report",
        "robustness_dimension_label": "📐 {label}",
        "robustness_stability_label": "Stability",
        "robustness_best_label": "Best",
        "robustness_worst_label": "Worst",

        # Dimension labels
        "dim_leverage": "Leverage",
        "dim_ema": "EMA Dual MA",
        "dim_atr_stop": "ATR Stop",
        "dim_fibonacci": "Fibonacci Lookback",
        "dim_volume": "Volume Multiplier",

        # Dimension format
        "dim_format_leverage": "{v}x",
        "dim_format_atr_off": "ATR Off",
        "dim_format_atr_on": "ATR(14)*{v}",
        "dim_format_fib": "{v} bars",
        "dim_format_vol": "{v}x",

        # ── Combo Optimization ──
        "combo_title": "🧬 Parameter Combo Optimization",
        "combo_subtitle": "Single-parameter optimum may degrade when combined, indicating **interaction effects**. This module performs multi-parameter grid scanning (EMA, Fibonacci, Volume, Leverage) with IS/OOS dual-phase validation to find long-term replicable stable parameter regions.",
        "combo_oos_ratio": "OOS Data Ratio",
        "combo_oos_help": "Last N% of data used as out-of-sample validation",
        "combo_min_trades": "Min Trade Count",
        "combo_min_trades_help": "Combinations with fewer trades will be skipped",
        "combo_total_combos": "Total Combos",
        "combo_est_time": "~{min}~{max}s",
        "combo_result_analysis": "📊 Combo Optimization Results",
        "combo_scanned": "Scanned",
        "combo_valid": "Valid Combos",
        "combo_recommended": "⭐Recommended",
        "combo_stable_region": "🟢Stable",
        "combo_top10_title": "🏆 Top 10 Stable Combinations",
        "combo_scoring_rules": "📐 Scoring Rules",
        "combo_scoring_table": """
| Dimension | Weight | Description |
|-----------|--------|-------------|
| Return | 40% | IS + OOS return (normalized) |
| Risk | 30% | Max drawdown (low) + Sharpe (high) + Calmar (high) |
| Stability | 20% | IS/OOS consistency + win rate stability |
| Activity | 10% | Moderate trade count (avoid overfitting) |
""",
        "combo_flag_legend": "🏷️ Flag Legend",
        "combo_flag_legend_text": """
- ⭐ **Recommended**: Score ≥70 + stable + positive OOS
- 🟢 **Stable**: IS & OOS both positive, diff <30%
- 🟠 **Overfit Suspect**: IS return > OOS × 2
- 🔴 **Severe Overfit**: IS positive but OOS negative
""",
        "combo_is_oos_chart": "📈 IS vs OOS Return (Top 10)",
        "combo_full_report": "📝 Full Combo Optimization Report",
        "combo_no_valid": "⚠️ No valid parameter combinations found. Check: 1) Sufficient data 2) Min trade threshold",
        "combo_error": "❌ Combo optimization error",

        "combo_rank": "Rank",
        "combo_flag": "Flag",
        "combo_params": "Parameters",
        "combo_score": "Score",
        "combo_is_return": "IS Return%",
        "combo_oos_return": "OOS Return%",

        # ── Stability Verdicts ──
        "verdict_robust": "[ROBUST] Robust",
        "verdict_overfit": "[OVERFIT] Overfit Risk",
        "verdict_sensitive": "[SENSITIVE] Sensitive",
        "verdict_moderate": "[MODERATE] Moderate",
        "verdict_insufficient": "[INSUFFICIENT] Insufficient Data",
        "verdict_unknown": "[?] Unknown",
        "verdict_data_error": "[ERROR] Data Error",
        "verdict_calc_error": "[ERROR] Calc Error",

        "overall_robust": "Strategy is insensitive to parameter changes. Multiple parameter regions are effective — good robustness.",
        "overall_overfit": "Small parameter changes cause large return swings — possible overfitting. Sensitive dimensions: {dims}. Consider simplifying strategy or increasing sample size.",
        "overall_sensitive": "Strategy is sensitive to certain parameters. Sensitive dimensions: {dims}. More validation recommended.",
        "overall_moderate": "Strategy shows moderate parameter sensitivity; some dimensions need attention.",
        "overall_insufficient": "All dimensions have insufficient data to generate a comprehensive rating.",
        "overall_no_data": "No valid backtest data. Cannot generate stability assessment. Please run a backtest first.",
        "overall_unknown": "Overall rating could not be determined.",

        # ── Report Generation ──
        "report_overall_rating": "**Overall Rating**: {verdict} {summary}",
        "report_stability_detail": "- Stability: {verdict} CV={cv:.3f}, Return range={range:.1f}%",
        "report_best": "- Best: {best} (return {ret:+.1f}%)",
        "report_worst": "- Worst: {worst} (return {ret:+.1f}%)",
        "report_dim_errors": "- ⚠️ {count} test(s) failed in this dimension: {labels}",
        "report_footer": "Report auto-generated by QuantCode Robustness Lab",
        "report_generation_error": "Report generation error — invalid stability structure",
        "report_stability_error": "[ERROR] stability is not a dict: {type}",

        # ── Combo Report ──
        "combo_report_title": "Parameter Combo Optimization Report",
        "combo_report_total": "Total Combos",
        "combo_report_valid": "Valid Combos",
        "combo_report_skipped": "Skipped (low trades)",
        "combo_report_errors": "Errors",
        "combo_report_oos_ratio": "OOS Ratio",
        "combo_report_top10_header": "Top 10 Stable Combinations",
        "combo_report_table_header": "| # | Flag | Params | Score | IS% | OOS% | Sharpe | DD% |",
        "combo_report_separator": "|---|------|--------|-------|-----|------|--------|------|",
        "combo_report_recommended": "⭐ Recommended Live Params ({count} groups)",
        "combo_report_stable": "🟢 Stable Region ({count} groups)",
        "combo_report_overfit": "🔴 Overfit Risk ({count} groups)",
        "combo_report_overfit_desc": "The following combos show strong IS but significant OOS degradation — possible overfitting:",
        "combo_report_generated_by": "Report auto-generated by QuantCode Combo Optimization module",

        # ── Score Dimensions ──
        "score_return": "Return Ability",
        "score_risk": "Risk Control",
        "score_stability": "Stability",
        "score_trade_activity": "Trade Activity",

        # ── Flags ──
        "flag_recommended": "Recommended Live",
        "flag_stable": "Stable Region",
        "flag_overfit_suspect": "Overfit Suspect",
        "flag_overfit_severe": "Severe Overfit",
        "flag_high_return": "High Return",

        # ── Dashboard ──
        "dashboard_metrics": "📊 Key Metrics",
        "dashboard_equity_curve": "📈 Equity Curve",
        "dashboard_trade_list": "📋 Trade List",
        "dashboard_monthly_heatmap": "📅 Monthly Return Heatmap",
        "dashboard_drawdown": "📉 Drawdown Curve",
        "dashboard_strategy_summary": "Strategy Evaluation Report",
        "dashboard_market_attr": "Market Attribution",
        "dashboard_trade_freq": "Trade Frequency Analysis",
        "dashboard_param_audit": "Parameter Audit",
        "dashboard_oos_test": "Out-of-Sample Test",
        "dashboard_walk_forward": "Walk Forward Analysis",

        # ── Market States ──
        "market_bull": "Bull",
        "market_range": "Range",
        "market_bear": "Bear",
        "market_attribution_title": "Market Attribution Analysis",

        # ── Risk Report ──
        "risk_report_title": "[Risk Report] Risk Configuration Report",
        "risk_position_mode": "Position Mode",
        "risk_active_stop": "Active Stop",
        "risk_ignored_params": "Ignored Params",
        "risk_formula": "Risk Formula",
        "risk_regime_multipliers": "Regime Multipliers",
        "risk_leverage": "Leverage",
        "risk_max_notional": "Max Notional",
        "risk_lock": "Lock",
        "risk_initial_equity": "Initial Equity",

        # ── Strategy Modes ──
        "mode_classic": "Classic",
        "mode_delta_neutral": "Delta Neutral Hedge",
        "mode_spot_only": "Spot Only",

        # ── Position Modes ──
        "pos_mode_fixed_capital": "Fixed Margin x Position",
        "pos_mode_fixed_pct": "Fixed Equity %",
        "pos_mode_full_equity": "Full Equity",

        # ── Language Switcher ──
        "lang_selector": "🌐 Language / 语言",
        "lang_zh": "中文",
        "lang_en": "English",

        # ── Debug ──
        "debug_info": "🔧 Debug Info",
        "debug_stability_keys": "stability.keys() = {keys}",
        "debug_signature": "run_sweep signature: {sig}",
        "debug_dynamic_type": "DynamicStrategy type: {type}",
        "debug_git_commit": "Git commit: {hash}",

        # ── Param ──
        "param_label": "Parameter",

        # ── Errors/Exceptions ──
        "err_import_strategy": "Cannot import DynamicStrategy. Please pass strategy_class parameter from app.py.",
        "err_oos_insufficient": "OOS data insufficient (< 50 bars)",
        "err_trades_insufficient": "Trade count insufficient ({actual} < {min})",
        "err_unexpected": "❌ Error: {type}: {msg}",
        "err_type_error_call": "❌ TypeError calling run_sweep!",

        # ── General ──
        "close": "Close",
        "expand": "Expand",
        "collapse": "Collapse",
        "yes": "Yes",
        "no": "No",
        "unknown": "Unknown",
        "none": "None",
        "is_test": "In-Sample Test",
        "oos_test": "Out-of-Sample Test",
        "stop_test": "Stop Test",
        "start_backtest": "Start Backtest",
        "risk_of_overfitting": "Overfitting Risk",
        "insufficient_data": "Insufficient Data",
    }
}


def detect_browser_lang() -> str:
    """检测浏览器语言，默认返回中文"""
    try:
        # Streamlit 不直接暴露浏览器语言，但我们可以从 session_state 读取已保存的语言
        saved = st.session_state.get("lang")
        if saved in ("zh", "en"):
            return saved
    except Exception:
        pass
    return "zh"


def init_lang():
    """初始化语言：从 session_state 读取或使用默认值"""
    if "lang" not in st.session_state:
        st.session_state.lang = "zh"
    set_lang(st.session_state.lang)
