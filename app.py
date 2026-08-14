"""
马总量化控制台 — 百种指标积木 + AI策略导师
============================================================
启动: streamlit run app.py
"""
import streamlit as st
import pandas as pd, numpy as np, os, sys, time, json, base64, copy
from datetime import datetime, timedelta
import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 加载 .env 中的 API Key
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from engine_core import (
    DataEngine, StrategyBase, BacktestEngineV2, PerformanceAnalyzer,
    TAKER_FEE, SLIPPAGE, MultiFactorRegime,
)
from i18n import (
    t, set_lang, get_lang, init_lang,
    INDICATOR_I18N, CATEGORY_I18N, PARAM_LABEL_I18N, PARAM_HELP_I18N,
    of_risk_label, trend_dep_label,
)

# ============================================================
# 登录
# ============================================================
def _parse_auth_users(raw: str) -> dict:
    """解析登录用户配置，格式 'user:pass,user2:pass2'。"""
    users = {}
    if raw:
        for pair in str(raw).split(","):
            if ":" in pair:
                u, p = pair.split(":", 1)
                users[u.strip()] = p.strip()
    return users

# 登录用户从环境变量读取（本地 .env / Streamlit Cloud Secrets），禁止在代码中硬编码明文密码
_AUTH_RAW = os.environ.get("APP_USERS", "")
try:
    _SECRET_USERS = st.secrets.get("APP_USERS", "")
    if _SECRET_USERS:
        _AUTH_RAW = _SECRET_USERS
except Exception:
    pass
AUTH_CONFIG = {"enabled": True, "users": _parse_auth_users(_AUTH_RAW)}

def check_login():
    if not AUTH_CONFIG["enabled"]: return True
    if "logged_in" not in st.session_state: st.session_state.logged_in = False
    if st.session_state.logged_in: return True
    st.markdown(f"<h1 style='text-align:center;margin-top:60px'>{t('app_login_title')}</h1>", unsafe_allow_html=True)
    st.markdown(f"<p style='text-align:center;opacity:0.5'>{t('login_prompt')}</p>", unsafe_allow_html=True)
    _, c, _ = st.columns([1, 2, 1])
    with c:
        u = st.text_input(t("login_username"), key="lu"); p = st.text_input(t("login_password"), type="password", key="lp")
        if st.button(t("login_btn"), width="stretch", type="primary"):
            if AUTH_CONFIG["users"].get(u) == p: st.session_state.logged_in = True; st.rerun()
            else: st.error(t("login_error"))
    return False

