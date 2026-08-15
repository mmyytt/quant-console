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

# 交易频率 / 策略风格（供假设列表展示 + research_context）
_FREQ_OF = {"5m": "高频", "15m": "高频", "1h": "日内", "4h": "波段", "1d": "低频"}
_STYLE_OF = {"trend": "趋势跟踪", "momentum": "动量", "volatility": "均值回归", "volume": "量价配合"}
# 指标分类（INDICATOR_SCHEMA.category）→ 因子大类（供 build_strategy_config 推断默认风格）
_CATEGORY_TO_CLASS = {"趋势类": "trend", "摆动类": "momentum", "通道/支撑": "volatility", "成交量": "volume"}
_PRIMARY_CLASSES = ("trend", "momentum", "volatility")


def _freq_of(timeframe):
    return _FREQ_OF.get(str(timeframe or "").strip().lower(), "波段")


def _style_of(primary_class):
    return _STYLE_OF.get(primary_class, "综合")


def parse_research_context(goal):
    """从研究目标文本解析 research_context（symbol/timeframe/strategy_style/target_return）。

    例："研究BTC 1小时高频策略，目标年化50%" → {symbol:BTC, timeframe:1h, strategy_style:高频, target_return:50}。
    解析不到的字段用默认值（ETH/4h/综合/None），绝不返回空。
    """
    import re
    g = str(goal or "")
    symbol = "ETH"
    for c in ("BTC", "ETH", "SOL"):
        if c.lower() in g.lower():
            symbol = c
            break
    low = g.lower()
    if "15m" in low or "15分钟" in g or "15分" in g:
        timeframe = "15m"
    elif "5m" in low or "5分钟" in g or "5分" in g:
        timeframe = "5m"
    elif "1h" in low or "1小时" in g or "小时" in g:
        timeframe = "1h"
    elif "4h" in low or "4小时" in g:
        timeframe = "4h"
    elif "1d" in low or "日线" in g:
        timeframe = "1d"
    else:
        timeframe = "4h"
    target_return = None
    m = re.search(r'年化\s*(\d+(?:\.\d+)?)\s*%', g) or re.search(r'(\d+(?:\.\d+)?)\s*%\s*年化', g)
    if m:
        target_return = float(m.group(1))
    max_drawdown = None
    m2 = re.search(r'(?:最大)?回撤\s*(?:低于|不超过|控制在|小于|≤|<=)?\s*(\d+(?:\.\d+)?)\s*%', g)
    if m2:
        max_drawdown = float(m2.group(1))
    strategy_style = "综合"
    for k, v in (("高频", "高频"), ("趋势", "趋势跟踪"), ("均值回归", "均值回归"),
                 ("动量", "动量"), ("突破", "突破"), ("日内", "日内")):
        if k in g:
            strategy_style = v
            break
    return {"symbol": symbol, "timeframe": timeframe,
            "strategy_style": strategy_style, "target_return": target_return,
            "max_drawdown": max_drawdown}


