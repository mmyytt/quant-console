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
# 〇.五、因子探索引擎（逻辑约束组合，防随机堆砌/过拟合）
# ============================================================
# 核心研究因子池（4 类 17 个，从 INDICATOR_SCHEMA 精选，排除 K线形态等噪音指标）
RESEARCH_POOL = {
    "trend":      ["EMA 双均线", "SMA 三均线", "ADX/DMI 趋势强度", "SuperTrend 超级趋势", "Ichimoku 一目均衡"],
    "momentum":   ["RSI 相对强弱", "MACD 异同均线", "KDJ 随机指标", "CCI 商品通道"],
    "volatility": ["布林带 Bollinger", "Keltner 通道", "斐波那契回调", "Donchian 通道"],
    "volume":     ["量比 Volume Ratio", "OBV 能量潮", "CMF 柴金流量", "MFI 资金流量"],
}
_PRIMARY_CLASSES = ("trend", "momentum", "volatility")   # 可做主信号的类
_REGIME_BY_CLASS = {"trend": "趋势行情", "momentum": "趋势/震荡", "volatility": "震荡/均值回归", "volume": "通用"}


def _class_of(name):
    for c, names in RESEARCH_POOL.items():
        if name in names:
            return c
    return None


def factor_pool():
    """因子池元数据（名称/类别/作用），供 UI 展示与假设生成。"""
    return [{"name": n, "category": c, "role": indicator_roles([n]).get(n, "")}
            for c, names in RESEARCH_POOL.items() for n in names]


def generate_factor_combos(min_factors=2, max_factors=4):
    """逻辑约束生成因子组合：
      1. 同类最多 1 个（防冗余堆叠，如 EMA+SMA 都做趋势）
      2. 必须有主信号（趋势/动量/波动），成交量不能单独成策略
      3. 因子数 2~4（≤5 防过拟合）
    返回按「因子数→名称」排序的组合列表（list of list[显示名]）。"""
    from itertools import combinations
    names = [n for ns in RESEARCH_POOL.values() for n in ns]
    combos = []
    for k in range(min_factors, max_factors + 1):
        for combo in combinations(names, k):
            cats = [_class_of(n) for n in combo]
            if len(set(cats)) != len(cats):                    # 同类冗余因子不堆叠
                continue
            if not any(c in _PRIMARY_CLASSES for c in cats):   # 必须有主信号
                continue
            combos.append(sorted(combo))
    combos.sort(key=lambda c: (len(c), c))
    return combos


def combo_to_hypothesis(combo_names, goal, asset="ETH", timeframe="4h",
                        leverage=DEFAULT_LEVERAGE, tp_pct=DEFAULT_TP, sl_pct=DEFAULT_SL):
    """因子组合 → 研究假设 dict（含每指标作用/预期环境/风险假设，可直接喂 verify_hypothesis）。"""
    roles = indicator_roles(combo_names)
    params = {n: {pk: pv["default"] for pk, pv in INDICATOR_REGISTRY[n]["params"].items()}
              for n in combo_names}
    cats = [_class_of(n) for n in combo_names]
    primary = next((c for c in cats if c in _PRIMARY_CLASSES), cats[0])
    regime = _REGIME_BY_CLASS.get(primary, "通用")
    logic = "；".join(f"{n}：{roles.get(n, '辅助信号')}" for n in combo_names)
    return {
        "hypothesis_text": f"{' + '.join(combo_names)}组合",
        "user_goal": goal,
        "related_indicators": combo_names,
        "parameters": params,
        "asset": asset, "timeframe": timeframe, "leverage": leverage,
        "tp_pct": tp_pct, "sl_pct": sl_pct,
        "expected_logic": logic,
        "expected_market_condition": regime,
        "failure_environment": "震荡市" if regime != "趋势行情" else "趋势反转/低波动",
        "risk_assumption": f"预期 Sharpe ≥ {CRITERIA['sharpe_min']}，最大回撤 < {CRITERIA['mdd_max']:.0f}%",
    }


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