def export_json(params):
    s = json.dumps({"exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "params": params}, ensure_ascii=False, indent=2)
    b = base64.b64encode(s.encode()).decode()
    return f'<a href="data:application/json;base64,{b}" download="strategy_config.json" style="text-decoration:none;">Download strategy_config.json</a>'

# ============================================================
# 页面配置 + CSS
# ============================================================
# i18n 语言初始化 (放在最前, 让 page_title 和登录页也支持双语)
init_lang()
st.set_page_config(page_title=t("page_title"), page_icon="📊", layout="wide", initial_sidebar_state="expanded")
st.markdown("""<style>
/* === 全局暗黑科技风 === */
body { font-family: 'Inter', 'Microsoft YaHei', sans-serif; }
.stApp { background: #0b1120; }
section[data-testid="stSidebar"] { background: #0d1320; }
.stButton>button {
    border-radius: 8px !important; border: 1px solid rgba(255,255,255,0.08) !important;
    background: #1a1f35 !important; transition: all 0.2s;
}
.stButton>button:hover { background: #252b48 !important; border-color: rgba(129,140,248,0.3) !important; }
.stButton>button[kind="primary"] { background: #4f46e5 !important; border-color: #4f46e5 !important; }
div[data-testid="stForm"] { border: 1px solid rgba(255,255,255,0.06); border-radius: 10px; padding: 12px; }
div[data-testid="stExpander"] { border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); }
.stChatMessage { border-radius: 8px; }
hr { margin: 10px 0; border-color: rgba(255,255,255,0.06); }
input, select, .stNumberInput input, .stSlider div { border-radius: 6px !important; }

/* === 手机端 === */
@media (max-width:768px){
    body{padding:4px!important;font-size:13px!important}
    .stButton>button{padding:10px!important;font-size:14px!important}h1{font-size:18px!important}
}
.metric-card{background:#1a1f35;border-radius:8px;padding:14px;text-align:center;margin:4px 0;
             border:1px solid rgba(255,255,255,0.06)}
.g{color:#22c55e}.r{color:#ef4444}.b{color:#60a5fa}.y{color:#eab308}
</style>""", unsafe_allow_html=True)

# 登录鉴权拦截 (放在所有UI之前, 登录页即使数据未就绪也能展示)
logged_in = check_login()
if not logged_in:
    st.stop()

# ============================================================
# 数据新鲜度校验: 最新K线超过1天 → 弹警告
# ============================================================
def check_data_freshness():
    from data_loader import get_data_freshness
    stale_coins = []
    for c in ["ETH", "BTC", "SOL"]:
        f = get_data_freshness(c)
        if f["status"] == "stale":
            stale_coins.append(f"{c}({f['gap_hours']:.0f}{t('stale_hours_ago')})")
    if stale_coins:
        st.error(t("data_stale", coins=", ".join(stale_coins)))

check_data_freshness()

if st.sidebar.button(t("btn_clear_cache"), width="stretch"):
    st.cache_data.clear(); st.cache_resource.clear()
    st.rerun()

# ── 语言切换器 ──
lang_col1, lang_col2 = st.sidebar.columns([3, 2])
with lang_col1:
    st.caption(t("lang_selector"))
with lang_col2:
    current_lang_display = "🇨🇳 " + t("lang_zh") if st.session_state.lang == "zh" else "🇺🇸 " + t("lang_en")
    if st.button(current_lang_display, width="stretch", key="lang_switcher",
                 help=t("lang_switch_help")):
        st.session_state.lang = "en" if st.session_state.lang == "zh" else "zh"
        set_lang(st.session_state.lang)
        st.rerun()

# ============================================================
# 统一指标元数据 Schema (Schema-Driven UI) — 已抽离至 indicator_schema.py
# ============================================================
from indicator_schema import INDICATOR_SCHEMA, INDICATOR_REGISTRY



# ============================================================
# i18n 显示辅助: 指标名/类别/参数label/help 按当前语言翻译
# ============================================================
def _ind(name):
    """指标中文名(key) → 当前语言显示名"""
    if get_lang() == "zh":
        return name
    return INDICATOR_I18N.get(name, (name, ""))[0]

def _ind_desc(name):
    """指标中文名(key) → 当前语言描述"""
    if get_lang() == "zh":
        return INDICATOR_REGISTRY.get(name, {}).get("desc", "")
    return INDICATOR_I18N.get(name, (name, ""))[1]

def _cat(cat):
    """指标类别中文(key) → 当前语言显示名"""
    if get_lang() == "zh":
        return cat
    return CATEGORY_I18N.get(cat, cat)

def _plabel(label):
    """参数label中文 → 当前语言显示名"""
    if get_lang() == "zh":
        return label
    return PARAM_LABEL_I18N.get(label, label)

def _phelp(help_text):
    """参数help中文 → 当前语言显示名"""
    if get_lang() == "zh":
        return help_text
    return PARAM_HELP_I18N.get(help_text, help_text)


# ============================================================
# 动态策略 (从注册表组装)
# ============================================================
class DynamicStrategy(StrategyBase):
    def __init__(self, selected: dict, use_and: bool = True, mf_params: dict = None):
        super().__init__("DynamicStrategy")
        self.selected = selected  # {name: {enabled: True, params: {...}}}
        self.use_and = use_and
        self.mf = mf_params or {}

    def generate_signals(self, df):
        df = df.copy(); long_conds = []; short_conds = []
        for name, cfg in self.selected.items():
            # 类型安全检查: 跳过元数据键(_weighted/_resonance_factors等)
            if not isinstance(cfg, dict): continue
            if not cfg.get("enabled", True): continue
            info = INDICATOR_REGISTRY.get(name)
            if not info: continue
            try:
                info["compute"](df, cfg.get("params", {}))
                if "_long" in df.columns: long_conds.append(df["_long"]); df.drop("_long", axis=1, inplace=True)
                if "_short" in df.columns: short_conds.append(df["_short"]); df.drop("_short", axis=1, inplace=True)
            except: pass

        if not long_conds and not short_conds:
            df['signal'] = 0
        elif self.selected.get("_weighted", False):
            # 加权打分模式: 统计满足的指标数, 超过阈值才触发
            threshold = self.selected.get("_weighted_threshold", 2)
            long_score = pd.Series(0, index=df.index)
            short_score = pd.Series(0, index=df.index)
            for c in long_conds:
                long_score += c.fillna(False).astype(int)
            for c in short_conds:
                short_score += c.fillna(False).astype(int)
            df['signal'] = 0
            df.loc[(long_score >= threshold) & (short_score < threshold), 'signal'] = 1
            df.loc[(short_score >= threshold) & (long_score < threshold), 'signal'] = -1
            df['long_score'] = long_score; df['short_score'] = short_score
        else:
            ls = long_conds[0].fillna(False) if long_conds else pd.Series(False, index=df.index)
            for c in long_conds[1:]:
                c = c.fillna(False)
                ls = (ls & c) if self.use_and else (ls | c)
            ss = short_conds[0].fillna(False) if short_conds else pd.Series(False, index=df.index)
            for c in short_conds[1:]:
                c = c.fillna(False)
                ss = (ss & c) if self.use_and else (ss | c)
            ls = ls.fillna(False); ss = ss.fillna(False)
            df['signal'] = 0
            df.loc[ls & ~ss, 'signal'] = 1; df.loc[ss & ~ls, 'signal'] = -1

        # 多因子牛熊
        if self.mf.get("enabled", True):
            mf = MultiFactorRegime(
                ema_weight=self.mf.get("ema_w", 0.40), adx_weight=self.mf.get("adx_w", 0.35),
                adx_threshold=self.mf.get("adx_th", 25), bull_threshold=self.mf.get("bull_th", 0.30),
            )
            df = mf.evaluate(df); df['regime'] = df.get('regime_mf', 'range'); df['br'] = df.get('br_mf', 0)
        else:
            c = df['close'].shift(1); ema50 = c.ewm(span=50, adjust=False).mean()
            slope = (ema50 - ema50.shift(20)) / ema50.shift(20).replace(0, np.nan)
            df['regime'] = 'range'; df.loc[slope > 0.02, 'regime'] = 'bull'; df.loc[slope < -0.02, 'regime'] = 'bear'
            df['br'] = (df['regime'] == 'bear').astype(int).rolling(200, min_periods=1).mean()

        # === 交易方向过滤: 牛熊绑定 + 交易模式 ===
        trade_mode = self.selected.get("_trade_mode", "双向")
        regime_filter = self.selected.get("_regime_filter", True)

        if regime_filter:
            # 牛市: 禁止做空
            if 'regime' in df.columns:
                df.loc[(df['regime'] == 'bull') & (df['signal'] == -1), 'signal'] = 0
            # 熊市: 禁止做多
            if 'regime' in df.columns:
                df.loc[(df['regime'] == 'bear') & (df['signal'] == 1), 'signal'] = 0

        # 交易模式覆盖
        if trade_mode == "仅做多":
            df.loc[df['signal'] == -1, 'signal'] = 0
        elif trade_mode == "仅做空":
            df.loc[df['signal'] == 1, 'signal'] = 0

        # === 共振评分: 统计用户指定的3个因子同时触发的情况 ===
        res_factors = self.selected.get("_resonance_factors")
        if not isinstance(res_factors, list): res_factors = []
        df['resonance_score'] = 0
        if res_factors:
            for fname in res_factors:
                if not fname: continue
                info = INDICATOR_REGISTRY.get(fname)
                if not info: continue
                fcfg = self.selected.get(fname)
                if not isinstance(fcfg, dict): continue
                try:
                    info["compute"](df, fcfg.get("params", {}))
                    has_l = "_long" in df.columns
                    has_s = "_short" in df.columns
                    if has_l or has_s:
                        long_col = df["_long"].fillna(False) if has_l else pd.Series(False, index=df.index)
                        short_col = df["_short"].fillna(False) if has_s else pd.Series(False, index=df.index)
                        df['resonance_score'] += (long_col | short_col).astype(int)
                        if has_l: df.drop("_long", axis=1, inplace=True)
                        if has_s: df.drop("_short", axis=1, inplace=True)
                except: pass
        df['score'] = df['resonance_score'] if res_factors else abs(df['signal'])
        return df


# ============================================================
# Sidebar: 基础设置
# ============================================================
st.sidebar.title(t("sidebar_console_title"))

# 日期预设按钮 (在form外面, 立即可用)
if "date_range" not in st.session_state: st.session_state.date_range = None

st.sidebar.caption(t("quick_period"))
dp1, dp2, dp3 = st.sidebar.columns(3)
for label, dr, col in [
    (t("date_preset_2021"), ("2021-01-01","2021-12-31"), dp1),
    (t("date_preset_2022"), ("2022-01-01","2022-12-31"), dp2),
    (t("date_preset_2324"), ("2023-01-01","2024-12-31"), dp3),
]:
    if col.button(label, width="stretch", key=f"dp_{label}"):
        st.session_state.date_range = dr; st.rerun()
if st.sidebar.button(t("btn_all_history"), width="stretch"):
    st.session_state.date_range = None; st.rerun()

# 自定义日期选择器 (恢复!)
st.sidebar.caption(t("custom_date"))
dc1, dc2 = st.sidebar.columns(2)
d_start = dc1.date_input(t("date_start"), datetime(2020,1,1), key="d_start")
d_end = dc2.date_input(t("date_end"), datetime.now(), key="d_end")
dp3, dp4 = st.sidebar.columns(2)
if dp3.button(t("btn_last_1y"), width="stretch"):
    st.session_state.date_range = ((datetime.now()-timedelta(days=365)).strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d"))
    st.rerun()
if dp4.button(t("btn_last_3y"), width="stretch"):
    st.session_state.date_range = ((datetime.now()-timedelta(days=1095)).strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d"))
    st.rerun()
if st.sidebar.button(t("btn_apply_date"), width="stretch"):
    st.session_state.date_range = (d_start.strftime("%Y-%m-%d"), d_end.strftime("%Y-%m-%d"))
    st.rerun()
if st.session_state.date_range:
    st.sidebar.caption(t("current_date_range", start=st.session_state.date_range[0], end=st.session_state.date_range[1]))

st.sidebar.divider()

# 策略模式默认值 (form内可能被覆盖, 这里提前声明防NameError)
strat_mode_key = "classic"
hedge_ratio = 0.5; unlock_pct = 5.0; max_pyramid = 3
pyramid_first = 0.3; pyramid_step_pct = 1.5; trailing_pct = 0.0
spot_tp = 5.0; spot_sl = 2.0; short_sl = 3.0; funding_threshold = 0.01
tp_pct = 10.0; sl_pct = 5.0; bull_a = 100.0; range_a = 50.0; bear_a = 30.0
lock_streak_val = 3; lock_days = 2; risk_per_trade = 1.0; pyr_init_pct = 30
use_atr_stop = False; atr_period_val = 14; atr_mult_val = 2.0
oos_enabled = False; oos_ratio = 70
use_weighted = False; weighted_threshold = 2
resonance_enabled = False; mf_enabled = True
ema_w = 0.40; adx_w = 0.35; adx_th = 25; bull_th = 0.30
res_f1 = res_f2 = res_f3 = ""
unlock_indicator = "price"; use_ema_unlock = True; use_rsi_unlock = False; use_vol_unlock = False
is_dual_leg = False

# ============================================================
# 配置表单: 所有参数控件包在form里, 勾选/拖动不触发重渲染
# ============================================================
with st.sidebar.form(key="config_form", clear_on_submit=False):
    st.caption(t("sidebar_title"))
    c1, c2 = st.columns(2)
    coin = c1.selectbox(t("coin_select"), ["ETH", "BTC", "SOL"], 0)
    timeframe = c2.selectbox(t("timeframe_select"), ["5m", "15m", "1h", "4h", "1d"], 3)
    c1, c2 = st.columns(2)
    leverage = c1.slider(t("leverage_label"), 1, 20, 3, 1)
    initial_capital = c2.number_input(t("capital_label"), 100, 1000000, 10000, 1000)
    st.caption(t("direction_control"))
    c1, c2 = st.columns(2)
    _trade_mode_options = ["双向交易 (做多+做空)", "仅做多 (Only Long)", "仅做空 (Only Short)"]
    trade_mode = c1.selectbox(t("trade_mode_label"), _trade_mode_options, index=0,
                              format_func=lambda x: {"双向交易 (做多+做空)": t("trade_mode_both"),
                                                     "仅做多 (Only Long)": t("trade_mode_long"),
                                                     "仅做空 (Only Short)": t("trade_mode_short")}.get(x, x),
                              help=t("trade_mode_help"))
    regime_filter_enabled = c2.checkbox(t("regime_filter_label"), True,
        help=t("regime_filter_help"))

    st.divider(); st.caption(t("strategy_params_section"))
    _strategy_mode_options = [
        "经典单向策略 (Classic Directional)",
        "Delta中性对冲 (Delta-Neutral Hedging)",
        "动能突破解封 (Momentum Unlocking)",
    ]
    strategy_mode = st.selectbox(t("strategy_mode_label"), _strategy_mode_options, index=0,
        format_func=lambda x: {
            "经典单向策略 (Classic Directional)": t("strategy_mode_classic"),
            "Delta中性对冲 (Delta-Neutral Hedging)": t("strategy_mode_hedging"),
            "动能突破解封 (Momentum Unlocking)": t("strategy_mode_unlocking"),
        }.get(x, x), help=t("strategy_mode_help"))
    mode_map = {"经典": "classic", "Delta": "hedging", "动能": "unlocking"}
    strat_mode_key = [v for k, v in mode_map.items() if k in strategy_mode][0]
    is_dual_leg = strat_mode_key in ("hedging", "unlocking")

    # === 动态参数面板 ===
    hedge_ratio = 0.5; unlock_pct = 5.0; max_pyramid = 3
    pyramid_first = 0.3; pyramid_step_pct = 1.5; trailing_pct = 0.0
    spot_tp = 5.0; spot_sl = 2.0; short_sl = 3.0; funding_threshold = 0.01

    if strat_mode_key == "classic":
        st.caption(t("classic_mode_caption"))
        # 通用仓位控制 (不受加仓开关影响)
        pyr_init_pct = st.slider(t("single_pos_pct"), 10, 100, 30, 5,
            help=t("single_pos_help"))
        # 加仓子面板
        with st.expander(t("pyramiding_title"), expanded=False):
            enable_pyramiding = st.checkbox(t("enable_pyramiding"), False)
            pyr_trigger_pct = 2.0; pyr_add_pct = 0.5; pyr_max = 3; pyr_trail = False
            if enable_pyramiding:
                c1, c2 = st.columns(2)
                pyr_trigger_pct = c1.number_input(t("pyr_trigger_pct"), 0.5, 20.0, 2.0, 0.5,
                    help=t("pyr_trigger_help"))
                pyr_add_pct = c2.slider(t("pyr_add_pct"), 10, 100, 50, 5,
                    help=t("pyr_add_help")) / 100
                pyr_max = st.number_input(t("pyr_max"), 1, 10, 3)
                pyr_trail = st.checkbox(t("pyr_trail"), False,
                    help=t("pyr_trail_help"))
    elif strat_mode_key == "hedging":
        st.caption(t("hedge_legs_caption"))
        hedge_ratio = st.slider(t("spot_futures_ratio"), 0.1, 1.0, 0.5, 0.1)
        st.caption(t("spot_leg_caption"))
        c1, c2 = st.columns(2)
        spot_tp = c1.number_input(t("spot_tp_label"), 1.0, 50.0, 5.0, 0.5, key="hedge_spot_tp")
        spot_sl = c2.number_input(t("spot_sl_label"), 0.5, 20.0, 2.0, 0.5, key="hedge_spot_sl")
        st.caption(t("futures_leg_caption"))
        short_sl = st.number_input(t("short_sl_label"), 1.0, 20.0, 3.0, 0.5, key="hedge_short_sl")
        st.caption(t("combo_protection_caption"))
    elif strat_mode_key == "unlocking":
        st.caption(t("unlock_triggers_caption"))
        c1, c2 = st.columns(2)
        unlock_pct = c1.number_input(t("price_break_pct"), 1.0, 20.0, 5.0, 0.5)
        use_ema_unlock = c2.checkbox(t("ema_unlock"), True)
        use_rsi_unlock = st.checkbox(t("rsi_unlock"), False)
        use_vol_unlock = st.checkbox(t("vol_unlock"), False)
        st.caption(t("unlock_risk_caption"))
        c1, c2 = st.columns(2)
        unlock_sl = c1.number_input(t("unlock_sl"), 1.0, 15.0, 3.0, 0.5)
        unlock_tp = c2.number_input(t("unlock_tp"), 5.0, 50.0, 15.0, 0.5)

    # === 单向风控 (仅非对冲模式渲染) ===
    if not is_dual_leg:
        st.divider(); st.caption(t("one_way_risk_caption"))

        # ── 仓位模式 ──
        st.caption(t("pos_mode_caption"))
        _pos_mode_options = ["固定资金比例 (Fixed Capital)", "固定风险比例 (Fixed Risk)", "动态止损 (Dynamic Stop)"]
        pos_mode = st.radio(t("pos_mode_label"), _pos_mode_options,
            index=0, horizontal=True,
            format_func=lambda x: (t("pos_mode_dynamic_stop_opt") if "Dynamic" in x
                                   else t("pos_mode_fixed_capital_opt") if "Capital" in x
                                   else t("pos_mode_fixed_risk_opt")))
        use_fixed_risk = "Risk" in pos_mode
        use_dynamic_stop = "Dynamic" in pos_mode  # P3-6: 暴露动态止损仓位模式

        if use_fixed_risk:
            st.info(t("fixed_risk_info"))
        elif use_dynamic_stop:
            st.info(t("dynamic_stop_info"))
        else:
            st.info(t("fixed_capital_info"))

        # ── 单笔建仓比例 (动态语义) ──
        st.caption(t("alloc_caption"))
        if use_fixed_risk:
            alloc_label = t("risk_budget_pct")
            alloc_help = t("alloc_help_risk")
        else:
            alloc_label = t("init_capital_pct")
            alloc_help = t("alloc_help_capital")
        pyr_init_pct = st.slider(alloc_label, 10, 100, 30, 5, help=alloc_help)
        if use_fixed_risk:
            st.caption(t("example_risk"))
        else:
            st.caption(t("example_capital"))

        # ── 止盈/止损模式 ──
        st.caption(t("tp_sl_mode_caption"))
        c1, c2 = st.columns(2)
        _tp_mode_options = ["保证金收益率 (Margin%)", "价格百分比 (Price%)"]
        tp_mode = c1.radio(t("tp_mode_label"), _tp_mode_options,
                           index=0, horizontal=True,
                           format_func=lambda x: t("tp_mode_margin") if "Margin" in x else t("tp_mode_price"),
                           help=t("tp_mode_help"))
        _sl_mode_options = ["保证金亏损率 (Margin%)", "价格百分比 (Price%)"]
        sl_mode = c2.radio(t("sl_mode_label"), _sl_mode_options,
                           index=0, horizontal=True,
                           format_func=lambda x: t("sl_mode_margin") if "Margin" in x else t("sl_mode_price"),
                           help=t("sl_mode_help"))
        c1, c2 = st.columns(2)
        use_margin_tp = "Margin" in tp_mode
        tp_label = t("margin_tp_pct") if use_margin_tp else t("price_tp_pct")
        tp_pct = c1.slider(tp_label, 2.0, 50.0, 10.0, 0.5,
            help=t("tp_help_margin") if use_margin_tp else t("tp_help_price"))
        use_margin_sl = "Margin" in sl_mode
        sl_label = t("margin_sl_pct") if use_margin_sl else t("price_sl_pct")
        sl_pct = c2.slider(sl_label, 1.0, 30.0, 5.0, 0.5,
            help=t("sl_help_margin") if use_margin_sl else t("sl_help_price"))

        # ── 市场系数 (动态标签) ──
        coeff_label = t("risk_budget_coeff") if use_fixed_risk else t("market_coeff")
        st.caption(t("coeff_caption", coeff=coeff_label,
                     target=t("risk_budget_word") if use_fixed_risk else t("position_word")))
        c1, c2, c3 = st.columns(3)
        bull_label = t("bull_label") + (t("budget_pct") if use_fixed_risk else t("position_pct"))
        bull_help = t("bull_help_risk") if use_fixed_risk else t("bull_help_capital")
        bull_a = c1.number_input(bull_label, 0, 200, 100, 5, help=bull_help)
        range_label = t("range_label") + (t("budget_pct") if use_fixed_risk else t("position_pct"))
        range_help = t("range_help_risk") if use_fixed_risk else t("range_help_capital")
        range_a = c2.number_input(range_label, 0, 200, 50, 5, help=range_help)
        bear_label = t("bear_label") + (t("budget_pct") if use_fixed_risk else t("position_pct"))
        bear_help = t("bear_help_risk") if use_fixed_risk else t("bear_help_capital")
        bear_a = c3.number_input(bear_label, 0, 200, 30, 5, help=bear_help)

        # ── 连亏锁仓 ──
        c1, c2 = st.columns(2)
        lock_streak_val = c1.number_input(t("lock_streak"), 1, 10, 3,
            help=t("lock_streak_help"))
        lock_days = c2.number_input(t("lock_days"), 1, 30, 2,
            help=t("lock_days_help"))

        # ── 风险参数 ──
        st.caption(t("risk_params_caption"))
        c1, c2 = st.columns(2)
        if use_fixed_risk or use_dynamic_stop:
            risk_per_trade = c1.number_input(t("risk_per_trade_pct"), 0.1, 30.0, 1.0, 0.5,
                help=t("risk_help", equity=f"{initial_capital:,.0f}", max_loss=f"{initial_capital*0.01:,.0f}"))
            # 收敛警告 (仅 Fixed Risk: 风险% 与 止损% 撞车才有意义)
            if use_fixed_risk and abs(risk_per_trade - sl_pct) < 0.05:
                st.warning(t("risk_converge_warning", risk=risk_per_trade, sl=sl_pct,
                             suggested=f"{max(0.1, sl_pct*0.4):.1f}"))
        else:
            risk_per_trade = 1.0
            c1.metric(t("risk_per_trade_pct"), "N/A",
                help=t("risk_n_a_help"))

        # ── ATR 动态止损 (含覆盖状态) ──
        use_atr_stop = c2.checkbox(t("atr_entry_stop"), False, help=t("atr_help"))
        atr_period_val, atr_mult_val = 14, 2.0
        if use_atr_stop:
            c1, c2 = st.columns(2)
            atr_period_val = c1.number_input(t("atr_period"), 5, 30, 14, 1,
                help=t("atr_period_help"))
            atr_mult_val = c2.number_input(t("atr_mult"), 1.0, 5.0, 2.0, 0.5,
                help=t("atr_mult_help"))

            # ATR覆盖状态提示
            sl_mode_label = t("margin_mode_label") if "Margin" in sl_mode else t("price_mode_label")
            effective_sl_display = f"{sl_pct}%（{sl_mode_label}）"
            risk_desc = t("atr_override_risk_desc") if use_fixed_risk else t("atr_override_capital_desc")
            st.warning(t("atr_override_warning", sl=effective_sl_display,
                         period=atr_period_val, mult=atr_mult_val, risk_desc=risk_desc))
        else:
            # ATR关闭时显示当前有效止损
            sl_mode_label2 = t("margin_mode_label") if "Margin" in sl_mode else t("price_mode_label")
            st.caption(t("current_effective_sl", sl=sl_pct, mode=sl_mode_label2))
    else:
        tp_pct = 10.0; sl_pct = 5.0
        bull_a = 100.0; range_a = 50.0; bear_a = 30.0
        lock_streak_val = 3; lock_days = 2; risk_per_trade = 1.0
        use_atr_stop = False; atr_period_val = 14; atr_mult_val = 2.0
        use_fixed_risk = False; use_dynamic_stop = False  # P3-6: 双端模式无仓位模式, 给默认值

    oos_enabled = st.checkbox(t("oos_toggle"), False)
    oos_ratio = st.slider(t("train_pct"), 50, 90, 70, 5, disabled=not oos_enabled)

    st.divider(); st.caption(t("indicator_blocks_caption"))
    st.caption(t("indicator_tf_hint", tf=timeframe))
    _logic_options = ["AND 全部满足 (严格)", "OR 任一满足 (灵敏)", "加权打分 N个以上触发 (推荐)"]
    logic_mode = st.radio(t("signal_logic_label"), _logic_options,
        horizontal=False, key="logic_mode",
        format_func=lambda x: t("logic_and") if "AND" in x else (t("logic_weighted") if "加权" in x else t("logic_or")))
    use_and = "AND" in logic_mode
    use_weighted = "加权" in logic_mode
    weighted_threshold = 2
    if use_weighted:
        weighted_threshold = st.slider(t("min_indicator_count"), 1, 10, 2, 1,
            help=t("min_indicator_help"))

    categories = {}
    for name, info in INDICATOR_REGISTRY.items():
        cat = info["category"]
        if cat not in categories: categories[cat] = []
        categories[cat].append(name)
    if "selected_indicators" not in st.session_state:
        st.session_state.selected_indicators = {"EMA 双均线": {"enabled": True, "params": {"EMA_short": 7, "EMA_long": 21}}}

    for cat_name, ind_names in categories.items():
        with st.expander(t("indicator_category_fmt", cat=_cat(cat_name), n=len(ind_names)), expanded=False):
            for name in ind_names:
                info = INDICATOR_REGISTRY[name]
                sel = st.session_state.selected_indicators
                checked = name in sel and sel[name].get("enabled", False)
                new_checked = st.checkbox(_ind(name), checked, key=f"ind_{name}", help=_ind_desc(name))
                if new_checked and name not in sel:
                    # 用schema key初始化params, 不是label
                    sel[name] = {"enabled": True, "params": {
                        pk: pv["default"] for pk, pv in info["params"].items()
                    }}
                elif not new_checked and name in sel:
                    sel[name]["enabled"] = False

                # 展开子参数: 显示label, 存储用schema key
                if new_checked and info["params"]:
                    param_items = list(info["params"].items())
                    cols = st.columns(min(2, len(param_items)))
                    for i, (pk, pdef) in enumerate(param_items):
                        label = _plabel(pdef["label"])
                        val = cols[i % 2].number_input(
                            label, pdef["min"], pdef["max"],
                            sel[name]["params"].get(pk, pdef["default"]),
                            pdef["step"], key=f"p_{name}_{pk}",
                            help=_phelp(pdef.get("help", "")),
                        )
                        sel[name]["params"][pk] = val

    st.divider(); st.caption(t("mf_resonance_caption"))
    c1, c2 = st.columns(2)
    mf_enabled = c1.checkbox(t("mf_toggle"), True, key="mf_on")
    resonance_enabled = c2.checkbox(t("resonance_toggle"), False, key="res_on")
    res_f1, res_f2, res_f3 = "", "", ""
    if resonance_enabled:
        cat_list = ["趋势类", "摆动类", "通道/支撑", "成交量", "K线形态"]
        cat1_opts = [""] + [n for n, i in INDICATOR_REGISTRY.items() if i["category"] == cat_list[0]]
        cat23_opts = [""] + [n for n, i in INDICATOR_REGISTRY.items() if i["category"] in cat_list[1:3]]
        cat45_opts = [""] + [n for n, i in INDICATOR_REGISTRY.items() if i["category"] in cat_list[3:]]
        # 堆叠布局, 避免文字截断
        _ind_fmt = lambda x: _ind(x) if x else ""
        res_f1 = st.selectbox(t("factor1"), cat1_opts, key="rf1", format_func=_ind_fmt)
        res_f2 = st.selectbox(t("factor2"), cat23_opts, key="rf2", format_func=_ind_fmt)
        res_f3 = st.selectbox(t("factor3"), cat45_opts, key="rf3", format_func=_ind_fmt)
        # 展开选中因子的子参数
        _factor_short = {"因子1": "factor1_short", "因子2": "factor2_short", "因子3": "factor3_short"}
        for label, fname in [("因子1", res_f1), ("因子2", res_f2), ("因子3", res_f3)]:
            if fname and fname in INDICATOR_REGISTRY and INDICATOR_REGISTRY[fname]["params"]:
                with st.expander(t("factor_params", label=t(_factor_short[label]), fname=_ind(fname)), expanded=False):
                    fparams = INDICATOR_REGISTRY[fname]["params"]
                    cols = st.columns(min(2, len(fparams)))
                    for i, (pk, pdef) in enumerate(fparams.items()):
                        val_key = f"rf_param_{fname}_{pk}"
                        # 从selected_indicators获取或默认值
                        cur_val = st.session_state.selected_indicators.get(fname, {}).get("params", {}).get(pk, pdef["default"])
                        new_val = cols[i % 2].number_input(
                            _plabel(pdef["label"]), pdef["min"], pdef["max"],
                            cur_val, pdef["step"], key=val_key,
                            help=_phelp(pdef.get("help", "")),
                        )
                        if fname not in st.session_state.selected_indicators:
                            st.session_state.selected_indicators[fname] = {"enabled": True, "params": {}}
                        st.session_state.selected_indicators[fname]["params"][pk] = new_val
    if mf_enabled:
        c1, c2 = st.columns(2)
        ema_w = c1.slider(t("ema_w_label"), 0.0, 1.0, 0.40, 0.05, key="mf_ew")
        adx_w = c2.slider(t("adx_w_label"), 0.0, 1.0, 0.35, 0.05, key="mf_aw")
        adx_th = st.slider(t("adx_th_label"), 10, 50, 25, 5, key="mf_at")
        bull_th = st.slider(t("bull_th_label"), 0.10, 0.60, 0.30, 0.05, key="mf_bt")

    st.divider()
    submitted = st.form_submit_button(t("btn_run_backtest"), width="stretch", type="primary")

st.sidebar.divider()
# 导出 + 刷新 + 登出 (在form外面)
c_refresh1, c_refresh2 = st.sidebar.columns(2)
if c_refresh1.button(t("btn_refresh_quote"), width="stretch"):
    st.cache_data.clear(); st.cache_resource.clear()
    st.success(t("refresh_success")); time.sleep(1); st.rerun()
if c_refresh2.button(t("btn_force_refresh"), width="stretch",
                     help=t("force_refresh_help")):
    with st.spinner(t("force_refresh_spinner")):
        try:
            from data_loader import force_redownload
            for c in ["ETH", "BTC", "SOL"]:
                force_redownload(c)
            st.cache_data.clear(); st.cache_resource.clear()
            st.success(t("force_refresh_done")); time.sleep(1); st.rerun()
        except Exception as e:
            st.error(t("download_failed", error=e))
# ============================================================
# 预设管理器
# ============================================================
PRESET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.json")

def load_presets():
    if os.path.exists(PRESET_FILE):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return {}

def save_presets(data):
    with open(PRESET_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

presets = load_presets()
st.sidebar.divider()
with st.sidebar.expander(t("preset_manager"), expanded=False):
    preset_names = list(presets.keys())
    if preset_names:
        selected_preset = st.selectbox(t("saved_presets"), [""] + preset_names, key="preset_sel")
        c1, c2 = st.columns(2)
        if c1.button(t("btn_load"), width="stretch", disabled=not selected_preset):
            p = presets[selected_preset]
            # 恢复参数到session_state
            for k, v in p.get("params", {}).items():
                if k == "indicators":
                    st.session_state.selected_indicators = v
                elif k in st.session_state:
                    st.session_state[k] = v
            st.success(t("loaded_preset", name=selected_preset))
            time.sleep(1); st.rerun()
        if c2.button(t("btn_delete"), width="stretch", disabled=not selected_preset):
            del presets[selected_preset]
            save_presets(presets)
            st.rerun()

    preset_name = st.text_input(t("preset_name"), placeholder=t("preset_placeholder"), key="preset_name")
    if st.button(t("btn_save_strategy"), width="stretch", disabled=not preset_name):
        # 收集所有参数
        bt_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_result.json")
        saved_metrics = {}
        if os.path.exists(bt_file):
            try:
                with open(bt_file) as f: saved_metrics = json.load(f)
            except: pass
        presets[preset_name] = {
            "saved_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "params": {
                "coin": coin, "timeframe": timeframe, "leverage": leverage,
                "initial_capital": initial_capital,
                "tp_pct": tp_pct, "sl_pct": sl_pct,
                "bull_a": bull_a, "range_a": range_a, "bear_a": bear_a,
                "indicators": st.session_state.selected_indicators,
                "strategy_mode": strat_mode_key,
                "pos_mode": "fixed_risk" if use_fixed_risk else ("dynamic_stop" if use_dynamic_stop else "fixed_capital"),
                "risk_per_trade": risk_per_trade,
                "hedge_ratio": hedge_ratio,
            },
            "metrics": saved_metrics,
        }
        save_presets(presets)
        st.success(t("preset_saved", name=preset_name))

cur_params = {"coin": coin, "tf": timeframe, "lev": leverage,
              "tp": tp_pct, "sl": sl_pct, "indicators": list(st.session_state.selected_indicators.keys())}
st.sidebar.markdown(export_json(cur_params), unsafe_allow_html=True)
if st.sidebar.button(t("btn_logout")): st.session_state.logged_in = False; st.rerun()

# ============================================================
# 缓存数据加载 (避免每次切换参数都重新读取)
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def load_cached_15min(coin: str):
    """缓存15min数据加载, 1小时有效"""
    de = DataEngine()
    return de.load_15min(coin)

@st.cache_data(ttl=600, show_spinner=False)
def resample_cached(df_15m, period: str):
    """缓存重采样结果, 10分钟有效"""
    from pandas import DataFrame
    if period == "15m":
        return df_15m.copy()
    rule_map = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1D": "1d"}
    rule = rule_map.get(period, "4h")
    df = df_15m.resample(rule, label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "vol": "sum",
    }).dropna()
    df["quote_vol"] = ((df["high"] + df["low"] + df["close"]) / 3 * df["vol"])
    return df

# (旧AI模块已整合到 🤖 翔哥 AI 对话舱 Tab)
# 主界面头部
# ============================================================
set_lang(st.session_state.lang)
st.title(t("app_title"))

# ── 版本标识 (2026-08-11 新增) ──
_QUANTCODE_VERSION = "v3.2"
_QUANTCODE_BUILD = "2026-08-11"
try:
    import subprocess
    _git_hash = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        text=True, stderr=subprocess.DEVNULL
    ).strip()
except Exception:
    _git_hash = "unknown"
st.caption(t("version_label", version=_QUANTCODE_VERSION, commit=_git_hash, build=_QUANTCODE_BUILD))

# Tab 导航
if "active_tab" not in st.session_state: st.session_state.active_tab = "回测看板"
tc1, tc2, tc3, tc4 = st.columns([1, 1, 1, 1])
with tc1:
    if st.button(t("nav_backtest"), width="stretch",
                 type="primary" if "回测" in st.session_state.active_tab else "secondary"):
        st.session_state.active_tab = "回测看板"; st.rerun()
with tc2:
    if st.button(t("nav_ai_chat"), width="stretch",
                 type="primary" if "AI" in st.session_state.active_tab else "secondary"):
        st.session_state.active_tab = "AI 对话舱"; st.rerun()
with tc3:
    if st.button(t("nav_robustness"), width="stretch",
                 type="primary" if "鲁棒性" in st.session_state.active_tab else "secondary"):
        st.session_state.active_tab = "鲁棒性实验室"; st.rerun()
with tc4:
    if st.button(t("nav_live_trading"), width="stretch",
                 type="primary" if "交易中心" in st.session_state.active_tab else "secondary"):
        st.session_state.active_tab = "交易中心"; st.rerun()

st.divider()

# ============================================================
# ============================================================
# 统一 API 调用 (DeepSeek / OpenAI / Anthropic / Gemini)
# ============================================================
def _call_unified_api(messages: list, api_key: str, model_name: str, trading_notes: str) -> dict:
    import requests
    if trading_notes.strip():
        np = {"role": "system", "content": t("unified_api_prompt", notes=trading_notes.strip())}
        hs = any(m["role"] == "system" for m in messages)
        if hs:
            for m in messages:
                if m["role"] == "system": m["content"] = np["content"] + "\n\n" + m["content"]
        else: messages.insert(0, np)
    if "DeepSeek" in model_name:
        mdl = "deepseek-chat" if "V3" in model_name else "deepseek-reasoner"
        r = requests.post("https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": mdl, "messages": messages, "max_tokens": 2000, "temperature": 0.7}, timeout=45)
        if r.status_code == 200: d = r.json(); return {"success": True, "content": d["choices"][0]["message"]["content"], "model": d.get("model", mdl)}
        return {"success": False, "error": f"DeepSeek {r.status_code}: {r.text[:200]}"}
    if "OpenAI" in model_name or "GPT" in model_name:
        mdl = "gpt-4o" if "mini" not in model_name else "gpt-4o-mini"
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": mdl, "messages": messages, "max_tokens": 2000}, timeout=45)
        if r.status_code == 200: d = r.json(); return {"success": True, "content": d["choices"][0]["message"]["content"], "model": d.get("model", mdl)}
        return {"success": False, "error": f"OpenAI {r.status_code}: {r.text[:200]}"}
    if "Claude" in model_name or "Anthropic" in model_name:
        sm = next((m for m in messages if m["role"] == "system"), None)
        cm = [m for m in messages if m["role"] != "system"]
        body = {"model": "claude-sonnet-4-20250514", "max_tokens": 2000, "messages": cm}
        if sm: body["system"] = sm["content"]
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=body, timeout=45)
        if r.status_code == 200: d = r.json(); return {"success": True, "content": d["content"][0]["text"], "model": d.get("model", "claude")}
        return {"success": False, "error": f"Claude {r.status_code}: {r.text[:200]}"}
    if "Gemini" in model_name:
        mdl = "gemini-2.0-flash"
        contents = [{"role": "user" if m["role"] != "assistant" else "model", "parts": [{"text": m["content"]}]} for m in messages]
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"}, json={"contents": contents}, timeout=45)
        if r.status_code == 200:
            d = r.json(); txt = d["candidates"][0]["content"]["parts"][0]["text"]
            return {"success": True, "content": txt, "model": mdl}
        return {"success": False, "error": f"Gemini {r.status_code}: {r.text[:200]}"}
    return {"success": False, "error": f"Unknown model: {model_name}"}