def build_strategy_config(indicators, asset="ETH", timeframe="4h",
                          leverage=DEFAULT_LEVERAGE, tp_pct=DEFAULT_TP, sl_pct=DEFAULT_SL,
                          strategy_style=None, entry_rules=None, exit_rules=None,
                          risk_parameters=None, target_return=None):
    """生成完整 strategy_config（asset/timeframe/indicators/entry_rules/exit_rules/leverage/risk_parameters）。

    保证 hypothesis 永远携带非空 strategy_config（禁止空假设）。entry_rules/exit_rules
    缺省时按指标角色 + TP/SL 自动生成，绝不返回空列表。
    """
    indicators = [n for n in (indicators or []) if n]
    cats = [_CATEGORY_TO_CLASS.get(INDICATOR_REGISTRY[n]["category"]) for n in indicators if n in INDICATOR_REGISTRY]
    primary = next((c for c in cats if c in _PRIMARY_CLASSES), (cats[0] if cats else None))
    style = strategy_style or _style_of(primary)
    if not entry_rules:
        main = " + ".join(indicators[:2]) if indicators else "指标"
        entry_rules = [f"{main} 给出同向信号", "确认指标全部共振（AND）时开仓"]
    if not exit_rules:
        exit_rules = [f"固定止盈 {tp_pct}%", f"固定止损 {sl_pct}%", "信号反向 / 趋势破坏时平仓"]
    if risk_parameters is None:
        risk_parameters = {"tp_pct": tp_pct, "sl_pct": sl_pct,
                           "leverage": leverage, "pos_mode": "fixed_risk"}
    return {
        "asset": asset, "timeframe": timeframe,
        "strategy_style": style, "frequency": _freq_of(timeframe),
        "indicators": indicators,
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "leverage": leverage,
        "risk_parameters": risk_parameters,
        "target_return": target_return,
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


def full_fingerprint(indicator_names, param_overrides=None, leverage=None, tp_pct=None, sl_pct=None):
    """完整实验指纹 = 指标组合 + 指标参数 + 杠杆 + TP/SL。

    与 fingerprint 的区别：把参数/风控也纳入，用于「完全重复实验」去重。
    同一指标组合但参数不同 → 不同指纹（这正是参数搜索要测试的变体，不能误跳过）。
    """
    base = fingerprint(indicator_names)
    seg = []
    for name in sorted(param_overrides or {}):
        for k in sorted(param_overrides[name] or {}):
            seg.append(f"{_NAME_TO_KEY.get(name, name)}.{k}={param_overrides[name][k]}")
    if leverage is not None:
        seg.append(f"lev={leverage}")
    if tp_pct is not None:
        seg.append(f"tp={tp_pct}")
    if sl_pct is not None:
        seg.append(f"sl={sl_pct}")
    return (base + "#" + "&".join(seg)).upper() if seg else base


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
    from strategy_models import DynamicStrategy
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


GRADE_MEANING = {
    "A": "进入模拟盘观察",
    "B": "继续优化",
    "C": "研究价值",
    "D": "淘汰",
}


# ============================================================
# 六、AI 假设生成提示 + 解析
# ============================================================


def hypothesis_prompt(goal, assets="ETH, BTC, SOL", timeframes="5m, 15m, 1h, 4h, 1d"):
    from platform_context import format_context_text
    return f"""你是 QuantCode 的量化研究助手。用户研究目标是：
"{goal}"

你的任务：解析研究目标 → 生成一个全新的、可回测的研究假设。
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
  "strategy_style": "趋势跟踪",
  "entry_rules": ["开仓条件1", "开仓条件2"],
  "exit_rules": ["平仓条件1", "平仓条件2"],
  "expected_logic": "策略逻辑说明（为什么认为有效）",
  "expected_market_condition": "适用市场环境（趋势/震荡）",
  "failure_environment": "预期失效市场环境（哪些行情下会亏损）",
  "risk_assumption": "风险假设（预期最大回撤/胜率等）"
}}

约束：
- 可用资产: {assets}；可用周期: {timeframes}。
- indicators 必须从下方「平台能力」的指标清单精确选择 1~4 个（用完整中文名，禁止自创指标）。
- params 的 key 必须用清单中给出的参数 key；值必须是数字。
- entry_rules / exit_rules 各 1~3 条，描述具体开仓 / 平仓条件（不能为空）。
- strategy_style 取：趋势跟踪 / 动量 / 均值回归 / 量价配合 / 高频 / 日内 之一。
- 最多 3 个核心指标，避免堆叠冗余因子。

## 平台能力（quant_context，必须以此为准）
{format_context_text()}
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
    L.append("## 交易逻辑")
    L.append(f"- 策略类型：{hyp.get('strategy_style') or '-'}")
    for r in hyp.get("entry_rules") or []:
        L.append(f"- 开仓：{r}")
    for r in hyp.get("exit_rules") or []:
        L.append(f"- 平仓：{r}")
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
    L.append("- 仓位/加仓：固定单仓（引擎默认，本轮参数搜索不覆盖仓位与加仓逻辑）")
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
        "status": "pending_review" if verdict["passed"] else "candidate",
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


# ============================================================
# 九、策略搜索模式（V2：多候选生成 → 去重 → 自动回测 → 排名）
# ============================================================
def search_prompt(goal, assets="ETH, BTC, SOL", timeframes="5m, 15m, 1h, 4h, 1d"):
    """让 LLM 生成 5~8 个不同的策略方向（指标组合 + 默认参数），参数搜索由系统展开。"""
    from platform_context import format_context_text
    return f"""你是 QuantCode 的量化研究助手。用户研究目标是：
"{goal}"

你的任务：解析研究目标 → 提出 5~8 个不同的「策略方向」（每个方向 = 一组指标组合 + 一套默认参数）。
系统会在每个方向上自动做参数空间搜索（杠杆 / 止盈止损 / 指标主参数），无需你穷举所有参数组合。
严格要求：只输出 JSON 数组（5~8 个元素），不要输出任何其他文字或解释。

JSON 结构（数组，每个元素字段名固定）：
[
  {{
    "hypothesis": "假设陈述：该指标组合为何可能有效",
    "indicators": ["指标名1", "指标名2", "指标名3"],
    "params": {{"指标名1": {{"参数key": 数值}}, "指标名2": {{}}}},
    "asset": "ETH",
    "timeframe": "5m",
    "leverage": 2,
    "tp_pct": 8.0,
    "sl_pct": 4.0,
    "strategy_style": "趋势跟踪",
    "entry_rules": ["开仓条件1"],
    "exit_rules": ["平仓条件1"],
    "expected_logic": "策略逻辑说明（为什么认为有效）",
    "expected_market_condition": "适用市场环境（趋势/震荡）",
    "failure_environment": "预期失效市场环境（哪些行情下会亏损）",
    "risk_assumption": "风险假设（预期最大回撤/胜率等）"
  }},
  ...（共 5~8 个，方向尽量覆盖不同类别：趋势/突破/均值回归/量价）
]

约束：
- 可用资产: {assets}；可用周期: {timeframes}。
- 5~8 个方向的指标组合必须互不相同（覆盖不同类别，避免全部同类堆叠）。
- 每个方向的 indicators 必须从下方「平台能力」的指标清单精确选择 1~4 个（用完整中文名，禁止自创指标）。
- params 的 key 必须用清单中给出的参数 key；值必须是数字，且落在清单给出的 min~max 范围内。
- entry_rules / exit_rules 各 1~3 条，描述具体开仓 / 平仓条件（不能为空）。
- strategy_style 取：趋势跟踪 / 动量 / 均值回归 / 量价配合 / 高频 / 日内 之一。
- 每个方向独立完整，字段不能为空。

## 平台能力（quant_context，必须以此为准）
{format_context_text()}
"""


def parse_hypothesis_array(text):
    """从 AI 输出中提取 JSON 数组；失败返回 []。"""
    if not text:
        return []
    t = text.strip()
    s, e = t.find("["), t.rfind("]")
    if s == -1 or e <= s:
        return []
    try:
        arr = json.loads(t[s:e + 1])
        return [x for x in arr if isinstance(x, dict)]
    except Exception:
        return []


def _previously_failed(fp):
    """相同指标组合（指纹）是否已在失败记忆中出现过。"""
    if not fp:
        return False
    fp = fp.upper()
    for f in db.list_failure_memory(200):
        if (f.get("fingerprint") or "").upper() == fp:
            return True
    return False


def _spec_to_hyp(spec, indicators, coin):
    """把候选 spec 映射为 verify_hypothesis 需要的 hyp dict。"""
    return {
        "related_indicators": indicators,
        "parameters": spec.get("params") or {},
        "leverage": spec.get("leverage"),
        "tp_pct": spec.get("tp_pct"),
        "sl_pct": spec.get("sl_pct"),
        "timeframe": spec.get("timeframe"),
        "asset": spec.get("asset") or coin,
        "failure_environment": spec.get("failure_environment"),
        "risk_assumption": spec.get("risk_assumption"),
        "hypothesis_text": spec.get("hypothesis"),
        "expected_logic": spec.get("expected_logic"),
        "expected_market_condition": spec.get("expected_market_condition"),
        "user_goal": spec.get("goal"),
        "strategy_style": spec.get("strategy_style"),
        "entry_rules": spec.get("entry_rules"),
        "exit_rules": spec.get("exit_rules"),
    }


def run_strategy_search(candidates, df, coin, progress=None):
    """逐个去重 → 回测验证 → 评分，返回按综合分降序的结果列表。

    progress(i, total, label)：每处理一个候选前回调（用于 UI 进度）。
    复用 verify_hypothesis（自动落库 experiment / failure_memory / report）。
    """
    results = []
    total = len(candidates)
    for i, spec in enumerate(candidates):
        label = spec.get("hypothesis") or f"候选 {i + 1}"
        if progress:
            progress(i, total, label)
        indicators, _invalid = normalize_indicators(spec.get("indicators"))
        if not indicators:
            results.append({"spec": spec, "indicators": [], "skipped": True,
                            "reason": "无有效指标"})
            continue
        fp = fingerprint(indicators)
        if _previously_failed(fp):
            results.append({"spec": spec, "indicators": indicators, "skipped": True,
                            "reason": "历史失败（相同指标组合）"})
            continue
        hyp = _spec_to_hyp(spec, indicators, coin)
        try:
            verdict = verify_hypothesis(hyp, df, coin)
            results.append({"spec": spec, "indicators": indicators, "verdict": verdict,
                            "skipped": False})
        except Exception as e:
            results.append({"spec": spec, "indicators": indicators, "skipped": True,
                            "reason": f"回测异常: {e}"})
    # 按综合分降序（跳过项 score 视为 -inf，排最后）
    results.sort(key=lambda r: (r.get("verdict") or {}).get("score", {}).get("total", -999.0),
                 reverse=True)
    return results


# ============================================================
# 十、参数空间搜索（V3：方向探索 → 参数组合搜索 → 排名）
# ============================================================
# 搜索维度（有界，防组合爆炸；仓位/加仓未在引擎独立暴露，不在搜索列）
LEVERAGE_GRID = [1, 2, 3, 5, 10]
TPSL_GRID = [(8.0, 4.0), (4.0, 2.0), (15.0, 6.0), (25.0, 10.0)]


def _param_sample_values(schema_key):
    """取某指标第一个带 min/max 的参数，采样 [min, default, max] 三点（去重排序）。"""
    s = INDICATOR_SCHEMA.get(schema_key)
    if not s or not s.get("params"):
        return []
    for pk, pv in s["params"].items():
        if "min" in pv and "max" in pv:
            vals = sorted({pv["min"], pv["default"], pv["max"]})
            return [(pk, vals)]
    return []


def expand_parameter_grid(direction, max_combos=20):
    """把一个策略方向展开为有界参数组合列表（一次性扫描：杠杆/TP·SL/主参数）。

    direction: {"indicators": [...], "params": {显示名: {参数key: 值}}, "leverage", "tp_pct", "sl_pct"}
    返回: [{"label", "param_overrides", "leverage", "tp_pct", "sl_pct"}, ...]（已去重）
    """
    indicators = direction.get("indicators") or []
    base_params = direction.get("params") or {}
    lev0 = direction.get("leverage") or DEFAULT_LEVERAGE
    tp0 = direction.get("tp_pct") if direction.get("tp_pct") is not None else DEFAULT_TP
    sl0 = direction.get("sl_pct") if direction.get("sl_pct") is not None else DEFAULT_SL

    combos = []

    def _add(label, params, lev, tp, sl):
        combos.append({"label": label, "param_overrides": params,
                       "leverage": lev, "tp_pct": tp, "sl_pct": sl})

    # 1. 基准
    _add("基准参数", base_params, lev0, tp0, sl0)
    # 2. 杠杆扫描（保持参数/TP·SL 默认）
    for lev in LEVERAGE_GRID:
        _add(f"杠杆 {lev}x", base_params, lev, tp0, sl0)
    # 3. TP/SL 扫描（保持参数/杠杆默认）
    for tp, sl in TPSL_GRID:
        _add(f"TP{tp:g}%/SL{sl:g}%", base_params, lev0, tp, sl)
    # 4. 指标主参数扫描（每个指标取主参数 min/default/max，一次只动一个维度）
    for name in indicators:
        key = _NAME_TO_KEY.get(name)
        for pk, vals in _param_sample_values(key):
            for v in vals:
                po = {n: dict(p) for n, p in base_params.items()}
                po.setdefault(name, {})[pk] = v
                _add(f"{name} {pk}={v}", po, lev0, tp0, sl0)

    # 完全重复（指标+参数+杠杆+TP/SL 相同）去重
    seen, out = set(), []
    for c in combos:
        fp = full_fingerprint(indicators, c["param_overrides"], c["leverage"], c["tp_pct"], c["sl_pct"])
        if fp in seen:
            continue
        seen.add(fp)
        out.append(c)
    return out[:max_combos]


def run_parameter_search(directions, df, coin, progress=None, max_combos_per_direction=20):
    """系统化参数空间搜索：每个方向展开参数组合 → 逐个回测 → 记录 → 按综合分排名。

    与 run_strategy_search（V2 固定候选）不同：这里对每个方向做「杠杆/TP·SL/主参数」
    一次性扫描，复用 run_hypothesis_backtest（完整 IS/OOS/WF/MC），每个组合都落实验库 +
    失败记忆（全指纹去重）。progress(i, total, label) 为全局进度。
    """
    # 先展开全部任务（方向 × 参数组合），便于全局进度与总数
    tasks = []  # (spec, indicators, combo|None)
    for spec in directions:
        indicators, _inv = normalize_indicators(spec.get("indicators"))
        if not indicators:
            tasks.append((spec, [], None))
            continue
        direction = {"indicators": indicators, "params": spec.get("params") or {},
                     "leverage": spec.get("leverage"), "tp_pct": spec.get("tp_pct"),
                     "sl_pct": spec.get("sl_pct")}
        for combo in expand_parameter_grid(direction, max_combos_per_direction):
            tasks.append((spec, indicators, combo))

    total = len(tasks)
    results = []
    for i, (spec, indicators, combo) in enumerate(tasks):
        label = spec.get("hypothesis") or f"方向 {i + 1}"
        if combo is None:
            if progress:
                progress(i, total, label)
            results.append({"spec": spec, "indicators": [], "skipped": True, "reason": "无有效指标"})
            continue
        if progress:
            progress(i, total, f"{label} · {combo['label']}")

        fp = full_fingerprint(indicators, combo["param_overrides"],
                              combo["leverage"], combo["tp_pct"], combo["sl_pct"])
        if _previously_failed(fp):
            results.append({"spec": spec, "indicators": indicators, "combo": combo,
                            "skipped": True, "reason": "历史失败（相同参数组合）"})
            continue

        try:
            m = run_hypothesis_backtest(df, coin, indicators, combo["param_overrides"],
                                        combo["leverage"], combo["tp_pct"], combo["sl_pct"])
            passed, failures = judge_pass(m)
            score = research_score(m)
            name = " + ".join(indicators[:3]) if indicators else "未命名策略"
            exp_id = db.add_experiment(
                strategy_name=name, indicator_combination=indicators,
                parameters=combo["param_overrides"], asset=coin,
                timeframe=spec.get("timeframe"), leverage=combo["leverage"],
                tp_pct=combo["tp_pct"], sl_pct=combo["sl_pct"],
                total_return=m.get("total_return"), annual_return=m.get("annual_return"),
                sharpe=m.get("sharpe"), max_drawdown=m.get("max_drawdown"),
                win_rate=m.get("win_rate"), trade_count=m.get("trade_count"),
                walk_forward_score=m.get("wf_profit_ratio"), monte_carlo_score=m.get("mc_p5"),
                final_rating=score["grade"], oos_return=m.get("oos_return"),
                research_score=score["total"], grade=score["grade"],
                failure_reason="；".join(failures) if failures else None, fingerprint=fp,
            )
            verdict = {"passed": passed, "failures": failures, "score": score, "metrics": m,
                       "indicators": indicators, "params": combo["param_overrides"],
                       "coin": coin, "leverage": combo["leverage"], "tp_pct": combo["tp_pct"],
                       "sl_pct": combo["sl_pct"], "fingerprint": fp, "experiment_id": exp_id}
            hyp = _spec_to_hyp(spec, indicators, coin)
            hyp["parameters"] = combo["param_overrides"]
            verdict["report"] = build_report(hyp, indicators, combo["param_overrides"], m, verdict)
            if not passed:
                db.add_failure_memory(
                    strategy_name=name, indicator_combination=indicators,
                    parameters=combo["param_overrides"], fingerprint=fp,
                    failure_reason="；".join(failures) if failures else None,
                    failure_env=spec.get("failure_environment") or spec.get("risk_assumption"),
                    metrics={"sharpe": m.get("sharpe"), "oos_return": m.get("oos_return"),
                             "max_drawdown": m.get("max_drawdown"), "trade_count": m.get("trade_count")},
                    failure_category=classify_failure(m), avoid=1,
                )
            results.append({"spec": spec, "indicators": indicators, "combo": combo,
                            "verdict": verdict, "skipped": False})
        except Exception as e:
            results.append({"spec": spec, "indicators": indicators, "combo": combo,
                            "skipped": True, "reason": f"回测异常: {e}"})

    results.sort(key=lambda r: (r.get("verdict") or {}).get("score", {}).get("total", -999.0),
                 reverse=True)
    return results
