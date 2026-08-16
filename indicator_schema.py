"""
QuantCode 指标元数据 Schema（从 app.py 抽离）
============================================================
单一事实来源：指标名称/分类/描述/参数 + 纯 pandas 计算函数。
app.py 通过 `from indicator_schema import INDICATOR_SCHEMA, INDICATOR_REGISTRY` 复用。
研究 Agent 通过本模块读取平台能力（不 import app.py，避免触发 Streamlit 副作用）。
"""
import pandas as pd
import numpy as np


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

def _volume_ratio(df, period, threshold):
    """量比 = volume / SMA(volume, period), 超过阈值=放量(方向中性, 供组合过滤)"""
    v = df['vol'].shift(1)
    ma = v.rolling(period).mean()
    vr = v / (ma + 1e-9)
    df['volume_ratio'] = vr
    df['_long'] = vr > threshold
    df['_short'] = vr > threshold

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
            "FIB_lookback": {"label": "回看K线数", "default": 50, "min": 50, "max": 500, "step": 50, "help": "计算高低点的回看周期（50~500）"},
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
            "VOL_ma": {"label": "均量周期", "default": 20, "min": 10, "max": 100, "help": "成交量均线周期"},
            "VOL_mult": {"label": "放大倍数", "default": 1.5, "min": 1.0, "max": 5.0, "step": 0.1, "help": "量>均量*倍数视为放量"},
        },
        "compute": lambda df, p: _vol_breakout(df, p["VOL_ma"], p["VOL_mult"]),
    },
    "volume_ratio": {
        "name": "量比 Volume Ratio", "category": "成交量",
        "desc": "量比 > 阈值 = 放量(方向中性, 可与其他指标组合)",
        "params": {
            "VR_period": {"label": "均量周期", "default": 20, "min": 5, "max": 100, "help": "成交量SMA周期"},
            "VR_threshold": {"label": "量比阈值", "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1, "help": "量比超过此值视为放量"},
        },
        "compute": lambda df, p: _volume_ratio(df, p["VR_period"], p["VR_threshold"]),
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
        "params": _reg_params,
        "param_labels": _param_labels,
        "desc": _schema["desc"],
        "compute": _schema["compute"],
    }
