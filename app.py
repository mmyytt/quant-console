"""
马总量化控制台 — 百种指标积木 + AI策略导师
============================================================
启动: streamlit run app.py
"""
import streamlit as st
import pandas as pd, numpy as np, os, sys, time, json, base64
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

# ============================================================
# 登录
# ============================================================
AUTH_CONFIG = {"enabled": True, "users": {"xiangge": "quant2024", "admin": "admin123"}}

def check_login():
    if not AUTH_CONFIG["enabled"]: return True
    if "logged_in" not in st.session_state: st.session_state.logged_in = False
    if st.session_state.logged_in: return True
    st.markdown("<h1 style='text-align:center;margin-top:60px'>马总量化控制台</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align:center;opacity:0.5'>请输入账号密码</p>", unsafe_allow_html=True)
    _, c, _ = st.columns([1, 2, 1])
    with c:
        u = st.text_input("账号", key="lu"); p = st.text_input("密码", type="password", key="lp")
        if st.button("登录", use_container_width=True, type="primary"):
            if AUTH_CONFIG["users"].get(u) == p: st.session_state.logged_in = True; st.rerun()
            else: st.error("账号或密码错误")
    return False

def export_json(params):
    s = json.dumps({"exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "params": params}, ensure_ascii=False, indent=2)
    b = base64.b64encode(s.encode()).decode()
    return f'<a href="data:application/json;base64,{b}" download="strategy_config.json" style="text-decoration:none;">Download strategy_config.json</a>'

# ============================================================
# 页面配置 + CSS
# ============================================================
st.set_page_config(page_title="马总量化", page_icon="📊", layout="wide", initial_sidebar_state="expanded")
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
            stale_coins.append(f"{c}({f['gap_hours']:.0f}h前)")
    if stale_coins:
        st.error(f"实时行情 API 请求失败，当前显示为本地历史缓存！ "
                 f"过期币种: {', '.join(stale_coins)}")

check_data_freshness()

if st.sidebar.button("🧹 强制清全部缓存", use_container_width=True):
    st.cache_data.clear(); st.cache_resource.clear()
    st.rerun()

# ============================================================
# 统一指标元数据 Schema (Schema-Driven UI)
# ============================================================
# 格式: {"key": {"name": "显示名", "category": "分类", "desc": "描述",
#               "params": {"param_key": {"label": "参数名", "default": v, "min": a, "max": b, "step": s, "help": "..."}}}}
INDICATOR_SCHEMA = {
    # ---- 趋势类 ----
    "ema": {
        "name": "EMA 双均线", "category": "趋势类",
        "desc": "快线上穿慢线=做多, 下穿=做空",
        "params": {
            "EMA_short": {"label": "短期快线周期", "default": 7, "min": 3, "max": 50, "help": "反应敏捷的短线趋势, 用于捕捉短期冲高/回调"},
            "EMA_long":  {"label": "长期慢线周期", "default": 21, "min": 10, "max": 200, "help": "反应平缓的长线趋势, 用于判断大方向顺逆"},
        },
        "compute": lambda df, p: _ema_cross(df, p["EMA_short"], p["EMA_long"]),
    },
    "sma": {
        "name": "SMA 三均线", "category": "趋势类",
        "desc": "短>中>长=多头排列",
        "params": {
            "SMA_s": {"label": "短期均线", "default": 5, "min": 2, "max": 30},
            "SMA_m": {"label": "中期均线", "default": 10, "min": 5, "max": 60},
            "SMA_l": {"label": "长期均线", "default": 30, "min": 10, "max": 120},
        },
        "compute": lambda df, p: _sma_align(df, p["SMA_s"], p["SMA_m"], p["SMA_l"]),
    },
    "supertrend": {
        "name": "SuperTrend 超级趋势", "category": "趋势类",
        "desc": "价格上穿SuperTrend=做多, 下穿=做空",
        "params": {
            "ATR_period": {"label": "ATR周期", "default": 10, "min": 5, "max": 30, "help": "计算波动率的周期"},
            "multiplier": {"label": "乘数", "default": 3.0, "min": 1.0, "max": 5.0, "step": 0.5, "help": "触发信号的波动倍数"},
        },
        "compute": lambda df, p: _supertrend(df, p["ATR_period"], p["multiplier"]),
    },
    "adx": {
        "name": "ADX/DMI 趋势强度", "category": "趋势类",
        "desc": "+DI>-DI 且 ADX>阈值=做多",
        "params": {
            "ADX_period": {"label": "ADX周期", "default": 14, "min": 5, "max": 30, "help": "ADX平滑周期"},
            "ADX_threshold": {"label": "强趋势阈值", "default": 25, "min": 10, "max": 50, "help": "ADX超过此值视为强趋势"},
        },
        "compute": lambda df, p: _adx_signal(df, p["ADX_period"], p["ADX_threshold"]),
    },
    "ichimoku": {
        "name": "Ichimoku 一目均衡", "category": "趋势类",
        "desc": "价格>云层=做多, <云层=做空",
        "params": {
            "tenkan": {"label": "转换线周期", "default": 9, "min": 5, "max": 20, "help": "短期转折线"},
            "kijun": {"label": "基准线周期", "default": 26, "min": 10, "max": 40, "help": "中期基准线"},
        },
        "compute": lambda df, p: _ichimoku(df, p["tenkan"], p["kijun"]),
    },
    "psar": {
        "name": "Parabolic SAR", "category": "趋势类",
        "desc": "SAR反转=反向信号",
        "params": {
            "step": {"label": "加速步长", "default": 0.02, "min": 0.01, "max": 0.1, "step": 0.01, "help": "AF加速因子步长"},
            "maximum": {"label": "最大加速", "default": 0.2, "min": 0.1, "max": 0.5, "step": 0.05, "help": "AF最大值"},
        },
        "compute": lambda df, p: _psar(df, p["step"], p["maximum"]),
    },

    # ---- 摆动类 ----
    "rsi": {
        "name": "RSI 相对强弱", "category": "摆动类",
        "desc": "RSI<超卖=做多, >超买=做空",
        "params": {
            "RSI_period": {"label": "RSI周期", "default": 14, "min": 2, "max": 50, "help": "标准值14, 越小越灵敏"},
            "RSI_oversold": {"label": "超卖阈值", "default": 30, "min": 10, "max": 40, "help": "低于此值视为超卖, 做多信号"},
            "RSI_overbought": {"label": "超买阈值", "default": 70, "min": 60, "max": 90, "help": "高于此值视为超买, 做空信号"},
        },
        "compute": lambda df, p: _rsi_signal(df, p["RSI_period"], p["RSI_oversold"], p["RSI_overbought"]),
    },
    "kdj": {
        "name": "KDJ 随机指标", "category": "摆动类",
        "desc": "K上穿D且<30=做多, K下穿D且>70=做空",
        "params": {
            "K_period": {"label": "RSV周期", "default": 9, "min": 2, "max": 20, "help": "KDJ计算周期"},
            "K_smooth": {"label": "K平滑周期", "default": 3, "min": 2, "max": 10, "help": "K值平滑参数"},
            "D_smooth": {"label": "D平滑周期", "default": 3, "min": 2, "max": 10, "help": "D值平滑参数"},
        },
        "compute": lambda df, p: _kdj_signal(df, p["K_period"], p["K_smooth"], p["D_smooth"]),
    },
    "macd": {
        "name": "MACD 异同均线", "category": "摆动类",
        "desc": "MACD上穿信号线=做多, 动能指标",
        "params": {
            "MACD_fast": {"label": "快线周期", "default": 12, "min": 2, "max": 30, "help": "短期EMA, 反应灵敏"},
            "MACD_slow": {"label": "慢线周期", "default": 26, "min": 5, "max": 50, "help": "长期EMA, 反应平缓"},
            "MACD_signal": {"label": "信号线周期", "default": 9, "min": 2, "max": 20, "help": "MACD的EMA平滑线"},
        },
        "compute": lambda df, p: _macd_signal(df, p["MACD_fast"], p["MACD_slow"], p["MACD_signal"]),
    },
    "cci": {
        "name": "CCI 商品通道", "category": "摆动类",
        "desc": "CCI<超卖=做多, >超买=做空",
        "params": {
            "CCI_period": {"label": "CCI周期", "default": 20, "min": 5, "max": 50, "help": "商品通道指数周期"},
            "CCI_oversold": {"label": "超卖阈值", "default": -100, "min": -200, "max": -50, "help": "低于此值做多"},
            "CCI_overbought": {"label": "超买阈值", "default": 100, "min": 50, "max": 200, "help": "高于此值做空"},
        },
        "compute": lambda df, p: _cci_signal(df, p["CCI_period"], p["CCI_oversold"], p["CCI_overbought"]),
    },
    "stochrsi": {
        "name": "StochRSI", "category": "摆动类",
        "desc": "StochRSI<超卖=做多, >超买=做空",
        "params": {
            "Stoch_period": {"label": "周期", "default": 14, "min": 5, "max": 30, "help": "StochRSI平滑周期"},
            "Stoch_oversold": {"label": "超卖阈值", "default": 20, "min": 10, "max": 40, "help": "低于此值做多"},
            "Stoch_overbought": {"label": "超买阈值", "default": 80, "min": 60, "max": 90, "help": "高于此值做空"},
        },
        "compute": lambda df, p: _stochrsi(df, p["Stoch_period"], p["Stoch_oversold"], p["Stoch_overbought"]),
    },
    "willr": {
        "name": "Williams %R", "category": "摆动类",
        "desc": "%R<超卖=做多, >超买=做空",
        "params": {
            "WR_period": {"label": "周期", "default": 14, "min": 5, "max": 30, "help": "威廉指标周期"},
            "WR_oversold": {"label": "超卖阈值", "default": -80, "min": -100, "max": -50, "help": "低于此值做多"},
            "WR_overbought": {"label": "超买阈值", "default": -20, "min": -50, "max": 0, "help": "高于此值做空"},
        },
        "compute": lambda df, p: _willr(df, p["WR_period"], p["WR_oversold"], p["WR_overbought"]),
    },
    "ao": {
        "name": "Awesome Oscillator", "category": "摆动类",
        "desc": "AO上穿0轴=做多, 下穿=做空",
        "params": {},
        "compute": lambda df, p: _ao_signal(df),
    },

    # ---- 通道/支撑 ----
    "bollinger": {
        "name": "布林带 Bollinger", "category": "通道/支撑",
        "desc": "价格触下轨=做多, 触上轨=做空",
        "params": {
            "BB_period": {"label": "中轨周期", "default": 20, "min": 5, "max": 50, "help": "布林带中轨均线周期"},
            "BB_std": {"label": "标准差倍数", "default": 2.0, "min": 1.0, "max": 4.0, "step": 0.5, "help": "带宽=标准差*倍数"},
        },
        "compute": lambda df, p: _bb_signal(df, p["BB_period"], p["BB_std"]),
    },
    "keltner": {
        "name": "Keltner 通道", "category": "通道/支撑",
        "desc": "价格触下轨=做多, 触上轨=做空",
        "params": {
            "KC_ema": {"label": "EMA周期", "default": 20, "min": 5, "max": 50, "help": "中轨EMA周期"},
            "KC_mult": {"label": "ATR倍数", "default": 2.0, "min": 1.0, "max": 4.0, "step": 0.5, "help": "带宽=ATR*倍数"},
        },
        "compute": lambda df, p: _keltner(df, p["KC_ema"], p["KC_mult"]),
    },
    "donchian": {
        "name": "Donchian 通道", "category": "通道/支撑",
        "desc": "突破上轨=做多, 突破下轨=做空",
        "params": {
            "DC_period": {"label": "通道周期", "default": 20, "min": 5, "max": 100, "help": "最高/最低价回看周期"},
        },
        "compute": lambda df, p: _donchian(df, p["DC_period"]),
    },
    "fibonacci": {
        "name": "斐波那契回调", "category": "通道/支撑",
        "desc": "回调到0.382/0.618=做多, 跌破0.618=做空",
        "params": {
            "FIB_lookback": {"label": "回看K线数", "default": 50, "min": 20, "max": 200, "help": "计算高低点的回看周期"},
        },
        "compute": lambda df, p: _fibonacci(df, p["FIB_lookback"]),
    },

    # ---- 成交量 ----
    "obv": {
        "name": "OBV 能量潮", "category": "成交量",
        "desc": "OBV上穿MA=做多, 下穿=做空",
        "params": {
            "OBV_ma": {"label": "OBV均线周期", "default": 20, "min": 5, "max": 50, "help": "OBV的均线平滑周期"},
        },
        "compute": lambda df, p: _obv_signal(df, p["OBV_ma"]),
    },
    "vwap": {
        "name": "VWAP 均价", "category": "成交量",
        "desc": "价格>VWAP=做多, <VWAP=做空",
        "params": {},
        "compute": lambda df, p: _vwap_signal(df),
    },
    "mfi": {
        "name": "MFI 资金流量", "category": "成交量",
        "desc": "MFI<超卖=做多, >超买=做空",
        "params": {
            "MFI_period": {"label": "MFI周期", "default": 14, "min": 5, "max": 30, "help": "资金流量指数周期"},
            "MFI_oversold": {"label": "超卖阈值", "default": 20, "min": 10, "max": 40, "help": "低于此值做多"},
            "MFI_overbought": {"label": "超买阈值", "default": 80, "min": 60, "max": 90, "help": "高于此值做空"},
        },
        "compute": lambda df, p: _mfi_signal(df, p["MFI_period"], p["MFI_oversold"], p["MFI_overbought"]),
    },
    "cmf": {
        "name": "CMF 柴金流量", "category": "成交量",
        "desc": "CMF>0=做多, <0=做空",
        "params": {
            "CMF_period": {"label": "CMF周期", "default": 20, "min": 5, "max": 50, "help": "柴金流量指数平滑周期"},
        },
        "compute": lambda df, p: _cmf_signal(df, p["CMF_period"]),
    },
    "vol_break": {
        "name": "成交量突破", "category": "成交量",
        "desc": "量>均量*倍数 + 收阳=做多",
        "params": {
            "VOL_ma": {"label": "均量周期", "default": 20, "min": 5, "max": 50, "help": "成交量均线周期"},
            "VOL_mult": {"label": "放大倍数", "default": 1.5, "min": 1.0, "max": 5.0, "step": 0.1, "help": "量>均量*倍数视为放量"},
        },
        "compute": lambda df, p: _vol_breakout(df, p["VOL_ma"], p["VOL_mult"]),
    },

    # ---- K线形态 ----
    "hammer": {
        "name": "锤头/倒锤", "category": "K线形态",
        "desc": "下影线>实体2倍=锤头做多",
        "params": {},
        "compute": lambda df, p: _hammer(df),
    },
    "engulfing": {
        "name": "吞没形态", "category": "K线形态",
        "desc": "阳包阴=做多, 阴包阳=做空",
        "params": {},
        "compute": lambda df, p: _engulfing(df),
    },
    "star": {
        "name": "早晨/黄昏之星", "category": "K线形态",
        "desc": "早晨之星=做多, 黄昏之星=做空",
        "params": {},
        "compute": lambda df, p: _star(df),
    },
    "soldiers": {
        "name": "三连兵", "category": "K线形态",
        "desc": "三连阳=做多, 三连阴=做空",
        "params": {},
        "compute": lambda df, p: _three_soldiers(df),
    },
    "doji": {
        "name": "十字星 Doji", "category": "K线形态",
        "desc": "下影线>上影线=做多",
        "params": {
            "DOJI_ratio": {"label": "影线比", "default": 1.0, "min": 0.5, "max": 2.0, "step": 0.1, "help": "下影/上影长度比"},
        },
        "compute": lambda df, p: _doji(df, p["DOJI_ratio"]),
    },
    "pinbar": {
        "name": "Pinbar 反转", "category": "K线形态",
        "desc": "长下影线拒绝低位=做多",
        "params": {},
        "compute": lambda df, p: _pinbar(df),
    },
}

# 从 Schema 自动生成 Registry (参数key用schema原始key, 显示用label)
INDICATOR_REGISTRY = {}
for _key, _schema in INDICATOR_SCHEMA.items():
    _reg_params = {}
    _param_labels = {}  # schema_key → 中文label
    for _pk, _pv in _schema["params"].items():
        _reg_params[_pk] = {
            "label": _pv["label"],
            "default": _pv["default"],
            "min": _pv["min"],
            "max": _pv["max"],
            "step": _pv.get("step", 1),
            "help": _pv.get("help", ""),
        }
        _param_labels[_pk] = _pv["label"]
    INDICATOR_REGISTRY[_schema["name"]] = {
        "category": _schema["category"],
        "params": _reg_params,          # {schema_key: {label, default, min, max, step, help}}
        "param_labels": _param_labels,  # {schema_key: "中文标签"}
        "desc": _schema["desc"],
        "compute": _schema["compute"],  # 直接用schema key取值
    }

# (旧注册表已由Schema自动生成, 此段删除)


# ============================================================
# 指标计算函数 (纯 numpy/pandas, shift(1)防未来函数)
# ============================================================
def _s(series, n=1): return series.shift(n)

def _ema_cross(df, fast, slow):
    """EMA双均线交叉"""
    c = df['close'].shift(1)
    ef = c.ewm(span=fast, adjust=False).mean(); es = c.ewm(span=slow, adjust=False).mean()
    df['ema_fast'] = ef; df['ema_slow'] = es
    df['_long'] = ef > es; df['_short'] = ef < es

def _sma_align(df, s, m, l):
    """SMA三均线多头/空头排列"""
    c = df['close'].shift(1)
    sma_s = c.rolling(s).mean(); sma_m = c.rolling(m).mean(); sma_l = c.rolling(l).mean()
    df['_long'] = (sma_s > sma_m) & (sma_m > sma_l)
    df['_short'] = (sma_s < sma_m) & (sma_m < sma_l)

def _supertrend(df, atr_p, mul):
    """SuperTrend"""
    c, h, l = df['close'].shift(1), df['high'].shift(1), df['low'].shift(1)
    tr1 = h - l; tr2 = abs(h - c.shift(1)); tr3 = abs(l - c.shift(1))
    atr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).ewm(span=atr_p, adjust=False).mean()
    hl2 = (h + l) / 2
    up = hl2 - mul * atr; dn = hl2 + mul * atr
    trend = pd.Series(1, index=df.index)
    for i in range(1, len(df)):
        if c.iloc[i] > up.iloc[i-1]: up.iloc[i] = max(up.iloc[i], up.iloc[i-1])
        if c.iloc[i] < dn.iloc[i-1]: dn.iloc[i] = min(dn.iloc[i], dn.iloc[i-1])
        if c.iloc[i] > dn.iloc[i-1]: trend.iloc[i] = 1
        elif c.iloc[i] < up.iloc[i-1]: trend.iloc[i] = -1
        else: trend.iloc[i] = trend.iloc[i-1]
    df['_long'] = trend == 1; df['_short'] = trend == -1

def _adx_signal(df, period, threshold):
    """ADX/DMI"""
    h, l, c = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1)
    tr1 = h - l; tr2 = abs(h - c.shift(1)); tr3 = abs(l - c.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    up = h.diff(); dn = -l.diff()
    pDM = pd.Series(0.0, index=df.index); nDM = pd.Series(0.0, index=df.index)
    pDM[(up > dn) & (up > 0)] = up; nDM[(dn > up) & (dn > 0)] = dn
    pDI = 100 * pDM.ewm(alpha=1/period, adjust=False).mean() / atr
    nDI = 100 * nDM.ewm(alpha=1/period, adjust=False).mean() / atr
    dx = 100 * abs(pDI - nDI) / (pDI + nDI + 1e-9)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    df['_long'] = (pDI > nDI) & (adx > threshold)
    df['_short'] = (nDI > pDI) & (adx > threshold); df['adx'] = adx

def _ichimoku(df, tenkan, kijun):
    """Ichimoku 云图"""
    h, l, c = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1)
    ten = (h.rolling(tenkan).max() + l.rolling(tenkan).min()) / 2
    kij = (h.rolling(kijun).max() + l.rolling(kijun).min()) / 2
    senA = ((ten + kij) / 2).shift(kijun)
    senB = ((h.rolling(kijun*2).max() + l.rolling(kijun*2).min()) / 2).shift(kijun)
    df['_long'] = c > senA; df['_short'] = c < senB

