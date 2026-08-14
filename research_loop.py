"""
Phase 2: AI 量化研究闭环（外挂模块）
============================================================
把「聊天机器人」升级为「量化研究员」：假设生成 → 回测验证 → 评审判定 → 评分 → 报告 → 沉淀。

纯逻辑模块：不 import app.py（避免触发 Streamlit 副作用），不修改交易核心。
回测只调用 research_phase1 已验证接口（run_single / simple_walk_forward / _monte_carlo），
不重新实现撮合 / PnL / 未来函数检测。
"""
import json

from indicator_schema import INDICATOR_REGISTRY
from research_storage import db
from research_phase1 import (
    make_engine_kwargs, run_single, simple_walk_forward,
    wf_summary, _monte_carlo, RISK_CONFIG,
)

# 默认实验设计（AI 未指定时兜底）
DEFAULT_LEVERAGE = 2
DEFAULT_TP = 8.0
DEFAULT_SL = 4.0
IS_END_YEAR = 2022  # IS ≤ 2022, OOS ≥ 2023（与研究脚本一致）

# 通过门禁（全满足才 passed）
CRITERIA = {
    "sharpe_min": 1.0,
    "mdd_max": 30.0,
    "oos_ret_min": 0.0,
    "mc_p5_min": 0.0,
    "trades_min": 30,
    "wf_win_ratio_min": 50.0,   # Walk-Forward 盈利窗口占比 ≥ 50%
    "wf_windows_min": 2,         # 至少 2 个 OOS 窗口才有统计意义
}


def _loads(v):
    if isinstance(v, (list, dict)):
        return v
    if not v:
        return []
    try:
        return json.loads(v)
    except Exception:
        return []


def _clip(x, lo=0.0, hi=1.0):
    if x is None:
        return 0.0
    x = float(x)
    if x != x:  # NaN
        return 0.0
    return max(lo, min(hi, x))


def _f(x, nd=2, suffix=""):
    if x is None:
        return "-"
    return f"{float(x):.{nd}f}{suffix}"


def _coerce(v, default):
    if v is None:
        return default
    if isinstance(default, bool):
        return bool(v)
    if isinstance(default, (int, float)):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    return v


# ============================================================
# 一、指标 / 参数解析
# ============================================================
def normalize_indicators(raw):
    """把 AI 给出的指标名匹配到平台注册表显示名。返回 (valid, invalid)。"""
    valid, invalid = [], []
    for name in raw or []:
        if not name:
            continue
        n = str(name).strip()
        if n in INDICATOR_REGISTRY:
            valid.append(n)
            continue
        match = None
        for rn in INDICATOR_REGISTRY:
            if n.lower() in rn.lower() or rn.lower() in n.lower():
                match = rn
                break
        if match:
            valid.append(match)
        else:
            invalid.append(n)
    seen, out = set(), []
    for v in valid:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out, invalid


def build_selected(indicator_names, param_overrides=None):
    """构建 DynamicStrategy 的 selected dict。param_overrides: {指标名: {参数key: 值}}。"""
    selected = {}
    for name in indicator_names:
        info = INDICATOR_REGISTRY.get(name)
        if not info:
            continue
        params = {pk: pv["default"] for pk, pv in info["params"].items()}
        for pk, v in (param_overrides or {}).get(name, {}).items():
            if pk in params:
                params[pk] = _coerce(v, params[pk])
        selected[name] = {"enabled": True, "params": params}
    return selected


def _default_strategy_factory(selected):
    from app import DynamicStrategy
    return DynamicStrategy(selected, use_and=True, mf_params={"enabled": False})


def _make_strategy(selected, strategy_factory):
    selected.update(dict(RISK_CONFIG))   # fixed_risk + price_pct（漏掉则默认 margin_pct 会扭曲止损）
    selected["_regime_filter"] = False   # 纯因子组合，隔离因子自身 alpha
    selected["_trade_mode"] = "双向"
    return (strategy_factory or _default_strategy_factory)(selected)