# Tab 2: 翔哥 AI 研究仓（研究状态 + 记忆 + 持久化）
# ============================================================
if "AI" in st.session_state.active_tab:
    from ai_assistant import build_context, DEFAULT_TRADING_NOTES, get_quick_prompts
    import research_agent as ra
    from research_storage import db
    set_lang(st.session_state.lang)

    # ---- 持久化初始化 + 会话管理（退出重进仍保留） ----
    db.init_db()
    if "research_session_id" not in st.session_state:
        recent = db.list_sessions(1)
        st.session_state.research_session_id = recent[0]["id"] if recent else db.create_session()
    sid = st.session_state.research_session_id
    cur_session = db.get_session(sid)
    if "ai_chat_history" not in st.session_state:
        st.session_state.ai_chat_history = [
            {"role": m["role"], "content": m["content"]} for m in db.list_messages(sid)
        ]

    # ---- 研究状态展示 ----
    memory = ra.load_memory_summary()
    stats = ra.memory_stats(memory)
    st.markdown(f"### {t('research_current_project')}")
    gcol, bcol = st.columns([4, 1])
    with gcol:
        if "research_goal" not in st.session_state:
            st.session_state.research_goal = (cur_session["user_goal"] if cur_session and cur_session["user_goal"] else "")
        def _on_goal_change():
            db.update_session(st.session_state.research_session_id, user_goal=st.session_state.research_goal)
        st.text_input(t("research_goal_label"), key="research_goal",
                      placeholder=t("research_goal_placeholder"), on_change=_on_goal_change)
    with bcol:
        if st.button(t("research_new_session"), width="stretch"):
            st.session_state.research_session_id = db.create_session()
            st.session_state.ai_chat_history = []
            st.session_state.pop("research_goal", None)
            st.rerun()

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric(t("research_stats_hypotheses"), stats["hypotheses"])
    m2.metric(t("research_stats_passed"), stats["passed"])
    m3.metric(t("research_stats_failed"), stats["failed"])
    m4.metric(t("research_stats_pending"), stats["pending"])
    m5.metric(t("research_stats_experiments"), stats["experiments"])

    with st.expander(t("research_experiments_title"), expanded=False):
        if memory["experiments"]:
            st.dataframe(pd.DataFrame([{
                "策略": e["strategy_name"] or "未命名", "资产": e["asset"], "周期": e["timeframe"],
                "收益%": round(e["total_return"] or 0, 2), "Sharpe": round(e["sharpe"] or 0, 2),
                "MDD%": round(e["max_drawdown"] or 0, 2), "交易数": e["trade_count"], "评级": e["final_rating"] or "-",
            } for e in memory["experiments"]]), use_container_width=True)
        else:
            st.caption(t("research_no_data"))

    with st.expander(t("research_hypotheses_title"), expanded=False):
        if memory["hypotheses"]:
            st.dataframe(pd.DataFrame([{
                "假设": h["hypothesis_text"], "状态": h["status"], "创建": h["created_time"],
            } for h in memory["hypotheses"]]), use_container_width=True)
        else:
            st.caption(t("research_no_data"))

    st.caption(t("research_memory_note", n=len(memory["recent_sessions"]), h=stats["hypotheses"], e=stats["experiments"]))

    # 模型预设 (整合所有主流API)
    ALL_MODELS = {
        "DeepSeek-V3 (推荐)":  {"base": "https://api.deepseek.com",      "model": "deepseek-chat"},
        "DeepSeek-R1 (推理)":  {"base": "https://api.deepseek.com",      "model": "deepseek-reasoner"},
        "OpenAI GPT-4o":       {"base": "https://api.openai.com",        "model": "gpt-4o"},
        "OpenAI GPT-4o-mini":  {"base": "https://api.openai.com",        "model": "gpt-4o-mini"},
        "Anthropic Claude 3.5":{"base": "https://api.anthropic.com",     "model": "claude-sonnet-4-20250514"},
        "Google Gemini 2.0":   {"base": "https://generativelanguage.googleapis.com", "model": "gemini-2.0-flash"},
    }

    with st.expander(t("ai_config"), expanded=not st.session_state.get("ai_configured", False)):
        c1, c2 = st.columns(2)
        ai_key = c1.text_input(t("api_key_input"), type="password",
                               value=os.environ.get("AI_API_KEY", ""),
                               key="ai_main_key", placeholder=t("api_key_placeholder"))
        ai_model_name = c2.selectbox(t("model_select"), list(ALL_MODELS.keys()), index=0, key="ai_mdl",
            format_func=lambda x: {"DeepSeek-V3 (推荐)": t("model_dsv3"),
                                   "DeepSeek-R1 (推理)": t("model_dsr1")}.get(x, x))

        if "trading_notes" not in st.session_state:
            st.session_state.trading_notes = DEFAULT_TRADING_NOTES
        st.caption(t("trading_notes_caption"))
        trading_notes = st.text_area(t("notes_area"), value=st.session_state.trading_notes, height=100,
                                      key="tnotes", label_visibility="collapsed")
        st.session_state.trading_notes = trading_notes

    if not ai_key:
        st.info(t("ai_key_hint"))
    else:
        st.session_state.ai_configured = True

    # 快捷按钮行
    qcols = st.columns(5)
    quick_msgs = get_quick_prompts()
    quick_clicked = None
    for i, (label, prompt) in enumerate(quick_msgs):
        if qcols[i].button(label, width="stretch", key=f"qp_{i}", disabled=not ai_key):
            quick_clicked = prompt

    # 一键诊断按钮
    dcol, _ = st.columns([1, 3])
    if dcol.button(t("btn_diagnose"), width="stretch", type="primary", disabled=not ai_key):
        quick_clicked = t("diagnose_report_prompt")

    # 聊天记录
    for msg in st.session_state.ai_chat_history:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    user_msg = st.chat_input(t("chat_input_placeholder"), key="ai_main_chat", disabled=not ai_key)
    if quick_clicked:
        user_msg = quick_clicked

    if user_msg and ai_key:
        st.session_state.ai_chat_history.append({"role": "user", "content": user_msg})
        db.add_message(sid, "user", user_msg)
        if cur_session and not (cur_session["conversation_title"] or "").strip():
            db.update_session(sid, conversation_title=user_msg[:30])
        with st.chat_message("user"): st.write(user_msg)

        # 构建实时上下文
        try:
            df_ctx = load_cached_15min(coin)
            if not isinstance(df_ctx.index, pd.DatetimeIndex):
                df_ctx.index = pd.to_datetime(df_ctx.index)
            if hasattr(df_ctx.index, 'tz') and df_ctx.index.tz is not None:
                df_ctx.index = df_ctx.index.tz_localize(None)
            px = float(df_ctx['close'].iloc[-1])
            ind_ctx = {}
            for name, cfg in st.session_state.selected_indicators.items():
                if not cfg.get("enabled"): continue
                info = INDICATOR_REGISTRY.get(name)
                if not info: continue
                try:
                    dft = df_ctx.tail(200).copy()
                    info["compute"](dft, cfg.get("params", {}))
                    l = "_long" in dft.columns and dft["_long"].iloc[-1]
                    s = "_short" in dft.columns and dft["_short"].iloc[-1]
                    ind_ctx[name] = t("signal_long") if l else (t("signal_short") if s else t("signal_none"))
                except: ind_ctx[name] = t("diag_abnormal")
        except: px = 0; ind_ctx = {}

        bt = {}
        btf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_result.json")
        if os.path.exists(btf):
            try:
                with open(btf) as f: bt = json.load(f)
            except: pass

        system_prompt = ra.build_system_prompt(None, memory)
        try:
            system_prompt += "\n\n## 当前市场快照\n" + build_context(coin, timeframe, px, ind_ctx, bt)
        except Exception:
            pass
        msgs = [{"role": "system", "content": system_prompt}] + st.session_state.ai_chat_history[-8:]

        with st.chat_message("assistant"):
            with st.spinner(t("spinner_thinking")):
                result = _call_unified_api(msgs, ai_key, ai_model_name, trading_notes)
                if result["success"]:
                    st.write(result["content"])
                    st.caption(t("model_caption", model=result.get('model','?')))
                    st.session_state.ai_chat_history.append({"role": "assistant", "content": result["content"]})
                    db.add_message(sid, "assistant", result["content"])
                else:
                    st.error(result["error"])

    if st.session_state.ai_chat_history:
        if st.button(t("btn_clear_chat"), width="stretch"):
            st.session_state.ai_chat_history = []; st.rerun()

    st.stop()


# ============================================================
# Tab 3.5: 交易中心 Live Trading Dashboard (2026-08-13 新增)
# ============================================================
if "交易中心" in st.session_state.active_tab:
    from live_trading.trading_dashboard import render as render_live_trading
    set_lang(st.session_state.lang)
    render_live_trading()
    st.stop()


