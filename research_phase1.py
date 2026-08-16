#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 1 策略研究 — 5 个核心候选 × BTC/ETH × 4H × 1x/2x

只读研究脚本: 只调用现有 BacktestEngineV2 / PerformanceAnalyzer /
FutureLeakDetector(引擎内建) / 自研简单 Walk-Forward / Monte Carlo bootstrap,
不修改 engine_core.py 任何交易逻辑, 不写入生产 strategy 模块。

5 个候选:
  S1 EMA + ADX 趋势确认
  S2 Donchian 突破 + 放量确认
  S3 Supertrend + MACD
  S4 Bollinger + RSI + ADX(区间过滤) 均值回归(仅做多)
  S5 MultiFactorRegime 自适应(趋势/均值回归切换)

防未来函数: 所有指标输入先 shift(1), 信号[i] 只用 bar i-1 及更早数据;
引擎用下一根开盘价撮合。FutureLeakDetector 由引擎 run() 内建执行。

验证门禁(全满足才 "进入模拟盘候选"):
  FutureLeak=0, IS/OOS严格分离, WF avg OOS>0, Sharpe>1, MDD<30%,
  MC 5%分位>0, 交易次数>100, 最大连亏<8。
"""
import sys, os, json, time
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd

from engine_core import (DataEngine, StrategyBase, BacktestEngineV2,
                         PerformanceAnalyzer, MultiFactorRegime)

# ============================================================
# 指标助手 (输入均为已 shift(1) 的 series; 与 app.py 公式一致)
# ============================================================
def _adx(h, l, c, period=14):
    """ADX/DMI。h,l,c 已 shift(1)。返回 (adx, +DI, -DI)。"""
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()],
                   axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    up = h.diff(); dn = -l.diff()
    pDM = pd.Series(0.0, index=h.index); nDM = pd.Series(0.0, index=h.index)
    pDM[(up > dn) & (up > 0)] = up; nDM[(dn > up) & (dn > 0)] = dn
    pDI = 100 * pDM.ewm(alpha=1 / period, adjust=False).mean() / atr
    nDI = 100 * nDM.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (pDI - nDI).abs() / (pDI + nDI + 1e-9)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx, pDI, nDI


def _rsi(c, period=14):
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-9))


def _macd(c, fast=12, slow=26, sig=9):
    ef = c.ewm(span=fast, adjust=False).mean()
    es = c.ewm(span=slow, adjust=False).mean()
    macd = ef - es
    return macd, macd.ewm(span=sig, adjust=False).mean()


def _supertrend_trend(h, l, c, atr_p=10, mul=3.0):
    """SuperTrend 方向序列: +1 多头 / -1 空头。h,l,c 已 shift(1)。"""
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()],
                   axis=1).max(axis=1)
    atr = tr.ewm(span=atr_p, adjust=False).mean()
    hl2 = (h + l) / 2
    up = hl2 - mul * atr; dn = hl2 + mul * atr
    trend = pd.Series(1.0, index=h.index)
    n = len(h)
    for i in range(1, n):
        if c.iloc[i] > up.iloc[i - 1]: up.iloc[i] = max(up.iloc[i], up.iloc[i - 1])
        if c.iloc[i] < dn.iloc[i - 1]: dn.iloc[i] = min(dn.iloc[i], dn.iloc[i - 1])
        if c.iloc[i] > dn.iloc[i - 1]: trend.iloc[i] = 1.0
        elif c.iloc[i] < up.iloc[i - 1]: trend.iloc[i] = -1.0
        else: trend.iloc[i] = trend.iloc[i - 1]
    return trend


# ============================================================
# 风控配置 (fixed_risk: 每笔风险 = 权益 × 1% × 市场乘数)
# ============================================================
RISK_CONFIG = {
    "_pos_mode": "fixed_risk",
    "_risk_per_trade": 1.0,   # 单笔风险 1% 权益
    "_use_atr_sl": False,      # Phase 1 用固定 SL (ATR 留 Phase 2)
    # P0: 仓位/加仓默认值 (与引擎默认一致, 供 AI 搜索覆盖)
    "_init_alloc_pct": 30.0,   # 初始建仓比例% (fixed_capital 模式生效)
    "_enable_pyramiding": False,  # 加仓开关
    "_pyr_add_pct": 0.5,       # 加仓比例 (初始保证金的比例, 引擎语义为小数)
    "_pyr_max": 3,             # 最大加仓次数
    "_bull_alloc": 100.0,      # 百分比, 引擎 ÷100
    "_range_alloc": 50.0,
    "_bear_alloc": 30.0,
    # 关键: 用 price_pct 使 TP/SL 价格距离与杠杆无关 (margin_pct 会把止损距离除以杠杆,
    # 2x 时止损距离减半 → 交易翻倍 → 优势被震荡吃掉)。
    "_tp_mode": "price_pct",
    "_sl_mode": "price_pct",
}

# P0: AI 可搜索的仓位/加仓/市场状态参数 (引擎在 run() 时从 strategy.selected 读取)
POSITION_PARAM_KEYS = (
    '_init_alloc_pct', '_enable_pyramiding', '_pyr_add_pct', '_pyr_max',
    '_bull_alloc', '_range_alloc', '_bear_alloc',
)


def make_engine_kwargs(leverage, tp_pct, sl_pct, **position_overrides):
    """构造引擎参数。仓位/加仓/牛熊系数为 AI 可搜索维度 (P0)。

    position_overrides 可覆盖 POSITION_PARAM_KEYS 中任意项 (键名与 strategy.selected 一致)。
    返回 dict 含 '_position_params' 子键, 由 run_single 剥离并注入 strategy.selected;
    引擎在 run() 时从 selected 读取同一通道, 不改变已验证的开仓/加仓/收益/手续费公式。
    """
    pos = {k: RISK_CONFIG.get(k) for k in POSITION_PARAM_KEYS}
    pos.update({k: v for k, v in position_overrides.items() if k in POSITION_PARAM_KEYS})
    kw = dict(
        initial_capital=10000.0,
        leverage=leverage,
        max_positions=1,
        tp_pct=tp_pct, sl_pct=sl_pct,
        tp_mode='price_pct', sl_mode='price_pct',
        # 牛熊系数与 selected 注入值保持一致 (引擎 run() 会以 selected 覆盖)
        bull_alloc=pos['_bull_alloc'] / 100.0,
        range_alloc=pos['_range_alloc'] / 100.0,
        bear_alloc=pos['_bear_alloc'] / 100.0,
        bear_ratio_limit=0.5,
        max_notional_pct=3.0,
        lock_streak=3, lock_bars=12, cooldown_bars=2,
        verbose=False,
    )
    kw['_position_params'] = pos
    return kw


def _regime_br(df, es, lookback=20):
    """简单 EMA 斜率 regime + 空头比例(200根), 供引擎动态仓位/空头过滤用。"""
    slope = (es - es.shift(lookback)) / es.shift(lookback).replace(0, np.nan)
    df['regime'] = 'range'
    df.loc[slope > 0.02, 'regime'] = 'bull'
    df.loc[slope < -0.02, 'regime'] = 'bear'
    df['br'] = (df['regime'] == 'bear').astype(int).rolling(200, min_periods=1).mean()
    return df


# ============================================================
# 5 个策略 (仅用平台已有指标)
# ============================================================
class S1_EMA_ADX_Trend(StrategyBase):
    """趋势确认: EMA(20/50)方向 + ADX>25 强度 + DI方向一致。"""
    def __init__(self, fast=20, slow=50, adx_period=14, adx_th=25):
        super().__init__("S1_EMA_ADX_Trend")
        self.fast, self.slow, self.adx_period, self.adx_th = fast, slow, adx_period, adx_th
        self.selected = dict(RISK_CONFIG)

    def generate_signals(self, df, funding_rate=None):
        df = df.copy()
        c = df['close'].shift(1); h = df['high'].shift(1); l = df['low'].shift(1)
        ef = c.ewm(span=self.fast, adjust=False).mean()
        es = c.ewm(span=self.slow, adjust=False).mean()
        adx, pdi, mdi = _adx(h, l, c, self.adx_period)
        df['signal'] = 0
        df.loc[(ef > es) & (adx > self.adx_th) & (pdi > mdi), 'signal'] = 1
        df.loc[(ef < es) & (adx > self.adx_th) & (mdi > pdi), 'signal'] = -1
        return _regime_br(df, es)


class S2_Donchian_VolBreak(StrategyBase):
    """海龟突破 + 放量确认: 突破 N 根通道 + 成交量放大。"""
    def __init__(self, n=55, vol_p=20, vol_mul=1.5):
        super().__init__("S2_Donchian_VolBreak")
        self.n, self.vol_p, self.vol_mul = n, vol_p, vol_mul
        self.selected = dict(RISK_CONFIG)

    def generate_signals(self, df, funding_rate=None):
        df = df.copy()
        c = df['close'].shift(1); h = df['high'].shift(1); l = df['low'].shift(1)
        v = df['vol'].shift(1)
        upper = h.rolling(self.n).max().shift(1)
        lower = l.rolling(self.n).min().shift(1)
        volma = v.rolling(self.vol_p).mean()
        df['signal'] = 0
        df.loc[(c > upper) & (v > volma * self.vol_mul), 'signal'] = 1
        df.loc[(c < lower) & (v > volma * self.vol_mul), 'signal'] = -1
        es = c.ewm(span=50, adjust=False).mean()
        return _regime_br(df, es)


class S3_Supertrend_MACD(StrategyBase):
    """超级趋势 + MACD 动量确认: 方向一致才交易。"""
    def __init__(self, atr_p=10, mul=3.0, macd_f=12, macd_s=26, macd_sig=9):
        super().__init__("S3_Supertrend_MACD")
        self.atr_p, self.mul = atr_p, mul
        self.macd_f, self.macd_s, self.macd_sig = macd_f, macd_s, macd_sig
        self.selected = dict(RISK_CONFIG)

    def generate_signals(self, df, funding_rate=None):
        df = df.copy()
        c = df['close'].shift(1); h = df['high'].shift(1); l = df['low'].shift(1)
        trend = _supertrend_trend(h, l, c, self.atr_p, self.mul)
        macd, sig = _macd(c, self.macd_f, self.macd_s, self.macd_sig)
        df['signal'] = 0
        df.loc[(trend == 1.0) & (macd > sig), 'signal'] = 1
        df.loc[(trend == -1.0) & (macd < sig), 'signal'] = -1
        es = c.ewm(span=50, adjust=False).mean()
        return _regime_br(df, es)


class S4_BB_RSI_ADX_MR(StrategyBase):
    """均值回归(仅做多): 触布林下轨 + RSI超卖 + ADX低(区间市)。"""
    def __init__(self, bb_p=20, bb_std=2.0, rsi_p=14, rsi_os=30,
                 adx_p=14, adx_max=20):
        super().__init__("S4_BB_RSI_ADX_MR")
        self.bb_p, self.bb_std = bb_p, bb_std
        self.rsi_p, self.rsi_os = rsi_p, rsi_os
        self.adx_p, self.adx_max = adx_p, adx_max
        self.selected = dict(RISK_CONFIG)

    def generate_signals(self, df, funding_rate=None):
        df = df.copy()
        c = df['close'].shift(1); h = df['high'].shift(1); l = df['low'].shift(1)
        mid = c.rolling(self.bb_p).mean()
        sd = c.rolling(self.bb_p).std()
        lower = mid - self.bb_std * sd
        rsi = _rsi(c, self.rsi_p)
        adx, _, _ = _adx(h, l, c, self.adx_p)
        df['signal'] = 0
        df.loc[(c < lower) & (rsi < self.rsi_os) & (adx < self.adx_max), 'signal'] = 1
        es = c.ewm(span=50, adjust=False).mean()
        return _regime_br(df, es)


class S5_RegimeAdaptive(StrategyBase):
    """MultiFactorRegime 自适应: 牛/熊→趋势, range→RSI均值回归。"""
    def __init__(self, fast=20, slow=50, rsi_p=14, rsi_os=30, rsi_ob=70):
        super().__init__("S5_RegimeAdaptive")
        self.fast, self.slow, self.rsi_p, self.rsi_os, self.rsi_ob = \
            fast, slow, rsi_p, rsi_os, rsi_ob
        self.selected = dict(RISK_CONFIG)

    def generate_signals(self, df, funding_rate=None):
        df = df.copy()
        c = df['close'].shift(1)
        ef = c.ewm(span=self.fast, adjust=False).mean()
        es = c.ewm(span=self.slow, adjust=False).mean()
        rsi = _rsi(c, self.rsi_p)
        mf = MultiFactorRegime()
        dreg = mf.evaluate(df, funding_rate=funding_rate)  # evaluate 内部已 shift(1)
        regime = dreg['regime_mf']
        df['signal'] = 0
        df.loc[(regime == 'bull') & (ef > es), 'signal'] = 1
        df.loc[(regime == 'bear') & (ef < es), 'signal'] = -1
        df.loc[(regime == 'range') & (rsi < self.rsi_os), 'signal'] = 1
        df.loc[(regime == 'range') & (rsi > self.rsi_ob), 'signal'] = -1
        df['regime'] = regime
        df['br'] = (regime == 'bear').astype(int).rolling(200, min_periods=1).mean()
        return df


# ============================================================
# 运行辅助
# ============================================================
def run_single(df, coin, strategy, kwargs):
    """跑单次回测。kwargs 可含 '_position_params' (仓位/加仓/牛熊系数, P0),
    从构造参数中剥离并注入 strategy.selected; 引擎 run() 从 selected 读取 (与 UI 主路径同通道)。"""
    kwargs = dict(kwargs)
    pos_params = kwargs.pop('_position_params', None)
    if pos_params:
        sel = getattr(strategy, 'selected', None)
        if isinstance(sel, dict):
            sel.update(pos_params)  # 覆盖 RISK_CONFIG 默认值
    engine = BacktestEngineV2(**kwargs)
    result = engine.run({coin: df}, strategy)
    metrics = PerformanceAnalyzer.analyze(result)
    return result, metrics


def closed_trades(result):
    return result.get('closed_trades', result.get('trades', []))


def annual_returns(equity_curve, initial_capital):
    if not equity_curve:
        return {}
    edf = pd.DataFrame(equity_curve)
    edf['ts'] = pd.to_datetime(edf['timestamp'])
    edf['year'] = edf['ts'].dt.year
    eoy = edf.groupby('year')['equity'].last()
    out = {}; prev = initial_capital
    for y in sorted(eoy.index):
        ret = (eoy[y] / prev - 1) * 100 if prev > 0 else None
        out[int(y)] = round(float(ret), 2) if ret is not None else None
        prev = eoy[y]
    return out


def _json_default(o):
    if isinstance(o, np.floating): return float(o)
    if isinstance(o, np.integer): return int(o)
    if isinstance(o, np.bool_): return bool(o)
    if isinstance(o, np.ndarray): return o.tolist()
    return str(o)


def return_source(trades):
    by = defaultdict(lambda: {'pnl': 0.0, 'n': 0})
    for t in trades:
        r = str(t.get('reason', '?'))
        by[r]['pnl'] += float(t.get('pnl', 0) or 0)
        by[r]['n'] += 1
    return {k: {'pnl': round(v['pnl'], 2), 'n': v['n']} for k, v in by.items()}


# P2: 周期 → 每年bar数 (Monte Carlo 年度化不再硬编码 4h)
_TIMEFRAME_BARS_PER_YEAR = {
    '5m': 365 * 24 * 12,   # 5分钟
    '15m': 365 * 24 * 4,   # 15分钟
    '1h': 365 * 24,        # 1小时
    '4h': 365 * 6,         # 4小时
    '1d': 365,             # 日线
}


def _infer_timeframe(df):
    """从 K 线索引中位数间隔推断周期标签 (5m/15m/1h/4h/1d)。失败返回 None。"""
    try:
        d = df.index.to_series().diff().dropna().median()
        if pd.isna(d) or d <= pd.Timedelta(0):
            return None
        mins = d.total_seconds() / 60.0
        if mins <= 7:
            return '5m'
        if mins <= 20:
            return '15m'
        if mins <= 90:
            return '1h'
        if mins <= 300:
            return '4h'
        return '1d'
    except Exception:
        return None


def _monte_carlo(equity_arr, n_boot=200, timeframe=None):
    if equity_arr is None or len(equity_arr) < 3:
        return None
    arr = np.asarray(equity_arr, dtype=float)
    rets = np.diff(arr) / np.maximum(arr[:-1], 1e-9)
    rets = rets[np.isfinite(rets)]
    if len(rets) < 3:
        return None
    # P2: 按周期自动换算 (未指定/未知周期回退 4h 旧默认)
    bars_per_year = _TIMEFRAME_BARS_PER_YEAR.get(timeframe) or _TIMEFRAME_BARS_PER_YEAR['4h']
    rng = np.random.default_rng(42)
    anns = []
    for _ in range(n_boot):
        s = rng.choice(rets, size=len(rets), replace=True)
        total = np.prod(1 + s) - 1
        years = len(rets) / bars_per_year
        ann = (1 + total) ** (1 / max(years, 1e-9)) - 1 if total > -1 else -1.0
        anns.append(ann * 100)
    return round(float(np.percentile(anns, 5)), 2)


def simple_walk_forward(df, coin, strategy, kwargs,
                        start_year=2017, end_year=2026, train_years=2, test_years=1):
    """固定参数滚动 WF (无参数调优, 避免参数偷窥)。返回 OOS 窗口列表。"""
    wins = []
    span = train_years + test_years
    num = (end_year - start_year + 1) - span + 1
    for i in range(num):
        ts = start_year + i
        te = ts + train_years - 1
        os = te + 1
        oe = os + test_years - 1
        tr = df[(df.index.year >= ts) & (df.index.year <= te)]
        tt = df[(df.index.year >= os) & (df.index.year <= oe)]
        if len(tr) < 300 or len(tt) < 50:
            continue
        try:
            _, tr_m = run_single(tr, coin, strategy, kwargs)
            _, tt_m = run_single(tt, coin, strategy, kwargs)
        except Exception as e:
            wins.append({'test_range': f"{os}-{oe}", 'error': str(e)})
            continue
        wins.append({
            'test_range': f"{os}-{oe}",
            'train_ret': tr_m.get('total_return'),
            'oos_ret': tt_m.get('total_return'),
            'oos_sharpe': tt_m.get('sharpe_ratio'),
            'oos_mdd': tt_m.get('max_drawdown'),
            'oos_trades': tt_m.get('total_trades'),
        })
    return wins


def wf_summary(wins):
    oos = [w['oos_ret'] for w in wins if 'oos_ret' in w and w['oos_ret'] is not None]
    if not oos:
        return {'avg_oos_return': None, 'profitable_windows': 0, 'total_windows': 0,
                'profit_ratio': None}
    prof = sum(1 for r in oos if r > 0)
    return {
        'avg_oos_return': round(float(np.mean(oos)), 2),
        'profitable_windows': prof,
        'total_windows': len(oos),
        'profit_ratio': round(prof / len(oos) * 100, 1),
        'oos_std': round(float(np.std(oos)), 2),
    }


# ============================================================
# 主流程
# ============================================================
STRATEGIES = [
    ("S1_EMA_ADX_Trend", S1_EMA_ADX_Trend, dict(tp_pct=8.0, sl_pct=4.0)),
    ("S2_Donchian_VolBreak", S2_Donchian_VolBreak, dict(tp_pct=8.0, sl_pct=4.0)),
    ("S3_Supertrend_MACD", S3_Supertrend_MACD, dict(tp_pct=8.0, sl_pct=4.0)),
    ("S4_BB_RSI_ADX_MR", S4_BB_RSI_ADX_MR, dict(tp_pct=4.0, sl_pct=2.0)),
    ("S5_RegimeAdaptive", S5_RegimeAdaptive, dict(tp_pct=8.0, sl_pct=4.0)),
]
COINS = ["BTC", "ETH"]
LEVERAGES = [1, 2]


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    de = DataEngine()
    data = {}
    for coin in COINS:
        all_tf = de.get_multi_timeframe(coin)
        data[coin] = all_tf['4h']

    report = {"meta": {"purpose": "phase1_strategy_research",
                       "timeframe": "4h", "coins": COINS,
                       "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
              "strategies": {}}

    for sname, scls, tpsl in STRATEGIES:
        report["strategies"][sname] = {"config": {}, "coins": {}}
        print(f"\n{'='*70}\n[{sname}]\n{'='*70}", flush=True)
        for coin in COINS:
            for lev in LEVERAGES:
                kwargs = make_engine_kwargs(lev, tpsl['tp_pct'], tpsl['sl_pct'])
                strat = scls()
                key = f"{coin}_x{lev}"
                t0 = time.time()
                try:
                    # 全周期
                    res, m = run_single(data[coin], coin, strat, kwargs)
                    # IS/OOS 单次切分
                    is_df = data[coin][data[coin].index.year <= 2022]
                    oos_df = data[coin][data[coin].index.year >= 2023]
                    _, is_m = run_single(is_df, coin, scls(), kwargs)
                    _, oos_m = run_single(oos_df, coin, scls(), kwargs)
                    # Monte Carlo (复用全周期权益)
                    mc_p5 = _monte_carlo(res.get('equity_array'), timeframe=_infer_timeframe(data[coin]))
                    # Walk Forward (仅 2x 跑, 省算力)
                    wf = {}
                    if lev == 2:
                        wins = simple_walk_forward(data[coin], coin, scls(), kwargs)
                        wf = wf_summary(wins)
                        wf['windows'] = wins
                except Exception as e:
                    report["strategies"][sname]["coins"][key] = {"error": f"{type(e).__name__}: {e}"}
                    print(f"  [{key}] ERROR: {e}", flush=True)
                    continue

                entry = {
                    "full": {
                        "total_return": m.get('total_return'),
                        "annual_return": m.get('annual_return'),
                        "max_drawdown": m.get('max_drawdown'),
                        "sharpe": m.get('sharpe_ratio'),
                        "sortino": m.get('sortino_ratio'),
                        "calmar": m.get('calmar_ratio'),
                        "win_rate": m.get('win_rate'),
                        "profit_factor": m.get('profit_factor'),
                        "payoff_ratio": m.get('payoff_ratio'),
                        "total_trades": m.get('total_trades'),
                        "max_consecutive_losses": m.get('max_consecutive_losses'),
                        "years": m.get('years'),
                    },
                    "leak_count": len(res.get('leak_warnings', [])),
                    "monte_carlo_p5": mc_p5,
                    "annual_returns": annual_returns(res.get('equity_curve'), 10000.0),
                    "return_source": return_source(closed_trades(res)),
                    "is_2022": {"total_return": is_m.get('total_return'),
                                "sharpe": is_m.get('sharpe_ratio'),
                                "mdd": is_m.get('max_drawdown'),
                                "trades": is_m.get('total_trades')},
                    "oos_2023": {"total_return": oos_m.get('total_return'),
                                 "sharpe": oos_m.get('sharpe_ratio'),
                                 "mdd": oos_m.get('max_drawdown'),
                                 "trades": oos_m.get('total_trades')},
                    "walk_forward": wf,
                }
                report["strategies"][sname]["coins"][key] = entry

                m_full = entry['full']
                print(f"  [{key}] ret={m_full['total_return']}% ann={m_full['annual_return']}% "
                      f"mdd={m_full['max_drawdown']}% sharpe={m_full['sharpe']} "
                      f"trades={m_full['total_trades']} consLoss={m_full['max_consecutive_losses']} "
                      f"leak={entry['leak_count']} mcP5={mc_p5} oos23={entry['oos_2023']['total_return']}% "
                      f"({time.time()-t0:.1f}s)", flush=True)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "research_phase1_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=_json_default)
    print(f"\n[OK] 结果已保存: {out_path}")


if __name__ == "__main__":
    main()