def _psar(df, step, maximum):
    """Parabolic SAR (简化版)"""
    c, h, l = df['close'].shift(1), df['high'].shift(1), df['low'].shift(1)
    n = len(df); sar = pd.Series(0.0, index=df.index); ep = pd.Series(0.0, index=df.index)
    af = step; trend = 1; sar.iloc[0] = l.iloc[0]; ep.iloc[0] = h.iloc[0]
    for i in range(1, n):
        sar.iloc[i] = sar.iloc[i-1] + af * (ep.iloc[i-1] - sar.iloc[i-1])
        if trend == 1:
            if l.iloc[i] < sar.iloc[i]: trend = -1; sar.iloc[i] = ep.iloc[i-1]; ep.iloc[i] = l.iloc[i]; af = step
            else:
                if h.iloc[i] > ep.iloc[i-1]: ep.iloc[i] = h.iloc[i]; af = min(af + step, maximum)
                else: ep.iloc[i] = ep.iloc[i-1]
        else:
            if h.iloc[i] > sar.iloc[i]: trend = 1; sar.iloc[i] = ep.iloc[i-1]; ep.iloc[i] = h.iloc[i]; af = step
            else:
                if l.iloc[i] < ep.iloc[i-1]: ep.iloc[i] = l.iloc[i]; af = min(af + step, maximum)
                else: ep.iloc[i] = ep.iloc[i-1]
    df['sar'] = sar
    cross_up = (_s(c) <= _s(sar)) & (c > sar); cross_dn = (_s(c) >= _s(sar)) & (c < sar)
    df['_long'] = cross_up; df['_short'] = cross_dn