# ============================================================
# 二、回测执行（复用 research_phase1 已验证接口）
# ============================================================
def run_hypothesis_backtest(df, coin, indicator_names, param_overrides=None,
                            leverage=DEFAULT_LEVERAGE, tp_pct=DEFAULT_TP, sl_pct=DEFAULT_SL,
                            strategy_factory=None):
    """全周期 + IS/OOS + Walk-Forward + Monte Carlo。返回扁平指标 dict。"""
    base_selected = build_selected(indicator_names, param_overrides)
    kw = make_engine_kwargs(leverage, tp_pct, sl_pct)

    def _fresh():
        return _make_strategy(dict(base_selected), strategy_factory)

    res, m = run_single(df, coin, _fresh(), kw)
    out = {
        "total_return": m.get("total_return"),
        "annual_return": m.get("annual_return"),
        "sharpe": m.get("sharpe_ratio"),
        "max_drawdown": m.get("max_drawdown"),
        "win_rate": m.get("win_rate"),
        "profit_factor": m.get("profit_factor"),
        "trade_count": m.get("total_trades"),
        "max_consecutive_losses": m.get("max_consecutive_losses"),
        "leak_count": len(res.get("leak_warnings", [])),
    }

    # OOS（样本外单次切分）
    oos_df = df[df.index.year >= IS_END_YEAR + 1]
    if len(oos_df) > 100:
        try:
            _, oos_m = run_single(oos_df, coin, _fresh(), kw)
            out["oos_return"] = oos_m.get("total_return")
            out["oos_sharpe"] = oos_m.get("sharpe_ratio")
            out["oos_mdd"] = oos_m.get("max_drawdown")
            out["oos_trades"] = oos_m.get("total_trades")
        except Exception:
            pass
    else:
        out["oos_return"] = out["oos_sharpe"] = out["oos_mdd"] = out["oos_trades"] = None

    # Monte Carlo 5% 分位
    out["mc_p5"] = _monte_carlo(res.get("equity_array"))

    # Walk-Forward 滚动（固定参数，无参数偷窥）
    try:
        wf = wf_summary(simple_walk_forward(df, coin, _fresh(), kw))
    except Exception:
        wf = {"avg_oos_return": None, "profitable_windows": 0, "total_windows": 0,
              "profit_ratio": None}
    out["wf_avg_oos"] = wf.get("avg_oos_return")
    out["wf_profit_ratio"] = wf.get("profit_ratio")
    out["wf_windows"] = wf.get("total_windows", 0)
    out["wf_profitable"] = wf.get("profitable_windows", 0)
    return out


# ============================================================
# 三、评审判定（通过 / 失败 + 失败原因）
# ============================================================
def judge_pass(m):
    """返回 (passed, failures)。failures 为中文失败原因列表。"""
    failures = []
    sharpe = m.get("sharpe") or 0.0
    mdd = m.get("max_drawdown") if m.get("max_drawdown") is not None else 100.0
    oos = m.get("oos_return")
    mc = m.get("mc_p5")
    trades = m.get("trade_count") or 0
    wf_ratio = m.get("wf_profit_ratio")
    wf_windows = m.get("wf_windows") or 0

    if sharpe <= CRITERIA["sharpe_min"]:
        failures.append(f"Sharpe {sharpe:.2f} 未达 {CRITERIA['sharpe_min']}，风险调整后收益不足")
    if mdd >= CRITERIA["mdd_max"]:
        failures.append(f"最大回撤 {mdd:.2f}% 超过 {CRITERIA['mdd_max']:.0f}%，回撤过大")
    if oos is None or oos <= CRITERIA["oos_ret_min"]:
        failures.append(f"样本外(OOS)收益 {'无数据' if oos is None else f'{oos:.2f}%'} 未 > 0，样本内收益不可信")
    if wf_windows < CRITERIA["wf_windows_min"]:
        failures.append(f"Walk-Forward 窗口 {wf_windows} 个不足 {CRITERIA['wf_windows_min']}，稳定性无法评估")
    elif wf_ratio is None or wf_ratio < CRITERIA["wf_win_ratio_min"]:
        failures.append(f"Walk-Forward 盈利窗口占比 {_f(wf_ratio, 1, '%')} 未达 {CRITERIA['wf_win_ratio_min']:.0f}%，滚动表现不稳定")
    if mc is None or mc <= CRITERIA["mc_p5_min"]:
        failures.append(f"Monte Carlo 5% 分位 {_f(mc, 2, '%')} 未 > 0，尾部风险为正")
    if trades <= CRITERIA["trades_min"]:
        failures.append(f"交易次数 {trades} 不足 {CRITERIA['trades_min']}，统计意义不足")
    return (len(failures) == 0, failures)


