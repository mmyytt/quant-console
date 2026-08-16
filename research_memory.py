"""
研究先验记忆层（Research Prior Memory）
========================================
长期可维护的量化研究先验：市场常用参数、机构常用范围、周期适配、风控常识、过拟合风险。

AI 策略研究中心在生成搜索空间时参考此记忆，采用「70% 经验优先 + 30% 探索」采样，
替代纯 min-max 均匀网格，减少无意义参数测试，但**不关闭探索**。

独立模块：只提供数据 + 纯函数，不 import app.py / engine_core.py，无副作用。
禁止在此处写交易撮合 / PnL / 风控执行逻辑（那些属于 engine_core）。
"""

# ============================================================
# 采样比例：经验优先 / 探索
# ============================================================
PREFERRED_RATIO = 0.7

# ============================================================
# 一、指标参数先验
# schema_key → param_key → {"preferred": 常用值, "extended": 边缘探索值}
# preferred 来自市场常见习惯 / 机构常用范围；extended 用于保留有限探索，防止完全关闭。
# ============================================================
INDICATOR_PRIORS = {
    # ---- 趋势类 ----
    "ema": {
        "EMA_short": {"preferred": [5, 8, 10, 12, 15, 20, 25, 30], "extended": [3, 40, 50]},
        "EMA_long": {"preferred": [50, 60, 80, 100], "extended": [120, 150, 200]},
    },
    "sma": {
        "SMA_s": {"preferred": [5, 8, 10, 15, 20], "extended": [2, 25, 30]},
        "SMA_m": {"preferred": [10, 20, 30, 40], "extended": [5, 50, 60]},
        "SMA_l": {"preferred": [30, 60, 90, 120], "extended": [10, 20]},
    },
    "supertrend": {
        "ATR_period": {"preferred": [10, 14, 20], "extended": [5, 25, 30]},
        "multiplier": {"preferred": [2.0, 3.0], "extended": [1.0, 4.0, 5.0]},
    },
    "adx": {
        "ADX_period": {"preferred": [14, 20], "extended": [5, 10, 30]},
        "ADX_threshold": {"preferred": [20, 25, 30], "extended": [10, 40, 50]},
    },
    # ---- 摆动类 ----
    "rsi": {
        "RSI_period": {"preferred": [7, 14, 21], "extended": [2, 5, 30, 50]},
        "RSI_oversold": {"preferred": [30, 35], "extended": [20, 25]},
        "RSI_overbought": {"preferred": [70, 75], "extended": [80, 85]},
    },
    "kdj": {
        "K_period": {"preferred": [9, 14], "extended": [5, 20]},
        "K_smooth": {"preferred": [3, 5], "extended": [2, 8, 10]},
        "D_smooth": {"preferred": [3, 5], "extended": [2, 8, 10]},
    },
    "macd": {
        "MACD_fast": {"preferred": [8, 12, 16], "extended": [5, 20, 30]},
        "MACD_slow": {"preferred": [26, 30], "extended": [20, 40, 50]},
        "MACD_signal": {"preferred": [9, 12], "extended": [5, 15, 20]},
    },
    "cci": {
        "CCI_period": {"preferred": [14, 20, 30], "extended": [5, 40, 50]},
    },
    "stochrsi": {
        "Stoch_period": {"preferred": [14, 21], "extended": [5, 10, 30]},
    },
    "willr": {
        "WR_period": {"preferred": [14, 21], "extended": [5, 10, 30]},
    },
    # ---- 通道/支撑 ----
    "bollinger": {
        "BB_period": {"preferred": [20, 25, 30], "extended": [10, 40, 50]},
        "BB_std": {"preferred": [2.0, 2.5], "extended": [1.0, 3.0, 4.0]},
    },
    "keltner": {
        "KC_ema": {"preferred": [20, 30], "extended": [5, 40, 50]},
        "KC_mult": {"preferred": [2.0, 2.5], "extended": [1.0, 3.0, 4.0]},
    },
    "donchian": {
        "DC_period": {"preferred": [20, 30, 50], "extended": [5, 10, 100]},
    },
    # ---- 成交量 ----
    "obv": {"OBV_ma": {"preferred": [20, 30], "extended": [5, 40, 50]}},
    "mfi": {"MFI_period": {"preferred": [14, 20], "extended": [5, 30]}},
    "cmf": {"CMF_period": {"preferred": [20, 30], "extended": [5, 40, 50]}},
    "vol_break": {
        "VOL_ma": {"preferred": [20, 30], "extended": [10, 50, 100]},
        "VOL_mult": {"preferred": [1.5, 2.0], "extended": [1.0, 3.0, 5.0]},
    },
    "volume_ratio": {
        "VR_period": {"preferred": [20, 30], "extended": [5, 50, 100]},
        "VR_threshold": {"preferred": [1.5, 2.0], "extended": [0.5, 3.0, 5.0]},
    },
}