# ============================================================
# Tab 3: 策略鲁棒性分析实验室 (2026-08-11 新增)
# ============================================================
if "鲁棒性" in st.session_state.active_tab:
    from robustness_lab import RobustnessLab, SWEEP_DIMENSIONS, _dim_label
    set_lang(st.session_state.lang)

    st.title(t("robustness_title"))
    st.caption(t("robustness_subtitle"))

    # ── 前置条件检查 ──
    has_backtest = ("last_result" in st.session_state and
                    "last_engine_kwargs" in st.session_state and
                    "last_coin" in st.session_state)

    if not has_backtest:
        st.warning(t("warning_no_backtest"))
        st.info(t("hint_goto_backtest"))
        st.stop()

    # ── 基准参数摘要 ──
    last_ek = st.session_state.last_engine_kwargs
    last_sel = st.session_state.selected_indicators
    last_coin = st.session_state.last_coin
    last_tf = st.session_state.last_timeframe
    last_metrics = st.session_state.last_metrics

    with st.expander(t("robustness_baseline_params"), expanded=False):
        bc1, bc2, bc3, bc4 = st.columns(4)
        with bc1:
            st.metric(t("coin_select"), last_coin)
            st.metric(t("timeframe_select"), last_tf)
        with bc2:
            st.metric(t("leverage_label"), f"{last_ek.get('leverage', '?')}{t('leverage_unit')}")
            st.metric(t("capital_label"), f"${last_ek.get('initial_capital', '?'):,.0f}")
        with bc3:
            st.metric("TP/SL", f"{last_ek.get('tp_pct','?')}%/{last_ek.get('sl_pct','?')}%")
            st.metric(t("atr_sl_toggle"), t("yes") if last_ek.get('use_atr_sl') else t("no"))
        with bc4:
            st.metric(t("total_return"), f"{last_metrics.get('total_return', 0):+.2f}%")
            st.metric(t("sharpe_ratio"), f"{last_metrics.get('sharpe_ratio', 0):.3f}")

    # ── 活跃指标摘要 ──
    active_inds = [n for n, c in last_sel.items() if isinstance(c, dict) and c.get('enabled')]
    st.caption(f"{t('selected_indicators_label')} ({len(active_inds)}): " + ", ".join(active_inds[:8]) + ("..." if len(active_inds) > 8 else ""))

    st.divider()

    # ── 维度选择 ──
    st.subheader(t("robustness_dim_select"))
    st.caption(t("robustness_dim_desc"))

    dim_options = list(SWEEP_DIMENSIONS.keys())
    dim_labels = {k: _dim_label(k) for k, v in SWEEP_DIMENSIONS.items()}
    dim_labels_with_count = {
        k: f"{_dim_label(k)} ({len(v['values'])})" for k, v in SWEEP_DIMENSIONS.items()
    }

    dc1, dc2, dc3, dc4, dc5 = st.columns(5)
    selected_dims = []
    with dc1:
        if st.checkbox(dim_labels_with_count['leverage'], True, key="dim_leverage"): selected_dims.append('leverage')
    with dc2:
        if st.checkbox(dim_labels_with_count['ema'], True, key="dim_ema"): selected_dims.append('ema')
    with dc3:
        if st.checkbox(dim_labels_with_count['atr_stop'], True, key="dim_atr"): selected_dims.append('atr_stop')
    with dc4:
        if st.checkbox(dim_labels_with_count['fibonacci'], True, key="dim_fib"): selected_dims.append('fibonacci')
    with dc5:
        if st.checkbox(dim_labels_with_count['volume'], True, key="dim_vol"): selected_dims.append('volume')

    total_runs = sum(len(SWEEP_DIMENSIONS[d]['values']) for d in selected_dims)
    st.caption(t("robustness_est_time", count=total_runs, min=total_runs * 3, max=total_runs * 5))

    # ── 运行按钮 ──
    st.divider()
    run_col1, run_col2 = st.columns([2, 1])
    with run_col1:
        run_lab = st.button(t("btn_start_robustness"), width="stretch", type="primary",
                            disabled=len(selected_dims) == 0)
    with run_col2:
        st.caption(t("hint_robustness_tip"))

    if run_lab:
        # 构造 base_config
        # 重新加载数据
        with st.spinner(t("loading_data")):
            de = DataEngine()
            dfs = de.get_multi_timeframe(last_coin)
            df_raw = dfs.get(last_tf, dfs['4h'])
            # ── 日期索引标准化 (防御代码) ──
            if not isinstance(df_raw.index, pd.DatetimeIndex):
                df_raw.index = pd.to_datetime(df_raw.index)
            # 去除时区信息 (确保与 pd.Timestamp 比较兼容)
            if hasattr(df_raw.index, 'tz') and df_raw.index.tz is not None:
                df_raw.index = df_raw.index.tz_localize(None)
            df_raw = df_raw.sort_index()
            # ── 应用日期过滤 ──
            dr = st.session_state.get('date_range')
            if dr and dr[0] and dr[1]:
                try:
                    start_date = pd.Timestamp(dr[0])
                    end_date = pd.Timestamp(dr[1])
                    df_raw = df_raw.loc[start_date:end_date]
                except Exception as e:
                    st.warning(t("warning_date_filter_failed", error=e))
            if df_raw.empty:
                st.error(t("error_data_empty"))
                st.stop()

        # 构造 mf_params
        mf_enabled = last_sel.get('_regime_filter', True)
        v_ema_w = st.session_state.get('ema_w', 0.40)
        v_adx_w = st.session_state.get('adx_w', 0.35)
        v_adx_th = st.session_state.get('adx_th', 25)
        v_bull_th = st.session_state.get('bull_th', 0.30)

        base_config = {
            'engine_kwargs': dict(last_ek),
            'selected_indicators': copy.deepcopy(last_sel),
            'use_and': st.session_state.get('use_and', True) if 'use_and' in st.session_state else True,
            'mf_params': {'enabled': mf_enabled, 'ema_w': v_ema_w, 'adx_w': v_adx_w,
                          'adx_th': v_adx_th, 'bull_th': v_bull_th},
            'coin': last_coin,
            'df': df_raw,
        }

        # 运行扫描
        all_results = {}
        st.markdown("---")
        st.subheader(t("robustness_scan_progress"))

        progress_bar = st.progress(0)
        status_text = st.empty()
        total_all = sum(len(SWEEP_DIMENSIONS[d]['values']) for d in selected_dims)
        global_counter = [0]

        # DEBUG: 显示当前 run_sweep 函数签名
        import inspect as _inspect
        with st.expander(t("debug_info"), expanded=False):
            st.code(t("debug_signature", sig=str(_inspect.signature(RobustnessLab.run_sweep))) + "\n"
                    + t("debug_dynamic_type", type=str(type(DynamicStrategy))) + "\n"
                    + t("debug_git_commit", hash=_git_hash), language=None)

        for di, dim in enumerate(selected_dims):
            st.caption(t("scanning_progress", label=_dim_label(dim), current=di+1, total=len(selected_dims)))

            def make_progress(dim_label):
                def cb(cur, total, label):
                    global_counter[0] += 1
                    progress_bar.progress(min(global_counter[0] / total_all, 1.0))
                    status_text.caption(f"🔬 {dim_label}: {label} ({cur}/{total})")
                return cb

            try:
                sweeps = RobustnessLab.run_sweep(base_config, dim,
                                                       progress_callback=make_progress(_dim_label(dim)),
                                                       strategy_class=DynamicStrategy)
                all_results[dim] = sweeps
            except TypeError as te:
                st.error(t("err_type_error_call"))
                st.code(f"{t('debug_dim_label')}: {dim}\n"
                        f"{t('debug_error_label')}: {te}\n"
                        f"{t('debug_signature', sig=_inspect.signature(RobustnessLab.run_sweep))}\n"
                        f"{t('debug_check_strategy')} "
                        f"{'strategy_class' in _inspect.signature(RobustnessLab.run_sweep).parameters}")
                import traceback as _tb
                st.code(_tb.format_exc())
                all_results[dim] = [{'label': 'ERROR', 'error': str(te), 'metrics': None}]
            except Exception as e2:
                st.error(t("err_unexpected", type=type(e2).__name__, msg=e2))
                import traceback as _tb
                st.code(_tb.format_exc())
                all_results[dim] = [{'label': 'ERROR', 'error': str(e2), 'metrics': None}]

        progress_bar.progress(1.0)
        status_text.caption(t("scan_complete"))

        # 稳定性评分
        stability = RobustnessLab.stability_score(all_results)

        # DEBUG: 输出 stability 结构
        with st.expander(t("debug_stability_title"), expanded=False):
            st.write(f"stability.keys() = {list(stability.keys())}")
            st.write(f"overall = {stability.get('overall', 'MISSING')}")
            st.write(f"dim_scores keys = {list(stability.get('dim_scores', {}).keys())}")

        # ── 结果展示 ──
        st.markdown("---")
        st.subheader(t("robustness_result_analysis"))

        # 综合评分卡片 — 安全访问
        overall = stability.get('overall', 'unknown')
        stability_summary = RobustnessLab._build_summary(overall, stability.get('summary_dims', []))
        if overall == 'robust':
            st.success(f"✅ **{t('verdict_robust')}** — {stability_summary}")
        elif overall in ('overfit_risk', 'overfit'):
            st.error(f"⚠️ **{t('verdict_overfit')}** — {stability_summary}")
        elif overall == 'sensitive':
            st.warning(f"⚡ **{t('verdict_sensitive')}** — {stability_summary}")
        else:
            st.info(f"🔶 **{t('verdict_moderate')}** — {stability_summary}")

        # 每维度结果
        for dim in selected_dims:
            dim_def = SWEEP_DIMENSIONS[dim]
            sweeps = all_results[dim]
            ds = (stability.get('dim_scores') or {}).get(dim, {}) if isinstance(stability.get('dim_scores'), dict) else {}

            with st.expander(t("robustness_dimension_label", label=_dim_label(dim)) + f" — {ds.get('verdict','?')} | "
                            f"{t('robustness_best_label')}={ds.get('best','?')}({ds.get('best_return',0):+.1f}%) | "
                            f"CV={ds.get('cv',0):.3f}", expanded=True):
                # 参数收益矩阵
                mat = RobustnessLab.format_matrix(dim, sweeps)
                st.dataframe(mat.set_index(t('param_label')), width="stretch")

                # 如果存在错误，展示详细traceback
                errors_in_dim = [s for s in sweeps if s.get('error')]
                if errors_in_dim:
                    for ei, es in enumerate(errors_in_dim):
                        with st.expander(t("err_detail_fmt", label=es['label']), expanded=False):
                            st.code(es.get('traceback', es['error']))

                # 迷你折线图: 收益随参数变化（跳过错误项）
                returns = [s['total_return'] for s in sweeps if not s.get('error')]
                labels = [s['label'] for s in sweeps if not s.get('error')]
                if len(returns) >= 2:
                    chart_data = pd.DataFrame({f'{t("total_return")}%': returns}, index=labels)
                    st.line_chart(chart_data, width="stretch", height=200)

                # 稳定性指标
                dsc1, dsc2, dsc3 = st.columns(3)
                with dsc1:
                    st.metric(t("robustness_stability_cv"), f"{ds.get('cv', 0):.3f}",
                             delta=t("delta_stable") if ds.get('cv', 0) < 0.3 else t("delta_sensitive"))
                with dsc2:
                    st.metric(t("robustness_stability_range"), f"{ds.get('range_pct', 0):.1f}%")
                with dsc3:
                    verdict = ds.get('verdict', '?')
                    v_map = {'robust': '✅ ' + t('verdict_robust'), 'overfit': '⚠️ ' + t('verdict_overfit'),
                             'sensitive': '⚡ ' + t('verdict_sensitive'), 'moderate': '🔶 ' + t('verdict_moderate')}
                    st.metric(t("robustness_rating"), v_map.get(verdict, verdict))

        # 完整报告
        with st.expander(t("robustness_full_report"), expanded=False):
            report = RobustnessLab.generate_report(all_results, stability)
            st.markdown(report)

        st.success(t("success_robustness_done"))

        # 缓存结果
        st.session_state.lab_results = {'all_results': all_results, 'stability': stability}
    else:
        # 未运行: 检查是否有缓存结果
        if 'lab_results' in st.session_state:
            st.info(t("info_cached_result"))
            lr = st.session_state.lab_results
            all_results = lr.get('all_results', {})
            stability = lr.get('stability', {})

            overall = stability.get('overall', 'unknown')
            stability_summary = RobustnessLab._build_summary(overall, stability.get('summary_dims', []))
            if overall == 'robust':
                st.success(f"✅ **{t('verdict_robust')}** — {stability_summary}")
            elif overall in ('overfit_risk', 'overfit'):
                st.error(f"⚠️ **{t('verdict_overfit')}** — {stability_summary}")
            else:
                st.warning(f"⚡/🔶 **{t('verdict_sensitive')}** — {stability_summary}")

            for dim in all_results.keys():
                dim_def = SWEEP_DIMENSIONS.get(dim, {})
                sweeps = all_results.get(dim, [])
                ds = stability.get('dim_scores', {}).get(dim, {}) if isinstance(stability.get('dim_scores'), dict) else {}
                with st.expander(f"📐 {_dim_label(dim)} — {ds.get('verdict','?')}", expanded=False):
                    mat = RobustnessLab.format_matrix(dim, sweeps)
                    st.dataframe(mat.set_index(t('param_label')), width="stretch")

            with st.expander(t("robustness_full_report"), expanded=False):
                st.markdown(RobustnessLab.generate_report(all_results, stability))
        else:
            st.info(t("hint_select_dims"))

    # ── 参数组合优化 (独立于单维度扫描) ──
    if "鲁棒性" in st.session_state.active_tab:
        from robustness_lab import PARAM_COMBO_GRID

        st.divider()
        st.subheader(t("combo_title"))
        st.caption(t("combo_subtitle"))

        has_backtest_for_combo = ("last_result" in st.session_state and
                                   "last_engine_kwargs" in st.session_state and
                                   "last_coin" in st.session_state)

        if not has_backtest_for_combo:
            st.warning(t("warning_no_backtest"))
        else:
            combo_c1, combo_c2, combo_c3 = st.columns(3)
            with combo_c1:
                oos_ratio = st.slider(t("combo_oos_ratio"), 0.1, 0.5, 0.3, 0.05,
                                      help=t("combo_oos_help"))
            with combo_c2:
                min_trades = st.number_input(t("combo_min_trades"), 3, 20, 5,
                                             help=t("combo_min_trades_help"))
            with combo_c3:
                total_combos_preview = 1
                for d in PARAM_COMBO_GRID:
                    total_combos_preview *= len(PARAM_COMBO_GRID[d]['values'])
                st.metric(t("combo_total_combos"), total_combos_preview,
                          delta=t("combo_est_time", min=total_combos_preview * 2 * 4, max=total_combos_preview * 2 * 6))

            run_combo = st.button(t("btn_start_combo"), width="stretch", type="primary",
                                  key="run_combo_btn")

            if run_combo:
                # 加载数据
                with st.spinner(t("loading_data")):
                    de2 = DataEngine()
                    dfs2 = de2.get_multi_timeframe(st.session_state.last_coin)
                    tf2 = st.session_state.last_timeframe
                    df_raw2 = dfs2.get(tf2, dfs2['4h'])
                    if not isinstance(df_raw2.index, pd.DatetimeIndex):
                        df_raw2.index = pd.to_datetime(df_raw2.index)
                    if hasattr(df_raw2.index, 'tz') and df_raw2.index.tz is not None:
                        df_raw2.index = df_raw2.index.tz_localize(None)
                    df_raw2 = df_raw2.sort_index()
                    dr2 = st.session_state.get('date_range')
                    if dr2 and dr2[0] and dr2[1]:
                        try:
                            df_raw2 = df_raw2.loc[pd.Timestamp(dr2[0]):pd.Timestamp(dr2[1])]
                        except Exception:
                            pass

                last_ek2 = st.session_state.last_engine_kwargs
                last_sel2 = st.session_state.selected_indicators

                base_config2 = {
                    'engine_kwargs': dict(last_ek2),
                    'selected_indicators': copy.deepcopy(last_sel2),
                    'use_and': st.session_state.get('use_and', True) if 'use_and' in st.session_state else True,
                    'mf_params': {
                        'enabled': last_sel2.get('_regime_filter', True),
                        'ema_w': st.session_state.get('ema_w', 0.40),
                        'adx_w': st.session_state.get('adx_w', 0.35),
                        'adx_th': st.session_state.get('adx_th', 25),
                        'bull_th': st.session_state.get('bull_th', 0.30),
                    },
                    'coin': st.session_state.last_coin,
                    'df': df_raw2,
                }

                combo_progress = st.progress(0)
                combo_status = st.empty()

                def combo_progress_cb(cur, total, label):
                    combo_progress.progress(min(cur / total, 1.0))
                    combo_status.caption(f"🔬 {cur}/{total}: {label}")

                try:
                    combo_result = RobustnessLab.combo_optimize(
                        base_config2,
                        param_grid=PARAM_COMBO_GRID,
                        oos_ratio=oos_ratio,
                        min_trades=min_trades,
                        progress_callback=combo_progress_cb,
                        strategy_class=DynamicStrategy,
                    )

                    combo_progress.progress(1.0)
                    combo_status.caption(t("combo_complete"))

                    st.markdown("---")
                    st.subheader(t("combo_result_analysis"))

                    top10 = combo_result.get('top10', [])
                    all_combo = combo_result.get('combinations', [])

                    if top10:
                        # 统计
                        tc1, tc2, tc3, tc4 = st.columns(4)
                        with tc1:
                            st.metric(t("combo_scanned"), combo_result.get('total_combos', '?'))
                        with tc2:
                            st.metric(t("combo_valid"), len(all_combo))
                        with tc3:
                            rec_count = len([ct for ct in top10 if 'recommended' in ct.get('flags', [])])
                            st.metric(f"⭐{t('combo_recommended')}", rec_count)
                        with tc4:
                            stable_count = len([ct for ct in top10 if 'stable' in ct.get('flags', [])])
                            st.metric(f"🟢{t('combo_stable_region')}", stable_count)

                        # Top 10 表格
                        st.markdown(f"### {t('combo_top10_title')}")
                        table_df = RobustnessLab.combo_format_table(all_combo, top_n=10)
                        st.dataframe(table_df.set_index(t('combo_rank')), width="stretch",
                                     column_config={
                                         t('combo_flag'): st.column_config.TextColumn(t('combo_flag'), width='small'),
                                         t('combo_score'): st.column_config.ProgressColumn(t('combo_score'), min_value=0, max_value=100, format='%.0f'),
                                     })

                        # 评分维度说明
                        with st.expander(t("combo_scoring_rules"), expanded=False):
                            st.markdown(t("combo_scoring_table"))

                        # 标记图例
                        with st.expander(t("combo_flag_legend"), expanded=False):
                            st.markdown(t("combo_flag_legend_text"))

                        # 收益对比图
                        with st.expander(t("combo_is_oos_chart"), expanded=True):
                            if top10:
                                chart_is = [ct.get('is_return', 0) for ct in top10]
                                chart_oos = [ct.get('oos_return', 0) for ct in top10]
                                chart_labels = [ct['label'][:30] for ct in top10]
                                chart_df = pd.DataFrame({
                                    f'IS{t("total_return")}%': chart_is,
                                    f'OOS{t("total_return")}%': chart_oos,
                                }, index=chart_labels)
                                st.bar_chart(chart_df, width="stretch", height=300)

                        # 完整报告
                        with st.expander(t("combo_full_report"), expanded=False):
                            report = RobustnessLab.combo_generate_report(combo_result)
                            st.markdown(report)

                    else:
                        st.warning(t("combo_no_valid"))

                    # 缓存
                    st.session_state.combo_result = combo_result

                except Exception as combo_err:
                    st.error(t("err_unexpected", type=type(combo_err).__name__, msg=combo_err))
                    import traceback as _tb2
                    st.code(_tb2.format_exc())

            # 显示缓存结果
            elif 'combo_result' in st.session_state and st.session_state.combo_result:
                cr = st.session_state.combo_result
                top10_cached = cr.get('top10', [])
                if top10_cached:
                    st.info(t("info_cached_result"))
                    table_df2 = RobustnessLab.combo_format_table(cr.get('combinations', []), top_n=10)
                    st.dataframe(table_df2.set_index(t('combo_rank')), width="stretch")
                    st.caption(t("hint_rerun_combo"))

    st.stop()