# ============================================================
# 四、研究评分 + 等级（A/B/C/D）
# ============================================================
def research_score(m):
    """综合评分(0-100)：Sharpe 30% + OOS 25% + MDD 20% + 稳定性 15% + 交易数 10%。"""
    sharpe = m.get("sharpe") or 0.0
    mdd = m.get("max_drawdown") if m.get("max_drawdown") is not None else 100.0
    oos = m.get("oos_return") or 0.0
    mc = m.get("mc_p5") if m.get("mc_p5") is not None else 0.0
    wf_ratio = (m.get("wf_profit_ratio") or 0.0) / 100.0
    trades = m.get("trade_count") or 0

    s_sharpe = _clip(sharpe / 2.0)
    s_oos = _clip(oos / 20.0)
    s_mdd = _clip(1.0 - mdd / 30.0)
    s_stab = 0.5 * _clip(wf_ratio) + 0.5 * _clip(mc / 10.0)
    s_trades = _clip(trades / 100.0)

    total = round(100.0 * (0.30 * s_sharpe + 0.25 * s_oos + 0.20 * s_mdd
                           + 0.15 * s_stab + 0.10 * s_trades), 1)
    return {
        "total": total,
        "sharpe": round(s_sharpe * 100, 1),
        "oos": round(s_oos * 100, 1),
        "mdd": round(s_mdd * 100, 1),
        "stability": round(s_stab * 100, 1),
        "trades": round(s_trades * 100, 1),
        "grade": grade_from(total),
    }


def grade_from(total):
    if total >= 70:
        return "A"
    if total >= 50:
        return "B"
    if total >= 30:
        return "C"
    return "D"


GRADE_MEANING = {
    "A": "进入策略库候选",
    "B": "继续优化",
    "C": "淘汰",
    "D": "禁止重复研究",
}


# ============================================================
# 五、防重复研究
# ============================================================
def check_duplicate(indicator_names, param_overrides=None):
    """查历史假设/实验，识别指标组合高度重合的记录。返回命中列表（含失败原因）。"""
    names = set(indicator_names)
    if not names:
        return []
    hits = []
    for h in db.list_hypotheses(200):
        rel = set(_loads(h.get("related_indicators")))
        if not rel:
            continue
        overlap = len(names & rel) / len(names)
        if overlap >= 0.8:
            hits.append({
                "type": "hypothesis", "id": h["id"], "overlap": round(overlap, 2),
                "text": h.get("hypothesis_text"), "status": h.get("status"),
                "indicators": sorted(rel), "failure_reason": None,
            })
    for e in db.list_experiments(200):
        ic = set(_loads(e.get("indicator_combination")))
        if not ic:
            continue
        overlap = len(names & ic) / len(names)
        if overlap >= 0.8:
            hits.append({
                "type": "experiment", "id": e["id"], "overlap": round(overlap, 2),
                "text": e.get("strategy_name") or "未命名", "status": e.get("grade") or "-",
                "indicators": sorted(ic), "failure_reason": e.get("failure_reason"),
            })
    hits.sort(key=lambda x: -x["overlap"])
    return hits


def duplicate_warning(indicator_names, param_overrides=None):
    """生成重复研究提醒文案；无命中返回 None。"""
    hits = check_duplicate(indicator_names, param_overrides)
    if not hits:
        return None
    lines = ["⚠️ 该假设与历史研究高度重合，可能已在过去验证："]
    for h in hits[:5]:
        tag = "假设" if h["type"] == "hypothesis" else "实验"
        status = h["status"]
        lines.append(f"- [{tag}#{h['id']} · 状态 {status} · 重合 {int(h['overlap']*100)}%] "
                     f"{h['text']}")
        if h.get("failure_reason"):
            lines.append(f"    失败原因：{h['failure_reason']}")
    return "\n".join(lines)


# ============================================================
# 六、AI 假设生成提示 + 解析
# ============================================================
def indicator_catalog() -> str:
    lines = []
    for name, info in INDICATOR_REGISTRY.items():
        ps = info.get("params", {})
        if ps:
            plist = ", ".join(f"{pk}({pv['label']}默认{pv['default']})" for pk, pv in ps.items())
        else:
            plist = "无参数"
        lines.append(f"- {name} [{info['category']}]: {info['desc']}; 参数: {plist}")
    return "\n".join(lines)