def classify_failure(m):
    """失败原因结构化分类：OOS亏损/MDD过高/交易次数不足/参数敏感/市场迁移失败。"""
    tags = []
    oos = m.get("oos_return")
    if oos is not None and oos <= 0:
        tags.append("OOS亏损")
    if (m.get("max_drawdown") or 0) >= CRITERIA["mdd_max"]:
        tags.append("MDD过高")
    if (m.get("trade_count") or 0) < CRITERIA["trades_min"]:
        tags.append("交易次数不足")
    if (m.get("total_return") or 0) > 0 and oos is not None and oos < 0:
        tags.append("市场迁移失败")
    wf = m.get("wf_profit_ratio")
    if wf is not None and wf < CRITERIA["wf_win_ratio_min"]:
        tags.append("参数敏感")
    return tags


# ============================================================
# 四、研究评分 + 等级（A/B/C/D）+ 过拟合风险
# ============================================================
def research_score(m, param_stability=None):
    """综合评分(0-100)：收益 20% + Sharpe 20% + MDD 20% + OOS 20% + 参数稳定 10% + Monte Carlo 10%。

    禁止只按收益排序：6 个维度加权，收益仅占 20%。param_stability 缺省时用 Walk-Forward
    盈利窗口占比作为稳定性代理，保证「未跑敏感性」时评分仍可算。
    """
    total_ret = m.get("total_return") or 0.0
    sharpe = m.get("sharpe") or 0.0
    mdd = m.get("max_drawdown") if m.get("max_drawdown") is not None else 100.0
    oos = m.get("oos_return") or 0.0
    mc = m.get("mc_p5")

    s_ret = _clip(total_ret / 200.0)
    s_sharpe = _clip(sharpe / 2.0)
    s_mdd = _clip(1.0 - mdd / 30.0)
    s_oos = _clip(oos / 20.0)
    if param_stability is not None:
        s_param = _clip(param_stability / 100.0)
    else:
        s_param = _clip((m.get("wf_profit_ratio") or 0.0) / 100.0)
    s_mc = _clip((mc / 10.0) if mc is not None else 0.0)

    total = round(100.0 * (0.20 * s_ret + 0.20 * s_sharpe + 0.20 * s_mdd
                           + 0.20 * s_oos + 0.10 * s_param + 0.10 * s_mc), 1)
    return {
        "total": total,
        "return": round(s_ret * 100, 1),
        "sharpe": round(s_sharpe * 100, 1),
        "mdd": round(s_mdd * 100, 1),
        "oos": round(s_oos * 100, 1),
        "param_stability": round(s_param * 100, 1),
        "monte_carlo": round(s_mc * 100, 1),
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
            failure_category=classify_failure(m),
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


# ============================================================
# 九、智能参数搜索（IS-only）+ 研究任务模式（批量自主研究）
# ============================================================
def parameter_search(df, coin, indicators, base_params, lev, tp, sl,
                     strategy_factory=None, is_end_year=IS_END_YEAR):
    """IS-only 参数搜索：只在训练集(df.year<=is_end_year)做贪心坐标下降寻优，禁止偷看 OOS。

    每个参数在其邻域(±1/±2 步)扫描，用 IS Sharpe 选最优（无交易候选直接淘汰），
    返回 best_params + 搜索轨迹 history。OOS 仅由调用方在最终验证时使用一次。
    """
    is_df = df[df.index.year <= is_end_year]
    if len(is_df) < 100:
        return {"best_params": base_params, "is_end_year": is_end_year, "history": [], "note": "IS 样本不足"}
    best = json.loads(json.dumps(base_params or {}))
    history = []
    for name in indicators:
        info = INDICATOR_REGISTRY.get(name)
        if not info:
            continue
        pvals = (best or {}).get(name) or {}
        for pk, base_val in list(pvals.items()):
            pmeta = info["params"].get(pk)
            if not pmeta:
                continue
            best_v, best_score = base_val, None
            for v in [base_val] + _neighborhood(pmeta, base_val):
                trial = json.loads(json.dumps(best))
                trial[name][pk] = v
                try:
                    pm = run_sensitivity_point(is_df, coin, indicators, trial, lev, tp, sl, strategy_factory)
                except Exception:
                    continue
                trades = pm.get("trade_count") or 0
                history.append({"param": f"{name}.{pk}", "value": v,
                                "sharpe": round(pm.get("sharpe") or 0, 3),
                                "return": round(pm.get("total_return") or 0, 2),
                                "trades": trades})
                if trades < 10:
                    continue
                score = pm.get("sharpe") or 0.0
                if best_score is None or score > best_score:
                    best_score, best_v = score, v
            if best_v is not None:
                best[name][pk] = best_v
    return {"best_params": best, "is_end_year": is_end_year, "history": history}


def run_research_task(goal, df, coin, timeframe="4h", strategy_factory=None,
                      max_hypotheses=20, min_factors=2, max_factors=3,
                      leverage=DEFAULT_LEVERAGE, tp_pct=DEFAULT_TP, sl_pct=DEFAULT_SL,
                      progress=None):
    """研究任务模式：目标 → 生成因子组合 → 建假设 → 回测验证 → 排名 → 优秀策略进候选库。

    progress(done, total, label): 可选进度回调（供 UI 展示）。
    返回 {goal, coin, timeframe, ranked(按综合分降序), summary, library_added}。
    """
    combos = generate_factor_combos(min_factors, max_factors)[:max_hypotheses]
    ranked = []
    total = len(combos)
    for i, combo in enumerate(combos, 1):
        hyp = combo_to_hypothesis(combo, goal, coin, timeframe, leverage, tp_pct, sl_pct)
        hid = db.add_hypothesis(
            hyp["hypothesis_text"], related_indicators=combo, user_goal=goal,
            asset=coin, timeframe=timeframe, leverage=leverage,
            parameters=hyp["parameters"], tp_pct=tp_pct, sl_pct=sl_pct,
            expected_logic=hyp["expected_logic"],
            expected_market_condition=hyp["expected_market_condition"],
            risk_assumption=hyp["risk_assumption"],
            failure_environment=hyp["failure_environment"])
        hyp["id"] = hid
        if progress:
            progress(i, total, " + ".join(combo[:2]))
        try:
            v = verify_hypothesis(hyp, df, coin, strategy_factory)
        except Exception as e:
            ranked.append({"combo": combo, "hypothesis_id": hid, "passed": False,
                           "score": {"total": 0, "grade": "D"}, "metrics": {},
                           "failures": [], "error": str(e), "hyp": hyp})
            continue
        ranked.append({"combo": combo, "hypothesis_id": hid, "passed": v["passed"],
                       "score": v["score"], "metrics": v["metrics"],
                       "failures": v["failures"], "report": v["report"],
                       "fingerprint": v["fingerprint"], "hyp": hyp})
    ranked.sort(key=lambda r: -(r["score"].get("total") or 0))

    # 优秀策略（通过门禁）进候选库
    added = []
    for r in [x for x in ranked if x.get("passed")][:5]:
        entry = library_entry(r["hyp"], r["combo"], _loads(r["hyp"].get("parameters")),
                              r["metrics"], {"passed": True, "score": r["score"],
                                             "coin": coin, "leverage": leverage,
                                             "tp_pct": tp_pct, "sl_pct": sl_pct})
        db.add_strategy(**entry)
        added.append(entry["name"])

    passed = sum(1 for r in ranked if r.get("passed"))
    top = ranked[0]
    summary = (f"目标「{goal}」：生成 {total} 个假设，通过门禁 {passed} 个，"
               f"Top1 = {' + '.join(top['combo'])}（{top['score']['total']} 分）。"
               f"进候选库 {len(added)} 个。")
    return {"goal": goal, "coin": coin, "timeframe": timeframe,
            "ranked": ranked, "summary": summary, "library_added": added}