# ============================================================
# Tab 1: 回测看板
# ============================================================
set_lang(st.session_state.lang)
p1, p2, p3, p4 = st.columns(4)
p1.metric(t("coin_select"), coin); p2.metric(t("timeframe_select"), timeframe); p3.metric(t("leverage_label"), f"{leverage}{t('leverage_unit')}"); p4.metric(t("capital_label"), f"${initial_capital:,}")

# 已选指标摘要
active_inds = [n for n, c in st.session_state.selected_indicators.items() if isinstance(c, dict) and c.get("enabled")]
if is_dual_leg:
    st.caption(t("dual_leg_caption"))
else:
    st.caption(t("active_inds_summary", n=len(active_inds), list=", ".join(active_inds[:10]) + ("..." if len(active_inds) > 10 else "")))

st.divider()
if submitted:
    # ── 需求六: 实时风险摘要卡 ──
    if not is_dual_leg:
        with st.expander(t("risk_config_title"), expanded=True):
            c1, c2, c3 = st.columns(3)
            # 列1: 仓位模式 + 止损方式
            with c1:
                st.caption(t("risk_position_mode"))
                if use_fixed_risk:
                    st.markdown(t("fixed_risk_desc"))
                elif use_dynamic_stop:
                    st.markdown(t("dynamic_stop_desc"))
                else:
                    st.markdown(t("fixed_capital_desc"))

                st.caption(t("leverage_label"))
                st.markdown(f"**{leverage}x**")

                st.caption(t("risk_per_trade_caption"))
                if use_fixed_risk or use_dynamic_stop:
                    max_loss = initial_capital * risk_per_trade / 100
                    st.markdown(t("risk_pct_approx", pct=risk_per_trade, loss=max_loss))
                else:
                    st.markdown("**N/A**")

            # 列2: 有效止损 + 覆盖关系
            with c2:
                st.caption(t("effective_sl_caption"))
                if use_atr_stop:
                    st.markdown(f"**ATR({atr_period_val}) × {atr_mult_val}**")
                    st.caption(t("fixed_sl_covered", pct=sl_pct))
                else:
                    sl_mode_label3 = t("sl_mode_margin") if "Margin" in sl_mode else t("sl_mode_price")
                    st.markdown(t("fixed_sl_pct", pct=sl_pct, mode=sl_mode_label3))
                    st.caption(t("no_override"))

                st.caption(t("position_pct_caption"))
                st.markdown(f"**{pyr_init_pct}%**")

            # 列3: 市场调整 + 仓位计算方式
            with c3:
                st.caption(t("market_adjust_caption"))
                st.markdown(t("market_adjust_fmt", bull=bull_a, range=range_a, bear=bear_a))

                st.caption(t("position_calc_caption"))
                if use_fixed_risk:
                    st.markdown(t("auto_calc_desc"))
                else:
                    st.markdown(t("fixed_formula_desc"))

    # 强制刷新最新行情数据
    with st.spinner(t("spinner_check_data")):
        try:
            from data_loader import ensure_data
            ensure_data(coin)
            st.cache_data.clear()  # 清除旧缓存, 强制重新加载
        except Exception as e:
            st.caption(t("data_refresh_skipped", e=e))

    # 加载数据
    with st.spinner(t("spinner_load_data")):
        de = DataEngine()
        all_tf = de.get_multi_timeframe(coin)
        df = all_tf.get(timeframe, all_tf['4h'])
        # ── 日期索引标准化 (防御代码) ──
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        if hasattr(df.index, 'tz') and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df.sort_index()
        # 数据范围校验
        st.caption(t("data_loaded_fmt", tf=timeframe, n=len(df), start=df.index[0], end=df.index[-1]))

        # 时间范围过滤
        dr = st.session_state.date_range
        if dr and dr[0] and dr[1]:
            try:
                # 向前多取warmup_bars根用于指标预热
                warmup_bars = max(200, int(len(df) * 0.05))
                dr_start = pd.Timestamp(dr[0])
                dr_end = pd.Timestamp(dr[1])
                warmup_start = dr_start - pd.Timedelta(hours=warmup_bars * {'5m':5/60,'15m':0.25,'1h':1,'4h':4,'1d':24}.get(timeframe,4))
                df = df.loc[warmup_start:dr_end]
            except Exception as e:
                st.warning(t("warning_date_filter_failed", error=e))
        if len(df) < 200:
            st.error(t("data_insufficient", n=len(df))); st.stop()

        # OOS
        if oos_enabled:
            sp = int(len(df) * oos_ratio / 100)
            df_train, df_test = df.iloc[:sp].copy(), df.iloc[sp:].copy()
        else:
            df_train, df_test = df.copy(), None

        # 策略
        # 注入共振因子到selected
        if resonance_enabled:
            rf = [f for f in [res_f1, res_f2, res_f3] if f]
            if rf:
                st.session_state.selected_indicators["_resonance_factors"] = rf
        elif "_resonance_factors" in st.session_state.selected_indicators:
            del st.session_state.selected_indicators["_resonance_factors"]

        # 交易方向参数
        # 策略模式参数注入
        st.session_state.selected_indicators["_strategy_mode"] = strat_mode_key
        st.session_state.selected_indicators["_hedge_ratio"] = hedge_ratio
        st.session_state.selected_indicators["_max_pyramid"] = max_pyramid
        st.session_state.selected_indicators["_pyramid_first"] = pyramid_first
        st.session_state.selected_indicators["_pyramid_step"] = pyramid_step_pct
        st.session_state.selected_indicators["_enable_pyramiding"] = enable_pyramiding if 'enable_pyramiding' in dir() else False
        st.session_state.selected_indicators["_pyr_init_pct"] = pyr_init_pct if 'pyr_init_pct' in dir() else 30
        # 闭环重构: 单笔建仓比例% 参与仓位公式
        st.session_state.selected_indicators["_init_alloc_pct"] = pyr_init_pct if 'pyr_init_pct' in dir() else 30
        st.session_state.selected_indicators["_pyr_trigger_pct"] = pyr_trigger_pct if 'pyr_trigger_pct' in dir() else 2.0
        st.session_state.selected_indicators["_pyr_add_pct"] = pyr_add_pct if 'pyr_add_pct' in dir() else 0.5
        st.session_state.selected_indicators["_pyr_max"] = pyr_max if 'pyr_max' in dir() else 3
        st.session_state.selected_indicators["_pyr_trail"] = pyr_trail if 'pyr_trail' in dir() else False
        st.session_state.selected_indicators["_trailing_pct"] = trailing_pct
        st.session_state.selected_indicators["_unlock_pct"] = unlock_pct
        st.session_state.selected_indicators["_unlock_indicator"] = unlock_indicator
        st.session_state.selected_indicators["_unlock_sl"] = unlock_sl if strat_mode_key == "unlocking" else 3.0
        st.session_state.selected_indicators["_unlock_tp"] = unlock_tp if 'unlock_tp' in dir() else 15.0
        st.session_state.selected_indicators["_use_ema_unlock"] = use_ema_unlock if 'use_ema_unlock' in dir() else True
        st.session_state.selected_indicators["_use_rsi_unlock"] = use_rsi_unlock if 'use_rsi_unlock' in dir() else False
        st.session_state.selected_indicators["_use_vol_unlock"] = use_vol_unlock if 'use_vol_unlock' in dir() else False
        st.session_state.selected_indicators["_funding_threshold"] = funding_threshold
        st.session_state.selected_indicators["_spot_tp"] = spot_tp if 'spot_tp' in dir() else 5.0
        st.session_state.selected_indicators["_spot_sl"] = spot_sl if 'spot_sl' in dir() else 2.0
        st.session_state.selected_indicators["_short_sl"] = short_sl if 'short_sl' in dir() else 3.0

        st.session_state.selected_indicators["_pos_mode"] = "fixed_risk" if use_fixed_risk else ("dynamic_stop" if use_dynamic_stop else "fixed_capital")
        st.session_state.selected_indicators["_risk_per_trade"] = risk_per_trade
        # 闭环重构: 牛/震/熊宏观系数从UI覆盖引擎默认值
        st.session_state.selected_indicators["_bull_alloc"] = bull_a if 'bull_a' in dir() else 1.0
        st.session_state.selected_indicators["_range_alloc"] = range_a if 'range_a' in dir() else 0.5
        st.session_state.selected_indicators["_bear_alloc"] = bear_a if 'bear_a' in dir() else 0.3
        # P0: TP/SL 模式
        st.session_state.selected_indicators["_tp_mode"] = "margin_pct" if "Margin" in tp_mode else "price_pct"
        st.session_state.selected_indicators["_sl_mode"] = "margin_pct" if "Margin" in sl_mode else "price_pct"
        # 需求4修复: ATR参数透传 (之前断链!)
        st.session_state.selected_indicators["_use_atr_sl"] = use_atr_stop if 'use_atr_stop' in dir() else False
        st.session_state.selected_indicators["_atr_period"] = atr_period_val if 'atr_period_val' in dir() else 14
        st.session_state.selected_indicators["_atr_mult"] = atr_mult_val if 'atr_mult_val' in dir() else 2.0
        st.session_state.selected_indicators["_trade_mode"] = trade_mode
        st.session_state.selected_indicators["_regime_filter"] = regime_filter_enabled

        # 加权打分模式参数
        if use_weighted:
            st.session_state.selected_indicators["_weighted"] = True
            st.session_state.selected_indicators["_weighted_threshold"] = weighted_threshold
        else:
            st.session_state.selected_indicators.pop("_weighted", None)

        strategy = DynamicStrategy(
            selected=st.session_state.selected_indicators,
            use_and=use_and,
            mf_params={"enabled": mf_enabled, "ema_w": ema_w, "adx_w": adx_w, "adx_th": adx_th, "bull_th": bull_th},
        )

        # 回测
        lock_bars = int(lock_days * 6) if timeframe == '4h' else int(lock_days * 24)
        # 策略模式专属参数
        _spot_tp_val = spot_tp if 'spot_tp' in dir() else tp_pct
        _spot_sl_val = spot_sl if 'spot_sl' in dir() else sl_pct
        _short_sl_val = short_sl if 'short_sl' in dir() else sl_pct
        strat_kwargs = dict(
            initial_capital=initial_capital, leverage=leverage, tp_pct=tp_pct, sl_pct=sl_pct,
            max_positions=1, bull_alloc=bull_a/100.0, range_alloc=range_a/100.0, bear_alloc=bear_a/100.0,
            lock_streak=int(lock_streak_val), lock_bars=lock_bars, cooldown_bars=2, verbose=False,
            trailing_pct=trailing_pct, strategy_mode=strat_mode_key,
            hedge_ratio=hedge_ratio, max_pyramid=max_pyramid,
            pyramid_step=pyramid_step_pct / 100.0, unlock_pct=unlock_pct / 100.0,
            spot_tp=_spot_tp_val, spot_sl=_spot_sl_val, short_sl=_short_sl_val,
            # P0新增: TP/SL模式 + 风控保护
            tp_mode=('margin_pct' if 'Margin' in tp_mode else 'price_pct'),
            sl_mode=('margin_pct' if 'Margin' in sl_mode else 'price_pct'),
            max_notional_pct=5.0,
            # 需求4修复: ATR参数构造函数透传
            use_atr_sl=use_atr_stop if 'use_atr_stop' in dir() else False,
            atr_period=atr_period_val if 'atr_period_val' in dir() else 14,
            atr_mult=atr_mult_val if 'atr_mult_val' in dir() else 2.0,
        )
        engine = BacktestEngineV2(**strat_kwargs)
        result = engine.run({coin: df_train}, strategy)
        metrics = PerformanceAnalyzer.analyze(result)

        # 保存到 session_state 供鲁棒性实验室等模块使用
        st.session_state.last_result = result
        st.session_state.last_metrics = metrics
        st.session_state.last_engine_kwargs = dict(strat_kwargs)
        st.session_state.last_coin = coin
        st.session_state.last_timeframe = timeframe

        # Phase 1: 记录回测实验到研究库（外挂持久化，失败不影响回测展示）
        try:
            from research_storage import db as _rdb
            _rdb.init_db()
            _enabled = [n for n, c in st.session_state.selected_indicators.items()
                        if isinstance(c, dict) and c.get("enabled")]
            _name = " + ".join(_enabled[:3]) if _enabled else "未命名策略"
            _rdb.add_experiment(
                strategy_name=_name,
                indicator_combination=_enabled,
                parameters={k: v for k, v in strat_kwargs.items()
                            if isinstance(v, (int, float, bool, str))},
                asset=coin, timeframe=timeframe, leverage=leverage,
                total_return=metrics.get("total_return"),
                annual_return=metrics.get("annual_return"),
                sharpe=metrics.get("sharpe_ratio"),
                max_drawdown=metrics.get("max_drawdown"),
                win_rate=metrics.get("win_rate"),
                trade_count=metrics.get("total_trades"),
            )
        except Exception:
            pass

    # OOS
    oos_m = None
    if oos_enabled and df_test is not None and len(df_test) > 200:
        with st.spinner(t("oos_spinner")):
            e2 = BacktestEngineV2(**strat_kwargs)
            r2 = e2.run({coin: df_test}, strategy)
            oos_m = PerformanceAnalyzer.analyze(r2)

    # === 零交易检查 (三层诊断) ===
    if metrics.get('total_trades', 0) == 0:
        # 安全计算最长指标周期
        def _get_max_period(indict):
            mp = 200
            if not isinstance(indict, dict): return mp
            for name, cfg in indict.items():
                if isinstance(cfg, dict):
                    if not cfg.get("enabled", True): continue
                    params = cfg.get("params", {})
                    if isinstance(params, dict):
                        for v in params.values():
                            if isinstance(v, (int, float)) and v > mp: mp = int(v)
                elif isinstance(cfg, (int, float)):
                    if cfg > mp: mp = int(cfg)
            return mp
        total_warmup = _get_max_period(st.session_state.get("selected_indicators", {}))
        st.warning(t("no_trades_triggered", n=f"{len(df_train):,}", warmup=total_warmup))
        st.caption(t("indicator_diagnosis"))

        diag_rows = []
        df_diag = df_train.tail(500).copy()
        for name, cfg in st.session_state.selected_indicators.items():
            if not isinstance(cfg, dict): continue
            if not cfg.get("enabled", True): continue
            info = INDICATOR_REGISTRY.get(name)
            if not info: continue
            try:
                df_tmp = df_diag.copy()
                info["compute"](df_tmp, cfg.get("params", {}))
                has_l = "_long" in df_tmp.columns
                has_s = "_short" in df_tmp.columns
                l_cnt = df_tmp["_long"].sum() if has_l else 0
                s_cnt = df_tmp["_short"].sum() if has_s else 0
                if l_cnt + s_cnt > 0:
                    status = t("diag_long_short", l=l_cnt, s=s_cnt)
                else:
                    status = t("diag_no_signal")
                diag_rows.append((_ind(name), status, t("diag_ok")))
            except Exception as e:
                diag_rows.append((_ind(name), t("diag_error", msg=str(e)[:60]), t("diag_abnormal")))

        if diag_rows:
            df_diag_out = pd.DataFrame(diag_rows, columns=[t("diag_ind_col"), t("diag_status_col"), t("diag_type_col")])
            st.dataframe(df_diag_out, width="stretch", hide_index=True)

        st.info(t("diag_suggestion"))
        st.stop()

    # === 指标卡片 ===
    st.subheader(t("backtest_result_title"))
    c = st.columns(7)
    c[0].metric(t("total_return"), f"{metrics.get('total_return',0):+.1f}%")
    c[1].metric(t("metric_annual_short"), f"{metrics.get('annual_return',0):+.1f}%",
                help=t("annual_help"))
    c[2].metric(t("max_drawdown"), f"{metrics.get('max_drawdown',0):.1f}%")
    c[3].metric(t("win_rate"), f"{metrics.get('win_rate',0):.1f}%")
    c[4].metric(t("profit_factor"), f"{metrics.get('profit_factor',0):.2f}" if metrics.get('profit_factor') != float('inf') else "inf")
    c[5].metric(t("metric_trades_short"), metrics.get('total_trades', 0))
    # 新增风险指标
    st.caption(t("metrics_caption", sharpe=metrics.get('sharpe_ratio',0), sortino=metrics.get('sortino_ratio',0),
                 calmar=metrics.get('calmar_ratio',0),
                 losses=metrics.get('max_consecutive_losses',0), recovery=metrics.get('recovery_factor',0)))
    # 用真实时间戳算回测时长 (不依赖K线数)
    ec = result.get('equity_curve', [])
    if ec and len(ec) > 1:
        real_start = pd.Timestamp(ec[0]['timestamp'])
        real_end = pd.Timestamp(ec[-1]['timestamp'])
        real_days = (real_end - real_start).days
        real_yrs = round(real_days / 365.25, 1)
    else:
        real_yrs = metrics.get('years', 0); real_days = int(real_yrs * 365.25)
    st.caption(t("backtest_duration_fmt", yrs=real_yrs, days=real_days))
    final_eq = result.get('final_equity', initial_capital)
    c[6].metric(t("final_equity"), f"${final_eq:,.0f}")

    # Delta暴露曲线 & 仓位状态 (多Leg模式)
    portfolio_data = result.get('portfolio_curve', [])
    if portfolio_data:
        st.subheader(t("delta_exposure_title"))
        delta_times = [p['timestamp'] for p in portfolio_data]
        delta_vals = [p['net_delta'] for p in portfolio_data]
        states = [p.get('state', '?') for p in portfolio_data]

        fig_delta = go.Figure()
        fig_delta.add_trace(go.Scattergl(x=delta_times, y=delta_vals, mode='lines',
                                          name='Net Delta', line=dict(color='#60a5fa', width=2),
                                          fill='tozeroy', fillcolor='rgba(96,165,250,0.1)'))
        fig_delta.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5,
                             annotation_text="Delta Neutral")
        # 标记状态切换
        prev_s = None
        for i, s in enumerate(states):
            if s != prev_s and i > 0:
                fig_delta.add_vline(x=delta_times[i], line_dash="dot",
                                     line_color="#eab308", opacity=0.5,
                                     annotation_text=s)
            prev_s = s
        fig_delta.update_layout(height=250, template="plotly_dark",
                                 margin=dict(l=0,r=0,t=0,b=0),
                                 paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
        st.plotly_chart(fig_delta, width="stretch",
                        config={'responsive': True, 'displayModeBar': False})

    # 多空统计
    closed_trades = result.get('closed_trades', [])
    long_trades = [t for t in closed_trades if t.get('side') == 'LONG']
    short_trades = [t for t in closed_trades if t.get('side') == 'SHORT']
    long_wr = len([t for t in long_trades if t.get('pnl_pct', 0) > 0]) / len(long_trades) * 100 if long_trades else 0
    short_wr = len([t for t in short_trades if t.get('pnl_pct', 0) > 0]) / len(short_trades) * 100 if short_trades else 0
    st.caption(t("long_short_stats_fmt", long=len(long_trades), long_wr=long_wr, short=len(short_trades), short_wr=short_wr))

    if oos_m:
        st.subheader(t("oos_title"))
        # 折算年化后再对比 (消除时长差异)
        train_yrs = metrics.get('years', 1)
        test_yrs = oos_m.get('years', 1)
        train_ret = metrics['total_return'] / 100.0
        test_ret = oos_m['total_return'] / 100.0
        train_ann = ((1 + train_ret) ** (1 / max(train_yrs, 0.1)) - 1) * 100 if train_ret > -1 else -100
        test_ann = ((1 + test_ret) ** (1 / max(test_yrs, 0.1)) - 1) * 100 if test_ret > -1 else -100
        ann_decay = test_ann - train_ann

        oc1, oc2, oc3, oc4, oc5, oc6 = st.columns(6)
        oc1.metric(t("oos_train_annual"), f"{train_ann:+.1f}%")
        oc2.metric(t("oos_test_annual"), f"{test_ann:+.1f}%",
                   delta=f"{ann_decay:+.1f}%", delta_color="inverse")
        oc3.metric(t("oos_train_return"), f"{metrics['total_return']:+.1f}%")
        oc4.metric(t("oos_test_return"), f"{oos_m['total_return']:+.1f}%")
        oc5.metric(t("oos_train_wr"), f"{metrics.get('win_rate',0):.1f}%")
        oc6.metric(t("oos_test_wr"), f"{oos_m.get('win_rate',0):.1f}%")

        # 判定逻辑: 测试亏钱或年化衰减>50%才算过拟合
        if test_ret < 0:
            oos_status = t("oos_status_severe")
        elif train_ann > 0 and test_ann < train_ann * 0.5:
            oos_status = t("oos_status_moderate")
        elif train_ann > 0 and test_ann >= train_ann * 0.8:
            oos_status = t("oos_status_pass")
        else:
            oos_status = t("oos_status_slight")
        st.caption(t("oos_eval_fmt", status=oos_status, train=train_yrs, test=test_yrs, decay=ann_decay))

        # 智能实盘建议卡片
        if oos_m:
            test_wr = oos_m.get('win_rate', 0)
            test_dd = oos_m.get('max_drawdown', 100)
            test_ann_val = test_ann
            if test_ann_val > 80 and test_dd < 25:
                st.success(t("oos_advice_excellent"))
            elif test_wr < 35:
                st.warning(t("oos_advice_low_wr"))
            if test_dd > 50:
                st.error(t("oos_advice_high_dd"))

    # === 权益曲线 ===
    st.subheader(t("equity_curve_title"))
    ec = result.get('equity_curve', [])
    if ec:
        times = [e['timestamp'] for e in ec]; eqs = [e['equity'] for e in ec]
        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scattergl(x=times, y=eqs, mode='lines', name=t("equity_trace_name"),
                                     line=dict(color='#818cf8', width=2),
                                     fill='tozeroy', fillcolor='rgba(129,140,248,0.1)'))
        fig_eq.add_hline(y=initial_capital, line_dash="dash", line_color="gray")
        fig_eq.update_layout(height=350, template="plotly_dark", margin=dict(l=0,r=0,t=0,b=0),
                              paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
        st.plotly_chart(fig_eq, width="stretch")

    # === K线图 (带缩放按钮) ===
    st.subheader(t("kline_title"))
    # 缩放按钮
    zc1, zc2, zc3, zc4, zc5, zc6 = st.columns([1,1,1,1,1,2])
    zoom_n = min(2000, len(df_train))  # 默认最多2000根, 点[全部]显示所有
    tf_hours = {"5m": 1/12, "15m": 0.25, "1h": 1, "4h": 4, "1d": 24}
    if zc1.button(t("zoom_1m"), width="stretch", key="z1m"): zoom_n = int(30 * 24 / tf_hours.get(timeframe, 4))
    if zc2.button(t("zoom_6m"), width="stretch", key="z6m"): zoom_n = int(180 * 24 / tf_hours.get(timeframe, 4))
    if zc3.button(t("zoom_1y"), width="stretch", key="z1y"): zoom_n = int(365 * 24 / tf_hours.get(timeframe, 4))
    if zc4.button(t("zoom_3y"), width="stretch", key="z3y"): zoom_n = int(1095 * 24 / tf_hours.get(timeframe, 4))
    if zc5.button(t("zoom_all"), width="stretch", key="zall"): zoom_n = len(df_train)
    zc6.caption(t("zoom_display_fmt", n=min(zoom_n, len(df_train))))

    df_show = df_train.tail(zoom_n)
    fig_kl = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.7, 0.3])

    # 计算成交额 (Quote Volume)
    df_show["quote_vol"] = ((df_show["high"] + df_show["low"] + df_show["close"]) / 3 * df_show["vol"])

    fig_kl.add_trace(go.Candlestick(
        x=df_show.index, open=df_show['open'], high=df_show['high'],
        low=df_show['low'], close=df_show['close'],
        name=t("kline_candle_name"), increasing_line_color='#26a69a', decreasing_line_color='#ef5350',
        showlegend=False,
        hovertemplate=t("kline_hovertemplate"),
    ), row=1, col=1)

    # 叠加EMA
    if 'ema_fast' in df_show.columns:
        fig_kl.add_trace(go.Scattergl(x=df_show.index, y=df_show['ema_fast'], mode='lines',
                                     line=dict(color='#FFD700', width=1), name=t("ema_fast_name")), row=1, col=1)
        fig_kl.add_trace(go.Scattergl(x=df_show.index, y=df_show['ema_slow'], mode='lines',
                                     line=dict(color='#FF6B6B', width=1), name=t("ema_slow_name")), row=1, col=1)

    # 布林带
    if 'bb_upper' in df_show.columns:
        fig_kl.add_trace(go.Scattergl(x=df_show.index, y=df_show['bb_upper'], mode='lines',
                                     line=dict(color='gray', width=0.5, dash='dot'), name=t("bb_upper_name")), row=1, col=1)
        fig_kl.add_trace(go.Scattergl(x=df_show.index, y=df_show['bb_lower'], mode='lines',
                                     line=dict(color='gray', width=0.5, dash='dot'), name=t("bb_lower_name")), row=1, col=1)

    # 交易标记
    trades_list = result.get('trades', [])
    closed = [t for t in trades_list if t.get('reason') in ('TP', 'SL', 'EOD')]
    ts_start = str(df_show.index[0]); ts_end = str(df_show.index[-1])
    buy_t, buy_p = [], []; sell_t, sell_p = [], []
    for tr in closed:
        ot = tr.get('open_time', '')
        if ts_start <= ot <= ts_end:
            if tr['side'] == 'LONG': buy_t.append(ot); buy_p.append(tr['entry'])
            else: sell_t.append(ot); sell_p.append(tr['entry'])
    if buy_t:
        fig_kl.add_trace(go.Scattergl(x=buy_t, y=buy_p, mode='markers',
                                     marker=dict(symbol='triangle-up', size=12, color='#22c55e'),
                                     name=t("long_trades")), row=1, col=1)
    if sell_t:
        fig_kl.add_trace(go.Scattergl(x=sell_t, y=sell_p, mode='markers',
                                     marker=dict(symbol='triangle-down', size=12, color='#ef4444'),
                                     name=t("short_trades")), row=1, col=1)

    colors = ['#26a69a' if df_show['close'].iloc[i] >= df_show['open'].iloc[i] else '#ef5350' for i in range(len(df_show))]
    fig_kl.add_trace(go.Bar(
        x=df_show.index, y=df_show['vol'], marker_color=colors, opacity=0.4,
        name=t("volume_name"), showlegend=False,
        hovertemplate=t("volume_hovertemplate"),
        customdata=df_show['quote_vol'],
    ), row=2, col=1)

    # XY轴: 贴边零空白, 精确匹配显示范围
    x0, x1 = df_show.index[0], df_show.index[-1]
    y_lo = df_show['low'].min() * 0.999; y_hi = df_show['high'].max() * 1.001

    fig_kl.update_layout(
        height=500, template="plotly_dark", margin=dict(l=0,r=0,t=0,b=0),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        hovermode='x unified', xaxis_rangeslider_visible=False,
    )
    fig_kl.update_xaxes(type='date', range=[x0, x1], autorange=False, row=1, col=1)
    fig_kl.update_xaxes(type='date', range=[x0, x1], autorange=False, row=2, col=1)
    fig_kl.update_yaxes(title_text=t("yaxis_price"), range=[y_lo, y_hi], autorange=False, row=1, col=1)
    fig_kl.update_yaxes(title_text=t("yaxis_volume"), autorange=True, row=2, col=1)
    st.plotly_chart(fig_kl, width="stretch",
                    config={'responsive': True, 'displayModeBar': False,
                            'scrollZoom': False})

    # 共振对比 (仅当启用共振时显示)
    if resonance_enabled and res_f1 and closed:
        strong = [t for t in closed if t.get('resonance_score', 0) >= 3]
        weak = [t for t in closed if 1 <= t.get('resonance_score', 0) <= 2]
        if strong or weak:
            st.subheader(t("resonance_title"))
            rc1, rc2, rc3, rc4 = st.columns(4)
            def _wr(ts): return len([t for t in ts if t.get('pnl_pct',0)>0])/len(ts)*100 if ts else 0
            def _ar(ts): return sum(t.get('pnl_pct',0) for t in ts) if ts else 0
            rc1.metric(t("resonance_type"), t("resonance_strong"), delta=t("trades_count_unit", n=len(strong)))
            rc2.metric(t("win_rate"), f"{_wr(strong):.0f}%", delta=t("resonance_wr_vs_all", d=_wr(strong)-_wr(closed)))
            rc3.metric(t("cum_pnl"), f"{_ar(strong):+.1f}%")
            rc4.metric(t("resonance_weak"), t("trades_count_unit", n=len(weak)), delta=t("resonance_weak_wr", wr=_wr(weak)))

    # 年度筛选 + 月度热力图
    if closed:
        # 按年份汇总
        closed_df = pd.DataFrame(closed)
        closed_df['year'] = pd.to_datetime(closed_df['close_time']).dt.year
        closed_df['month'] = pd.to_datetime(closed_df['close_time']).dt.month
        years_available = sorted(closed_df['year'].unique())

        st.subheader(t("annual_monthly_title"))
        yr_cols = st.columns([1, 4])
        selected_year = yr_cols[0].selectbox(t("year_filter"), [t("year_all")] + [str(y) for y in years_available], key="year_filter")
        # 月度热力图
        monthly = closed_df.pivot_table(values='pnl_pct', index='year', columns='month', aggfunc='sum')
        # 补全12个月
        for m in range(1, 13):
            if m not in monthly.columns: monthly[m] = 0.0
        monthly = monthly[sorted(monthly.columns)]
        monthly['YTD'] = monthly.sum(axis=1)
        # 颜色映射
        def color_monthly(val):
            if pd.isna(val) or val == 0: return ''
            intensity = min(abs(val) / 20.0, 1.0)
            if val > 0: return f'background-color: rgba(34,197,94,{intensity:.2f})'
            return f'background-color: rgba(239,68,68,{intensity:.2f})'
        if hasattr(monthly.style, 'map'):
            styled = monthly.style.format("{:+.1f}%").map(color_monthly)
        else:
            styled = monthly.style.format("{:+.1f}%").applymap(color_monthly)
        st.dataframe(styled, width="stretch")

        # 年度过滤 → 重新计算指标
        if selected_year != t("year_all"):
            yr = int(selected_year)
            yr_closed = closed_df[closed_df['year'] == yr]
            yr_wins = yr_closed[yr_closed['pnl_pct'] > 0]
            yr_pnl = yr_closed['pnl_pct'].sum()
            st.caption(t("year_summary_fmt", year=selected_year, n=len(yr_closed),
                         wr=len(yr_wins)/max(len(yr_closed),1)*100, pnl=yr_pnl))

    # ================================================================
    # 收益质量审计模块 (2026-08-11 新增)
    # 所有数据来自真实 trade history
    # ================================================================
    if closed:
        st.divider()
        audit = PerformanceAnalyzer.quality_audit(result, metrics)

        # 折叠: 默认展开核心指标
        with st.expander(t("quality_audit_title"), expanded=False):
            st.caption(t("quality_audit_caption"))

            # ── Tab1: 年度表现明细表 ──
            annual = audit.get('annual_table', [])
            if annual:
                st.markdown(t("annual_detail_table"))
                yr_df = pd.DataFrame(annual)
                yr_df.columns = [t("col_year"), t("col_return_pct"), t("col_maxdd_pct"), t("col_trades"), t("col_wr_pct"), t("col_pf")]
                st.dataframe(yr_df.set_index(t("col_year")), width="stretch")

            # ── Tab2: 交易贡献分析 ──
            contrib = audit.get('contribution', {})
            st.markdown(t("contribution_analysis"))
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric(t("top1_profit"), f"${contrib.get('top1_amount', 0):+,.0f}",
                         delta=t("pct_of_total_profit", pct=contrib.get('top1_pct', 0)))
            with c2:
                st.metric(t("top5_profit"), f"${contrib.get('top5_amount', 0):+,.0f}",
                         delta=t("pct_of_total_profit", pct=contrib.get('top5_pct', 0)))
            with c3:
                st.metric(t("top10_profit"), f"${contrib.get('top10_amount', 0):+,.0f}",
                         delta=t("pct_of_total_profit", pct=contrib.get('top10_pct', 0)))

            # 集中度风险提示
            level = contrib.get('level', t('unanalyzed'))
            warn = contrib.get('warning', 'grey')
            if warn == 'green':
                st.success(t("concentration_green", level=level))
            elif warn == 'yellow':
                st.warning(t("concentration_yellow", level=level, pct=contrib.get('top5_pct',0)))
            elif warn == 'red':
                st.error(t("concentration_red", level=level, pct=contrib.get('top5_pct',0)))
            else:
                st.caption(t("concentration_plain", level=level))

            # ── Tab3: 极端收益剔除测试 ──
            removal = audit.get('extreme_removal', [])
            if removal:
                st.markdown(t("extreme_removal_title"))
                st.caption(t("extreme_removal_caption"))
                rem_rows = []
                for r in removal:
                    rem_rows.append({
                        t('col_operation'): r['label'],
                        t('col_removed_amount'): f"${r['removed_amount']:+,.0f}",
                        t('col_remaining_return'): f"{r['new_return']:+.2f}%",
                        t('col_remaining_annual'): f"{r['new_annual']:+.2f}%",
                        t('col_remaining_maxdd'): f"{r['new_maxdd']:.2f}%",
                    })
                st.dataframe(pd.DataFrame(rem_rows), width="stretch", hide_index=True)

            # ── Tab4: 风险贡献分析 ──
            risk = audit.get('risk_contrib', {})
            st.markdown(t("risk_contrib_title"))
            rc1, rc2, rc3, rc4 = st.columns(4)
            with rc1:
                st.metric(t("max_single_loss"), f"${risk.get('max_single_loss', 0):+,.0f}")
            with rc2:
                st.metric(t("max_consecutive_losses"), t("trades_count_unit", n=risk.get('max_consecutive_losses', 0)))
            with rc3:
                period = risk.get('max_consecutive_period', 'N/A')
                st.metric(t("max_consecutive_period"), period[:10] if period != 'N/A' else 'N/A')
            with rc4:
                st.metric(t("top5_loss_pct"), f"{risk.get('top5_loss_pct', 0):.1f}%",
                         delta=t("amount_delta", amt=risk.get('top5_loss_amount', 0)))

            # ── Tab5: 交易统计 ──
            tstats = audit.get('trade_stats', {})
            st.markdown(t("trade_stats_title"))
            tc1, tc2, tc3, tc4 = st.columns(4)
            with tc1:
                st.metric(t("avg_win"), f"${tstats.get('avg_win', 0):+,.0f}")
                st.metric(t("max_profit"), f"${tstats.get('max_win', 0):+,.0f}")
            with tc2:
                st.metric(t("avg_loss"), f"${tstats.get('avg_loss', 0):+,.0f}")
                st.metric(t("max_loss"), f"${tstats.get('max_loss', 0):+,.0f}")
            with tc3:
                avg_h = tstats.get('avg_hold_hours', 0)
                max_h = tstats.get('max_hold_hours', 0)
                if avg_h >= 24:
                    st.metric(t("avg_hold"), t("days_unit", d=avg_h/24))
                else:
                    st.metric(t("avg_hold"), t("hours_unit", h=avg_h))
                if max_h >= 24:
                    st.metric(t("max_hold"), t("days_unit", d=max_h/24))
                else:
                    st.metric(t("max_hold"), t("hours_unit", h=max_h))
            with tc4:
                st.metric(t("total_win_trades"), f"{(pd.DataFrame(closed)['pnl'] > 0).sum() if closed else 0}")
                st.metric(t("total_loss_trades"), f"{(pd.DataFrame(closed)['pnl'] <= 0).sum() if closed else 0}")

    # ================================================================
    # 交易频率分析 (2026-08-11 新增)
    # ================================================================
    if closed:
        st.divider()
        st.subheader(t("trade_freq_title"))
        freq = PerformanceAnalyzer.trading_frequency(result)
        fc1, fc2, fc3, fc4 = st.columns(4)
        with fc1:
            st.metric(t("total_trades_count"), t("trades_count_unit", n=freq['total_trades']))
        with fc2:
            st.metric(t("avg_per_year"), t("per_year_unit", n=freq['avg_per_year']))
        with fc3:
            st.metric(t("avg_per_month"), t("per_month_unit", n=freq['avg_per_month']))
        with fc4:
            avg_yr = freq['avg_per_year']
            if avg_yr < 6: delta = t("freq_low_risk")
            elif avg_yr >= 100: delta = t("freq_high_risk")
            else: delta = t("freq_moderate")
            st.metric(t("freq_class_label"), freq['level'], delta=delta)
        st.caption(t("freq_period_fmt", period=freq['period'], years=freq['total_years']))

    # ================================================================
    # 市场状态归因分析 (2026-08-11 新增)
    # ================================================================
    if closed:
        st.divider()
        st.subheader(t("market_attr_title"))
        attr = PerformanceAnalyzer.market_attribution(result)

        # 三列: 牛市/震荡/熊市
        ac1, ac2, ac3 = st.columns(3)
        with ac1:
            st.markdown(t("market_bull_title"))
            st.metric(t("cum_pnl"), f"${attr['bull_pnl']:+,.0f}",
                     delta=t("pct_share", pct=attr['bull_pct']))
            st.metric(t("col_trades"), t("trades_count_unit", n=attr['bull_trades']))
            st.metric(t("win_rate"), f"{attr['bull_wr']:.0f}%")
        with ac2:
            st.markdown(t("market_range_title"))
            st.metric(t("cum_pnl"), f"${attr['range_pnl']:+,.0f}",
                     delta=t("pct_share", pct=attr['range_pct']))
            st.metric(t("col_trades"), t("trades_count_unit", n=attr['range_trades']))
            st.metric(t("win_rate"), f"{attr['range_wr']:.0f}%")
        with ac3:
            st.markdown(t("market_bear_title"))
            st.metric(t("cum_pnl"), f"${attr['bear_pnl']:+,.0f}",
                     delta=t("pct_share", pct=attr['bear_pct']))
            st.metric(t("col_trades"), t("trades_count_unit", n=attr['bear_trades']))
            st.metric(t("win_rate"), f"{attr['bear_wr']:.0f}%")

        # 归因结论
        st.info(t("attr_conclusion_fmt", conclusion=attr['conclusion']))

    # ================================================================
    # 策略评价报告 (2026-08-11 新增 — 自动生成)
    # ================================================================
    if closed:
        st.divider()
        st.subheader(t("strategy_summary_title"))
        summary = PerformanceAnalyzer.generate_strategy_summary(result, metrics, audit)
        with st.expander(t("auto_summary_expander"), expanded=True):
            # 语言无关行样式匹配：用翻译模板前缀定位，杜绝硬编码中文导致英文失效
            def _sp(key):
                tpl = t(key)
                i = tpl.find('{')
                return (tpl if i == -1 else tpl[:i]).rstrip()
            _styles = [
                (_sp('summary_strategy_type'), 'bold'),
                (_sp('summary_return_feature'), '📈'),
                (_sp('summary_frequency'), '⏱️'),
                (_sp('summary_return_source'), '📊'),
                (_sp('summary_return_judge'), 'caption'),
                (_sp('summary_concentration'), '🎯'),
                (_sp('summary_extreme_dependent'), '⚠️'),
                (_sp('summary_extreme_stable'), '⚠️'),
                (_sp('summary_risk_feature'), '🛡️'),
                (_sp('summary_suggestion_label'), 'warn'),
            ]
            for line in summary.split('\n'):
                style = next((s for p, s in _styles if line.startswith(p)), None)
                if style == 'bold':
                    st.markdown(f"**{line}**")
                elif style == 'caption':
                    st.caption(f"   {line}")
                elif style == 'warn':
                    st.warning(f"💡 {line}")
                elif style:
                    st.markdown(f"{style} {line}")
                else:
                    st.text(line)

    # ================================================================
    # 参数一致性审计 (2026-08-11 新增)
    # ================================================================
    if closed:
        st.divider()
        st.subheader(t("param_audit_title"))
        p_report = PerformanceAnalyzer.param_audit_report(result, metrics)

        pc1, pc2 = st.columns(2)
        with pc1:
            st.metric(t("ui_params_total"), t("params_count_unit", n=p_report['total_params']),
                     delta=t("all_checked"))
            st.caption(t("params_list", list=", ".join(p_report['ui_params'])))
        with pc2:
            confirmed = len(p_report['engine_params'])
            anomalies = len(p_report['anomalies'])
            if anomalies == 0:
                st.metric(t("engine_params_confirmed"), t("items_effective", n=confirmed),
                         delta=t("no_anomaly"))
            else:
                st.metric(t("engine_params_confirmed"), t("items_effective", n=confirmed),
                         delta=t("anomaly_count", n=anomalies))

        # 引擎参数详情
        if p_report['engine_params']:
            with st.expander(t("engine_params_detail"), expanded=False):
                for ep in p_report['engine_params']:
                    st.caption(f"✓ {ep}")

        # 异常报告
        if p_report['anomalies']:
            st.error(t("param_anomaly_warning"))
            for a in p_report['anomalies']:
                st.warning(f"• {a}")

    # 交易记录
    if closed:
        st.subheader(t("recent_trades_title"))
        rows = [{t("col_time"): tr.get('close_time','')[:16], t("col_coin"): tr.get('coin',coin),
                 t("col_side"): tr.get('side','?'), t("col_entry"): f"${tr.get('entry',0):.2f}",
                 t("col_exit"): f"${tr.get('exit',0):.2f}", t("col_reason"): tr.get('reason','?'),
                 t("col_pnl_pct"): f"{tr.get('pnl_pct',0):+.2f}%"} for tr in closed[-15:]]
        st.dataframe(pd.DataFrame(rows[::-1]), width="stretch", height=300)

    # === AI 量化审计分析 ===
    if "show_audit" not in st.session_state:
        st.session_state.show_audit = False
    if "audit_cache" not in st.session_state:
        st.session_state.audit_cache = None
    if "wf_cache" not in st.session_state:
        st.session_state.wf_cache = None

    st.divider()
    st.caption(t("audit_section_caption"))
    c_audit, c_wf = st.columns(2)

    # --- 按钮1: 基础审计 (不含Walk Forward) ---
    if c_audit.button(t("btn_audit"), width="stretch", type="primary"):
        st.session_state.show_audit = True
        st.session_state.wf_cache = None  # 清除旧WF数据
        with st.spinner(t("spinner_audit")):
            from audit_engine import AuditEngine, StrategyScorer, AIReportGenerator
            audit_data = AuditEngine.audit(result, metrics)
            prog_scores = StrategyScorer.score(audit_data)
            total_score = prog_scores['total_program_score']

            # 得分卡片
            sc1, sc2, sc3, sc4, sc5 = st.columns(5)
            sc1.metric(t("total_score"), f"{total_score}/70",
                       delta=t("score_excellent") if total_score >= 55 else (t("score_good") if total_score >= 40 else t("score_needs_improve")))
            sc2.metric(t("return_ability"), f"{prog_scores['return_score']}/20")
            sc3.metric(t("score_risk"), f"{prog_scores['risk_score']}/20")
            sc4.metric(t("reward_risk"), f"{prog_scores['reward_risk_score']}/15")
            sc5.metric(t("stability_realism"), f"{prog_scores['stability_score']+prog_scores['realism_score']}/15")

            # 审计详情
            with st.expander(t("audit_detail_expander"), expanded=True):
                ad = audit_data
                st.caption(t("return_annual_fmt", ret=ad['returns']['annual_return'], std=ad['returns']['return_stability_std']))
                st.caption(t("risk_dd_fmt", dd=ad['risk']['max_drawdown'], sharpe=ad['risk']['sharpe_ratio'],
                             sortino=ad['risk']['sortino_ratio'], calmar=ad['risk']['calmar_ratio']))
                st.caption(t("trading_audit_fmt", n=ad['trading']['total_trades'], wr=ad['trading']['win_rate'],
                             long=ad['trading']['long_trades'], short=ad['trading']['short_trades'],
                             liq=ad['trading']['liquidation_count']))
                st.caption(t("stability_audit_fmt", risk=ad['stability']['overfitting_risk']))
                st.caption(t("realism_fmt", grade=ad['realism']['grade'], score=ad['realism']['realism_score'],
                             max=ad['realism']['max_score']))

                # 优势/劣势
                summary = ad['summary']
                if summary['strengths']:
                    st.success(t("strengths_label", list="; ".join(summary['strengths'])))
                if summary['weaknesses']:
                    st.warning(t("weaknesses_label", list="; ".join(summary['weaknesses'])))

            # AI报告 (可选)
            ai_k = os.environ.get("AI_API_KEY", "")
            if ai_k:
                with st.expander(t("ai_report_expander"), expanded=False):
                    with st.spinner(t("spinner_ai")):
                        ai_result = AIReportGenerator.build_report(
                            ai_k, audit_data, metrics,
                            t("model_dsv3")
                        )
                        if ai_result.get('success'):
                            st.markdown(ai_result['report'])
                        else:
                            st.caption(t("ai_report_skipped", error=ai_result.get('error','')))
            # 缓存审计结果到session_state
            st.session_state.audit_cache = {
                'audit_data': audit_data,
                'prog_scores': prog_scores,
                'total_score': total_score,
                'metrics': metrics,
            }
            st.rerun()

    # --- 按钮2: Walk Forward 滚动样本外测试 ---
    if c_wf.button(t("btn_walk_forward"), width="stretch"):
        st.session_state.show_audit = True
        with st.spinner(t("spinner_wf")):
            from walk_forward import WalkForwardAnalyzer
            from audit_engine import AuditEngine, StrategyScorer, AIReportGenerator

            # 自动检测数据年份范围
            try:
                de_wf = DataEngine()
                all_tf_wf = de_wf.get_multi_timeframe(coin)
                df_wf = all_tf_wf.get(timeframe, all_tf_wf['4h'])
                if not isinstance(df_wf.index, pd.DatetimeIndex):
                    df_wf.index = pd.to_datetime(df_wf.index)
                if hasattr(df_wf.index, 'tz') and df_wf.index.tz is not None:
                    df_wf.index = df_wf.index.tz_localize(None)
                data_start_yr = df_wf.index.min().year
                data_end_yr = df_wf.index.max().year
                # 确保至少有4年数据做滚动窗口
                if data_end_yr - data_start_yr < 3:
                    st.warning(t("data_range_insufficient", start=data_start_yr, end=data_end_yr))
                    st.stop()
                wf_start = max(data_start_yr, 2017)
                wf_end = min(data_end_yr, 2026)
            except Exception as e:
                st.error(t("data_load_failed", e=e)); st.stop()

            st.caption(t("wf_window_caption", start=wf_start, end=wf_end))

            # 构造 engine_kwargs (复用当前回测参数)
            lock_bars = int(lock_days * 6) if timeframe == '4h' else int(lock_days * 24)
            _spot_tp_val = spot_tp if 'spot_tp' in dir() else tp_pct
            _spot_sl_val = spot_sl if 'spot_sl' in dir() else sl_pct
            _short_sl_val = short_sl if 'short_sl' in dir() else sl_pct
            wf_engine_kwargs = dict(
                initial_capital=initial_capital, leverage=leverage,
                tp_pct=tp_pct, sl_pct=sl_pct,
                max_positions=1, bull_alloc=bull_a/100.0, range_alloc=range_a/100.0, bear_alloc=bear_a/100.0,
                lock_streak=int(lock_streak_val), lock_bars=lock_bars, cooldown_bars=2, verbose=False,
                trailing_pct=trailing_pct, strategy_mode=strat_mode_key,
                hedge_ratio=hedge_ratio, max_pyramid=max_pyramid,
                pyramid_step=pyramid_step_pct / 100.0, unlock_pct=unlock_pct / 100.0,
                spot_tp=_spot_tp_val, spot_sl=_spot_sl_val, short_sl=_short_sl_val,
                # P0新增
                tp_mode=('margin_pct' if 'Margin' in tp_mode else 'price_pct'),
                sl_mode=('margin_pct' if 'Margin' in sl_mode else 'price_pct'),
                max_notional_pct=5.0,
            )

            # 运行 Walk Forward
            wf_result = WalkForwardAnalyzer.analyze(
                coin=coin, timeframe=timeframe,
                start_year=wf_start, end_year=wf_end,
                strategy_config=st.session_state.selected_indicators,
                engine_kwargs=wf_engine_kwargs,
                mf_params={"enabled": mf_enabled, "ema_w": ema_w, "adx_w": adx_w,
                           "adx_th": adx_th, "bull_th": bull_th},
                use_and=use_and,
                strategy_class=DynamicStrategy,
                train_years=3, test_years=1,
            )

            if wf_result.get("error"):
                st.warning(t("wf_run_error", error=wf_result['error']))

            # 执行含 WF 数据的审计
            audit_data = AuditEngine.audit(result, metrics, wf_result)
            prog_scores = StrategyScorer.score(audit_data)
            total_score = prog_scores['total_program_score']

            # 得分卡片
            sc1, sc2, sc3, sc4, sc5 = st.columns(5)
            sc1.metric(t("total_score"), f"{total_score}/70",
                       delta=t("score_excellent") if total_score >= 55 else (t("score_good") if total_score >= 40 else t("score_needs_improve")))
            sc2.metric(t("return_ability"), f"{prog_scores['return_score']}/20")
            sc3.metric(t("score_risk"), f"{prog_scores['risk_score']}/20")
            sc4.metric(t("reward_risk"), f"{prog_scores['reward_risk_score']}/15")
            sc5.metric(t("stability_realism"), f"{prog_scores['stability_score']+prog_scores['realism_score']}/15")

            # === 🆕 Walk Forward 详细结果 ===
            with st.expander(t("wf_detail_expander"), expanded=True):
                wf_score = wf_result.get("score", {})
                adx = wf_result.get("adx_analysis", {})

                # Walk Forward 综合评分卡片
                wc1, wc2, wc3, wc4 = st.columns(4)
                wc1.metric(t("wf_score_label"), f"{wf_score.get('walk_forward_score', 0)}/100",
                           delta=wf_score.get("overfitting_risk", "?"))
                wc2.metric(t("oos_avg_annual"), f"{wf_score.get('avg_oos_return', 0):+.1f}%")
                wc3.metric(t("profitable_windows"), f"{wf_score.get('profitable_windows', 0)}/{wf_score.get('total_windows', 0)}")
                wc4.metric(t("trend_dependency"), adx.get("trend_dependency", "?"))

                # 窗口明细表
                windows = wf_result.get("windows", [])
                if windows:
                    wf_rows = []
                    for w in windows:
                        if w.get("error"):
                            wf_rows.append({
                                t("col_window"): w.get("window", "?"),
                                t("col_train_period"): w.get("train_range", ""),
                                t("col_test_period"): w.get("test_range", ""),
                                t("col_train_annual"): "-",
                                t("col_test_annual"): "-",
                                t("col_test_wr"): "-",
                                t("col_status"): f"❌ {w['error']}",
                            })
                            continue
                        train = w.get("train") or {}
                        test = w.get("test") or {}
                        test_ret = test.get("annual_return", 0)
                        wf_rows.append({
                            t("col_window"): w.get("window", "?"),
                            t("col_train_period"): w.get("train_range", ""),
                            t("col_test_period"): w.get("test_range", ""),
                            t("col_train_annual"): f"{train.get('annual_return', 0):+.1f}%",
                            t("col_test_annual"): f"{test_ret:+.1f}%",
                            t("col_test_wr"): f"{test.get('win_rate', 0):.1f}%",
                            t("col_test_dd"): f"{test.get('max_drawdown', 0):.1f}%",
                            t("col_status"): t("wf_profitable") if test_ret > 0 else t("wf_loss"),
                        })
                    st.dataframe(pd.DataFrame(wf_rows), width="stretch", hide_index=True)

                # ADX 趋势分析
                st.divider()
                st.caption(t("adx_analysis_caption"))
                adx_c1, adx_c2, adx_c3 = st.columns(3)
                adx_c1.metric(t("avg_adx_winning"), f"{adx.get('avg_adx_winning', 0):.1f}",
                              delta=t("trades_count_unit", n=adx.get('winning_trade_count', 0)))
                adx_c2.metric(t("avg_adx_losing"), f"{adx.get('avg_adx_losing', 0):.1f}",
                              delta=t("trades_count_unit", n=adx.get('losing_trade_count', 0)))
                adx_ratio = adx.get('adx_ratio', 1.0)
                adx_c3.metric(t("adx_ratio_label"), f"{adx_ratio:.2f}",
                              delta=t("trend_dependent") if adx_ratio > 1.3 else t("balanced"))
                st.caption(adx.get("dependency_detail", ""))

            # 审计详情 (含WF数据)
            with st.expander(t("audit_detail_expander"), expanded=False):
                ad = audit_data
                st.caption(t("return_annual_fmt", ret=ad['returns']['annual_return'], std=ad['returns']['return_stability_std']))
                st.caption(t("risk_dd_fmt", dd=ad['risk']['max_drawdown'], sharpe=ad['risk']['sharpe_ratio'],
                             sortino=ad['risk']['sortino_ratio'], calmar=ad['risk']['calmar_ratio']))
                st.caption(t("trading_audit_fmt_simple", n=ad['trading']['total_trades'], wr=ad['trading']['win_rate']))
                # WF 稳定性
                st.caption(t("wf_robustness_fmt", n=ad['stability']['walk_forward_robustness'],
                             max=ad['stability'].get('walk_forward_max', 100),
                             of=of_risk_label(ad['stability']['overfitting_risk']),
                             td=trend_dep_label(ad['stability'].get('trend_dependency', 'unknown'))))
                summary = ad['summary']
                if summary['strengths']:
                    st.success(t("strengths_label", list="; ".join(summary['strengths'])))
                if summary['weaknesses']:
                    st.warning(t("weaknesses_label", list="; ".join(summary['weaknesses'])))

            # AI报告 (含WF数据)
            ai_k = os.environ.get("AI_API_KEY", "")
            if ai_k:
                with st.expander(t("ai_report_expander_wf"), expanded=False):
                    with st.spinner(t("spinner_ai")):
                        ai_result = AIReportGenerator.build_report(
                            ai_k, audit_data, metrics,
                            t("model_dsv3")
                        )
                        if ai_result.get('success'):
                            st.markdown(ai_result['report'])
                        else:
                            st.caption(t("ai_report_skipped", error=ai_result.get('error','')))

            # 缓存
            st.session_state.audit_cache = {
                'audit_data': audit_data,
                'prog_scores': prog_scores,
                'total_score': total_score,
                'metrics': metrics,
            }
            st.session_state.wf_cache = {
                'wf_result': wf_result,
                'audit_data': audit_data,
                'prog_scores': prog_scores,
            }
            st.rerun()

    if not st.session_state.get("show_audit"):
        st.caption(t("audit_hint"))

    # === AI 策略诊断 (保留原有) ===
    with st.expander(t("ai_diag_expander"), expanded=False):
        if st.button(t("btn_gen_diag"), width="stretch"):
            ai_k = os.environ.get("AI_API_KEY", "")
            if not ai_k:
                st.warning(t("warn_config_api"))
            else:
                with st.spinner(t("spinner_ai_analyzing")):
                    ytd = ""
                    if closed:
                        try:
                            ydf = pd.DataFrame(closed)
                            ydf['year'] = pd.to_datetime(ydf['close_time']).dt.year
                            for yr, grp in ydf.groupby('year'):
                                ytd += t("ytd_line_fmt", year=yr, pnl=grp['pnl_pct'].sum(), n=len(grp))
                        except: pass
                    diag_prompt = t("diag_prompt_template",
                                    coin=coin, tf=timeframe, lev=leverage,
                                    total=metrics.get('total_return',0), annual=metrics.get('annual_return',0),
                                    dd=metrics.get('max_drawdown',0), sharpe=metrics.get('sharpe_ratio',0),
                                    wr=metrics.get('win_rate',0), trades=metrics.get('total_trades',0), ytd=ytd)
                    result = _call_unified_api(
                        [{"role": "user", "content": diag_prompt}],
                        ai_k, t("model_dsv3"), st.session_state.get("trading_notes", ""))
                    if result["success"]:
                        st.markdown(result["content"])
                    else:
                        st.error(result["error"])