def hypothesis_prompt(goal, assets="ETH, BTC, SOL", timeframes="5m, 15m, 1h, 4h, 1d"):
    return f"""你是 QuantCode 的量化研究员。用户研究目标是：
"{goal}"

请基于平台真实能力，输出一个可回测的研究假设。严格要求：只输出 JSON，不要输出任何其他文字或解释。

JSON 结构（字段名固定）：
{{
  "goal": "研究目标（简短）",
  "hypothesis": "假设陈述：该指标组合为何可能有效",
  "indicators": ["指标名1", "指标名2", "指标名3"],
  "params": {{"指标名1": {{"参数key": 数值}}, "指标名2": {{}}}},
  "asset": "ETH",
  "timeframe": "4h",
  "leverage": 2,
  "tp_pct": 8.0,
  "sl_pct": 4.0,
  "expected_logic": "策略逻辑说明",
  "expected_market_condition": "适用市场环境（趋势/震荡）",
  "risk_assumption": "风险假设（预期最大回撤/胜率等）"
}}

约束：
- 可用资产: {assets}；可用周期: {timeframes}。
- indicators 必须从下方清单精确选择 1~4 个（用完整中文名，禁止自创指标）。
- params 的 key 必须用清单中给出的参数 key；值必须是数字。
- 最多 3 个核心指标，避免堆叠冗余因子。

可用指标清单（名称 | 分类 | 参数）：
{indicator_catalog()}
"""