def _rsi_signal(df, period, os, ob):
    c = df['close'].shift(1); delta = c.diff()
    gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / (loss + 1e-9))
    df['rsi'] = rsi; df['_long'] = rsi < os; df['_short'] = rsi > ob

def _kdj_signal(df, n, m1, m2):
    h, l, c = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1)
    ln = l.rolling(n).min(); hn = h.rolling(n).max()
    rsv = ((c - ln) / (hn - ln + 1e-9) * 100).clip(0, 100)
    k = rsv.ewm(com=m1-1, adjust=False).mean(); d = k.ewm(com=m2-1, adjust=False).mean()
    df['kdj_k'] = k; df['kdj_d'] = d
    df['_long'] = (k > d) & (_s(k) <= _s(d)) & (k < 30)
    df['_short'] = (k < d) & (_s(k) >= _s(d)) & (k > 70)

def _macd_signal(df, fast, slow, sig):
    c = df['close'].shift(1)
    ef = c.ewm(span=fast, adjust=False).mean(); es = c.ewm(span=slow, adjust=False).mean()
    macd = ef - es; signal = macd.ewm(span=sig, adjust=False).mean()
    df['macd'] = macd; df['macd_sig'] = signal
    df['_long'] = (macd > signal) & (_s(macd) <= _s(signal))
    df['_short'] = (macd < signal) & (_s(macd) >= _s(signal))

def _cci_signal(df, period, os, ob):
    h, l, c = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1)
    tp = (h + l + c) / 3; ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    cci = (tp - ma) / (0.015 * md + 1e-9)
    df['_long'] = cci < os; df['_short'] = cci > ob

def _stochrsi(df, period, os, ob):
    c = df['close'].shift(1); delta = c.diff()
    gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / (loss + 1e-9))
    rsi_l = rsi.rolling(period).min(); rsi_h = rsi.rolling(period).max()
    stoch = ((rsi - rsi_l) / (rsi_h - rsi_l + 1e-9) * 100).clip(0, 100)
    df['_long'] = stoch < os; df['_short'] = stoch > ob

def _willr(df, period, os, ob):
    h, l, c = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1)
    hh = h.rolling(period).max(); ll = l.rolling(period).min()
    wr = (hh - c) / (hh - ll + 1e-9) * -100
    df['_long'] = wr < os; df['_short'] = wr > ob

def _ao_signal(df):
    h, l = df['high'].shift(1), df['low'].shift(1)
    mp = (h + l) / 2
    ao = mp.rolling(5).mean() - mp.rolling(34).mean()
    df['_long'] = (ao > 0) & (_s(ao) <= 0); df['_short'] = (ao < 0) & (_s(ao) >= 0)

def _bb_signal(df, period, std):
    c = df['close'].shift(1)
    mid = c.rolling(period).mean(); s = c.rolling(period).std()
    df['bb_upper'] = mid + std * s; df['bb_lower'] = mid - std * s; df['bb_mid'] = mid
    df['_long'] = c < df['bb_lower']; df['_short'] = c > df['bb_upper']

def _keltner(df, ema_p, mul):
    c, h, l = df['close'].shift(1), df['high'].shift(1), df['low'].shift(1)
    tr1 = h - l; tr2 = abs(h - c.shift(1)); tr3 = abs(l - c.shift(1))
    atr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).ewm(span=ema_p, adjust=False).mean()
    ema = c.ewm(span=ema_p, adjust=False).mean()
    df['_long'] = c < ema - mul * atr; df['_short'] = c > ema + mul * atr

def _donchian(df, period):
    h, l, c = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1)
    upper = h.rolling(period).max(); lower = l.rolling(period).min()
    df['_long'] = c > _s(upper); df['_short'] = c < _s(lower)

def _fibonacci(df, lookback):
    h, l, c = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1)
    hh = h.rolling(lookback).max(); ll = l.rolling(lookback).min()
    rng = hh - ll
    fib382 = hh - rng * 0.382; fib618 = hh - rng * 0.618
    df['_long'] = (c < fib382) & (c > fib618)
    df['_short'] = c < fib618