# === 审计报告持久视图 ===
if st.session_state.get("show_audit") and st.session_state.get("audit_cache"):
    ac = st.session_state.audit_cache
    wf_cache = st.session_state.get("wf_cache")

    st.subheader(t("audit_report_title"))
    if st.button(t("btn_back_to_dashboard"), width="stretch"):
        st.session_state.show_audit = False
        st.rerun()

    # 🆕 报告类型标识
    if wf_cache:
        st.caption(t("report_type_wf"))

    sc1, sc2, sc3, sc4, sc5 = st.columns(5)
    total_score = ac['total_score']
    sc1.metric(t("total_score"), f"{total_score}/70",
               delta=t("score_excellent") if total_score >= 55 else (t("score_good") if total_score >= 40 else t("score_needs_improve")))
    ps = ac['prog_scores']
    sc2.metric(t("return_ability"), f"{ps['return_score']}/20")
    sc3.metric(t("risk_control"), f"{ps['risk_score']}/20")
    sc4.metric(t("reward_risk"), f"{ps['reward_risk_score']}/15")
    sc5.metric(t("stability_realism"), f"{ps['stability_score']+ps['realism_score']}/15")

    # === 🆕 Walk Forward 持久展示 ===
    if wf_cache:
        wf_result = wf_cache.get('wf_result', {})
        wf_score = wf_result.get("score", {})
        adx = wf_result.get("adx_analysis", {})

        with st.expander(t("wf_detail_expander"), expanded=True):
            # 评分卡片
            wc1, wc2, wc3, wc4 = st.columns(4)
            wc1.metric(t("wf_score_label"), f"{wf_score.get('walk_forward_score', 0)}/100",
                       delta=of_risk_label(wf_score.get("overfitting_risk", "unknown")))
            wc2.metric(t("oos_avg_annual"), f"{wf_score.get('avg_oos_return', 0):+.1f}%")
            wc3.metric(t("profitable_windows"), f"{wf_score.get('profitable_windows', 0)}/{wf_score.get('total_windows', 0)}")
            wc4.metric(t("trend_dependency"), trend_dep_label(adx.get("trend_dependency", "unknown")))

            # 窗口表
            windows = wf_result.get("windows", [])
            if windows:
                wf_rows = []
                for w in windows:
                    if w.get("error"):
                        wf_rows.append({
                            t("col_window"): w.get("window", "?"), t("col_train_period"): w.get("train_range", ""),
                            t("col_test_period"): w.get("test_range", ""),
                            t("col_train_annual"): "-", t("col_test_annual"): "-", t("col_test_wr"): "-",
                            t("col_status"): f"❌ {w['error']}",
                        })
                        continue
                    train = w.get("train") or {}
                    test = w.get("test") or {}
                    test_ret = test.get("annual_return", 0)
                    wf_rows.append({
                        t("col_window"): w.get("window", "?"),
                        t("col_train_period"): w.get("train_range", ""),
                        t("col_test_period"): w.get("test_range", ""),
                        t("col_train_annual"): f"{train.get('annual_return', 0):+.1f}%",
                        t("col_test_annual"): f"{test_ret:+.1f}%",
                        t("col_test_wr"): f"{test.get('win_rate', 0):.1f}%",
                        t("col_test_dd"): f"{test.get('max_drawdown', 0):.1f}%",
                        t("col_status"): t("wf_profitable") if test_ret > 0 else t("wf_loss"),
                    })
                st.dataframe(pd.DataFrame(wf_rows), width="stretch", hide_index=True)

            # ADX 分析
            st.divider()
            st.caption(t("adx_analysis_caption"))
            adx_c1, adx_c2, adx_c3 = st.columns(3)
            adx_c1.metric(t("avg_adx_winning"), f"{adx.get('avg_adx_winning', 0):.1f}",
                          delta=t("winning_trades_delta", n=adx.get('winning_trade_count', 0)))
            adx_c2.metric(t("avg_adx_losing"), f"{adx.get('avg_adx_losing', 0):.1f}",
                          delta=t("losing_trades_delta", n=adx.get('losing_trade_count', 0)))
            adx_ratio = adx.get('adx_ratio', 1.0)
            adx_c3.metric(t("adx_ratio_label"), f"{adx_ratio:.2f}",
                          delta=t("trend_dependent") if adx_ratio > 1.3 else t("balanced"))
            st.caption(adx.get("dependency_detail", ""))

    # 审计详情
    ad = ac['audit_data']
    with st.expander(t("audit_detail_expander"), expanded=not wf_cache):
        st.caption(t("return_annual_fmt", ret=ad['returns']['annual_return'],
                     std=ad['returns']['return_stability_std']))
        st.caption(t("risk_dd_fmt", dd=ad['risk']['max_drawdown'],
                     sharpe=ad['risk']['sharpe_ratio'], sortino=ad['risk']['sortino_ratio'],
                     calmar=ad['risk']['calmar_ratio']))
        st.caption(t("trading_audit_fmt", n=ad['trading']['total_trades'],
                     wr=ad['trading']['win_rate'], long=ad['trading']['long_trades'],
                     short=ad['trading']['short_trades'], liq=ad['trading']['liquidation_count']))
        # 稳定性 (含WF)
        wf_robust = ad['stability'].get('walk_forward_robustness', 0)
        if wf_robust > 0:
            st.caption(t("wf_robustness_fmt", n=wf_robust,
                         max=ad['stability'].get('walk_forward_max', 100),
                         of=of_risk_label(ad['stability']['overfitting_risk']),
                         td=trend_dep_label(ad['stability'].get('trend_dependency', 'unanalyzed'))))
        else:
            st.caption(t("stability_audit_fmt", risk=of_risk_label(ad['stability']['overfitting_risk'])))
        st.caption(t("realism_fmt", grade=ad['realism']['grade'],
                     score=ad['realism']['realism_score'], max=ad['realism']['max_score']))
        summary = ad['summary']
        if summary['strengths']: st.success(t("strengths_label", list="; ".join(summary['strengths'])))
        if summary['weaknesses']: st.warning(t("weaknesses_label", list="; ".join(summary['weaknesses'])))

    st.stop()