def parse_hypothesis_json(text):
    """从 AI 输出中提取 JSON dict；失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    try:
        return json.loads(t[s:e + 1])
    except Exception:
        return None


# ============================================================
# 七、研究报告 + 策略库沉淀
# ============================================================
def build_report(hyp, indicators, params, m, verdict):
    score = verdict["score"]
    rec = GRADE_MEANING.get(score["grade"], "-")
    if verdict["passed"]:
        rec = "✅ 推荐进入策略库候选（通过全部门禁）"
    elif score["grade"] == "B":
        rec = "🟡 继续优化（未完全通过，但有研究价值）"
    else:
        rec = f"❌ 不推荐继续研究（等级 {score['grade']}：{GRADE_MEANING.get(score['grade'], '-')}）"

    L = []
    L.append(f"# 研究报告：{hyp.get('hypothesis_text', '未命名假设')}")
    L.append("")
    L.append(f"> 资产 {verdict.get('coin', hyp.get('asset'))} · 周期 {hyp.get('timeframe')} · "
             f"杠杆 {verdict.get('leverage', hyp.get('leverage'))}x · "
             f"评级 **{score['grade']}** · 综合分 {score['total']}")
    L.append("")
    L.append("## 1. 研究假设")
    L.append(hyp.get("hypothesis_text") or "-")
    L.append("")
    L.append("## 2. 使用因子")
    L.append("、".join(indicators) if indicators else "-")
    L.append("")
    L.append("## 3. 策略逻辑")
    L.append(hyp.get("expected_logic") or "-")
    L.append("")
    L.append("## 4. 参数")
    for name in indicators:
        info = INDICATOR_REGISTRY.get(name, {})
        ps = params.get(name) or {}
        row = ", ".join(f"{pk}={v}" for pk, v in ps.items()) if ps else "默认值"
        L.append(f"- {name}: {row}")
    L.append(f"- 杠杆 {verdict.get('leverage', hyp.get('leverage'))}x · "
             f"TP {verdict.get('tp_pct', hyp.get('tp_pct'))}% · SL {verdict.get('sl_pct', hyp.get('sl_pct'))}%")
    L.append("")
    L.append("## 5. 回测结果")
    L.append(f"- 总收益 {_f(m.get('total_return'))}% · 年化 {_f(m.get('annual_return'))}%")
    L.append(f"- Sharpe {_f(m.get('sharpe'))} · 最大回撤 {_f(m.get('max_drawdown'))}% · 胜率 {_f(m.get('win_rate'))}%")
    L.append(f"- 交易次数 {m.get('trade_count') or 0} · 盈亏比 {_f(m.get('profit_factor'))}")
    L.append(f"- OOS 收益 {_f(m.get('oos_return'))}% · Monte Carlo 5% 分位 {_f(m.get('mc_p5'), 2, '%')}")
    L.append(f"- Walk-Forward 盈利窗口 {m.get('wf_profitable')}/{m.get('wf_windows')} "
             f"({_f(m.get('wf_profit_ratio'), 1, '%')})")
    L.append("")
    L.append("## 6. 有效市场环境")
    L.append(hyp.get("expected_market_condition") or "-")
    L.append("")
    L.append("## 7. 失败风险")
    L.append(hyp.get("risk_assumption") or "-")
    if not verdict["passed"]:
        L.append("")
        L.append("**未通过门禁：**")
        for f in verdict["failures"]:
            L.append(f"- {f}")
    L.append("")
    L.append("## 8. 是否推荐继续研究")
    L.append(rec)
    L.append("")
    return "\n".join(L)


def library_entry(hyp, indicators, params, m, verdict):
    """从验证结果生成 strategy_library 入库字段。"""
    score = verdict["score"]
    perf = {
        "total_return": m.get("total_return"), "annual_return": m.get("annual_return"),
        "sharpe": m.get("sharpe"), "max_drawdown": m.get("max_drawdown"),
        "win_rate": m.get("win_rate"), "trade_count": m.get("trade_count"),
        "oos_return": m.get("oos_return"), "mc_p5": m.get("mc_p5"),
        "grade": score["grade"], "research_score": score["total"],
    }
    return {
        "name": f"{hyp.get('asset') or verdict.get('coin')}{hyp.get('timeframe') or ''} "
                f"{' + '.join(indicators[:3])}",
        "logic_description": hyp.get("expected_logic") or hyp.get("hypothesis_text"),
        "indicator_logic": indicators,
        "parameters": params,
        "risk_control": {"leverage": verdict.get("leverage"), "tp_pct": verdict.get("tp_pct"),
                         "sl_pct": verdict.get("sl_pct"), "pos_mode": "fixed_risk"},
        "performance_summary": perf,
        "applicable_market": hyp.get("expected_market_condition"),
        "applicable_timeframe": hyp.get("timeframe"),
        "core_indicators": indicators,
        "failure_env": hyp.get("risk_assumption"),
        "research_score": score["total"],
        "grade": score["grade"],
        "status": "passed" if verdict["passed"] else "candidate",
    }


# ============================================================
# 八、完整闭环编排（回测 → 判定 → 评分 → 报告 → 落库）
# ============================================================
def verify_hypothesis(hyp, df, coin, strategy_factory=None):
    """跑通研究闭环并落库，返回 verdict dict（含 metrics/score/report/experiment_id）。"""
    indicators = _loads(hyp.get("related_indicators")) or []
    params = _loads(hyp.get("parameters")) or {}
    lev = hyp.get("leverage") or DEFAULT_LEVERAGE
    tp = hyp.get("tp_pct") if hyp.get("tp_pct") is not None else DEFAULT_TP
    sl = hyp.get("sl_pct") if hyp.get("sl_pct") is not None else DEFAULT_SL

    m = run_hypothesis_backtest(df, coin, indicators, params, lev, tp, sl, strategy_factory)
    passed, failures = judge_pass(m)
    score = research_score(m)
    verdict = {
        "passed": passed, "failures": failures, "score": score,
        "metrics": m, "indicators": indicators, "params": params,
        "coin": coin, "leverage": lev, "tp_pct": tp, "sl_pct": sl,
    }

    name = " + ".join(indicators[:3]) if indicators else "未命名策略"
    exp_id = db.add_experiment(
        strategy_name=name, indicator_combination=indicators, parameters=params,
        asset=coin, timeframe=hyp.get("timeframe"), leverage=lev,
        total_return=m.get("total_return"), annual_return=m.get("annual_return"),
        sharpe=m.get("sharpe"), max_drawdown=m.get("max_drawdown"),
        win_rate=m.get("win_rate"), trade_count=m.get("trade_count"),
        walk_forward_score=m.get("wf_profit_ratio"), monte_carlo_score=m.get("mc_p5"),
        final_rating=score["grade"],
        hypothesis_id=hyp.get("id"), oos_return=m.get("oos_return"),
        research_score=score["total"], grade=score["grade"],
        failure_reason="；".join(failures) if failures else None,
    )
    report_text = build_report(hyp, indicators, params, m, verdict)
    db.add_report(experiment_id=exp_id, hypothesis_id=hyp.get("id"),
                  grade=score["grade"], report_text=report_text)
    db.update_hypothesis_status(hyp.get("id"), "passed" if passed else "failed")

    verdict["experiment_id"] = exp_id
    verdict["report"] = report_text
    return verdict