def _obv_signal(df, period):
    c, v = df['close'].shift(1), df['vol'].shift(1)
    obv = pd.Series(0.0, index=df.index)
    for i in range(1, len(df)):
        if c.iloc[i] > c.iloc[i-1]: obv.iloc[i] = obv.iloc[i-1] + v.iloc[i]
        elif c.iloc[i] < c.iloc[i-1]: obv.iloc[i] = obv.iloc[i-1] - v.iloc[i]
        else: obv.iloc[i] = obv.iloc[i-1]
    ma = obv.rolling(period).mean()
    df['_long'] = (obv > ma) & (_s(obv) <= _s(ma)); df['_short'] = (obv < ma) & (_s(obv) >= _s(ma))

def _vwap_signal(df):
    h, l, c, v = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1), df['vol'].shift(1)
    tp = (h + l + c) / 3
    cum_vp = (tp * v).cumsum(); cum_v = v.cumsum()
    vwap = cum_vp / (cum_v + 1e-9)
    df['_long'] = c > vwap; df['_short'] = c < vwap

def _mfi_signal(df, period, os, ob):
    h, l, c, v = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1), df['vol'].shift(1)
    tp = (h + l + c) / 3; mf = tp * v
    pos_flow = pd.Series(0.0, index=df.index); neg_flow = pd.Series(0.0, index=df.index)
    pos_flow[tp > tp.shift(1)] = mf[tp > tp.shift(1)]
    neg_flow[tp < tp.shift(1)] = mf[tp < tp.shift(1)]
    pos_sum = pos_flow.rolling(period).sum(); neg_sum = neg_flow.rolling(period).sum()
    mfi = 100 - 100 / (1 + pos_sum / (neg_sum + 1e-9))
    df['_long'] = mfi < os; df['_short'] = mfi > ob

def _cmf_signal(df, period):
    h, l, c, v = df['high'].shift(1), df['low'].shift(1), df['close'].shift(1), df['vol'].shift(1)
    clv = ((c - l) - (h - c)) / (h - l + 1e-9)
    cmf = (clv * v).rolling(period).sum() / v.rolling(period).sum()
    df['_long'] = cmf > 0; df['_short'] = cmf < 0

def _vol_breakout(df, period, mul):
    c, v = df['close'].shift(1), df['vol'].shift(1)
    ma = v.rolling(period).mean()
    df['_long'] = (v > ma * mul) & (c > df['open'].shift(1))
    df['_short'] = (v > ma * mul) & (c < df['open'].shift(1))

# K线形态
def _hammer(df):
    o, h, l, c = df['open'].shift(1), df['high'].shift(1), df['low'].shift(1), df['close'].shift(1)
    body = abs(c - o); lower = np.minimum(c, o) - l; upper = h - np.maximum(c, o)
    df['_long'] = (lower > body * 2) & (upper < body * 0.5) & (body > 0)
    df['_short'] = (upper > body * 2) & (lower < body * 0.5) & (body > 0)

def _engulfing(df):
    o, c = df['open'].shift(1), df['close'].shift(1)
    po, pc = o.shift(1), c.shift(1)
    df['_long'] = (c > o) & (pc < po) & (c > po) & (o < pc)
    df['_short'] = (c < o) & (pc > po) & (c < po) & (o > pc)

def _star(df):
    o, c = df['open'].shift(1), df['close'].shift(1)
    po, pc = o.shift(1), c.shift(1); ppo, ppc = o.shift(2), c.shift(2)
    # Morning star: down day + small body gap down + up day
    body = abs(c - o); pbody = abs(pc - po)
    df['_long'] = (ppc < ppo) & (pbody < body * 0.5) & (c > o) & (o < pc)
    df['_short'] = (ppc > ppo) & (pbody < body * 0.5) & (c < o) & (o > pc)

def _three_soldiers(df):
    o, c = df['open'].shift(1), df['close'].shift(1)
    df['_long'] = (c > o) & (c.shift(1) > o.shift(1)) & (c.shift(2) > o.shift(2))
    df['_short'] = (c < o) & (c.shift(1) < o.shift(1)) & (c.shift(2) < o.shift(2))

def _doji(df, ratio):
    o, c = df['open'].shift(1), df['close'].shift(1)
    h, l = df['high'].shift(1), df['low'].shift(1)
    body = abs(c - o); total = h - l
    is_doji = (body < total * 0.1) & (total > 0)
    lower = np.minimum(c, o) - l; upper = h - np.maximum(c, o)
    df['_long'] = is_doji & (lower > upper * ratio)
    df['_short'] = is_doji & (upper > lower * ratio)

def _pinbar(df):
    o, c = df['open'].shift(1), df['close'].shift(1)
    h, l = df['high'].shift(1), df['low'].shift(1)
    body = abs(c - o); total = (h - l).rolling(20).mean()
    nose_l = np.minimum(c, o) - l; nose_h = h - np.maximum(c, o)
    df['_long'] = (nose_l > total * 0.5) & (body < nose_l * 0.5) & (total > 0)
    df['_short'] = (nose_h > total * 0.5) & (body < nose_h * 0.5) & (total > 0)


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
st.sidebar.title("📊 马总量化控制台")

# 日期预设按钮 (在form外面, 立即可用)
if "date_range" not in st.session_state: st.session_state.date_range = None

st.sidebar.caption("快捷时段:")
dp1, dp2, dp3 = st.sidebar.columns(3)
for label, dr, col in [
    ("🐂21牛", ("2021-01-01","2021-12-31"), dp1),
    ("🐻22熊", ("2022-01-01","2022-12-31"), dp2),
    ("📈23-24", ("2023-01-01","2024-12-31"), dp3),
]:
    if col.button(label, use_container_width=True, key=f"dp_{label}"):
        st.session_state.date_range = dr; st.rerun()
if st.sidebar.button("🔁 全部历史", use_container_width=True):
    st.session_state.date_range = None; st.rerun()