elif not submitted:
    st.info(t("backtest_hint"))

    # === 图表专属周期切换器 (独立于回测周期) ===
    chart_periods = ["5m", "15m", "1h", "4h", "1D"]
    if "chart_period" not in st.session_state:
        st.session_state.chart_period = timeframe  # 初始同步侧边栏

    st.divider()
    st.subheader(t("data_preview_title", coin=coin))
    cc_cols = st.columns([1, 1, 1, 1, 1, 3])
    for i, period in enumerate(chart_periods):
        col = cc_cols[i]
        is_active = st.session_state.chart_period == period
        if col.button(period, width="stretch",
                      type="primary" if is_active else "secondary",
                      key=f"cp_{period}"):
            st.session_state.chart_period = period
            st.rerun()

    try:
        # 缓存加载 + 缓存重采样
        df_15m = load_cached_15min(coin)
        if not isinstance(df_15m.index, pd.DatetimeIndex):
            df_15m.index = pd.to_datetime(df_15m.index)
        if hasattr(df_15m.index, 'tz') and df_15m.index.tz is not None:
            df_15m.index = df_15m.index.tz_localize(None)
        dr = st.session_state.get('date_range')
        if dr and dr[0] and dr[1]:
            try:
                df_15m = df_15m.loc[pd.Timestamp(dr[0]):pd.Timestamp(dr[1])]
            except Exception:
                pass  # 日期过滤失败则使用全部数据

        chart_period = st.session_state.chart_period
        df_pv = resample_cached(df_15m, chart_period)

        print(f"[预览] {coin} {chart_period}: {len(df_pv)}根, "
              f"{df_pv.index.min()} ~ {df_pv.index.max()}", flush=True)

        if len(df_pv) < 2:
            st.warning(t("insufficient_range"))
            st.stop()

        # 摘要指标
        mc1, mc2, mc3, mc4, mc5 = st.columns(5)
        mc1.metric(t("kline_count"), f"{len(df_pv):,}")
        mc2.metric(t("start_label"), str(df_pv.index[0])[:16])
        mc3.metric(t("end_label"), str(df_pv.index[-1])[:16])
        chg = (df_pv['close'].iloc[-1] / df_pv['close'].iloc[0] - 1) * 100
        mc4.metric(t("change_pct"), f"{chg:+.1f}%")
        mc5.metric(t("latest_price"), f"${df_pv['close'].iloc[-1]:.2f}")

        # === 双子图: 最多显示1000根, 防卡顿 ===
        MAX_BARS = 1000
        df_show = df_pv if len(df_pv) <= MAX_BARS else df_pv.tail(MAX_BARS)
        if len(df_pv) > MAX_BARS:
            st.caption(t("preview_bars_fmt", n=len(df_pv), max=MAX_BARS))
        # DEBUG
        july_bars = len(df_pv["2026-07-01":"2026-07-31"]) if len(df_pv) > 0 else 0
        st.sidebar.info(t("sidebar_data_debug", n=len(df_pv), start=df_pv.index[0], end=df_pv.index[-1], july=july_bars))

        fig_pv = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            vertical_spacing=0.02, row_heights=[0.75, 0.25],
        )

        # 上图: OHLC 蜡烛
        fig_pv.add_trace(go.Candlestick(
            x=df_show.index,
            open=df_show['open'], high=df_show['high'],
            low=df_show['low'], close=df_show['close'],
            name=t("kline_candle_name"),
            increasing_line_color='#26a69a', decreasing_line_color='#ef5350',
            showlegend=False,
            hovertemplate=t("kline_hovertemplate"),
        ), row=1, col=1)

        # 下图: 成交量柱 (红绿)
        vol_colors = [
            '#26a69a' if df_show['close'].iloc[i] >= df_show['open'].iloc[i] else '#ef5350'
            for i in range(len(df_show))
        ]
        has_qv = "quote_vol" in df_show.columns
        fig_pv.add_trace(go.Bar(
            x=df_show.index, y=df_show['vol'],
            marker_color=vol_colors, opacity=0.45,
            name=t("volume_name"),
            hovertemplate=(t("volume_hovertemplate") if has_qv else t("volume_hovertemplate_basic")),
            customdata=df_show['quote_vol'] if has_qv else None,
            showlegend=False,
        ), row=2, col=1)

        # 布局: X轴贴边(无空白), Y轴自适应
        x0, x1 = df_show.index[0], df_show.index[-1]
        y_lo = df_show['low'].min() * 0.999
        y_hi = df_show['high'].max() * 1.001

        fig_pv.update_layout(
            height=450, template="plotly_dark",
            margin=dict(l=0, r=0, t=10, b=0),
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
            hovermode='x unified', xaxis_rangeslider_visible=False,
        )
        # X轴: date类型, 精确匹配显示范围的起止, 零padding
        fig_pv.update_xaxes(type='date', range=[x0, x1], autorange=False,
                            row=1, col=1, title_text="")
        fig_pv.update_xaxes(type='date', range=[x0, x1], autorange=False,
                            row=2, col=1, title_text="")
        # Y轴: 自适应价格范围
        fig_pv.update_yaxes(title_text=t("price_usd"), range=[y_lo, y_hi],
                            autorange=False, row=1, col=1)
        fig_pv.update_yaxes(title_text=t("volume_axis"), autorange=True, row=2, col=1)

        st.plotly_chart(fig_pv, width="stretch",
                        config={'responsive': True, 'displayModeBar': False,
                                'scrollZoom': False})

    except FileNotFoundError as e:
        st.warning(t("file_not_ready"))
        st.caption(t("detail_label", error=e))
    except Exception as e:
        st.warning(t("preview_load_failed", e=e))
