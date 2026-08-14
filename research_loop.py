"""
Phase 2: AI 量化研究闭环（外挂模块）
============================================================
把「聊天机器人」升级为「量化研究员」：假设生成 → 回测验证 → 评审判定 → 评分 → 报告 → 沉淀。

纯逻辑模块：不 import app.py（避免触发 Streamlit 副作用），不修改交易核心。
回测只调用 research_phase1 已验证接口（run_single / simple_walk_forward / _monte_carlo），
不重新实现撮合 / PnL / 未来函数检测。
"""
import json

from indicator_schema import INDICATOR_REGISTRY, INDICATOR_SCHEMA
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

# 显示名 → schema key（用于指纹/角色/敏感性分析的参数 key 解析）
_NAME_TO_KEY = {info["name"]: key for key, info in INDICATOR_SCHEMA.items()}

# 指标作用解释（角色）。优先用显式覆盖，其余按分类兜底。
_ROLE_OVERRIDES = {
    "ema": "判断中长期趋势方向",
    "sma": "判断均线排列方向",
    "supertrend": "判断趋势方向与止损位",
    "adx": "判断趋势强度（过滤震荡）",
    "ichimoku": "判断趋势与云层支撑阻力",
    "psar": "判断趋势反转点",
    "rsi": "判断超买超卖/动量强弱",
    "kdj": "判断短期超买超卖",
    "macd": "判断动能与趋势背离",
    "cci": "判断价格偏离程度",
    "stochrsi": "判断超买超卖（更灵敏）",
    "willr": "判断超买超卖",
    "ao": "判断动量方向",
    "bollinger": "识别均值回归与波动区间",
    "keltner": "识别波动通道",
    "donchian": "识别通道突破",
    "fibonacci": "寻找趋势回调区域",
    "obv": "判断量价配合",
    "vwap": "判断均价偏离",
    "mfi": "判断资金流入流出",
    "cmf": "判断资金流量方向",
    "vol_break": "确认真实放量突破",
    "volume_ratio": "确认真实资金进入",
    "hammer": "识别反转形态",
    "engulfing": "识别反转形态",
    "star": "识别反转形态",
    "soldiers": "识别持续形态",
    "doji": "识别犹豫/反转形态",
    "pinbar": "识别反转形态",
}
_CATEGORY_ROLE = {
    "趋势类": "判断趋势方向",
    "摆动类": "判断动量/超买超卖",
    "通道/支撑": "识别支撑阻力与突破",
    "成交量": "确认资金与成交量",
    "K线形态": "识别反转/持续形态",
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
# 〇、假设指纹 + 策略相似度（同一思想不能换名字重复测试）
# ============================================================
def indicator_keys(indicator_names):
    """把显示名列表 → schema key 列表（排序去重）。无法识别则跳过。"""
    keys = []
    for name in indicator_names or []:
        k = _NAME_TO_KEY.get(name)
        if k:
            keys.append(k)
    return sorted(set(keys))


def fingerprint(indicator_names):
    """生成唯一指纹，如 ['EMA 双均线','量比 Volume Ratio'] → 'EMA_VOLUME_RATIO'。"""
    return "_".join(indicator_keys(indicator_names)).upper()


def indicator_roles(indicator_names):
    """返回 {显示名: 作用解释}，供报告/策略库展示指标角色。"""
    roles = {}
    for name in indicator_names or []:
        k = _NAME_TO_KEY.get(name)
        if not k:
            continue
        roles[name] = _ROLE_OVERRIDES.get(k) or _CATEGORY_ROLE.get(
            INDICATOR_REGISTRY[name]["category"], "辅助信号")
    return roles


def param_proximity(params_a, params_b):
    """两个参数字典的数值相似度 0~1。无重叠参数返回 None（无法比较）。"""
    pa = params_a or {}
    pb = params_b or {}
    keys = set(pa) & set(pb)
    if not keys:
        return None
    scores = []
    for k in keys:
        a, b = pa.get(k), pb.get(k)
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            continue
        denom = max(abs(a), abs(b), 1e-9)
        scores.append(max(0.0, 1.0 - abs(a - b) / denom))
    return sum(scores) / len(scores) if scores else None


def strategy_similarity(new_indicators, new_params, old_indicators, old_params=None):
    """综合相似度 0~1：指标集合 Jaccard 80% + 参数邻近 20%。"""
    nk = set(indicator_keys(new_indicators))
    ok = set(indicator_keys(old_indicators))
    if not nk or not ok:
        return 0.0
    jaccard = len(nk & ok) / len(nk | ok)
    prox = param_proximity(new_params, old_params)
    if prox is None:
        return round(jaccard, 3)
    return round(0.8 * jaccard + 0.2 * prox, 3)


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
# 四、研究评分 + 等级（A/B/C/D）+ 过拟合风险
# ============================================================
def research_score(m, param_stability=None):
    """综合评分(0-100)：收益质量 40% + Sharpe 20% + MDD 15% + OOS 15% + 参数稳定性 10%。

    param_stability: 0~100 的参数稳定性评分（敏感性分析得出）。缺省时用 Walk-Forward
    盈利窗口占比作为稳定性代理，保证「未跑敏感性」时评分仍可算。
    """
    sharpe = m.get("sharpe") or 0.0
    mdd = m.get("max_drawdown") if m.get("max_drawdown") is not None else 100.0
    oos = m.get("oos_return") or 0.0
    total_ret = m.get("total_return") or 0.0
    pf = m.get("profit_factor") or 0.0
    wr = m.get("win_rate") or 0.0

    # 收益质量 = 绝对收益 40% + 盈亏比 30% + 胜率 30%
    s_ret = _clip(total_ret / 200.0)
    s_pf = _clip(pf / 2.0)
    s_wr = _clip(wr / 60.0)
    s_rq = 0.40 * s_ret + 0.30 * s_pf + 0.30 * s_wr

    s_sharpe = _clip(sharpe / 2.0)
    s_mdd = _clip(1.0 - mdd / 30.0)
    s_oos = _clip(oos / 20.0)
    if param_stability is not None:
        s_param = _clip(param_stability / 100.0)
    else:
        s_param = _clip((m.get("wf_profit_ratio") or 0.0) / 100.0)

    total = round(100.0 * (0.40 * s_rq + 0.20 * s_sharpe + 0.15 * s_mdd
                           + 0.15 * s_oos + 0.10 * s_param), 1)
    return {
        "total": total,
        "return_quality": round(s_rq * 100, 1),
        "sharpe": round(s_sharpe * 100, 1),
        "mdd": round(s_mdd * 100, 1),
        "oos": round(s_oos * 100, 1),
        "param_stability": round(s_param * 100, 1),
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


def overfitting_risk(stability):
    """参数稳定性(0~100) → 过拟合风险标签。"""
    if stability is None:
        return "Unknown"
    if stability >= 70:
        return "Low"
    if stability >= 40:
        return "Medium"
    return "High"


GRADE_MEANING = {
    "A": "进入模拟盘观察",
    "B": "继续优化",
    "C": "研究价值",
    "D": "淘汰",
}


# ============================================================
# 五、防重复研究（指纹 + 参数邻近 + 失败记忆）
# ============================================================
_SIMILARITY_THRESHOLD = 0.8   # 相似度 ≥ 80% 判定「同一思想」


def check_duplicate(indicator_names, param_overrides=None):
    """查历史假设/实验/失败记忆，识别高度相似记录。返回命中列表（含失败原因/相似度）。"""
    keys = indicator_keys(indicator_names)
    if not keys:
        return []
    hits = []
    # 历史假设
    for h in db.list_hypotheses(200):
        rel = _loads(h.get("related_indicators"))
        sim = strategy_similarity(indicator_names, param_overrides, rel,
                                  _loads(h.get("parameters")))
        if sim >= _SIMILARITY_THRESHOLD:
            hits.append({
                "type": "hypothesis", "id": h["id"], "overlap": sim,
                "text": h.get("hypothesis_text"), "status": h.get("status"),
                "indicators": rel, "failure_reason": None,
            })
    # 历史实验
    for e in db.list_experiments(200):
        ic = _loads(e.get("indicator_combination"))
        sim = strategy_similarity(indicator_names, param_overrides, ic,
                                  _loads(e.get("parameters")))
        if sim >= _SIMILARITY_THRESHOLD:
            hits.append({
                "type": "experiment", "id": e["id"], "overlap": sim,
                "text": e.get("strategy_name") or "未命名", "status": e.get("grade") or "-",
                "indicators": ic, "failure_reason": e.get("failure_reason"),
            })
    # 失败记忆（关键：避免重复验证已失败策略）
    for fm in db.search_failure_memory(fingerprint=fingerprint(indicator_names),
                                       indicator_combination=indicator_names):
        hits.append({
            "type": "failure_memory", "id": fm["id"], "overlap": 1.0,
            "text": fm.get("strategy_name") or "历史失败策略", "status": "failed",
            "indicators": _loads(fm.get("indicator_combination")),
            "failure_reason": fm.get("failure_reason"), "failure_env": fm.get("failure_env"),
        })
    hits.sort(key=lambda x: -x["overlap"])
    return hits


def failure_memory_context(limit=15):
    """失败记忆的文本摘要，注入 AI 提示，避免重复失败方向。"""
    rows = db.list_failure_memory(limit)
    if not rows:
        return "（尚无失败研究记录）"
    lines = []
    for r in rows:
        ic = "、".join(_loads(r.get("indicator_combination")) or []) or "未命名组合"
        reason = r.get("failure_reason") or "-"
        env = r.get("failure_env") or "-"
        lines.append(f"- [{r.get('fingerprint') or ic}] {ic}｜失败原因:{reason}｜失效环境:{env}")
    return "\n".join(lines)


def duplicate_warning(indicator_names, param_overrides=None):
    """生成重复研究提醒文案；无命中返回 None。"""
    hits = check_duplicate(indicator_names, param_overrides)
    if not hits:
        return None
    lines = ["⚠️ 该假设与历史研究高度相似，可能已在过去验证："]
    for h in hits[:5]:
        if h["type"] == "failure_memory":
            tag, status = "失败记忆", "failed"
        elif h["type"] == "hypothesis":
            tag, status = "假设", h["status"]
        else:
            tag, status = "实验", h["status"]
        lines.append(f"- [{tag}#{h['id']} · 状态 {status} · 相似度 {int(h['overlap']*100)}%] "
                     f"{h['text']}")
        if h.get("failure_reason"):
            lines.append(f"    失败原因：{h['failure_reason']}")
        if h.get("failure_env"):
            lines.append(f"    失效环境：{h['failure_env']}")
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
    return f"""你是 QuantCode 的量化研究员（主动研究模式）。用户研究目标是：
"{goal}"

请先分析下方「失败研究记忆」，避免重复已经失败的策略方向；再输出一个全新的、可回测的研究假设。
严格要求：只输出 JSON，不要输出任何其他文字或解释。

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
  "expected_logic": "策略逻辑说明（为什么认为有效）",
  "expected_market_condition": "适用市场环境（趋势/震荡）",
  "failure_environment": "预期失效市场环境（哪些行情下会亏损）",
  "risk_assumption": "风险假设（预期最大回撤/胜率等）"
}}

约束：
- 可用资产: {assets}；可用周期: {timeframes}。
- indicators 必须从下方清单精确选择 1~4 个（用完整中文名，禁止自创指标）。
- params 的 key 必须用清单中给出的参数 key；值必须是数字。
- 最多 3 个核心指标，避免堆叠冗余因子。
- 不得生成与「失败研究记忆」指纹相同的指标组合。

## 失败研究记忆（禁止重复这些方向）
{failure_memory_context()}

## 可用指标清单（名称 | 分类 | 参数）
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
# 六.五、参数敏感性分析（重点：参数稳定区域 + 过拟合风险）
# ============================================================
def _viable(m, base_trades=30):
    """宽松生存判定：附近参数点是否仍具备统计意义（不崩塌即视为有效）。"""
    trades = m.get("trade_count") or 0
    if trades < max(15, int((base_trades or 0) * 0.4)):
        return False
    sharpe = m.get("sharpe") or 0.0
    mdd = m.get("max_drawdown") if m.get("max_drawdown") is not None else 100.0
    return (sharpe >= 0.3) or (mdd < 60.0 and (m.get("total_return") or 0) > 0)


def _neighborhood(pmeta, base_val):
    """在 base 附近生成 ±1/±2 步长邻域点（clamp 到 min/max，整型参数保留整型）。"""
    step = pmeta.get("step", 1) or 1
    lo, hi = pmeta.get("min"), pmeta.get("max")
    base = float(base_val)
    is_int = float(step).is_integer() and float(base_val).is_integer()
    out = []
    for mult in (-2, -1, 1, 2):
        v = base + mult * step
        if lo is not None and v < lo:
            continue
        if hi is not None and v > hi:
            continue
        out.append(int(round(v)) if is_int else round(v, 6))
    return sorted(set(out))


def run_sensitivity_point(df, coin, indicators, params, lev, tp, sl, strategy_factory=None):
    """单点回测（只跑全周期 run_single，不跑 WF/MC，省算力）。返回指标 dict。"""
    base_selected = build_selected(indicators, params)
    kw = make_engine_kwargs(lev, tp, sl)
    _, m = run_single(df, coin, _make_strategy(dict(base_selected), strategy_factory), kw)
    return {
        "total_return": m.get("total_return"), "sharpe": m.get("sharpe_ratio"),
        "max_drawdown": m.get("max_drawdown"), "trade_count": m.get("total_trades"),
        "win_rate": m.get("win_rate"), "profit_factor": m.get("profit_factor"),
    }


def sensitivity_analysis(df, coin, indicators, base_params, lev, tp, sl,
                         strategy_factory=None):
    """OAT 参数敏感性：逐参数扰动附近值，统计生存率 → 稳定区域 + 过拟合风险。

    返回 dict：stable_ranges / param_viability / stability(0~100) / overfitting /
    points_tested / points_viable / base_metrics / grid（供热力图）。
    """
    base = run_sensitivity_point(df, coin, indicators, base_params, lev, tp, sl, strategy_factory)
    base_trades = base.get("trade_count") or 0
    grid = [dict(base, params=base_params, viable=_viable(base, base_trades))]
    param_viability = {}
    stable_ranges = {}

    for name in indicators:
        info = INDICATOR_REGISTRY.get(name)
        if not info:
            continue
        pvals = (base_params or {}).get(name) or {}
        for pk, base_val in pvals.items():
            pmeta = info["params"].get(pk)
            if not pmeta:
                continue
            label = f"{name}.{pk}"
            pv = {base_val: _viable(base, base_trades)}
            for v in _neighborhood(pmeta, base_val):
                perturb = json.loads(json.dumps(base_params))  # 深拷贝
                perturb[name][pk] = v
                try:
                    pm = run_sensitivity_point(df, coin, indicators, perturb,
                                               lev, tp, sl, strategy_factory)
                except Exception:
                    pm = {"trade_count": 0, "sharpe": 0.0, "max_drawdown": 100.0,
                          "total_return": 0.0, "win_rate": 0.0, "profit_factor": 0.0}
                ok = _viable(pm, base_trades)
                pv[v] = ok
                grid.append(dict(pm, params=perturb, viable=ok))
            param_viability[label] = pv
            viable_vals = sorted([k for k, ok in pv.items() if ok])
            if viable_vals:
                stable_ranges[label] = [viable_vals[0], viable_vals[-1]]

    total_points = sum(len(pv) for pv in param_viability.values())
    viable_points = sum(sum(1 for ok in pv.values() if ok) for pv in param_viability.values())
    stability = round(100.0 * viable_points / max(total_points, 1), 1)

    return {
        "base_metrics": base,
        "stable_ranges": stable_ranges,
        "param_viability": param_viability,
        "stability": stability,
        "overfitting": overfitting_risk(stability),
        "points_tested": total_points,
        "points_viable": viable_points,
        "grid": grid,
    }


# ============================================================
# 七、研究报告 + 策略库沉淀
# ============================================================
def build_report(hyp, indicators, params, m, verdict, sensitivity=None):
    score = verdict["score"]
    rec = GRADE_MEANING.get(score["grade"], "-")
    if verdict["passed"]:
        rec = "✅ 通过全部门禁，建议进入模拟盘观察"
    elif score["grade"] == "B":
        rec = "🟡 继续优化（未完全通过，但有研究价值）"
    elif score["grade"] == "C":
        rec = "🔵 研究价值（记录，不投入）"
    else:
        rec = "❌ 淘汰（不建议继续研究）"

    roles = indicator_roles(indicators)
    L = []
    L.append(f"# 策略研究报告：{hyp.get('hypothesis_text', '未命名假设')}")
    L.append("")
    L.append(f"> 资产 {verdict.get('coin', hyp.get('asset'))} · 周期 {hyp.get('timeframe')} · "
             f"杠杆 {verdict.get('leverage', hyp.get('leverage'))}x · "
             f"评级 **{score['grade']}** · 综合分 {score['total']}"
             f" · 过拟合风险 **{sensitivity['overfitting'] if sensitivity else '未评估'}**")
    L.append("")
    L.append("## 研究目标")
    L.append(hyp.get("user_goal") or hyp.get("hypothesis_text") or "-")
    L.append("")
    L.append("## 策略假设")
    L.append(hyp.get("hypothesis_text") or "-")
    L.append("")
    L.append("## 为什么认为有效")
    L.append(hyp.get("expected_logic") or "-")
    L.append("")
    L.append("## 使用因子解释")
    for name in indicators:
        L.append(f"- **{name}**：{roles.get(name, '辅助信号')}")
    if not indicators:
        L.append("-")
    L.append("")
    L.append("## 参数")
    for name in indicators:
        ps = params.get(name) or {}
        row = ", ".join(f"{pk}={v}" for pk, v in ps.items()) if ps else "默认值"
        L.append(f"- {name}: {row}")
    L.append(f"- 杠杆 {verdict.get('leverage', hyp.get('leverage'))}x · "
             f"TP {verdict.get('tp_pct', hyp.get('tp_pct'))}% · SL {verdict.get('sl_pct', hyp.get('sl_pct'))}%")
    L.append("")
    L.append("## 历史表现（样本内）")
    L.append(f"- 总收益 {_f(m.get('total_return'))}% · 年化 {_f(m.get('annual_return'))}%")
    L.append(f"- Sharpe {_f(m.get('sharpe'))} · 最大回撤 {_f(m.get('max_drawdown'))}% · 胜率 {_f(m.get('win_rate'))}%")
    L.append(f"- 交易次数 {m.get('trade_count') or 0} · 盈亏比 {_f(m.get('profit_factor'))}")
    L.append("")
    L.append("## 样本外表现（OOS）")
    L.append(f"- OOS 收益 {_f(m.get('oos_return'))}% · OOS Sharpe {_f(m.get('oos_sharpe'))} · OOS 回撤 {_f(m.get('oos_mdd'))}%")
    L.append("")
    L.append("## Walk Forward")
    L.append(f"- 盈利窗口 {m.get('wf_profitable')}/{m.get('wf_windows')} "
             f"({_f(m.get('wf_profit_ratio'), 1, '%')})")
    L.append("")
    L.append("## Monte Carlo")
    L.append(f"- 5% 分位年化收益 {_f(m.get('mc_p5'), 2, '%')}")
    L.append("")
    L.append("## 参数稳定性")
    if sensitivity:
        L.append(f"- 稳定性评分 **{sensitivity['stability']}** / 100（过拟合风险 {sensitivity['overfitting']}）")
        if sensitivity.get("stable_ranges"):
            for lab, rng in sensitivity["stable_ranges"].items():
                L.append(f"  - {lab} 稳定区间 [{rng[0]}, {rng[1]}]")
        else:
            L.append("  - 无稳定区域（附近参数普遍失效 → 高过拟合风险）")
    else:
        L.append("- 未评估（可点「参数敏感性分析」）")
    L.append("")
    L.append("## 风险")
    L.append(f"- 风险假设：{hyp.get('risk_assumption') or '-'}")
    L.append(f"- 最大回撤 {_f(m.get('max_drawdown'))}% · 最大连亏 {m.get('max_consecutive_losses') or 0} 次")
    if not verdict["passed"]:
        L.append("")
        L.append("**未通过门禁：**")
        for f in verdict["failures"]:
            L.append(f"- {f}")
    L.append("")
    L.append("## 适用环境")
    L.append(hyp.get("expected_market_condition") or "-")
    L.append("")
    L.append("## 不适用环境")
    L.append(hyp.get("failure_environment") or "-")
    L.append("")
    L.append("## 最终建议")
    L.append(rec)
    L.append("")
    return "\n".join(L)


def library_entry(hyp, indicators, params, m, verdict, sensitivity=None):
    """从验证结果生成 strategy_library 入库字段（Phase 3：含指标角色/稳定区间/过拟合/验证次数）。"""
    score = verdict["score"]
    perf = {
        "total_return": m.get("total_return"), "annual_return": m.get("annual_return"),
        "sharpe": m.get("sharpe"), "max_drawdown": m.get("max_drawdown"),
        "win_rate": m.get("win_rate"), "trade_count": m.get("trade_count"),
        "oos_return": m.get("oos_return"), "mc_p5": m.get("mc_p5"),
        "grade": score["grade"], "research_score": score["total"],
    }
    fp = fingerprint(indicators)
    validations = sum(1 for e in db.list_experiments(500)
                      if fingerprint(_loads(e.get("indicator_combination"))) == fp)
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
        "failure_env": hyp.get("failure_environment") or hyp.get("risk_assumption"),
        "research_score": score["total"],
        "grade": score["grade"],
        "status": "passed" if verdict["passed"] else "candidate",
        "indicator_roles": indicator_roles(indicators),
        "param_stable_range": (sensitivity or {}).get("stable_ranges"),
        "overfitting_risk": (sensitivity or {}).get("overfitting"),
        "validation_count": validations,
    }


# ============================================================
# 八、完整闭环编排（回测 → 判定 → 评分 → 报告 → 落库）
# ============================================================
def verify_hypothesis(hyp, df, coin, strategy_factory=None):
    """跑通研究闭环并落库，返回 verdict dict（含 metrics/score/report/experiment_id）。

    失败策略自动写入 research_failure_memory（避免重复验证）；参数敏感性分析由
    run_sensitivity 单独触发（保持验证快速）。
    """
    indicators = _loads(hyp.get("related_indicators")) or []
    params = _loads(hyp.get("parameters")) or {}
    lev = hyp.get("leverage") or DEFAULT_LEVERAGE
    tp = hyp.get("tp_pct") if hyp.get("tp_pct") is not None else DEFAULT_TP
    sl = hyp.get("sl_pct") if hyp.get("sl_pct") is not None else DEFAULT_SL

    m = run_hypothesis_backtest(df, coin, indicators, params, lev, tp, sl, strategy_factory)
    passed, failures = judge_pass(m)
    score = research_score(m)  # 参数稳定性先用 WF 代理，敏感性分析后再修正
    fp = fingerprint(indicators)
    verdict = {
        "passed": passed, "failures": failures, "score": score,
        "metrics": m, "indicators": indicators, "params": params,
        "coin": coin, "leverage": lev, "tp_pct": tp, "sl_pct": sl,
        "fingerprint": fp,
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
        fingerprint=fp,
    )
    report_text = build_report(hyp, indicators, params, m, verdict)
    db.add_report(experiment_id=exp_id, hypothesis_id=hyp.get("id"),
                  grade=score["grade"], report_text=report_text)

    if not passed:
        db.add_failure_memory(
            strategy_name=name, indicator_combination=indicators, parameters=params,
            fingerprint=fp, failure_reason="；".join(failures) if failures else None,
            failure_env=hyp.get("failure_environment") or hyp.get("risk_assumption"),
            metrics={"sharpe": m.get("sharpe"), "oos_return": m.get("oos_return"),
                     "max_drawdown": m.get("max_drawdown"), "trade_count": m.get("trade_count")},
            avoid=1,
        )
    db.update_hypothesis_status(hyp.get("id"), "passed" if passed else "failed")

    verdict["experiment_id"] = exp_id
    verdict["report"] = report_text
    return verdict


def run_sensitivity(hyp, m, df, coin, exp_id, strategy_factory=None):
    """对已完成实验跑参数敏感性分析：更新实验记录（过拟合风险/参数稳定性/重评分），
    并重建含「参数稳定性」章节的报告。返回 sensitivity dict。"""
    indicators = _loads(hyp.get("related_indicators")) or []
    params = _loads(hyp.get("parameters")) or {}
    lev = hyp.get("leverage") or DEFAULT_LEVERAGE
    tp = hyp.get("tp_pct") if hyp.get("tp_pct") is not None else DEFAULT_TP
    sl = hyp.get("sl_pct") if hyp.get("sl_pct") is not None else DEFAULT_SL

    sen = sensitivity_analysis(df, coin, indicators, params, lev, tp, sl, strategy_factory)
    score = research_score(m, param_stability=sen["stability"])

    db.update_experiment(exp_id, overfitting_risk=sen["overfitting"],
                         param_stability=sen["stability"],
                         research_score=score["total"], grade=score["grade"])

    # 重建报告（带参数稳定性章节），并补一条报告记录
    passed, failures = judge_pass(m)
    verdict = {
        "passed": passed, "failures": failures, "score": score,
        "metrics": m, "indicators": indicators, "params": params,
        "coin": coin, "leverage": lev, "tp_pct": tp, "sl_pct": sl,
    }
    report_text = build_report(hyp, indicators, params, m, verdict, sensitivity=sen)
    db.add_report(experiment_id=exp_id, hypothesis_id=hyp.get("id"),
                  grade=score["grade"], report_text=report_text)

    sen["score"] = score
    sen["report"] = report_text
    return sen