# 自定义日期选择器 (恢复!)
st.sidebar.caption("自定义日期:")
dc1, dc2 = st.sidebar.columns(2)
d_start = dc1.date_input("起始", datetime(2020,1,1), key="d_start")
d_end = dc2.date_input("结束", datetime.now(), key="d_end")
dp3, dp4 = st.sidebar.columns(2)
if dp3.button("近1年", use_container_width=True):
    st.session_state.date_range = ((datetime.now()-timedelta(days=365)).strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d"))
    st.rerun()
if dp4.button("近3年", use_container_width=True):
    st.session_state.date_range = ((datetime.now()-timedelta(days=1095)).strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d"))
    st.rerun()
if st.sidebar.button("✓ 应用自定义日期", use_container_width=True):
    st.session_state.date_range = (d_start.strftime("%Y-%m-%d"), d_end.strftime("%Y-%m-%d"))
    st.rerun()
if st.session_state.date_range:
    st.sidebar.caption(f"当前: {st.session_state.date_range[0]} ~ {st.session_state.date_range[1]}")

st.sidebar.divider()

# 策略模式默认值 (form内可能被覆盖, 这里提前声明防NameError)
strat_mode_key = "classic"
hedge_ratio = 0.5; unlock_pct = 5.0; max_pyramid = 3
pyramid_first = 0.3; pyramid_step_pct = 1.5; trailing_pct = 0.0
spot_tp = 5.0; spot_sl = 2.0; short_sl = 3.0; funding_threshold = 0.01
tp_pct = 10.0; sl_pct = 5.0; bull_a = 1.0; range_a = 0.5; bear_a = 0.3
lock_streak_val = 3; lock_days = 2; risk_per_trade = 1.0
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
    st.caption("📋 基础设置")
    c1, c2 = st.columns(2)
    coin = c1.selectbox("标的", ["ETH", "BTC", "SOL"], 0)
    timeframe = c2.selectbox("K线周期", ["5m", "15m", "1h", "4h", "1d"], 3)
    c1, c2 = st.columns(2)
    leverage = c1.slider("杠杆", 1, 20, 3, 1)
    initial_capital = c2.number_input("初始资金", 100, 1000000, 10000, 1000)
    st.caption("交易方向控制")
    c1, c2 = st.columns(2)
    trade_mode = c1.selectbox("交易模式", ["双向交易 (做多+做空)", "仅做多 (Only Long)", "仅做空 (Only Short)"], index=0,
                              help="双向=牛市做多熊市做空; 仅做多=永远不开空; 仅做空=永远不开多")
    regime_filter_enabled = c2.checkbox("牛熊方向过滤", True,
        help="牛市禁止做空, 熊市禁止做多, 震荡市双向允许。关闭则不限制方向。")

    st.divider(); st.caption("🏗️ 策略模式与参数")
    strategy_mode = st.selectbox("策略模式", [
        "经典单向策略 (Classic Directional)",
        "Delta中性对冲 (Delta-Neutral Hedging)",
        "动能突破解封 (Momentum Unlocking)",
    ], index=0, help="经典=指标信号驱动 | 对冲=双腿锁仓 | 解封=突破平空裸多")
    mode_map = {"经典": "classic", "Delta": "hedging", "动能": "unlocking"}
    strat_mode_key = [v for k, v in mode_map.items() if k in strategy_mode][0]
    is_dual_leg = strat_mode_key in ("hedging", "unlocking")

    # === 动态参数面板 ===
    hedge_ratio = 0.5; unlock_pct = 5.0; max_pyramid = 3
    pyramid_first = 0.3; pyramid_step_pct = 1.5; trailing_pct = 0.0
    spot_tp = 5.0; spot_sl = 2.0; short_sl = 3.0; funding_threshold = 0.01

    if strat_mode_key == "classic":
        st.caption("经典模式: 下方指标积木 + 风控驱动")
        # 通用仓位控制 (不受加仓开关影响)
        pyr_init_pct = st.slider("单笔建仓比例%", 10, 100, 30, 5,
            help="信号触发时开仓占总资金的比例。设30%=留余量做加仓/对冲")
        # 加仓子面板
        with st.expander("📈 顺势加仓管理 (Pyramiding)", expanded=False):
            enable_pyramiding = st.checkbox("启用加仓", False)
            pyr_trigger_pct = 2.0; pyr_add_pct = 0.5; pyr_max = 3; pyr_trail = False
            if enable_pyramiding:
                c1, c2 = st.columns(2)
                pyr_trigger_pct = c1.number_input("触发涨幅%", 0.5, 20.0, 2.0, 0.5,
                    help="持仓均价浮盈超过X%时触发加仓")
                pyr_add_pct = c2.slider("加仓比例%", 10, 100, 50, 5,
                    help="每次加仓占初始仓位的百分比") / 100
                pyr_max = st.number_input("最大加仓次数", 1, 10, 3)
                pyr_trail = st.checkbox("加仓后移动止损至均价", False,
                    help="加仓时将止损位拉抬至最新持仓均价, 实现保本止损")
    elif strat_mode_key == "hedging":
        st.caption("双腿独立风控 (现货 vs 合约空单)")
        hedge_ratio = st.slider("现货/合约资金比例", 0.1, 1.0, 0.5, 0.1)
        st.caption("--- 现货腿 (SPOT LONG) ---")
        c1, c2 = st.columns(2)
        spot_tp = c1.number_input("现货止盈%", 1.0, 50.0, 5.0, 0.5, key="hedge_spot_tp")
        spot_sl = c2.number_input("现货止损%", 0.5, 20.0, 2.0, 0.5, key="hedge_spot_sl")
        st.caption("--- 合约空单腿 (FUTURES SHORT) ---")
        short_sl = st.number_input("空单止损%", 1.0, 20.0, 3.0, 0.5, key="hedge_short_sl")
        st.caption("组合保护: 总权益回撤3%强行双边全平")
    elif strat_mode_key == "unlocking":
        st.caption("解封触发条件 (多条件OR触发)")
        c1, c2 = st.columns(2)
        unlock_pct = c1.number_input("价格突破%", 1.0, 20.0, 5.0, 0.5)
        use_ema_unlock = c2.checkbox("EMA金叉解锁", True)
        use_rsi_unlock = st.checkbox("RSI突破解锁", False)
        use_vol_unlock = st.checkbox("放量突破解锁", False)
        st.caption("解封后裸多风控:")
        c1, c2 = st.columns(2)
        unlock_sl = c1.number_input("解封后止损%", 1.0, 15.0, 3.0, 0.5)
        unlock_tp = c2.number_input("解封后止盈%", 5.0, 50.0, 15.0, 0.5)

    # === 单向风控 (仅非对冲模式渲染) ===
    if not is_dual_leg:
        st.divider(); st.caption("🛡️ 单向风控")
        c1, c2 = st.columns(2)
        tp_pct = c1.slider("止盈%", 2.0, 50.0, 10.0, 0.5)
        sl_pct = c2.slider("止损%", 1.0, 30.0, 5.0, 0.5)
        c1, c2, c3 = st.columns(3)
        bull_a = c1.number_input("牛市%", 10, 100, 100, 5) / 100
        range_a = c2.number_input("震荡%", 10, 100, 50, 5) / 100
        bear_a = c3.number_input("熊市%", 0, 100, 30, 5) / 100
        c1, c2 = st.columns(2)
        lock_streak_val = c1.number_input("连亏锁仓(笔)", 1, 10, 3)
        lock_days = c2.number_input("锁仓天数", 1, 30, 2)
        c1, c2 = st.columns(2)
        risk_per_trade = c1.number_input("单笔风险占比%", 0.5, 5.0, 1.0, 0.5)
        use_atr_stop = c2.checkbox("ATR动态止损", False)
        atr_period_val, atr_mult_val = 14, 2.0
        if use_atr_stop:
            c1, c2 = st.columns(2)
            atr_period_val = c1.number_input("ATR周期", 5, 30, 14, 1)
            atr_mult_val = c2.number_input("止损倍数", 1.0, 5.0, 2.0, 0.5)
    else:
        tp_pct = 10.0; sl_pct = 5.0
        bull_a = 1.0; range_a = 0.5; bear_a = 0.3
        lock_streak_val = 3; lock_days = 2; risk_per_trade = 1.0
        use_atr_stop = False; atr_period_val = 14; atr_mult_val = 2.0

    oos_enabled = st.checkbox("样本外测试 (OOS)", False)
    oos_ratio = st.slider("训练集%", 50, 90, 70, 5, disabled=not oos_enabled)

    st.divider(); st.caption("🧱 指标积木")
    st.caption(f"💡 当前所有指标参数基于【{timeframe}】K线周期实时计算")
    logic_mode = st.radio("信号组合模式",
        ["AND 全部满足 (严格)", "OR 任一满足 (灵敏)", "加权打分 N个以上触发 (推荐)"],
        horizontal=False, key="logic_mode")
    use_and = "AND" in logic_mode
    use_weighted = "加权" in logic_mode
    weighted_threshold = 2
    if use_weighted:
        weighted_threshold = st.slider("最少满足指标数", 1, 10, 2, 1,
            help="勾选的指标中, 至少N个同时触发才开仓。值越大信号越少但质量越高")

    categories = {}
    for name, info in INDICATOR_REGISTRY.items():
        cat = info["category"]
        if cat not in categories: categories[cat] = []
        categories[cat].append(name)
    if "selected_indicators" not in st.session_state:
        st.session_state.selected_indicators = {"EMA 双均线": {"enabled": True, "params": {"EMA_short": 7, "EMA_long": 21}}}

    for cat_name, ind_names in categories.items():
        with st.expander(f"▸ {cat_name} ({len(ind_names)}种)", expanded=False):
            for name in ind_names:
                info = INDICATOR_REGISTRY[name]
                sel = st.session_state.selected_indicators
                checked = name in sel and sel[name].get("enabled", False)
                new_checked = st.checkbox(name, checked, key=f"ind_{name}", help=info["desc"])
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
                        label = pdef["label"]
                        val = cols[i % 2].number_input(
                            label, pdef["min"], pdef["max"],
                            sel[name]["params"].get(pk, pdef["default"]),
                            pdef["step"], key=f"p_{name}_{pk}",
                            help=pdef.get("help", ""),
                        )
                        sel[name]["params"][pk] = val

    st.divider(); st.caption("🔬 多因子牛熊 + 共振")
    c1, c2 = st.columns(2)
    mf_enabled = c1.checkbox("多因子牛熊", True, key="mf_on")
    resonance_enabled = c2.checkbox("共振打分", False, key="res_on")
    res_f1, res_f2, res_f3 = "", "", ""
    if resonance_enabled:
        cat_list = ["趋势类", "摆动类", "通道/支撑", "成交量", "K线形态"]
        cat1_opts = [""] + [n for n, i in INDICATOR_REGISTRY.items() if i["category"] == cat_list[0]]
        cat23_opts = [""] + [n for n, i in INDICATOR_REGISTRY.items() if i["category"] in cat_list[1:3]]
        cat45_opts = [""] + [n for n, i in INDICATOR_REGISTRY.items() if i["category"] in cat_list[3:]]
        # 堆叠布局, 避免文字截断
        res_f1 = st.selectbox("因子1 (趋势类)", cat1_opts, key="rf1")
        res_f2 = st.selectbox("因子2 (摆动/通道)", cat23_opts, key="rf2")
        res_f3 = st.selectbox("因子3 (量价/形态)", cat45_opts, key="rf3")
        # 展开选中因子的子参数
        for label, fname in [("因子1", res_f1), ("因子2", res_f2), ("因子3", res_f3)]:
            if fname and fname in INDICATOR_REGISTRY and INDICATOR_REGISTRY[fname]["params"]:
                with st.expander(f"{label}: {fname} 参数", expanded=False):
                    fparams = INDICATOR_REGISTRY[fname]["params"]
                    cols = st.columns(min(2, len(fparams)))
                    for i, (pk, pdef) in enumerate(fparams.items()):
                        val_key = f"rf_param_{fname}_{pk}"
                        # 从selected_indicators获取或默认值
                        cur_val = st.session_state.selected_indicators.get(fname, {}).get("params", {}).get(pk, pdef["default"])
                        new_val = cols[i % 2].number_input(
                            pdef["label"], pdef["min"], pdef["max"],
                            cur_val, pdef["step"], key=val_key,
                            help=pdef.get("help", ""),
                        )
                        if fname not in st.session_state.selected_indicators:
                            st.session_state.selected_indicators[fname] = {"enabled": True, "params": {}}
                        st.session_state.selected_indicators[fname]["params"][pk] = new_val
    if mf_enabled:
        c1, c2 = st.columns(2)
        ema_w = c1.slider("EMA权重", 0.0, 1.0, 0.40, 0.05, key="mf_ew")
        adx_w = c2.slider("ADX权重", 0.0, 1.0, 0.35, 0.05, key="mf_aw")
        adx_th = st.slider("ADX阈值", 10, 50, 25, 5, key="mf_at")
        bull_th = st.slider("牛市判定", 0.10, 0.60, 0.30, 0.05, key="mf_bt")

    st.divider()
    submitted = st.form_submit_button("🚀 确认参数并运行回测", use_container_width=True, type="primary")

st.sidebar.divider()
# 导出 + 刷新 + 登出 (在form外面)
c_refresh1, c_refresh2 = st.sidebar.columns(2)
if c_refresh1.button("🔄 刷新行情", use_container_width=True):
    st.cache_data.clear(); st.cache_resource.clear()
    st.success("缓存已清除, 正在检查断层并补全..."); time.sleep(1); st.rerun()
if c_refresh2.button("🔁 强制重刷", use_container_width=True,
                     help="删除全部本地缓存, 从交易所完整重新下载历史K线"):
    with st.spinner("正在重新下载全部历史数据 (2-3分钟)..."):
        try:
            from data_loader import force_redownload
            for c in ["ETH", "BTC", "SOL"]:
                force_redownload(c)
            st.cache_data.clear(); st.cache_resource.clear()
            st.success("全部数据已重新下载!"); time.sleep(1); st.rerun()
        except Exception as e:
            st.error(f"下载失败: {e}")
cur_params = {"coin": coin, "tf": timeframe, "lev": leverage,
              "tp": tp_pct, "sl": sl_pct, "indicators": list(st.session_state.selected_indicators.keys())}
st.sidebar.markdown(export_json(cur_params), unsafe_allow_html=True)
if st.sidebar.button("登出"): st.session_state.logged_in = False; st.rerun()

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
st.title("📊 马总量化控制台")

# Tab 导航
tab_names = ["📈 回测看板", "🤖 翔哥 AI 对话舱"]
if "active_tab" not in st.session_state: st.session_state.active_tab = "回测看板"
tc1, tc2 = st.columns([1, 1])
with tc1:
    if st.button("📈 回测看板", use_container_width=True,
                 type="primary" if "回测" in st.session_state.active_tab else "secondary"):
        st.session_state.active_tab = "回测看板"; st.rerun()
with tc2:
    if st.button("🤖 翔哥 AI 对话舱", use_container_width=True,
                 type="primary" if "AI" in st.session_state.active_tab else "secondary"):
        st.session_state.active_tab = "AI 对话舱"; st.rerun()

st.divider()

# ============================================================
# ============================================================
# 统一 API 调用 (DeepSeek / OpenAI / Anthropic / Gemini)
# ============================================================
def _call_unified_api(messages: list, api_key: str, model_name: str, trading_notes: str) -> dict:
    import requests
    if trading_notes.strip():
        np = {"role": "system", "content": f"你是翔哥，马总的专属量化导师。精通加密货币技术分析。\n\n【马总交易心法】\n{trading_notes.strip()}\n\n用简洁中文回答, 直接给结论。"}
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

# Tab 2: 翔哥 AI 对话舱
# ============================================================
if "AI" in st.session_state.active_tab:
    from ai_assistant import build_context, DEFAULT_TRADING_NOTES

    # 模型预设 (整合所有主流API)
    ALL_MODELS = {
        "DeepSeek-V3 (推荐)":  {"base": "https://api.deepseek.com",      "model": "deepseek-chat"},
        "DeepSeek-R1 (推理)":  {"base": "https://api.deepseek.com",      "model": "deepseek-reasoner"},
        "OpenAI GPT-4o":       {"base": "https://api.openai.com",        "model": "gpt-4o"},
        "OpenAI GPT-4o-mini":  {"base": "https://api.openai.com",        "model": "gpt-4o-mini"},
        "Anthropic Claude 3.5":{"base": "https://api.anthropic.com",     "model": "claude-sonnet-4-20250514"},
        "Google Gemini 2.0":   {"base": "https://generativelanguage.googleapis.com", "model": "gemini-2.0-flash"},
    }

    with st.expander("⚙️ AI 配置", expanded=not st.session_state.get("ai_configured", False)):
        c1, c2 = st.columns(2)
        ai_key = c1.text_input("API Key / 密钥", type="password",
                               value=os.environ.get("AI_API_KEY", ""),
                               key="ai_main_key", placeholder="sk-... 或 AIza...")
        ai_model_name = c2.selectbox("模型", list(ALL_MODELS.keys()), index=0, key="ai_mdl")

        if "trading_notes" not in st.session_state:
            st.session_state.trading_notes = DEFAULT_TRADING_NOTES
        st.caption("翔哥私房交易心法 (自动注入 System Prompt)")
        trading_notes = st.text_area("心法", value=st.session_state.trading_notes, height=100,
                                      key="tnotes", label_visibility="collapsed")
        st.session_state.trading_notes = trading_notes

    if not ai_key:
        st.info("展开上方 ⚙️ AI 配置, 填入 API Key 即可使用")
    else:
        st.session_state.ai_configured = True

    # 快捷按钮行
    qcols = st.columns(5)
    quick_msgs = {
        "💡 行情解读": "帮我解读当前行情是否符合我的交易心法。",
        "🎯 共振策略": "结合我选的指标，帮我设计一套三体共振策略并讲解原理。",
        "❓ 假突破检测": "现在的指标数据里，有没有出现'无量洗盘'或假突破的信号？",
        "📊 参数优化": "根据回测结果，给出3条止盈止损参数优化建议。",
        "🔍 风险诊断": "扫描我当前的策略配置，指出最大的3个风险点。",
    }
    quick_clicked = None
    for i, (label, prompt) in enumerate(quick_msgs.items()):
        if qcols[i].button(label, use_container_width=True, key=f"qp_{i}", disabled=not ai_key):
            quick_clicked = prompt

    # 一键诊断按钮
    dcol, _ = st.columns([1, 3])
    if dcol.button("🧠 一键诊断当前行情与策略", use_container_width=True, type="primary", disabled=not ai_key):
        quick_clicked = "请给我一份完整的策略诊断报告：1)当前多空格局解读 2)推荐的指标参数和止盈止损 3)Alpha因子优化建议。"

    # 聊天记录
    if "ai_chat_history" not in st.session_state:
        st.session_state.ai_chat_history = []

    for msg in st.session_state.ai_chat_history:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    user_msg = st.chat_input("和翔哥聊聊你的策略...", key="ai_main_chat", disabled=not ai_key)
    if quick_clicked:
        user_msg = quick_clicked

    if user_msg and ai_key:
        st.session_state.ai_chat_history.append({"role": "user", "content": user_msg})
        with st.chat_message("user"): st.write(user_msg)

        # 构建实时上下文
        try:
            df_ctx = load_cached_15min(coin)
            df_ctx.index = pd.to_datetime(df_ctx.index).tz_localize(None)
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
                    ind_ctx[name] = "做多" if l else ("做空" if s else "无信号")
                except: ind_ctx[name] = "异常"
        except: px = 0; ind_ctx = {}

        bt = {}
        btf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_result.json")
        if os.path.exists(btf):
            try:
                with open(btf) as f: bt = json.load(f)
            except: pass

        context = build_context(coin, timeframe, px, ind_ctx, bt)
        msgs = [{"role": "system", "content": context}] + st.session_state.ai_chat_history[-8:]

        with st.chat_message("assistant"):
            with st.spinner("翔哥思考中..."):
                result = _call_unified_api(msgs, ai_key, ai_model_name, trading_notes)
                if result["success"]:
                    st.write(result["content"])
                    st.caption(f"模型: {result.get('model','?')}")
                    st.session_state.ai_chat_history.append({"role": "assistant", "content": result["content"]})
                else:
                    st.error(result["error"])

    if st.session_state.ai_chat_history:
        if st.button("🗑️ 清空对话", use_container_width=True):
            st.session_state.ai_chat_history = []; st.rerun()

    st.stop()



# ============================================================
# Tab 1: 回测看板
# ============================================================
p1, p2, p3, p4 = st.columns(4)
p1.metric("标的", coin); p2.metric("周期", timeframe); p3.metric("杠杆", f"{leverage}x"); p4.metric("资金", f"${initial_capital:,}")

# 已选指标摘要
active_inds = [n for n, c in st.session_state.selected_indicators.items() if isinstance(c, dict) and c.get("enabled")]
if is_dual_leg:
    st.caption("Delta中性对冲/解锁模式 (无需技术指标，自动建仓对冲)")
else:
    st.caption(f"已选指标 ({len(active_inds)}): " + ", ".join(active_inds[:10]) + ("..." if len(active_inds) > 10 else ""))

st.divider()
if submitted:
    # 强制刷新最新行情数据
    with st.spinner("检查最新行情数据..."):
        try:
            from data_loader import ensure_data
            ensure_data(coin)
            st.cache_data.clear()  # 清除旧缓存, 强制重新加载
        except Exception as e:
            st.caption(f"数据刷新跳过: {e}")

    # 加载数据
    with st.spinner("加载数据 & 计算指标..."):
        de = DataEngine()
        all_tf = de.get_multi_timeframe(coin)
        df = all_tf.get(timeframe, all_tf['4h'])

        # 时间范围过滤
        dr = st.session_state.date_range
        if dr:
            df = df.loc[dr[0]:dr[1]]
        if len(df) < 200:
            st.error(f"数据不足 ({len(df)}根), 请扩大时间范围"); st.stop()

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
            max_positions=1, bull_alloc=bull_a, range_alloc=range_a, bear_alloc=bear_a,
            lock_streak=int(lock_streak_val), lock_bars=lock_bars, cooldown_bars=2, verbose=False,
            trailing_pct=trailing_pct, strategy_mode=strat_mode_key,
            hedge_ratio=hedge_ratio, max_pyramid=max_pyramid,
            pyramid_step=pyramid_step_pct / 100.0, unlock_pct=unlock_pct / 100.0,
            spot_tp=_spot_tp_val, spot_sl=_spot_sl_val, short_sl=_short_sl_val,
        )
        engine = BacktestEngineV2(**strat_kwargs)
        result = engine.run({coin: df_train}, strategy)
        metrics = PerformanceAnalyzer.analyze(result)

    # OOS
    oos_m = None
    if oos_enabled and df_test is not None and len(df_test) > 200:
        with st.spinner("样本外测试..."):
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
        st.warning(f"当前组合条件未触发任何交易！数据共 {len(df_train):,} 根K线 (含预热 {total_warmup} 根)")
        st.caption("已选指标逐项诊断 (扫描最近500根K线):")

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
                    status = f"✅ 做多{l_cnt}次 / 做空{s_cnt}次"
                else:
                    status = "⚪ 计算正常, 但未触发信号 (条件未满足)"
                diag_rows.append((name, status, "正常"))
            except Exception as e:
                diag_rows.append((name, f"❌ 计算失败: {str(e)[:60]}", "异常"))

        if diag_rows:
            df_diag_out = pd.DataFrame(diag_rows, columns=["指标", "状态", "类型"])
            st.dataframe(df_diag_out, use_container_width=True, hide_index=True)

        st.info("💡 建议: 1)切换为[OR/加权打分]模式 2)调松参数(如降低RSI阈值) 3)扩大时间范围")
        st.stop()

    # === 指标卡片 ===
    st.subheader("📈 回测结果")
    c = st.columns(7)
    c[0].metric("总收益", f"{metrics.get('total_return',0):+.1f}%")
    c[1].metric("年化", f"{metrics.get('annual_return',0):+.1f}%",
                help="基于实际回测天数折算的365天标准化复利收益率")
    c[2].metric("最大回撤", f"{metrics.get('max_drawdown',0):.1f}%")
    c[3].metric("胜率", f"{metrics.get('win_rate',0):.1f}%")
    c[4].metric("盈亏比", f"{metrics.get('profit_factor',0):.2f}" if metrics.get('profit_factor') != float('inf') else "inf")
    c[5].metric("交易数", metrics.get('total_trades', 0))
    yrs = metrics.get('years', 0)
    days = int(yrs * 365.25)
    st.caption(f"回测时长: {yrs:.1f}年 ({days}天) | 年化 = 总收益按此区间复利折算为365天标准收益率")
    final_eq = result.get('final_equity', initial_capital)
    c[6].metric("最终权益", f"${final_eq:,.0f}")

    # Delta暴露曲线 & 仓位状态 (多Leg模式)
    portfolio_data = result.get('portfolio_curve', [])
    if portfolio_data:
        st.subheader("📐 Delta 暴露 & 仓位状态")
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
        st.plotly_chart(fig_delta, use_container_width=True,
                        config={'responsive': True, 'displayModeBar': False})

    # 多空统计
    closed_trades = result.get('closed_trades', [])
    long_trades = [t for t in closed_trades if t.get('side') == 'LONG']
    short_trades = [t for t in closed_trades if t.get('side') == 'SHORT']
    long_wr = len([t for t in long_trades if t.get('pnl_pct', 0) > 0]) / len(long_trades) * 100 if long_trades else 0
    short_wr = len([t for t in short_trades if t.get('pnl_pct', 0) > 0]) / len(short_trades) * 100 if short_trades else 0
    st.caption(f"做多: {len(long_trades)}笔 (胜率{long_wr:.0f}%) | 做空: {len(short_trades)}笔 (胜率{short_wr:.0f}%)")

    if oos_m:
        c1, c2 = st.columns(2)
        decay = oos_m['total_return'] - metrics['total_return']
        c1.metric("训练集", f"{metrics['total_return']:+.1f}%")
        c2.metric("测试集", f"{oos_m['total_return']:+.1f}%", delta=f"{decay:+.1f}%", delta_color="inverse")

    # === 权益曲线 ===
    st.subheader("💰 权益曲线")
    ec = result.get('equity_curve', [])
    if ec:
        times = [e['timestamp'] for e in ec]; eqs = [e['equity'] for e in ec]
        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scattergl(x=times, y=eqs, mode='lines', name='权益',
                                     line=dict(color='#818cf8', width=2),
                                     fill='tozeroy', fillcolor='rgba(129,140,248,0.1)'))
        fig_eq.add_hline(y=initial_capital, line_dash="dash", line_color="gray")
        fig_eq.update_layout(height=350, template="plotly_dark", margin=dict(l=0,r=0,t=0,b=0),
                              paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
        st.plotly_chart(fig_eq, use_container_width=True)

    # === K线图 (带缩放按钮) ===
    st.subheader("📈 K线图 & 交易信号")
    # 缩放按钮
    zc1, zc2, zc3, zc4, zc5, zc6 = st.columns([1,1,1,1,1,2])
    zoom_n = min(2000, len(df_train))  # 默认最多2000根, 点[全部]显示所有
    tf_hours = {"5m": 1/12, "15m": 0.25, "1h": 1, "4h": 4, "1d": 24}
    if zc1.button("1月", use_container_width=True, key="z1m"): zoom_n = int(30 * 24 / tf_hours.get(timeframe, 4))
    if zc2.button("6月", use_container_width=True, key="z6m"): zoom_n = int(180 * 24 / tf_hours.get(timeframe, 4))
    if zc3.button("1年", use_container_width=True, key="z1y"): zoom_n = int(365 * 24 / tf_hours.get(timeframe, 4))
    if zc4.button("3年", use_container_width=True, key="z3y"): zoom_n = int(1095 * 24 / tf_hours.get(timeframe, 4))
    if zc5.button("全部", use_container_width=True, key="zall"): zoom_n = len(df_train)
    zc6.caption(f"显示最近 {min(zoom_n, len(df_train))} 根")

    df_show = df_train.tail(zoom_n)
    fig_kl = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.7, 0.3])

    # 计算成交额 (Quote Volume)
    df_show["quote_vol"] = ((df_show["high"] + df_show["low"] + df_show["close"]) / 3 * df_show["vol"])

    fig_kl.add_trace(go.Candlestick(
        x=df_show.index, open=df_show['open'], high=df_show['high'],
        low=df_show['low'], close=df_show['close'],
        name='K线', increasing_line_color='#26a69a', decreasing_line_color='#ef5350',
        showlegend=False,
        hovertemplate=(
            "时间: %{x}<br>"
            "开: %{open:.2f}<br>高: %{high:.2f}<br>"
            "低: %{low:.2f}<br>收: %{close:.2f}<extra></extra>"
        ),
    ), row=1, col=1)

    # 叠加EMA
    if 'ema_fast' in df_show.columns:
        fig_kl.add_trace(go.Scattergl(x=df_show.index, y=df_show['ema_fast'], mode='lines',
                                     line=dict(color='#FFD700', width=1), name='EMA快'), row=1, col=1)
        fig_kl.add_trace(go.Scattergl(x=df_show.index, y=df_show['ema_slow'], mode='lines',
                                     line=dict(color='#FF6B6B', width=1), name='EMA慢'), row=1, col=1)

    # 布林带
    if 'bb_upper' in df_show.columns:
        fig_kl.add_trace(go.Scattergl(x=df_show.index, y=df_show['bb_upper'], mode='lines',
                                     line=dict(color='gray', width=0.5, dash='dot'), name='BB上'), row=1, col=1)
        fig_kl.add_trace(go.Scattergl(x=df_show.index, y=df_show['bb_lower'], mode='lines',
                                     line=dict(color='gray', width=0.5, dash='dot'), name='BB下'), row=1, col=1)

    # 交易标记
    trades_list = result.get('trades', [])
    closed = [t for t in trades_list if t.get('reason') in ('TP', 'SL', 'EOD')]
    ts_start = str(df_show.index[0]); ts_end = str(df_show.index[-1])
    buy_t, buy_p = [], []; sell_t, sell_p = [], []
    for t in closed:
        ot = t.get('open_time', '')
        if ts_start <= ot <= ts_end:
            if t['side'] == 'LONG': buy_t.append(ot); buy_p.append(t['entry'])
            else: sell_t.append(ot); sell_p.append(t['entry'])
    if buy_t:
        fig_kl.add_trace(go.Scattergl(x=buy_t, y=buy_p, mode='markers',
                                     marker=dict(symbol='triangle-up', size=12, color='#22c55e'),
                                     name='做多'), row=1, col=1)
    if sell_t:
        fig_kl.add_trace(go.Scattergl(x=sell_t, y=sell_p, mode='markers',
                                     marker=dict(symbol='triangle-down', size=12, color='#ef4444'),
                                     name='做空'), row=1, col=1)

    colors = ['#26a69a' if df_show['close'].iloc[i] >= df_show['open'].iloc[i] else '#ef5350' for i in range(len(df_show))]
    fig_kl.add_trace(go.Bar(
        x=df_show.index, y=df_show['vol'], marker_color=colors, opacity=0.4,
        name='成交量', showlegend=False,
        hovertemplate="时间: %{x}<br>成交量: %{y:,.0f}<br>成交额: %{customdata:,.0f} USDT<extra></extra>",
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
    fig_kl.update_yaxes(title_text="价格", range=[y_lo, y_hi], autorange=False, row=1, col=1)
    fig_kl.update_yaxes(title_text="量", autorange=True, row=2, col=1)
    st.plotly_chart(fig_kl, use_container_width=True,
                    config={'responsive': True, 'displayModeBar': False,
                            'scrollZoom': False})

    # 共振对比 (仅当启用共振时显示)
    if resonance_enabled and res_f1 and closed:
        strong = [t for t in closed if t.get('resonance_score', 0) >= 3]
        weak = [t for t in closed if 1 <= t.get('resonance_score', 0) <= 2]
        if strong or weak:
            st.subheader("🔗 共振效果对比")
            rc1, rc2, rc3, rc4 = st.columns(4)
            def _wr(ts): return len([t for t in ts if t.get('pnl_pct',0)>0])/len(ts)*100 if ts else 0
            def _ar(ts): return sum(t.get('pnl_pct',0) for t in ts) if ts else 0
            rc1.metric("类型", "强信号(3分共振)", delta=f"{len(strong)}笔")
            rc2.metric("胜率", f"{_wr(strong):.0f}%", delta=f"{_wr(strong)-_wr(closed):+.0f}% vs 全部")
            rc3.metric("累计盈亏", f"{_ar(strong):+.1f}%")
            rc4.metric("弱信号(1-2分)", f"{len(weak)}笔", delta=f"胜率{_wr(weak):.0f}%")

    # 交易记录
    if closed:
        st.subheader("📋 最近交易")
        rows = [{"时间": t.get('close_time','')[:16], "币种": t.get('coin',coin),
                 "方向": t.get('side','?'), "入场": f"${t.get('entry',0):.2f}",
                 "出场": f"${t.get('exit',0):.2f}", "原因": t.get('reason','?'),
                 "盈亏%": f"{t.get('pnl_pct',0):+.2f}%"} for t in closed[-15:]]
        st.dataframe(pd.DataFrame(rows[::-1]), use_container_width=True, height=300)

else:
    st.info("👆 左侧配置策略 & 指标 → 点【运行策略回测】")

    # === 图表专属周期切换器 (独立于回测周期) ===
    chart_periods = ["5m", "15m", "1h", "4h", "1D"]
    if "chart_period" not in st.session_state:
        st.session_state.chart_period = timeframe  # 初始同步侧边栏

    st.divider()
    st.subheader(f"📈 {coin} 数据预览")
    cc_cols = st.columns([1, 1, 1, 1, 1, 3])
    for i, period in enumerate(chart_periods):
        col = cc_cols[i]
        is_active = st.session_state.chart_period == period
        if col.button(period, use_container_width=True,
                      type="primary" if is_active else "secondary",
                      key=f"cp_{period}"):
            st.session_state.chart_period = period
            st.rerun()

    try:
        # 缓存加载 + 缓存重采样
        df_15m = load_cached_15min(coin)
        df_15m.index = pd.to_datetime(df_15m.index).tz_localize(None)
        dr = st.session_state.date_range
        if dr:
            df_15m = df_15m.loc[dr[0]:dr[1]]

        chart_period = st.session_state.chart_period
        df_pv = resample_cached(df_15m, chart_period)

        print(f"[预览] {coin} {chart_period}: {len(df_pv)}根, "
              f"{df_pv.index.min()} ~ {df_pv.index.max()}", flush=True)

        if len(df_pv) < 2:
            st.warning("所选时间范围数据不足")
            st.stop()

        # 摘要指标
        mc1, mc2, mc3, mc4, mc5 = st.columns(5)
        mc1.metric("K线数", f"{len(df_pv):,}")
        mc2.metric("起始", str(df_pv.index[0])[:16])
        mc3.metric("结束", str(df_pv.index[-1])[:16])
        chg = (df_pv['close'].iloc[-1] / df_pv['close'].iloc[0] - 1) * 100
        mc4.metric("涨跌幅", f"{chg:+.1f}%")
        mc5.metric("最新价", f"${df_pv['close'].iloc[-1]:.2f}")

        # === 双子图: 最多显示1000根, 防卡顿 ===
        MAX_BARS = 1000
        df_show = df_pv if len(df_pv) <= MAX_BARS else df_pv.tail(MAX_BARS)
        if len(df_pv) > MAX_BARS:
            st.caption(f"数据共 {len(df_pv):,} 根, 图表显示最近 {MAX_BARS:,} 根 (流畅渲染)")
        # DEBUG
        july_bars = len(df_pv["2026-07-01":"2026-07-31"]) if len(df_pv) > 0 else 0
        st.sidebar.info(f"🔍 数据: {len(df_pv):,}根 | {df_pv.index[0]} ~ {df_pv.index[-1]} | 7月={july_bars}根")

        fig_pv = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            vertical_spacing=0.02, row_heights=[0.75, 0.25],
        )

        # 上图: OHLC 蜡烛
        fig_pv.add_trace(go.Candlestick(
            x=df_show.index,
            open=df_show['open'], high=df_show['high'],
            low=df_show['low'], close=df_show['close'],
            name="K线",
            increasing_line_color='#26a69a', decreasing_line_color='#ef5350',
            showlegend=False,
            hovertemplate=(
                "时间: %{x}<br>"
                "开: %{open:.2f}<br>"
                "高: %{high:.2f}<br>"
                "低: %{low:.2f}<br>"
                "收: %{close:.2f}<extra></extra>"
            ),
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
            name="成交量",
            hovertemplate=(
                "时间: %{x}<br>"
                "成交量: %{y:,.0f}<br>"
                + ("成交额: %{customdata:,.0f} USDT<extra></extra>" if has_qv else "<extra></extra>")
            ),
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
        fig_pv.update_yaxes(title_text="价格 (USD)", range=[y_lo, y_hi],
                            autorange=False, row=1, col=1)
        fig_pv.update_yaxes(title_text="成交量", autorange=True, row=2, col=1)

        st.plotly_chart(fig_pv, use_container_width=True,
                        config={'responsive': True, 'displayModeBar': False,
                                'scrollZoom': False})

    except FileNotFoundError as e:
        st.warning(f"数据文件未就绪, 正在后台下载中。请稍后刷新页面。")
        st.caption(f"详情: {e}")
    except Exception as e:
        st.warning(f"预览加载失败: {e}")