# ============================================================
# 二、周期适配规则：timeframe → schema_key → param_key → [合理区间]
# 不同交易周期对应不同的合理参数区域（短周期用小均线，长周期用大均线）。
# ============================================================
TIMEFRAME_RULES = {
    "5m":  {"ema": {"EMA_short": [3, 20], "EMA_long": [20, 100]}},
    "15m": {"ema": {"EMA_short": [5, 25], "EMA_long": [25, 120]}},
    "1h":  {"ema": {"EMA_short": [5, 30], "EMA_long": [30, 150]}},
    "4h":  {"ema": {"EMA_short": [10, 50], "EMA_long": [50, 200]}},
    "1d":  {"ema": {"EMA_short": [20, 100], "EMA_long": [50, 200]}},
}

# ============================================================
# 三、风控常识
# preferred = 常用/稳健；avoid = 过拟合/高危（禁止作为主要搜索资源，仅极端探索）。
# ============================================================
RISK_RULES = {
    "leverage":       {"preferred": [1, 2, 3, 5], "avoid": [10, 20]},
    "tp_pct":         {"preferred": [5, 8, 10, 15, 20], "avoid": [50]},
    "sl_pct":         {"preferred": [3, 5, 8, 10], "avoid": [30]},
    "init_alloc_pct": {"preferred": [30, 50, 70], "avoid": [100]},
}

# ============================================================
# 四、策略类型经验规则：style → schema_key → param_key → [优先区间]
# 趋势/突破/震荡等不同策略类型，各自有更合理的参数区域，避免每次随机生成。
# ============================================================
STRATEGY_RULES = {
    "趋势跟踪": {
        "ema": {"EMA_short": [5, 30]},
        "sma": {"SMA_s": [5, 20]},
        "supertrend": {"ATR_period": [10, 20]},
    },
    "突破": {
        "vol_break": {"VOL_ma": [20, 30]},
        "supertrend": {"ATR_period": [14, 20]},
        "donchian": {"DC_period": [20, 50]},
    },
    "均值回归": {
        "rsi": {"RSI_period": [14, 21]},
        "bollinger": {"BB_period": [20, 30]},
    },
    "动量": {
        "macd": {"MACD_fast": [8, 16]},
        "rsi": {"RSI_period": [7, 14]},
    },
    "日内": {
        "rsi": {"RSI_period": [7, 14]},
        "volume_ratio": {"VR_period": [20, 30]},
    },
    "高频": {
        "rsi": {"RSI_period": [7, 14]},
        "volume_ratio": {"VR_period": [20, 30]},
    },
    "综合": {},
}

# ============================================================
# 风险网格（由 RISK_RULES 派生，值均在 preferred 区间内，不含 avoid 高危值）
# ============================================================
LEVERAGE_GRID = RISK_RULES["leverage"]["preferred"]                     # [1, 2, 3, 5]
TP_SL_GRID = [(5.0, 3.0), (8.0, 3.0), (8.0, 5.0), (10.0, 5.0),
              (15.0, 8.0), (20.0, 8.0), (20.0, 10.0)]                   # TP 5-20 / SL 3-10
