"""
马总量化控制台 v3.2 — 百种指标积木 + AI策略导师
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
    st.markdown("<h1 style='text-align:center;margin-top:60px'>马总量化控制台 v3.2</h1>", unsafe_allow_html=True)
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
st.set_page_config(page_title="马总量化 v3.2", page_icon="📊", layout="wide", initial_sidebar_state="expanded")
st.markdown("""<style>
@media (max-width:768px){body{padding:4px!important;font-size:13px!important}.stButton>button{padding:10px!important;font-size:14px!important}h1{font-size:18px!important}}
.metric-card{background:#1e293b;border-radius:10px;padding:16px;text-align:center;margin:4px 0}
.metric-card .value{font-size:28px;font-weight:800}.metric-card .label{font-size:11px;opacity:.5;margin-top:2px}
.g{color:#22c55e}.r{color:#ef4444}.b{color:#60a5fa}.y{color:#eab308}
.stButton>button{width:100%}hr{margin:8px 0}
.zoom-btn{font-size:11px!important;padding:4px 8px!important}
</style>""", unsafe_allow_html=True)

# 登录鉴权拦截 (放在所有UI之前, 登录页即使数据未就绪也能展示)
logged_in = check_login()
if not logged_in:
    st.stop()

# ============================================================
# 百种指标注册表 (纯 pandas/numpy 实现, 无需 TA-Lib)
# ============================================================
INDICATOR_REGISTRY = {
    # ---- 趋势类 ----
    "EMA 双均线": {
        "category": "趋势类", "params": {"快线": (2, 50, 5), "慢线": (5, 200, 20)},
        "desc": "快线上穿慢线=做多, 下穿=做空",
        "compute": lambda df, p: _ema_cross(df, p["快线"], p["慢线"]),
    },
    "SMA 三均线": {
        "category": "趋势类", "params": {"短": (2, 30, 5), "中": (5, 60, 10), "长": (10, 120, 30)},
        "desc": "短>中>长=多头排列",
        "compute": lambda df, p: _sma_align(df, p["短"], p["中"], p["长"]),
    },
    "SuperTrend 超级趋势": {
        "category": "趋势类", "params": {"ATR周期": (5, 30, 10), "乘数": (1.0, 5.0, 3.0, 0.5)},
        "desc": "价格上穿SuperTrend=做多, 下穿=做空",
        "compute": lambda df, p: _supertrend(df, p["ATR周期"], p["乘数"]),
    },
    "ADX/DMI 趋势强度": {
        "category": "趋势类", "params": {"周期": (5, 30, 14), "阈值": (10, 50, 25)},
        "desc": "+DI>-DI 且 ADX>阈值=做多",
        "compute": lambda df, p: _adx_signal(df, p["周期"], p["阈值"]),
    },
    "Ichimoku 一目均衡": {
        "category": "趋势类", "params": {"转换线": (5, 20, 9), "基准线": (10, 40, 26)},
        "desc": "价格>云层=做多, <云层=做空",
        "compute": lambda df, p: _ichimoku(df, p["转换线"], p["基准线"]),
    },
    "Parabolic SAR": {
        "category": "趋势类", "params": {"步长": (0.01, 0.1, 0.02, 0.01), "最大": (0.1, 0.5, 0.2, 0.05)},
        "desc": "SAR反转=反向信号",
        "compute": lambda df, p: _psar(df, p["步长"], p["最大"]),
    },

    # ---- 摆动类 ----
    "RSI 相对强弱": {
        "category": "摆动类", "params": {"周期": (2, 50, 14), "超卖": (10, 40, 30), "超买": (60, 90, 70)},
        "desc": "RSI<超卖=做多, RSI>超买=做空",
        "compute": lambda df, p: _rsi_signal(df, p["周期"], p["超卖"], p["超买"]),
    },
    "KDJ 随机指标": {
        "category": "摆动类", "params": {"N": (2, 20, 9), "M1": (2, 10, 3), "M2": (2, 10, 3)},
        "desc": "K上穿D且<30=做多, K下穿D且>70=做空",
        "compute": lambda df, p: _kdj_signal(df, p["N"], p["M1"], p["M2"]),
    },
    "MACD 异同均线": {
        "category": "摆动类", "params": {"快": (2, 30, 12), "慢": (5, 50, 26), "信号": (2, 20, 9)},
        "desc": "MACD上穿Signal=做多, 下穿=做空",
        "compute": lambda df, p: _macd_signal(df, p["快"], p["慢"], p["信号"]),
    },
    "CCI 商品通道": {
        "category": "摆动类", "params": {"周期": (5, 50, 20), "超卖": (-200, -50, -100), "超买": (50, 200, 100)},
        "desc": "CCI<超卖=做多, CCI>超买=做空",
        "compute": lambda df, p: _cci_signal(df, p["周期"], p["超卖"], p["超买"]),
    },
    "StochRSI": {
        "category": "摆动类", "params": {"周期": (5, 30, 14), "超卖": (10, 40, 20), "超买": (60, 90, 80)},
        "desc": "StochRSI<超卖=做多, >超买=做空",
        "compute": lambda df, p: _stochrsi(df, p["周期"], p["超卖"], p["超买"]),
    },
    "Williams %R": {
        "category": "摆动类", "params": {"周期": (5, 30, 14), "超卖": (-100, -50, -80), "超买": (-50, 0, -20)},
        "desc": "%R<超卖=做多, %R>超买=做空",
        "compute": lambda df, p: _willr(df, p["周期"], p["超卖"], p["超买"]),
    },
    "Awesome Oscillator": {
        "category": "摆动类", "params": {},
        "desc": "AO上穿0轴=做多, 下穿=做空",
        "compute": lambda df, p: _ao_signal(df),
    },

    # ---- 通道/支撑阻力类 ----
    "布林带 Bollinger": {
        "category": "通道/支撑", "params": {"周期": (5, 50, 20), "标准差": (1.0, 4.0, 2.0, 0.5)},
        "desc": "价格触下轨=做多, 触上轨=做空",
        "compute": lambda df, p: _bb_signal(df, p["周期"], p["标准差"]),
    },
    "Keltner 通道": {
        "category": "通道/支撑", "params": {"EMA周期": (5, 50, 20), "ATR倍数": (1.0, 4.0, 2.0, 0.5)},
        "desc": "价格触下轨=做多, 触上轨=做空",
        "compute": lambda df, p: _keltner(df, p["EMA周期"], p["ATR倍数"]),
    },
    "Donchian 通道": {
        "category": "通道/支撑", "params": {"周期": (5, 100, 20)},
        "desc": "突破上轨=做多, 突破下轨=做空",
        "compute": lambda df, p: _donchian(df, p["周期"]),
    },
    "斐波那契回调": {
        "category": "通道/支撑", "params": {"回看K线": (20, 200, 50)},
        "desc": "回调到0.382/0.618=做多, 跌破0.618=做空",
        "compute": lambda df, p: _fibonacci(df, p["回看K线"]),
    },

    # ---- 成交量类 ----
    "OBV 能量潮": {
        "category": "成交量", "params": {"MA周期": (5, 50, 20)},
        "desc": "OBV上穿MA=做多, 下穿=做空",
        "compute": lambda df, p: _obv_signal(df, p["MA周期"]),
    },
    "VWAP 均价": {
        "category": "成交量", "params": {},
        "desc": "价格>VWAP=做多, <VWAP=做空",
        "compute": lambda df, p: _vwap_signal(df),
    },
    "MFI 资金流量": {
        "category": "成交量", "params": {"周期": (5, 30, 14), "超卖": (10, 40, 20), "超买": (60, 90, 80)},
        "desc": "MFI<超卖=做多, >超买=做空",
        "compute": lambda df, p: _mfi_signal(df, p["周期"], p["超卖"], p["超买"]),
    },
    "CMF 柴金流量": {
        "category": "成交量", "params": {"周期": (5, 50, 20)},
        "desc": "CMF>0=做多, <0=做空",
        "compute": lambda df, p: _cmf_signal(df, p["周期"]),
    },
    "成交量突破": {
        "category": "成交量", "params": {"均量周期": (5, 50, 20), "倍数": (1.0, 5.0, 1.5, 0.1)},
        "desc": "量>均量*倍数 + 收阳=做多, 收阴=做空",
        "compute": lambda df, p: _vol_breakout(df, p["均量周期"], p["倍数"]),
    },

    # ---- K线形态 ----
    "锤头/倒锤 (Hammer)": {
        "category": "K线形态", "params": {},
        "desc": "下影线>实体2倍=锤头做多, 上影线>实体2倍=倒锤做空",
        "compute": lambda df, p: _hammer(df),
    },
    "吞没形态 (Engulfing)": {
        "category": "K线形态", "params": {},
        "desc": "阳包阴=做多, 阴包阳=做空",
        "compute": lambda df, p: _engulfing(df),
    },
    "早晨/黄昏之星": {
        "category": "K线形态", "params": {},
        "desc": "早晨之星=做多, 黄昏之星=做空",
        "compute": lambda df, p: _star(df),
    },
    "三连兵 (Three Soldiers)": {
        "category": "K线形态", "params": {},
        "desc": "三连阳=做多, 三连阴=做空",
        "compute": lambda df, p: _three_soldiers(df),
    },
    "十字星 (Doji)": {
        "category": "K线形态", "params": {"影线比": (0.5, 2.0, 1.0, 0.1)},
        "desc": "下影线>上影线=做多, 反之=做空",
        "compute": lambda df, p: _doji(df, p["影线比"]),
    },
    "Pinbar 反转": {
        "category": "K线形态", "params": {},
        "desc": "长下影线拒绝低位=做多, 长上影线拒绝高位=做空",
        "compute": lambda df, p: _pinbar(df),
    },
}


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
            if not cfg.get("enabled"): continue
            info = INDICATOR_REGISTRY.get(name)
            if not info: continue
            try:
                info["compute"](df, cfg.get("params", {}))
                if "_long" in df.columns: long_conds.append(df["_long"]); df.drop("_long", axis=1, inplace=True)
                if "_short" in df.columns: short_conds.append(df["_short"]); df.drop("_short", axis=1, inplace=True)
            except: pass

        if not long_conds and not short_conds:
            df['signal'] = 0
        else:
            ls = long_conds[0] if long_conds else pd.Series(False, index=df.index)
            for c in long_conds[1:]: ls = ls & c if self.use_and else ls | c
            ss = short_conds[0] if short_conds else pd.Series(False, index=df.index)
            for c in short_conds[1:]: ss = ss & c if self.use_and else ss | c
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

        df['score'] = abs(df['signal'])
        return df


# ============================================================
# Sidebar: 基础设置
# ============================================================
st.sidebar.title("📊 控制台 v3.1")

with st.sidebar.expander("📋 基础设置", expanded=True):
    coin = st.selectbox("标的", ["ETH", "BTC", "SOL"], 0)
    timeframe = st.selectbox("K线周期", ["15m", "1h", "4h", "1d"], 2)
    leverage = st.slider("杠杆", 1, 20, 3, 1)
    initial_capital = st.number_input("初始资金", 100, 1000000, 10000, 1000)

# ============================================================
# Sidebar: 时间范围 (session_state修复)
# ============================================================
if "date_range" not in st.session_state: st.session_state.date_range = None

with st.sidebar.expander("📅 时间范围 & OOS", expanded=True):
    c1, c2, c3 = st.columns(3)
    preset_dates = {
        "🐂 2021牛": ("2021-01-01", "2021-12-31"),
        "🐻 2022熊": ("2022-01-01", "2022-12-31"),
        "📈 23-24震": ("2023-01-01", "2024-12-31"),
        "🔄 近1年": ((datetime.now()-timedelta(days=365)).strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d")),
        "📊 近3年": ((datetime.now()-timedelta(days=1095)).strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d")),
        "🔁 全部": None,
    }
    for label, dr in preset_dates.items():
        btn_key = f"preset_{label}"
        if (c1 if "牛" in label else c2 if "熊" in label or "震" in label else c3).button(label, use_container_width=True, key=btn_key):
            st.session_state.date_range = dr; st.rerun()

    d1 = st.date_input("起始", datetime(2020, 1, 1), key="date_start")
    d2 = st.date_input("结束", datetime(2026, 6, 30), key="date_end")
    if st.button("应用手动日期"):
        st.session_state.date_range = (d1.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d"))
        st.rerun()

    if st.session_state.date_range:
        st.caption(f"当前: {st.session_state.date_range[0]} ~ {st.session_state.date_range[1]}")
    else:
        st.caption("当前: 全部历史")

    st.divider()
    oos_enabled = st.checkbox("样本外测试 (OOS)", False)
    oos_ratio = st.slider("训练集%", 50, 90, 70, 5, disabled=not oos_enabled)

# ============================================================
# Sidebar: 指标积木 (按分类)
# ============================================================
# 按分类组织
categories = {}
for name, info in INDICATOR_REGISTRY.items():
    cat = info["category"]
    if cat not in categories: categories[cat] = []
    categories[cat].append(name)

if "selected_indicators" not in st.session_state:
    st.session_state.selected_indicators = {"EMA 双均线": {"enabled": True, "params": {"快线": 5, "慢线": 20}}}

with st.sidebar.expander("🧱 指标积木 (60+种)", expanded=False):
    logic_mode = st.radio("触发逻辑", ["AND 全部满足", "OR 任一满足"], horizontal=True, key="logic_mode")
    use_and = "AND" in logic_mode

    for cat_name, ind_names in categories.items():
        st.caption(f"▸ {cat_name} ({len(ind_names)}种)")
        for name in ind_names:
            info = INDICATOR_REGISTRY[name]
            sel = st.session_state.selected_indicators
            checked = name in sel and sel[name].get("enabled", False)
            new_checked = st.checkbox(name, checked, key=f"ind_{name}", help=info["desc"])
            if new_checked and name not in sel:
                sel[name] = {"enabled": True, "params": {k: v[2] for k, v in info["params"].items()}}
            elif not new_checked and name in sel:
                sel[name]["enabled"] = False

            if new_checked and info["params"]:
                with st.container():
                    cols = st.columns(min(3, len(info["params"])))
                    for i, (pname, prange) in enumerate(info["params"].items()):
                        step = prange[3] if len(prange) > 3 else 1
                        val = cols[i % 3].number_input(
                            f"{name[:8]}-{pname}", prange[0], prange[1],
                            sel[name]["params"].get(pname, prange[2]), step,
                            key=f"p_{name}_{pname}", label_visibility="collapsed"
                        )
                        sel[name]["params"][pname] = val

# ============================================================
# Sidebar: 风控 + 多因子
# ============================================================
with st.sidebar.expander("🛡️ 风控", expanded=False):
    tp_pct = st.slider("止盈 (保证金%)", 2.0, 50.0, 10.0, 0.5)
    sl_pct = st.slider("止损 (保证金%)", 1.0, 30.0, 5.0, 0.5)
    oco = st.checkbox("服务器端OCO挂单", True)
    c1, c2, c3 = st.columns(3)
    bull_a = c1.number_input("牛市%", 10, 100, 100, 5) / 100
    range_a = c2.number_input("震荡%", 10, 100, 50, 5) / 100
    bear_a = c3.number_input("熊市%", 0, 100, 30, 5) / 100
    lock_streak = st.number_input("连亏锁仓(笔)", 1, 10, 3)
    lock_days = st.number_input("锁仓天数", 1, 30, 2)

with st.sidebar.expander("🔬 多因子牛熊", expanded=False):
    mf_enabled = st.checkbox("启用", True, key="mf_on")
    c1, c2 = st.columns(2)
    ema_w = c1.slider("EMA权重", 0.0, 1.0, 0.40, 0.05, key="mf_ew")
    adx_w = c2.slider("ADX权重", 0.0, 1.0, 0.35, 0.05, key="mf_aw")
    adx_th = st.slider("ADX阈值", 10, 50, 25, 5, key="mf_at")
    bull_th = st.slider("牛市判定", 0.10, 0.60, 0.30, 0.05, key="mf_bt")

# 导出
st.sidebar.divider()
cur_params = {"coin": coin, "tf": timeframe, "lev": leverage, "cap": initial_capital,
              "tp": tp_pct, "sl": sl_pct, "indicators": list(st.session_state.selected_indicators.keys())}
st.sidebar.markdown(export_json(cur_params), unsafe_allow_html=True)
if st.sidebar.button("登出"): st.session_state.logged_in = False; st.rerun()

# ============================================================
# AI 策略导师 (侧边栏底部可折叠聊天区)
# ============================================================
def build_ai_context():
    """构建当前策略状态的上下文, 注入AI对话"""
    ctx = f"""【当前策略环境】
标的: {coin} | K线周期: {timeframe} | 杠杆: {leverage}x | 初始资金: ${initial_capital:,}
止盈: {tp_pct}% | 止损: {sl_pct}% | 牛市仓位: {int(bull_a*100)}% 震荡: {int(range_a*100)}% 熊市: {int(bear_a*100)}%
牛熊过滤器: {'开启' if mf_enabled else '关闭'} (EMA权重{ema_w} ADX权重{adx_w} ADX阈值{adx_th} 牛市阈值{bull_th})
信号逻辑: {'AND(全部满足)' if use_and else 'OR(任一满足)'}
已选指标: {', '.join(active_inds) if active_inds else '无'}
时间范围: {st.session_state.date_range or '全部历史'}
OOS: {'开启 ' + str(oos_ratio) + '%训练' if oos_enabled else '关闭'}
---
你是翔哥, 马总的专属量化策略导师。基于上面的策略配置, 帮马总分析问题、优化参数、诊断回测结果。
用简洁的中文回答, 直接给结论和建议, 不要废话。"""
    return ctx

st.sidebar.divider()
with st.sidebar.expander("🤖 AI 策略导师 (翔哥)", expanded=False):
    # API 配置
    if "ai_api_key" not in st.session_state:
        st.session_state.ai_api_key = os.environ.get("AI_API_KEY", "")
    if "ai_provider" not in st.session_state:
        st.session_state.ai_provider = "anthropic"
    if "ai_messages" not in st.session_state:
        st.session_state.ai_messages = []

    api_key = st.text_input("API Key", value=st.session_state.ai_api_key,
                            type="password", key="ai_key_input",
                            placeholder="sk-ant-... 或 sk-...",
                            help="Anthropic 或 OpenAI API Key")
    if api_key: st.session_state.ai_api_key = api_key

    provider = st.selectbox("模型", ["anthropic (Claude)", "openai (GPT)"],
                            index=0 if "anthropic" in st.session_state.ai_provider else 1,
                            key="ai_provider_select")
    st.session_state.ai_provider = "anthropic" if "anthropic" in provider else "openai"

    # 聊天历史
    for msg in st.session_state.ai_messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    # 输入框
    user_input = st.chat_input("问翔哥...", key="ai_chat_input")
    if user_input and st.session_state.ai_api_key:
        st.session_state.ai_messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"): st.write(user_input)

        # 构建完整 prompt
        system_prompt = build_ai_context()
        messages = [{"role": "system", "content": system_prompt}]
        # 最近5轮对话
        for m in st.session_state.ai_messages[-10:]:
            messages.append(m)

        with st.chat_message("assistant"):
            with st.spinner("翔哥思考中..."):
                try:
                    reply = call_ai_api(messages, st.session_state.ai_provider,
                                        st.session_state.ai_api_key)
                    st.write(reply)
                    st.session_state.ai_messages.append({"role": "assistant", "content": reply})
                except Exception as e:
                    st.error(f"API调用失败: {e}")

    elif user_input and not st.session_state.ai_api_key:
        st.warning("请先填入 API Key")
    elif not user_input:
        st.caption("输入问题, 翔哥帮你分析策略~")
        if st.button("诊断当前策略", use_container_width=True, key="ai_diag"):
            if st.session_state.ai_api_key:
                auto_prompt = f"请诊断我当前的策略配置, 给出3条具体优化建议。"
                st.session_state.ai_messages = [{"role": "user", "content": auto_prompt}]
                st.rerun()
            else:
                st.warning("请先填入 API Key")

    if st.button("清空对话", use_container_width=True, key="ai_clear"):
        st.session_state.ai_messages = []; st.rerun()


# ============================================================
# AI API 调用 (流式输出)
# ============================================================
def call_ai_api(messages: list, provider: str, api_key: str) -> str:
    """调用 Anthropic 或 OpenAI API"""
    import requests

    if "anthropic" in provider:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        # 分离 system prompt
        system_msg = next((m for m in messages if m["role"] == "system"), None)
        chat_msgs = [m for m in messages if m["role"] != "system"]
        body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": chat_msgs,
        }
        if system_msg:
            body["system"] = system_msg["content"]

        resp = requests.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            raise Exception(f"API错误 {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return data["content"][0]["text"]

    else:  # OpenAI
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": "gpt-4o-mini",
            "max_tokens": 1024,
            "messages": messages,
        }
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            raise Exception(f"API错误 {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]

# ============================================================
# 主界面头部
# ============================================================
st.title("📊 翔哥量化回测控制台 v3.1")
p1, p2, p3, p4 = st.columns(4)
p1.metric("标的", coin); p2.metric("周期", timeframe); p3.metric("杠杆", f"{leverage}x"); p4.metric("资金", f"${initial_capital:,}")

# 已选指标摘要
active_inds = [n for n, c in st.session_state.selected_indicators.items() if c.get("enabled")]
st.caption(f"已选指标 ({len(active_inds)}): " + ", ".join(active_inds[:10]) + ("..." if len(active_inds) > 10 else ""))

st.divider()
if st.button("🚀 运行策略回测", type="primary", use_container_width=True):
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
        strategy = DynamicStrategy(
            selected=st.session_state.selected_indicators,
            use_and=use_and,
            mf_params={"enabled": mf_enabled, "ema_w": ema_w, "adx_w": adx_w, "adx_th": adx_th, "bull_th": bull_th},
        )

        # 回测
        lock_bars = int(lock_days * 6) if timeframe == '4h' else int(lock_days * 24)
        engine = BacktestEngineV2(
            initial_capital=initial_capital, leverage=leverage, tp_pct=tp_pct, sl_pct=sl_pct,
            max_positions=1, bull_alloc=bull_a, range_alloc=range_a, bear_alloc=bear_a,
            lock_streak=int(lock_streak), lock_bars=lock_bars, cooldown_bars=2, verbose=False,
        )
        result = engine.run({coin: df_train}, strategy)
        metrics = PerformanceAnalyzer.analyze(result)

    # OOS
    oos_m = None
    if oos_enabled and df_test is not None and len(df_test) > 200:
        with st.spinner("样本外测试..."):
            e2 = BacktestEngineV2(
                initial_capital=initial_capital, leverage=leverage, tp_pct=tp_pct, sl_pct=sl_pct,
                max_positions=1, bull_alloc=bull_a, range_alloc=range_a, bear_alloc=bear_a,
                lock_streak=int(lock_streak), lock_bars=lock_bars, cooldown_bars=2, verbose=False,
            )
            r2 = e2.run({coin: df_test}, strategy)
            oos_m = PerformanceAnalyzer.analyze(r2)

    # === 指标卡片 ===
    st.subheader("📈 回测结果")
    c = st.columns(7)
    c[0].metric("总收益", f"{metrics.get('total_return',0):+.1f}%")
    c[1].metric("年化", f"{metrics.get('annual_return',0):+.1f}%")
    c[2].metric("最大回撤", f"{metrics.get('max_drawdown',0):.1f}%")
    c[3].metric("胜率", f"{metrics.get('win_rate',0):.1f}%")
    c[4].metric("盈亏比", f"{metrics.get('profit_factor',0):.2f}" if metrics.get('profit_factor') != float('inf') else "inf")
    c[5].metric("交易数", metrics.get('total_trades', 0))
    final_eq = result.get('final_equity', initial_capital)
    c[6].metric("最终权益", f"${final_eq:,.0f}")

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
        fig_eq.add_trace(go.Scatter(x=times, y=eqs, mode='lines', name='权益',
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
    if zc1.button("1月", use_container_width=True, key="z1m"): zoom_n = int(30 * 24 / {"15m":0.25,"1h":1,"4h":4,"1d":24}[timeframe])
    if zc2.button("6月", use_container_width=True, key="z6m"): zoom_n = int(180 * 24 / {"15m":0.25,"1h":1,"4h":4,"1d":24}[timeframe])
    if zc3.button("1年", use_container_width=True, key="z1y"): zoom_n = int(365 * 24 / {"15m":0.25,"1h":1,"4h":4,"1d":24}[timeframe])
    if zc4.button("3年", use_container_width=True, key="z3y"): zoom_n = int(1095 * 24 / {"15m":0.25,"1h":1,"4h":4,"1d":24}[timeframe])
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
        fig_kl.add_trace(go.Scatter(x=df_show.index, y=df_show['ema_fast'], mode='lines',
                                     line=dict(color='#FFD700', width=1), name='EMA快'), row=1, col=1)
        fig_kl.add_trace(go.Scatter(x=df_show.index, y=df_show['ema_slow'], mode='lines',
                                     line=dict(color='#FF6B6B', width=1), name='EMA慢'), row=1, col=1)

    # 布林带
    if 'bb_upper' in df_show.columns:
        fig_kl.add_trace(go.Scatter(x=df_show.index, y=df_show['bb_upper'], mode='lines',
                                     line=dict(color='gray', width=0.5, dash='dot'), name='BB上'), row=1, col=1)
        fig_kl.add_trace(go.Scatter(x=df_show.index, y=df_show['bb_lower'], mode='lines',
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
        fig_kl.add_trace(go.Scatter(x=buy_t, y=buy_p, mode='markers',
                                     marker=dict(symbol='triangle-up', size=12, color='#22c55e'),
                                     name='做多'), row=1, col=1)
    if sell_t:
        fig_kl.add_trace(go.Scatter(x=sell_t, y=sell_p, mode='markers',
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
    chart_periods = ["15m", "1h", "4h", "1D"]
    if "chart_period" not in st.session_state:
        st.session_state.chart_period = timeframe  # 初始同步侧边栏

    st.divider()
    st.subheader(f"📈 {coin} 数据预览")
    cc1, cc2, cc3, cc4, cc5 = st.columns([1, 1, 1, 1, 4])
    for i, period in enumerate(chart_periods):
        col = [cc1, cc2, cc3, cc4][i]
        is_active = st.session_state.chart_period == period
        if col.button(period, use_container_width=True,
                      type="primary" if is_active else "secondary",
                      key=f"cp_{period}"):
            st.session_state.chart_period = period
            st.rerun()

    try:
        de = DataEngine()
        # 始终加载15min基座, 再动态重采样 (首次可能触发下载)
        with st.spinner("加载数据中 (首次可能需要下载, 约2-3分钟)..."):
            df_15m = de.load_15min(coin)
        # 移除时区, 避免索引匹配失败
        df_15m.index = pd.to_datetime(df_15m.index).tz_localize(None)
        dr = st.session_state.date_range
        if dr:
            df_15m = df_15m.loc[dr[0]:dr[1]]
        print(f"[数据] 过滤前: {de.load_15min(coin).index.min()} ~ {de.load_15min(coin).index.max()}, "
              f"过滤后: {df_15m.index.min()} ~ {df_15m.index.max()}, {len(df_15m)}根", flush=True)

        chart_period = st.session_state.chart_period
        if chart_period == "15m":
            df_pv = df_15m.copy()
        else:
            rule_map = {"1h": "1h", "4h": "4h", "1D": "1d"}
            rule = rule_map.get(chart_period, "4h")
            df_pv = df_15m.resample(rule, label="left", closed="left").agg({
                "open": "first", "high": "max", "low": "min", "close": "last",
                "vol": "sum",
            }).dropna()
            # 计算成交额 (Quote Volume): 用典型价格 * 成交量估算
            df_pv["quote_vol"] = ((df_pv["high"] + df_pv["low"] + df_pv["close"]) / 3 * df_pv["vol"])

        # 调试: 打印过滤后数据起止
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

        # === 双子图: 100%完整展示, 不做任何截断 ===
        df_show = df_pv

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
