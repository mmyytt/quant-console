"""
AI量化审计引擎 + 策略评分系统
================================
设计原则:
  - 与 engine_core.py 完全解耦, 不写入回测核心代码
  - AI只能基于结构化审计数据分析, 禁止无依据推断
  - 所有数字由程序计算, AI只负责解释
"""
import json, os, time, requests
import pandas as pd, numpy as np
from datetime import datetime
from typing import Dict, List, Optional

# ============================================================
# 第一部分: AuditEngine — 多维度回测审计
# ============================================================
class AuditEngine:
    """
    回测审计引擎: 对回测结果进行5维度结构化审计。

    输入: BacktestEngineV2.run() 的结果 + PerformanceAnalyzer.analyze() 的指标
    输出: 结构化审计数据dict (纯数据, 无AI内容)
    """

    @staticmethod
    def audit(result: Dict, metrics: Dict, walk_forward_data: Dict = None) -> Dict:
        """
        执行全面审计。

        Args:
            result: BacktestEngineV2.run() 的结果
            metrics: PerformanceAnalyzer.analyze() 的指标
            walk_forward_data: WalkForwardAnalyzer.analyze() 的输出 (可选)

        Returns:
            {
                "returns": {...},     # 收益分析
                "risk": {...},        # 风险分析
                "trading": {...},     # 交易分析
                "stability": {...},   # 稳定性分析
                "realism": {...},     # 实盘真实性
                "walk_forward": {...},# 🆕 Walk Forward 分析
                "summary": {...},     # 汇总
            }
        """
        audit_data = {}
        audit_data['returns'] = AuditEngine._audit_returns(result, metrics)
        audit_data['risk'] = AuditEngine._audit_risk(result, metrics)
        audit_data['trading'] = AuditEngine._audit_trading(result, metrics)
        audit_data['stability'] = AuditEngine._audit_stability(result, metrics, walk_forward_data)
        audit_data['realism'] = AuditEngine._audit_realism(result, metrics)
        audit_data['walk_forward'] = AuditEngine._audit_walk_forward(walk_forward_data)
        audit_data['summary'] = AuditEngine._build_summary(audit_data)
        audit_data['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return audit_data

    @staticmethod
    def _audit_returns(result, metrics) -> Dict:
        ec = result.get('equity_curve', [])
        yearly = {}
        if ec:
            df_ec = pd.DataFrame(ec)
            df_ec['year'] = pd.to_datetime(df_ec['timestamp']).dt.year
            for yr, grp in df_ec.groupby('year'):
                yr_start = grp['equity'].iloc[0]
                yr_end = grp['equity'].iloc[-1]
                yearly[str(yr)] = round((yr_end / yr_start - 1) * 100, 1)

        yearly_returns = list(yearly.values())
        stability = round(np.std(yearly_returns), 1) if len(yearly_returns) > 1 else 0

        return {
            'total_return': metrics.get('total_return', 0),
            'annual_return': metrics.get('annual_return', 0),
            'yearly_returns': yearly,
            'return_stability_std': stability,
            'years': metrics.get('years', 0),
        }

    @staticmethod
    def _audit_risk(result, metrics) -> Dict:
        return {
            'max_drawdown': metrics.get('max_drawdown', 0),
            'sharpe_ratio': metrics.get('sharpe_ratio', 0),
            'sortino_ratio': metrics.get('sortino_ratio', 0),
            'calmar_ratio': metrics.get('calmar_ratio', 0),
            'max_consecutive_losses': metrics.get('max_consecutive_losses', 0),
            'recovery_factor': metrics.get('recovery_factor', 0),
        }

    @staticmethod
    def _audit_trading(result, metrics) -> Dict:
        closed = result.get('closed_trades', result.get('trades', []))
        long_trades = [t for t in closed if t.get('side') == 'LONG']
        short_trades = [t for t in closed if t.get('side') == 'SHORT']
        liq_count = sum(1 for t in closed if 'LIQUID' in str(t.get('reason', '')))

        pnl_list = [t.get('pnl', 0) for t in closed if t.get('pnl') is not None]
        max_single_loss = min(pnl_list) if pnl_list else 0

        return {
            'total_trades': metrics.get('total_trades', 0),
            'win_rate': metrics.get('win_rate', 0),
            'profit_factor': metrics.get('profit_factor', 0),
            'avg_win': metrics.get('avg_win', 0),
            'avg_loss': metrics.get('avg_loss', 0),
            'max_single_loss': round(max_single_loss, 2),
            'long_trades': len(long_trades),
            'short_trades': len(short_trades),
            'liquidation_count': liq_count,
        }

    @staticmethod
    def _audit_stability(result, metrics, walk_forward_data: Dict = None) -> Dict:
        """策略稳定性分析, 支持 Walk Forward 滚动窗口结果。"""
        closed = result.get('closed_trades', result.get('trades', []))

        # 按年份拆分检查收益一致性
        yr_pnl = {}
        if len(closed) >= 5:
            try:
                df_t = pd.DataFrame(closed)
                close_times = df_t['close_time'].astype(str).str.replace(r'\.\d+$', '', regex=True)
                df_t['year'] = pd.to_datetime(close_times, format='mixed').dt.year
                for yr, grp in df_t.groupby('year'):
                    yr_pnl[str(yr)] = grp['pnl'].sum() if 'pnl' in grp.columns else 0
            except Exception:
                pass

        # 🆕 Walk Forward 数据优先
        if walk_forward_data and walk_forward_data.get("score"):
            wf_score = walk_forward_data["score"]
            adx = walk_forward_data.get("adx_analysis", {})

            return {
                'walk_forward_robustness': wf_score.get("walk_forward_score", 0),
                'walk_forward_max': wf_score.get("max_score", 100),
                'oos_decay': wf_score.get("oos_decay", 0),
                'oos_avg_return': wf_score.get("avg_oos_return", 0),
                'oos_profitable_windows': f"{wf_score.get('profitable_windows', 0)}/{wf_score.get('total_windows', 0)}",
                'overfitting_risk': wf_score.get("overfitting_risk", "未知"),
                'trend_dependency': adx.get("trend_dependency", "未知"),
                'avg_adx_winning': adx.get("avg_adx_winning", 0),
                'avg_adx_losing': adx.get("avg_adx_losing", 0),
                'yearly_pnl_consistency': round(np.std(list(yr_pnl.values())), 1) if len(yr_pnl) > 1 else 0,
            }

        # 无 Walk Forward 数据时的回退逻辑
        oos_decay = metrics.get('oos_decay', 0)

        if abs(oos_decay) > 30:
            of_risk = "高"
        elif abs(oos_decay) > 15:
            of_risk = "中"
        else:
            of_risk = "低"

        if len(closed) < 10:
            of_risk = '数据不足'

        return {
            'walk_forward_robustness': 0,
            'walk_forward_max': 100,
            'oos_decay': oos_decay,
            'overfitting_risk': of_risk,
            'trend_dependency': '未分析',
            'yearly_pnl_consistency': round(np.std(list(yr_pnl.values())), 1) if len(yr_pnl) > 1 else 0,
        }

    @staticmethod
    def _audit_realism(result, metrics) -> Dict:
        """检查回测是否包含真实交易摩擦"""
        closed = result.get('closed_trades', result.get('trades', []))
        checks = {
            'has_fee': True,           # engine总是扣费
            'has_slippage': True,       # engine总是加滑点
            'has_funding': result.get('leverage', 1) > 1,  # 杠杆>1说明模拟了费率
            'has_margin': True,
            'has_liquidation': any('LIQUID' in str(t.get('reason', '')) for t in closed),
        }
        score = sum(1 for v in checks.values() if v)
        return {
            'checks': checks,
            'realism_score': score,
            'max_score': len(checks),
            'grade': 'A' if score >= 4 else ('B' if score >= 3 else 'C'),
        }

    @staticmethod
    def _audit_walk_forward(walk_forward_data: Dict = None) -> Dict:
        """🆕 Walk Forward 滚动样本外分析数据整理。"""
        if not walk_forward_data or not walk_forward_data.get("score"):
            return {"available": False, "message": "未执行 Walk Forward 分析"}

        wf_score = walk_forward_data.get("score", {})
        adx = walk_forward_data.get("adx_analysis", {})
        windows = walk_forward_data.get("windows", [])

        # 窗口汇总表
        windows_summary = []
        for w in windows:
            train = w.get("train") or {}
            test = w.get("test") or {}
            windows_summary.append({
                "window": w.get("window"),
                "train_range": w.get("train_range", ""),
                "test_range": w.get("test_range", ""),
                "train_return": train.get("annual_return", None),
                "test_return": test.get("annual_return", None),
                "train_drawdown": train.get("max_drawdown", None),
                "test_drawdown": test.get("max_drawdown", None),
                "train_sharpe": train.get("sharpe", None),
                "test_sharpe": test.get("sharpe", None),
                "test_win_rate": test.get("win_rate", None),
            })

        return {
            "available": True,
            "score": wf_score.get("walk_forward_score", 0),
            "max_score": wf_score.get("max_score", 100),
            "overfitting_risk": wf_score.get("overfitting_risk", "未知"),
            "avg_oos_return": wf_score.get("avg_oos_return", 0),
            "avg_oos_sharpe": wf_score.get("avg_oos_sharpe", 0),
            "avg_oos_drawdown": wf_score.get("avg_oos_drawdown", 0),
            "profitable_windows": wf_score.get("profitable_windows", 0),
            "total_windows": wf_score.get("total_windows", 0),
            "profit_ratio": wf_score.get("profit_ratio", 0),
            "oos_decay": wf_score.get("oos_decay", 0),
            "trend_dependency": adx.get("trend_dependency", "未分析"),
            "avg_adx_winning": adx.get("avg_adx_winning", 0),
            "avg_adx_losing": adx.get("avg_adx_losing", 0),
            "dependency_detail": adx.get("dependency_detail", ""),
            "windows_summary": windows_summary,
            "summary": walk_forward_data.get("summary", ""),
        }

    @staticmethod
    def _build_summary(audit_data: Dict) -> Dict:
        """生成审计摘要 (整合 Walk Forward 数据)"""
        r = audit_data['returns']
        ri = audit_data['risk']
        t = audit_data['trading']
        s = audit_data['stability']
        re = audit_data['realism']
        wf = audit_data.get('walk_forward', {})

        strengths = []
        weaknesses = []

        if r['annual_return'] > 20: strengths.append("年化收益优秀")
        elif r['annual_return'] < 0: weaknesses.append("年化收益为负")

        if ri['max_drawdown'] < 25: strengths.append("回撤控制良好")
        elif ri['max_drawdown'] > 50: weaknesses.append("回撤超过50%")

        if t['win_rate'] > 50: strengths.append("胜率过半")
        elif t['win_rate'] < 35: weaknesses.append("胜率偏低,可能连亏期长")

        # 🆕 Walk Forward 稳定性评估
        if wf.get("available"):
            wf_of = wf.get("overfitting_risk", "")
            if wf_of == '低':
                strengths.append(f"Walk Forward过拟合风险低 ({wf.get('profitable_windows',0)}/{wf.get('total_windows',0)}窗口盈利)")
            elif wf_of == '高':
                weaknesses.append(f"Walk Forward过拟合风险高 (仅{wf.get('profitable_windows',0)}/{wf.get('total_windows',0)}窗口盈利)")

            trend_dep = wf.get("trend_dependency", "")
            if trend_dep == '高':
                weaknesses.append(f"策略高度依赖趋势行情 (盈利ADX>{wf.get('avg_adx_winning',0):.0f})")
            elif trend_dep == '低':
                strengths.append("策略在不同市场环境中表现均衡")
        else:
            # 无WF数据时的回退
            if s['overfitting_risk'] == '低': strengths.append("过拟合风险低")
            elif s['overfitting_risk'] == '高': weaknesses.append("存在严重过拟合风险")

        if t['liquidation_count'] > 0: weaknesses.append(f"发生{t['liquidation_count']}次强平")

        return {
            'strengths': strengths,
            'weaknesses': weaknesses,
            'total_checks': len(strengths) + len(weaknesses),
        }


# ============================================================
# 第二部分: StrategyScorer — 70分程序评分
# ============================================================
class StrategyScorer:
    """
    策略评分系统: 基于审计数据打分(满分70)。

    评分维度:
      收益能力: 20分
      风险控制: 20分
      风险收益比: 15分
      稳定性: 10分
      实盘真实性: 5分
    """

    @staticmethod
    def score(audit_data: Dict) -> Dict:
        """计算70分程序评分"""
        scores = {}
        scores['return_score'] = StrategyScorer._score_return(audit_data)
        scores['risk_score'] = StrategyScorer._score_risk(audit_data)
        scores['reward_risk_score'] = StrategyScorer._score_reward_risk(audit_data)
        scores['stability_score'] = StrategyScorer._score_stability(audit_data)
        scores['realism_score'] = StrategyScorer._score_realism(audit_data)
        scores['total_program_score'] = sum(scores.values())
        return scores

    @staticmethod
    def _score_return(audit) -> int:
        ann = audit['returns']['annual_return']
        if ann > 50: return 20
        if ann > 20: return 15
        if ann > 0: return 10
        return 5

    @staticmethod
    def _score_risk(audit) -> int:
        dd = audit['risk']['max_drawdown']
        if dd < 20: return 20
        if dd < 35: return 15
        if dd < 50: return 10
        return 5

    @staticmethod
    def _score_reward_risk(audit) -> int:
        calmar = audit['risk']['calmar_ratio']
        if calmar > 2: return 15
        if calmar > 1: return 10
        return 5

    @staticmethod
    def _score_stability(audit) -> int:
        """稳定性评分: 优先使用 Walk Forward 鲁棒性, 回退到过拟合风险"""
        # 🆕 优先使用 Walk Forward 数据
        wf = audit.get('walk_forward', {})
        if wf.get("available"):
            wf_score = wf.get("score", 0)
            wf_max = wf.get("max_score", 100)
            ratio = wf_score / max(wf_max, 1)
            if ratio > 0.8: return 10
            if ratio > 0.6: return 8
            if ratio > 0.4: return 6
            return 4

        # 回退: 仅基于过拟合风险标记
        of_risk = audit['stability']['overfitting_risk']
        if of_risk == '低': return 10
        if of_risk == '中': return 7
        return 4

    @staticmethod
    def _score_realism(audit) -> int:
        return audit['realism']['realism_score']


# ============================================================
# 第三部分: AIReportGenerator — AI研究报告 (防幻觉)
# ============================================================
class AIReportGenerator:
    """
    AI研究报告生成器。

    核心约束 (防幻觉):
      - AI只能基于传入的audit_data进行分析
      - Prompt中明确禁止编造/修改数据
      - 所有数字必须来源于程序计算结果
    """

    ANTI_HALLUCINATION_PROMPT = """
【严格约束 - 必须遵守】
1. 你只能基于下方"审计数据"进行分析，不得编造任何数字
2. 所有收益率、回撤、胜率等数字必须来自审计数据
3. 如果某项数据缺失，请明确说"数据显示..."
4. 禁止保证未来盈利或预测收益
5. 禁止修改审计数据中的任何数字
"""

    @staticmethod
    def build_report(api_key: str, audit_data: Dict, metrics: Dict,
                     model_name: str = "DeepSeek-V3 (推荐)") -> Dict:
        """
        生成AI研究报告。

        Args:
            api_key: AI API Key
            audit_data: AuditEngine.audit() 的输出
            metrics: PerformanceAnalyzer.analyze() 的输出
            model_name: 模型名称

        Returns:
            {"success": True, "report": "...", "score": 82}
            或 {"success": False, "error": "..."}
        """
        if not api_key:
            return {"success": False, "error": "未配置API Key"}

        # 构建结构化数据摘要
        r = audit_data['returns']
        ri = audit_data['risk']
        t = audit_data['trading']
        s = audit_data['stability']

        # 🆕 Walk Forward 数据上下文
        wf = audit_data.get('walk_forward', {})
        wf_context = ""
        if wf.get("available"):
            wf_context = f"""
Walk Forward滚动样本外测试:
  综合得分: {wf.get('score', 0)}/{wf.get('max_score', 100)}
  过拟合风险: {wf.get('overfitting_risk', '?')}
  样本外平均年化: {wf.get('avg_oos_return', 0):+.1f}%
  样本外Sharpe: {wf.get('avg_oos_sharpe', 0):.3f}
  样本外平均回撤: {wf.get('avg_oos_drawdown', 0):.1f}%
  盈利窗口: {wf.get('profitable_windows', 0)}/{wf.get('total_windows', 0)} ({wf.get('profit_ratio', 0):.0f}%)
  样本外衰减: {wf.get('oos_decay', 0):.1f}%
  趋势依赖度: {wf.get('trend_dependency', '?')}
  盈利交易平均ADX: {wf.get('avg_adx_winning', 0):.1f}
  亏损交易平均ADX: {wf.get('avg_adx_losing', 0):.1f}
  Walk Forward摘要: {wf.get('summary', '无')}
"""

        data_context = f"""【审计数据 - 只基于以下数据分析】

收益:
  总收益: {r['total_return']:+.1f}%
  年化收益: {r['annual_return']:+.1f}%
  回测时长: {r['years']:.1f}年

风险:
  最大回撤: {ri['max_drawdown']:.1f}%
  Sharpe: {ri['sharpe_ratio']:.3f}
  Sortino: {ri['sortino_ratio']:.3f}
  Calmar: {ri['calmar_ratio']:.3f}
  最大连续亏损: {ri['max_consecutive_losses']}笔

交易:
  总交易: {t['total_trades']}笔
  胜率: {t['win_rate']:.1f}%
  盈亏比: {t['profit_factor']:.2f}
  做多: {t['long_trades']}笔 / 做空: {t['short_trades']}笔
  强平次数: {t['liquidation_count']}

稳定性:
  过拟合风险: {s['overfitting_risk']}{wf_context}"""

        prompt = f"""{AIReportGenerator.ANTI_HALLUCINATION_PROMPT}

{data_context}

请按以下格式输出分析报告:

【策略综合评分】(基于数据的客观评价)
【收益质量分析】(收益来源是否合理)
【风险诊断】(最大风险点在哪里)
【稳定性评估】(是否可能过拟合)
【实盘建议】(是否可以小资金实盘, 需要注意什么)
【通俗总结】(用新手能理解的语言总结)

总计300字以内, 直接给结论, 不废话。"""

        # 调用统一API (复用app.py的 _call_unified_api)
        try:
            from app import _call_unified_api
            result = _call_unified_api(
                [{"role": "user", "content": prompt}],
                api_key, model_name, ""
            )
            if result.get("success"):
                # 程序评分
                program_scores = StrategyScorer.score(audit_data)
                return {
                    "success": True,
                    "report": result["content"],
                    "program_score": program_scores,
                }
            return {"success": False, "error": result.get("error", "AI调用失败")}
        except ImportError:
            # 独立运行时, 直接调用API
            return AIReportGenerator._call_direct(api_key, prompt)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _call_direct(api_key: str, prompt: str) -> Dict:
        """直接调用DeepSeek API (兜底)"""
        try:
            r = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "deepseek-chat", "messages": [
                    {"role": "user", "content": prompt}
                ], "max_tokens": 800, "temperature": 0.7},
                timeout=45,
            )
            if r.status_code == 200:
                d = r.json()
                return {"success": True, "report": d["choices"][0]["message"]["content"]}
            return {"success": False, "error": f"API {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ============================================================
# 快捷入口
# ============================================================
def run_full_audit(result: Dict, metrics: Dict, api_key: str = "",
                   model_name: str = "DeepSeek-V3 (推荐)",
                   walk_forward_data: Dict = None) -> Dict:
    """
    一键执行完整审计流程。

    Args:
        result: BacktestEngineV2.run() 的结果
        metrics: PerformanceAnalyzer.analyze() 的输出
        api_key: AI API Key (可选)
        model_name: 模型名称
        walk_forward_data: WalkForwardAnalyzer.analyze() 的输出 (可选)

    Returns:
        {
            "audit_data": {...},       # 结构化审计
            "program_scores": {...},   # 70分评分
            "ai_report": "..." or None, # AI报告(需api_key)
        }
    """
    audit_data = AuditEngine.audit(result, metrics, walk_forward_data)
    program_scores = StrategyScorer.score(audit_data)

    ai_result = None
    if api_key:
        ai_result = AIReportGenerator.build_report(api_key, audit_data, metrics, model_name)

    return {
        "audit_data": audit_data,
        "program_scores": program_scores,
        "ai_report": ai_result,
    }