POSITION_PRESETS = [
    {"_init_alloc_pct": 30.0, "_enable_pyramiding": False},
    {"_init_alloc_pct": 50.0, "_enable_pyramiding": True, "_pyr_add_pct": 0.5, "_pyr_max": 2, "_pyr_trail": False},
    {"_init_alloc_pct": 50.0, "_enable_pyramiding": True, "_pyr_add_pct": 0.5, "_pyr_max": 2, "_pyr_trail": True},
    {"_init_alloc_pct": 70.0, "_enable_pyramiding": True, "_pyr_add_pct": 0.25, "_pyr_max": 3, "_pyr_trail": False},
    {"_init_alloc_pct": 70.0, "_enable_pyramiding": True, "_pyr_add_pct": 0.5, "_pyr_max": 3, "_pyr_trail": True},
]

# ============================================================
# 纯函数：先验查询 + 网格生成 + 评分
# ============================================================

def _lookup_indicator_prior(schema_key, param_key):
    """返回某指标的某参数先验 {preferred, extended}，无则 None。"""
    return (INDICATOR_PRIORS.get(schema_key) or {}).get(param_key)


def _timeframe_range(schema_key, param_key, timeframe):
    """周期适配区间 [lo, hi]，无则 None。"""
    return (TIMEFRAME_RULES.get(timeframe) or {}).get(schema_key, {}).get(param_key)


def _strategy_range(schema_key, param_key, style):
    """策略类型优先区间 [lo, hi]，无则 None。"""
    return (STRATEGY_RULES.get(style) or {}).get(schema_key, {}).get(param_key)


def _sample(values, k):
    """从升序列表等距取 k 个（不足则全取）。"""
    if not values or k <= 0:
        return []
    values = sorted(set(values))
    if k >= len(values):
        return list(values)
    if k == 1:
        return [values[len(values) // 2]]
    idx = sorted(set(int(round(i * (len(values) - 1) / (k - 1))) for i in range(k)))
    return [values[i] for i in idx]


def prior_grid(schema_key, param_key, n=5, timeframe=None, style=None):
    """生成参数网格：约 70% 经验优先 + 30% 探索。无先验返回 None（调用方回退均匀采样）。

    先应用策略类型优先区间裁剪 preferred，再应用周期适配区间裁剪 preferred/extended。
    禁止返回 schema 之外的参数值（裁剪只做缩小，不做扩张）。
    """
    prior = _lookup_indicator_prior(schema_key, param_key)
    if not prior:
        return None
    preferred = [v for v in prior["preferred"]]
    extended = [v for v in prior["extended"]]

    # 策略类型优先区间：只保留落在区间内的常用值（区间外视为非该策略常用，降为探索）
    srange = _strategy_range(schema_key, param_key, style)
    if srange:
        lo, hi = srange
        narrowed = [v for v in preferred if lo <= v <= hi]
        if narrowed:
            preferred = narrowed

    # 周期适配：裁剪 preferred/extended 到合理区间（若全被裁掉则保留原值，避免空网格）
    trange = _timeframe_range(schema_key, param_key, timeframe)
    if trange:
        lo, hi = trange
        preferred = [v for v in preferred if lo <= v <= hi] or preferred
        extended = [v for v in extended if lo <= v <= hi] or extended

    n_pref = max(1, round(n * PREFERRED_RATIO))
    n_ext = max(1, n - n_pref)
    return sorted(set(_sample(preferred, n_pref) + _sample(extended, n_ext)))


def param_prior_score(schema_key, param_key, value, is_extreme=False):
    """单个参数合理性评分（0~1）：1.0 经验优先 / 0.5 探索 / 0.3 中性 / 0.1 极端边界。"""
    if is_extreme:
        return 0.1
    prior = _lookup_indicator_prior(schema_key, param_key)
    if prior:
        if value in prior["preferred"]:
            return 1.0
        if value in prior["extended"]:
            return 0.5
    return 0.3


def prior_rating(score):
    """参数合理性评分 → 星级文案（研究报告展示）。"""
    if score >= 0.8:
        return "★★★★★ 符合机构常用参数区域"
    if score >= 0.5:
        return "★★★ 常用参数为主"
    if score >= 0.25:
        return "★★ 边缘探索参数"
    return "★ 极端/非标准参数"
