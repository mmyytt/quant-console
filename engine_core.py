"""
=============================================================================
 翔哥量化回测核心引擎 v3.0
=============================================================================
 工程规范:
   - 严禁未来函数: 信号用 shift(1), 成交用下一根开盘价
   - 多周期对齐: 15min 基座, 合成周期严格时间戳对齐
   - 真实摩擦: 手续费 0.05% + 滑点 0.02%
   - 模块化: DataEngine | StrategyBase | BacktestEngine | PerformanceAnalyzer
   - 可重复: 相同数据+相同参数 = 100% 一致结果

 使用方法:
   from engine_core import DataEngine, BacktestEngine, PerformanceAnalyzer
   from engine_core import MACrossStrategy  # 示例策略
=============================================================================
"""
import pandas as pd
import numpy as np
import os, warnings
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Union
from abc import ABC, abstractmethod

warnings.filterwarnings("ignore")

# ============================================================
# 全局配置
# ============================================================
# 数据目录: 惰性检测 (不在import时下载, 避免启动超时)
def _find_data_dir():
    """查找数据目录: 本地eth_all > 项目data/"""
    # 1) Windows 本地 eth_all
    local = r"C:\Users\myt\Desktop\eth_all"
    if os.path.isdir(local) and os.path.exists(os.path.join(local, "ETH_15m.parquet")):
        return local
    # 2) 项目内 data/ (云端已下载)
    proj = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if os.path.isdir(proj) and os.path.exists(os.path.join(proj, "ETH_15m.parquet")):
        return proj
    return proj  # 返回路径, 首次使用时会触发下载

DATA_DIR = _find_data_dir()

def ensure_data_ready(coin: str = "ETH"):
    """确保数据就绪, 不存在则下载 (惰性调用)"""
    pq = os.path.join(DATA_DIR, f"{coin}_15m.parquet")
    if os.path.exists(pq) and os.path.getsize(pq) > 100000:
        return True
    try:
        from data_loader import ensure_data
        ensure_data(coin)
        return True
    except Exception as e:
        print(f"[DataEngine] Data download failed: {e}")
        return False

# 交易成本
TAKER_FEE = 0.0005    # 手续费 0.05%
SLIPPAGE = 0.0002     # 滑点 0.02%
TOTAL_COST = TAKER_FEE + SLIPPAGE  # 单边总成本 0.07%


# ============================================================
# 多因子牛熊过滤器 (Multi-factor Regime Filter)
# ============================================================
class MultiFactorRegime:
    """
    多维度牛熊状态判断, 解决单一指标不准的问题。

    三个因子取加权平均:
      Factor 1: EMA 斜率  (趋势方向, 权重 40%)
      Factor 2: ADX 趋势强度 (趋势是否可靠, 权重 35%)
      Factor 3: 资金费率方向 (市场情绪, 权重 25%)

    输出:
      regime: 'bull' | 'bear' | 'range'
      strength: 0.0 ~ 1.0  (判断的置信度)
      scores: {ema_score, adx_score, funding_score}
    """

    def __init__(self,
                 ema_span: int = 50,
                 slope_lookback: int = 20,
                 adx_period: int = 14,
                 adx_threshold: int = 25,
                 ema_weight: float = 0.40,
                 adx_weight: float = 0.35,
                 funding_weight: float = 0.25,
                 bull_threshold: float = 0.30,   # 综合分 >30% → 牛
                 bear_threshold: float = -0.30,  # 综合分 <-30% → 熊
                 ):
        self.ema_span = ema_span
        self.slope_lookback = slope_lookback
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.ema_weight = ema_weight
        self.adx_weight = adx_weight
        self.funding_weight = funding_weight
        self.bull_threshold = bull_threshold
        self.bear_threshold = bear_threshold

    def compute_adx(self, high, low, close, period=14):
        """计算 ADX (Average Directional Index)"""
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()

        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm = pd.Series(0.0, index=high.index)
        minus_dm = pd.Series(0.0, index=high.index)
        plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
        minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

        plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)

        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)
        adx = dx.ewm(alpha=1/period, adjust=False).mean()

        return adx, plus_di, minus_di

    def evaluate(self, df: pd.DataFrame,
                 funding_rate: pd.Series = None) -> pd.DataFrame:
        """
        对 DataFrame 的每一行评估牛熊状态。

        Args:
            df: OHLCV (必须使用 shift(1) 防止未来函数)
            funding_rate: 可选, 资金费率序列 (每8小时一次, 向前填充)

        Returns:
            df 增加列: regime_mf, regime_strength,
                       ema_score, adx_score, funding_score
        """
        df = df.copy()
        close = df['close']; high = df['high']; low = df['low']

        # === Factor 1: EMA 斜率 ===
        ema = close.ewm(span=self.ema_span, adjust=False).mean()
        slope = (ema - ema.shift(self.slope_lookback)) / ema.shift(self.slope_lookback).replace(0, np.nan)

        # 归一化到 [-1, 1]: 斜率 ±3% 映射到 ±1
        ema_score = (slope / 0.03).clip(-1, 1)
        df['ema_score'] = ema_score

        # === Factor 2: ADX 趋势强度 ===
        adx, plus_di, minus_di = self.compute_adx(high, low, close, self.adx_period)

        # ADX > threshold 且 DI 有明确方向 → 强趋势
        # adx_score: 正=多头趋势强, 负=空头趋势强
        di_diff = (plus_di - minus_di) / (plus_di + minus_di + 1e-9)  # [-1, 1]
        adx_strength = (adx / (self.adx_threshold * 2)).clip(0, 1)     # [0, 1]
        adx_score = di_diff * adx_strength  # [-1, 1]
        df['adx'] = adx
        df['adx_score'] = adx_score
        df['plus_di'] = plus_di
        df['minus_di'] = minus_di

        # === Factor 3: 资金费率 (市场情绪, shift(1)防未来函数) ===
        if funding_rate is not None and len(funding_rate) > 0:
            # funding_rate > 0 → 多头拥挤(偏空信号), < 0 → 空头拥挤(偏多信号)
            fr = funding_rate.reindex(df.index, method='ffill').fillna(0).shift(1)
            # 极端费率: >0.05% → 强烈偏空信号, <-0.05% → 强烈偏多信号
            funding_score = -(fr / 0.0005).clip(-1, 1)  # 取反: 正费率→做空信号(负分)
            df['funding_score'] = funding_score
        else:
            funding_score = pd.Series(0.0, index=df.index)
            df['funding_score'] = 0.0

        # === 综合评分 ===
        composite = (
            self.ema_weight * ema_score.fillna(0) +
            self.adx_weight * adx_score.fillna(0) +
            self.funding_weight * funding_score.fillna(0)
        )
        df['regime_strength'] = composite

        # === 判定牛熊 ===
        df['regime_mf'] = 'range'
        df.loc[composite > self.bull_threshold, 'regime_mf'] = 'bull'
        df.loc[composite < self.bear_threshold, 'regime_mf'] = 'bear'

        # 附加: 空头比例 (200根)
        is_bear = (df['regime_mf'] == 'bear').astype(int)
        df['br_mf'] = is_bear.rolling(200, min_periods=1).mean()

        return df


# ============================================================
# 模块一: DataEngine — 数据加载与多周期合成
# ============================================================
class DataEngine:
    """
    数据引擎: 负责加载 15min 原始数据、重采样合成多周期、校验完整性。

    关键约束:
      - 重采样时使用 'left' closed label, 保证 4H K线在 00:00/04:00/08:00... 闭合
      - 所有时间戳对齐到标准周期边界
      - 提供 get_multi_timeframe() 一键获取所有周期
    """

    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        self._cache: Dict[str, pd.DataFrame] = {}  # 15min 原始数据缓存

    # ---- 加载 ----
    def load_15min(self, coin: str) -> pd.DataFrame:
        """
        加载 15min OHLCV 数据。

        Args:
            coin: 'ETH' | 'BTC' | 'SOL'
        Returns:
            DataFrame with columns [open, high, low, close, vol],
            DatetimeIndex sorted ascending, no duplicates.
        """
        if coin in self._cache:
            return self._cache[coin].copy()

        path = os.path.join(self.data_dir, f"{coin}_15m.parquet")
        if not os.path.exists(path):
            # 云端: 尝试下载
            if not ensure_data_ready(coin):
                raise FileNotFoundError(f"数据文件不存在且下载失败: {path}")

        df = pd.read_parquet(path)

        # 时间列修复: reset_index 后列名可能是 '0'
        time_col = df.columns[0]
        if time_col != 'index' and df[time_col].dtype == 'object':
            df[time_col] = pd.to_datetime(df[time_col])
        df = df.rename(columns={time_col: 'timestamp'})
        df = df.set_index('timestamp')

        # 去重 + 排序
        df = df[~df.index.duplicated()]
        df = df.sort_index()

        # 校验必要列
        required = ['open', 'high', 'low', 'close', 'vol']
        for col in required:
            if col not in df.columns:
                raise ValueError(f"缺少列: {col}")
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df = df.dropna()
        self._cache[coin] = df
        return df.copy()

    # ---- 重采样 ----
    def resample(self, df: pd.DataFrame, rule: str) -> pd.DataFrame:
        """
        将 15min 数据重采样为更大周期。

        关键约束:
          - 使用标准 OHLCV 聚合: open=first, high=max, low=min, close=last, vol=sum
          - 标签使用左闭区间, 保证周期边界对齐
          - 必须等当前周期完全闭合后才可用 -> 回测时只能用前一根合成 K 线

        Args:
            df: 15min 数据
            rule: '1h' | '4h' | '1d'
        Returns:
            重采样后的 DataFrame
        """
        agg = {
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'vol': 'sum',
        }
        # label='left': 时间戳标记在周期开始
        # closed='left': 左闭右开, 保证周期不重叠
        # 当前bar已闭合时才可用 → 无未来函数泄露
        resampled = df.resample(rule, label='left', closed='left').agg(agg)
        return resampled.dropna()

    # ---- 多周期一键获取 ----
    def get_multi_timeframe(self, coin: str) -> Dict[str, pd.DataFrame]:
        """
        获取单个币种的所有周期数据。

        Returns:
            {'15m': df, '1h': df, '4h': df, '1d': df}
        每个 DataFrame 的时间戳都对齐到对应周期的标准边界。
        """
        df_15m = self.load_15min(coin)
        return {
            '15m': df_15m,
            '1h': self.resample(df_15m, '1h'),
            '4h': self.resample(df_15m, '4h'),
            '1d': self.resample(df_15m, '1d'),
        }

    # ---- 校验 ----
    def validate(self, df: pd.DataFrame, min_bars: int = 500) -> bool:
        """
        校验数据完整性。

        Returns:
            True 如果数据充足且没有明显异常
        """
        if len(df) < min_bars:
            return False
        # 检查是否有 0 或负价格
        for col in ['open', 'high', 'low', 'close']:
            if (df[col] <= 0).any():
                return False
        # 检查 high >= low
        if (df['high'] < df['low']).any():
            return False
        return True


# ============================================================
# 模块二: StrategyBase — 策略基类 (严禁未来函数)
# ============================================================
class StrategyBase(ABC):
    """
    策略基类。

    核心约束:
      - generate_signals(df) 在计算 t 时刻信号时, 只能使用截至 t 时刻的数据
      - 返回的 signal 列:  1=做多, -1=做空, 0=观望
      - 信号生成后, 回测引擎用 "下一根K线开盘价" 撮合成交, 杜绝未来函数
    """

    def __init__(self, name: str = "BaseStrategy"):
        self.name = name

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        生成交易信号。

        Args:
            df: OHLCV DataFrame, 包含足量历史数据
        Returns:
            在 df 基础上添加 'signal' 列: 1 (做多), -1 (做空), 0 (观望)
        """
        pass

    def __repr__(self):
        return f"{self.name}"


# ============================================================
# 示例策略: 双均线交叉 (MACrossStrategy)
# ============================================================
class MACrossStrategy(StrategyBase):
    """
    双均线交叉策略 (接入引擎 V2 的完整示例)。

    输出列:
      signal: 1(做多) / -1(做空) / 0(观望)
      regime: bull / range / bear  (牛熊判断, 给引擎做动态仓位)
      score:  0~3  (ma_up + vol_up + trend_up, 给引擎做轮动选币)
      br:     空头比例 (给引擎做空仓过滤)
    """

    def __init__(self, fast: int = 5, slow: int = 20,
                 regime_span: int = 50, regime_lookback: int = 20,
                 name: str = "MACross"):
        super().__init__(name)
        self.fast = fast
        self.slow = slow
        self.regime_span = regime_span
        self.regime_lookback = regime_lookback

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = df['close']; vol = df['vol']

        # === 均线 (shift(1) 防未来函数) ===
        fast_ema = close.ewm(span=self.fast, adjust=False).mean().shift(1)
        slow_ema = close.ewm(span=self.slow, adjust=False).mean().shift(1)
        regime_ema = close.ewm(span=self.regime_span, adjust=False).mean().shift(1)

        # === 交叉信号 ===
        cross_up = (fast_ema > slow_ema) & (fast_ema.shift(1) <= slow_ema.shift(1))
        cross_down = (fast_ema < slow_ema) & (fast_ema.shift(1) >= slow_ema.shift(1))

        df['signal'] = 0
        df.loc[cross_up, 'signal'] = 1
        df.loc[cross_down, 'signal'] = -1

        # === 行情研判: 多因子牛熊过滤器 ===
        regime_filter = MultiFactorRegime(
            ema_span=self.regime_span,
            slope_lookback=self.regime_lookback,
            adx_period=14,
            adx_threshold=25,
            ema_weight=0.40, adx_weight=0.35, funding_weight=0.25,
        )
        df = regime_filter.evaluate(df)
        df['regime'] = df['regime_mf']          # 用多因子结果
        df['br'] = df['br_mf']                   # 用多因子空头比例

        # === 评分 (轮动用) ===
        vol_ma = vol.rolling(20).mean().shift(1)
        df['score'] = (
            (fast_ema > slow_ema).astype(int) +        # ma_up
            (vol.shift(1) > vol_ma).astype(int) +       # vol_up
            (close.shift(1) > regime_ema).astype(int)   # trend_up
        )

        # 保存指标
        df['fast_ema'] = fast_ema
        df['slow_ema'] = slow_ema
        df['regime_ema'] = regime_ema

        return df


# ============================================================
# 示例策略: 超跌反弹 (OversoldBounceStrategy)
# ============================================================
class OversoldBounceStrategy(StrategyBase):
    """
    1小时跌幅超阈值 → 超跌反弹做多。

    规则:
      - 计算过去 N 根 K 线的累计跌幅
      - 跌幅超过阈值 → signal = 1 (做多博反弹)
      - 其余 → signal = 0

    严禁未来函数措施:
      - 计算跌幅时用上一根收盘价 vs N根前收盘价
      - 当前根的数据完全不参与信号计算
    """

    def __init__(self, lookback: int = 12, threshold: float = -0.10,
                 name: str = "OversoldBounce"):
        """
        Args:
            lookback: 回看多少根K线 (12根1H = 12小时)
            threshold: 跌幅阈值 (默认 -10%)
        """
        super().__init__(name)
        self.lookback = lookback
        self.threshold = threshold

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        超跌反弹信号: N根K线跌幅超阈值 → 做多。

        ⚠️ 只用已闭合数据: close.shift(1) 确保不偷看当前根。
        """
        df = df.copy()

        # 用上一根收盘价计算跌幅 (shift(1) 防未来函数)
        close_prev = df['close'].shift(1)

        # N根累计跌幅
        returns = close_prev.pct_change(periods=self.lookback)

        df['signal'] = 0
        df['returns'] = returns
        df.loc[returns <= self.threshold, 'signal'] = 1  # 超跌 → 做多

        return df


# ============================================================
# 模块三: BacktestEngine — 现货回测 (保留兼容)
# ============================================================
class BacktestEngine:
    """
    现货回测引擎 (保留向后兼容, 内部委托给 BacktestEngineV2)。

    新项目请直接使用 BacktestEngineV2。
    """

    def __init__(self, initial_capital=10000.0, tp_pct=None, sl_pct=None,
                 position_pct=1.0, max_positions=1, verbose=True):
        self._v2 = BacktestEngineV2(
            initial_capital=initial_capital,
            leverage=1,  # 现货=1x
            tp_pct=tp_pct, sl_pct=sl_pct,
            max_positions=max_positions,
            bull_alloc=position_pct, range_alloc=position_pct, bear_alloc=position_pct,
            verbose=verbose,
        )

    def run(self, df, strategy):
        return self._v2.run({'_single': df}, strategy)


# ============================================================
# 模块三 V2: BacktestEngineV2 — 合约+轮动+动态仓位
# ============================================================
class BacktestEngineV2:
    """
    合约交易回测引擎 v2。

    核心升级:
      1. 杠杆合约: 支持 1x~200x, 做多做空, 保证金计算
      2. 多币轮动: 同时扫描多币种, 评分选最优, 单持仓/多持仓
      3. 动态仓位: 牛市100% / 震荡50% / 熊市30% / 空头>50%→空仓
      4. 风控: 断路器(连亏N笔锁仓), 平仓冷却, 手续费+滑点

    严禁未来函数 (与 v1 一致):
      - 信号基于 t-1 数据, 撮合用 t 时刻开盘价
      - TP/SL 用 bar 内 High/Low 模拟限价单

    Parameters:
      initial_capital:  初始资金 (USDT)
      leverage:         杠杆倍数 (默认 3)
      tp_pct:           止盈 (保证金%, 如 10 → 保证金+10%止盈)
      sl_pct:           止损 (保证金%, 如 5  → 保证金-5%止损)
      max_positions:    最大同时持仓数 (1=单币轮动)
      bull_alloc:       牛市仓位比例 (默认 1.0 = 全仓)
      range_alloc:      震荡仓位比例 (默认 0.5)
      bear_alloc:       熊市仓位比例 (默认 0.3)
      bear_ratio_limit: 空头比例上限 (超过则空仓, 默认 0.5)
      lock_streak:      触发锁仓的连亏次数 (默认 3)
      lock_bars:        锁仓 K 线数 (默认 12 = 2天 for 4H)
      cooldown_bars:    平仓后冷却 K 线数 (默认 2 = 8h for 4H)
      verbose:          是否打印日志
    """

    # 合约面值 (每张合约对应多少币)
    CONTRACT_FV = {'ETH': 0.1, 'BTC': 0.01, 'SOL': 1}

    def __init__(self,
                 initial_capital: float = 10000.0,
                 leverage: int = 3,
                 tp_pct: float = 10.0,         # 保证金止盈%
                 sl_pct: float = 5.0,          # 保证金止损%
                 max_positions: int = 1,
                 bull_alloc: float = 1.0,
                 range_alloc: float = 0.5,
                 bear_alloc: float = 0.3,
                 bear_ratio_limit: float = 0.5,
                 lock_streak: int = 3,
                 lock_bars: int = 12,
                 cooldown_bars: int = 2,
                 trailing_pct: float = 0.0,     # 移动止损%
                 strategy_mode: str = "classic",
                 hedge_ratio: float = 0.5,
                 max_pyramid: int = 3,
                 pyramid_step: float = 0.015,
                 unlock_pct: float = 0.05,
                 verbose: bool = True,
                 ):
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.tp_pct = tp_pct / 100.0     # 转为小数
        self.sl_pct = sl_pct / 100.0
        self.max_positions = max_positions
        self.bull_alloc = bull_alloc
        self.range_alloc = range_alloc
        self.bear_alloc = bear_alloc
        self.bear_ratio_limit = bear_ratio_limit
        self.lock_streak = lock_streak
        self.lock_bars = lock_bars
        self.cooldown_bars = cooldown_bars
        self.trailing_pct = trailing_pct   # 移动止损
        self.strategy_mode = strategy_mode
        self.hedge_ratio = hedge_ratio
        self.max_pyramid = max_pyramid
        self.pyramid_step = pyramid_step
        self.unlock_pct = unlock_pct
        self.verbose = verbose
        self._pyramid_count = 0
        self._last_entry_price = 0
        self._portfolio_curve = []
        # 对冲状态机
        self._hedge_state = "IDLE"     # IDLE → LOCKED → UNLOCKED → EXIT
        self._hedge_entry_price = 0     # 建仓价
        self._spot_leg = None           # 现货腿引用
        self._short_leg = None          # 空单腿引用
        self._hedge_open_time = None

        # 内部状态 (每次 run 时重置)
        self.equity = initial_capital
        self.positions: List[Dict] = []
        self.trades: List[Dict] = []
        self.equity_curve: List[Dict] = []
        self.cooldown: Dict[str, int] = {}      # coin -> bar when cooldown expires
        self.lock_until: int = -1               # bar index when circuit breaker expires
        self.losestreak: int = 0

    # ================================================================
    # 主回测循环
    # ================================================================
    def run(self, dfs: Dict[str, pd.DataFrame],
            strategy: StrategyBase,
            strategy_params: Dict = None) -> Dict:
        """
        多币种回测入口。

        Args:
            dfs: {'ETH': df_eth, 'BTC': df_btc, 'SOL': df_sol}
                 所有 DataFrame 必须时间对齐 (相同的时间索引)
            strategy: 策略对象 (对每个币种独立计算信号)
            strategy_params: 传递给 strategy.generate_signals 的额外参数

        Returns:
            dict with trades, equity_curve, metrics-ready fields
        """
        self._reset()

        # 1. 对每个币种计算信号
        coins = list(dfs.keys())
        dfs_with_sigs = {}
        for coin in coins:
            df = dfs[coin].copy()
            df = strategy.generate_signals(df)
            dfs_with_sigs[coin] = df

        # 2. 多币种对齐: 用主币时间轴 + ffill填充缺失
        max_ema = 200
        try:
            for name, cfg_d in strategy.selected.items():
                if not isinstance(cfg_d, dict): continue
                if not cfg_d.get("enabled", True): continue
                for pname, pval in cfg_d.get("params", {}).items():
                    if isinstance(pval, (int, float)) and pval > max_ema:
                        max_ema = int(pval)
        except: pass
        warmup = max(200, max_ema * 3)
        primary_index = dfs_with_sigs[coins[0]].index[warmup:]
        # 对其他币种用主时间轴 + ffill, 标记gap但不丢数据
        for coin in coins[1:]:
            df_coin = dfs_with_sigs[coin]
            # reindex到主时间轴, ffill前向填充
            aligned = df_coin.reindex(primary_index, method='ffill')
            # 标记gap列 (超过2根K线没数据 → 视为gap)
            aligned['_gap'] = (~df_coin.index.isin(primary_index)).astype(int)
            aligned['_gap'] = aligned['_gap'].rolling(3, min_periods=1).sum()
            dfs_with_sigs[coin] = aligned
        common_index = primary_index

        # 3. 逐根遍历
        for i, ts in enumerate(common_index):
            paused = i < self.lock_until
            just_closed = False

            # ---- 对冲状态机 (HEDGING / UNLOCKING 模式) ----
            if self.strategy_mode in ("hedging", "unlocking"):
                self._hedge_state_machine(ts, dfs_with_sigs, coins, i)

            # ---- 检查持仓 TP/SL ----
            self._check_positions(ts, dfs_with_sigs, i)

            # 更新 just_closed 标记
            if len(self.positions) < self._count_positions_before(i):
                just_closed = True
            # 简化: 用平仓记录判断
            just_closed = any(
                t['close_time'] == str(ts) and t['reason'] in ('TP', 'SL')
                for t in self.trades[-3:]  # 只检查最近3笔
            )

            # ---- 开仓: 轮动选币 ----
            if not paused and not just_closed and len(self.positions) < self.max_positions:
                self._try_rotate_entry(ts, dfs_with_sigs, coins, i)

            # ---- 权益曲线 + Delta暴露 ----
            eq = self._calc_total_equity(dfs_with_sigs, ts)
            # 计算净Delta (多头名义-空头名义)
            long_val = sum(p['notional'] for p in self.positions if p['side'] == 'LONG')
            short_val = sum(p['notional'] for p in self.positions if p['side'] == 'SHORT')
            net_delta = long_val - short_val
            self.equity_curve.append({
                'timestamp': ts, 'equity': round(eq, 2),
            })
            self._portfolio_curve.append({
                'timestamp': ts, 'net_delta': round(net_delta, 2),
                'state': 'LOCKED' if abs(net_delta) < max(long_val, short_val) * 0.05 and (long_val + short_val) > 0 else 'UNLOCKED',
            })

        # 4. 强制平仓
        for pos in list(self.positions):
            coin = pos['coin']
            last_close = float(dfs_with_sigs[coin]['close'].iloc[-1])
            self._close(pos, last_close, 'EOD', dfs_with_sigs[coin].index[-1])

        return self._build_result(strategy, coins, common_index)

    # ================================================================
    # 内部: 平仓检查
    # ================================================================
    def _check_positions(self, ts, dfs, bar_idx):
        """检查所有持仓的止盈止损触发"""
        for pos in list(self.positions):
            coin = pos['coin']
            df = dfs[coin]
            row = df.loc[ts]
            bh = float(row['high']); bl = float(row['low'])
            bo = float(row['open']); bc = float(row['close'])

            side = pos['side']; ep = pos['entry']
            tp_px = pos['tp_price']; sl_px = pos['sl_price']
            margin = pos['margin']

            exit_price = None; exit_reason = None

            if side == 'LONG':
                t_hit = bh >= tp_px; s_hit = bl <= sl_px
                if t_hit and s_hit:
                    # 保守原则: 同Bar双触发优先取SL (worst-case execution)
                    exit_price = sl_px; exit_reason = 'SL'
                elif t_hit:
                    exit_price = tp_px; exit_reason = 'TP'
                elif s_hit:
                    exit_price = sl_px; exit_reason = 'SL'
            else:  # SHORT
                t_hit = bl <= tp_px; s_hit = bh >= sl_px
                if t_hit and s_hit:
                    exit_price = sl_px; exit_reason = 'SL'
                elif t_hit:
                    exit_price = tp_px; exit_reason = 'TP'
                elif s_hit:
                    exit_price = sl_px; exit_reason = 'SL'

            # 移动止损检查
            if exit_price is None and self.trailing_pct > 0:
                if side == 'LONG':
                    trail_stop = pos['highest_price'] * (1 - self.trailing_pct)
                    if bl <= trail_stop and pos.get('trailing_activated'):
                        exit_price = trail_stop; exit_reason = 'TRAIL'
                else:
                    trail_stop = pos['lowest_price'] * (1 + self.trailing_pct)
                    if bh >= trail_stop and pos.get('trailing_activated'):
                        exit_price = trail_stop; exit_reason = 'TRAIL'

            # 更新极值 (移动止损用)
            if exit_price is None:
                pos['highest_price'] = max(pos.get('highest_price', ep), bh)
                pos['lowest_price'] = min(pos.get('lowest_price', ep), bl)
                if side == 'LONG' and pos['highest_price'] > ep * (1 + self.trailing_pct * 2):
                    pos['trailing_activated'] = True
                elif side == 'SHORT' and pos['lowest_price'] < ep * (1 - self.trailing_pct * 2):
                    pos['trailing_activated'] = True

            if exit_price is not None:
                self._close(pos, exit_price, exit_reason, ts)

                # 冷却 + 连亏追踪 + 断路器
                self.cooldown[coin] = bar_idx + self.cooldown_bars

                # 计算保证金盈亏%
                if side == 'LONG':
                    margin_pnl = (exit_price - ep) / ep * self.leverage
                else:
                    margin_pnl = (ep - exit_price) / ep * self.leverage

                if margin_pnl <= -self.sl_pct:  # 触发止损
                    self.losestreak += 1
                    if self.losestreak >= self.lock_streak:
                        self.lock_until = bar_idx + self.lock_bars
                        self.losestreak = 0
                else:
                    self.losestreak = 0

    # ================================================================
    # 内部: 轮动开仓
    # ================================================================
    def _try_rotate_entry(self, ts, dfs, coins, bar_idx):
        """扫描所有币种的信号, 选最优开仓"""
        candidates = []

        for coin in coins:
            # 冷却检查
            if self.cooldown.get(coin, -1) >= bar_idx:
                continue

            df = dfs[coin]
            row = df.loc[ts]
            signal = row.get('signal', 0)
            if pd.isna(signal) or signal == 0 or signal is None:
                continue
            # 额外保护: score也可能是NaN
            sc = row.get('score', 1)
            if pd.isna(sc): sc = 1

            # 读取策略附加信息
            regime = row.get('regime', 'range')      # 牛熊判断
            bear_ratio = row.get('br', 0)             # 空头比例
            score = row.get('score', abs(signal))      # 评分 (默认用信号绝对值)

            # 空头比例过滤
            if bear_ratio > self.bear_ratio_limit:
                continue

            # 共振分 (多因子共振策略用)
            resonance_score = int(row.get('resonance_score', 0)) if 'resonance_score' in row.index else 0
            candidates.append({
                'coin': coin,
                'side': 'LONG' if signal == 1 else 'SHORT',
                'score': int(score) if not pd.isna(score) else 1,
                'resonance_score': resonance_score,
                'regime': str(regime),
                'price': float(row['open']),  # 用开盘价撮合!
            })

        if not candidates:
            return

        # 选评分最高的
        best = max(candidates, key=lambda x: x['score'])

        # 策略模式过滤
        regime = best['regime']
        if self.strategy_mode == "unlocking":
            # 解锁模式: 只做多, 熊市不下注
            if regime == 'bear' or best['side'] == 'SHORT':
                return
        elif self.strategy_mode == "pyramiding":
            # 金字塔: 震荡市禁止加仓
            if regime == 'range' and self._pyramid_count > 0:
                return
            # 金字塔加仓间距检查
            if self._pyramid_count > 0 and self._last_entry_price > 0:
                gap = abs(best['price'] - self._last_entry_price) / self._last_entry_price
                if gap < self.pyramid_step:
                    return  # 间距不够, 不加

        # 动态仓位
        if self.strategy_mode == "pyramiding":
            # 首仓用 pyramid_first 比例, 后续等量
            alloc = 0.3 if self._pyramid_count == 0 else 0.3
        else:
            if regime == 'bull':
                alloc = self.bull_alloc
            elif regime == 'bear':
                alloc = self.bear_alloc
            else:
                alloc = self.range_alloc

        if alloc <= 0:
            return

        # 金字塔最大次数限制
        if self.strategy_mode == "pyramiding" and self._pyramid_count >= self.max_pyramid:
            return

        self._open(best['coin'], best['side'], best['price'], alloc, ts, regime,
                   best.get('resonance_score', 0))
        self._pyramid_count += 1
        self._last_entry_price = best['price']

    # ================================================================
    # 对冲状态机: LOCKED → UNLOCKED → EXIT
    # ================================================================
    def _hedge_state_machine(self, ts, dfs, coins, bar_idx):
        """通用对冲解封状态机。支持 HEDGING 和 UNLOCKING 两种模式。"""
        coin = coins[0]  # 主币种
        df = dfs[coin]
        row = df.loc[ts]
        px = float(row['open'])
        bh = float(row['high']); bl = float(row['low'])
        regime = row.get('regime', 'range')

        # === State: IDLE → LOCKED (建对冲仓) ===
        if self._hedge_state == "IDLE":
            if self.equity <= 0:
                return
            spot_alloc = self.hedge_ratio
            short_alloc = self.hedge_ratio
            # 开现货多头
            self._open_hedge_leg(coin, 'LONG', px, spot_alloc, ts, "SPOT")
            # 开合约空头
            self._open_hedge_leg(coin, 'SHORT', px, short_alloc, ts, "FUTURES")
            self._hedge_state = "LOCKED"
            self._hedge_entry_price = px
            self._hedge_open_time = ts
            if self.verbose:
                print(f"[HEDGE_LOCK] {ts} | {coin} | SPOT LONG + FUTURES SHORT | "
                      f"px={px:.2f} | ratio={self.hedge_ratio:.0%} | Delta=0")

        # === State: LOCKED → 检查解锁条件 ===
        elif self._hedge_state == "LOCKED":
            should_unlock = False
            unlock_reason = ""

            if self.strategy_mode == "unlocking":
                # 条件1: 价格突破%
                if px >= self._hedge_entry_price * (1 + self.unlock_pct):
                    should_unlock = True
                    unlock_reason = f"price_breakout_{self.unlock_pct*100:.0f}%"
                # 条件2: EMA金叉
                if 'ema_fast' in row.index and 'ema_slow' in row.index:
                    if row['ema_fast'] > row['ema_slow']:
                        should_unlock = True
                        unlock_reason = "ema_cross_up"
                # 条件3: RSI突破
                if 'rsi' in row.index and row['rsi'] > 60:
                    should_unlock = True
                    unlock_reason = f"rsi_{row['rsi']:.0f}"
                # 条件4: 放量突破
                if 'vol' in row.index and 'vol_ma' in row.index:
                    if row['vol'] > row['vol_ma'] * 1.5:
                        should_unlock = True
                        unlock_reason = "volume_breakout"

            # 对冲模式: 始终锁仓不做解锁 (或在特定条件解锁)
            elif self.strategy_mode == "hedging":
                pass  # 对冲模式不解锁, 一直锁仓

            if should_unlock:
                # 平掉合约空单
                for pos in list(self.positions):
                    if pos['side'] == 'SHORT':
                        self._close(pos, px, f"UNLOCK_{unlock_reason}", ts)
                self._hedge_state = "UNLOCKED"
                if self.verbose:
                    print(f"[UNLOCK] {ts} | {coin} | reason={unlock_reason} | "
                          f"now long-only | px={px:.2f}")

        # === State: UNLOCKED → 检查裸多止盈止损 ===
        elif self._hedge_state == "UNLOCKED":
            for pos in list(self.positions):
                if pos['side'] != 'LONG':
                    continue
                ep = pos['entry']; tp_px = pos.get('tp_price', ep*99)
                sl_px = pos.get('sl_price', 0)

                # 止盈
                if bh >= tp_px:
                    self._close(pos, tp_px, 'SPOT_TP', ts)
                    self._hedge_state = "IDLE"
                    if self.verbose:
                        print(f"[SPOT_TP] {ts} | {coin} | px={tp_px:.2f}")
                # 止损
                elif bl <= sl_px:
                    self._close(pos, sl_px, 'SPOT_SL', ts)
                    self._hedge_state = "IDLE"
                    if self.verbose:
                        print(f"[SPOT_SL] {ts} | {coin} | px={sl_px:.2f}")

    def _open_hedge_leg(self, coin: str, side: str, price: float,
                        alloc: float, ts, leg_label: str = ""):
        """开对冲腿 (现货/合约通用)"""
        if self.equity <= 0:
            return
        # 现货用1x杠杆
        lev = 1 if leg_label == "SPOT" else self.leverage
        fv = self.CONTRACT_FV.get(coin, 1.0)
        margin = self.equity * alloc
        notional = margin * lev
        cost = notional * (TAKER_FEE + SLIPPAGE)
        self.equity -= cost

        if side == 'LONG':
            tp_price = price * (1 + self.tp_pct / lev)
            sl_price = price * (1 - self.sl_pct / lev)
        else:
            tp_price = price * (1 - self.tp_pct / lev)
            sl_price = price * (1 + self.sl_pct / lev)

        pos = {
            'coin': coin, 'side': side, 'entry': price,
            'margin': margin, 'notional': notional,
            'alloc': alloc, 'regime': 'hedge',
            'resonance_score': 0, 'tp_price': tp_price,
            'sl_price': sl_price, 'open_time': ts,
            'cost': cost, 'leg_label': leg_label,
            'highest_price': price, 'lowest_price': price,
            'trailing_activated': False,
        }
        self.positions.append(pos)

    # ================================================================
    # 内部: 开仓 / 平仓
    # ================================================================
    def _open(self, coin: str, side: str, price: float, alloc: float, ts,
              regime: str = 'range', resonance_score: int = 0):
        """
        开仓 (合约模式)。
        """
        # 负权益拦截
        if self.equity <= 0:
            return

        fv = self.CONTRACT_FV.get(coin, 1.0)
        lev = self.leverage

        margin = self.equity * alloc
        notional = margin * lev  # 名义本金

        # 交易成本 (统一按名义本金计算)
        cost = notional * (TAKER_FEE + SLIPPAGE)
        self.equity -= cost

        # TP/SL 价格
        if side == 'LONG':
            tp_price = price * (1 + self.tp_pct / lev)
            sl_price = price * (1 - self.sl_pct / lev)
        else:
            tp_price = price * (1 - self.tp_pct / lev)
            sl_price = price * (1 + self.sl_pct / lev)

        pos = {
            'coin': coin, 'side': side, 'entry': price,
            'margin': margin, 'notional': notional,
            'alloc': alloc, 'regime': regime,
            'resonance_score': resonance_score,
            'tp_price': tp_price, 'sl_price': sl_price,
            'open_time': ts, 'cost': cost,
            'highest_price': price, 'lowest_price': price,
            'trailing_activated': False,
        }
        self.positions.append(pos)

        if self.verbose:
            print(f"[OPEN]  {ts} | {coin:4s} {side:5s} | "
                  f"px={price:.2f} | ctr={contracts:.4f} | "
                  f"margin={margin:.2f} | alloc={alloc*100:.0f}% | {regime}")

    def _close(self, pos: Dict, price: float, reason: str, ts):
        """平仓并结算盈亏"""
        side = pos['side']; ep = pos['entry']; margin = pos['margin']
        notional = pos.get('notional', margin * self.leverage)
        lev = self.leverage

        # 保证金收益率
        if side == 'LONG':
            margin_pnl_pct = (price - ep) / ep * lev
        else:
            margin_pnl_pct = (ep - price) / ep * lev

        # 爆仓判定: 浮亏超过保证金 → 强制归零
        if margin_pnl_pct <= -1.0:
            margin_pnl_pct = -1.0
            reason = 'LIQUIDATED'
            price = ep * (1 - 1.0/lev) if side == 'LONG' else ep * (1 + 1.0/lev)

        pnl_usd = margin * margin_pnl_pct

        # 平仓手续费 (统一按名义本金)
        exit_cost = notional * (TAKER_FEE + SLIPPAGE)
        pnl_usd -= exit_cost

        # 保证金从未离开账户(只是锁定), 平仓时只结算盈亏
        self.equity += pnl_usd

        trade = {
            'open_time': str(pos['open_time']),
            'close_time': str(ts),
            'coin': pos['coin'],
            'side': pos['side'],
            'entry': round(ep, 4),
            'exit': round(price, 4),
            'contracts': pos.get('contracts', pos.get('notional', 0) / (pos.get('entry', 1) * self.CONTRACT_FV.get(pos.get('coin', 'ETH'), 1.0))),
            'margin': round(margin, 2),
            'regime': pos.get('regime', '?'),
            'resonance_score': pos.get('resonance_score', 0),
            'reason': reason,
            'pnl': round(pnl_usd, 2),
            'pnl_pct': round(margin_pnl_pct * 100, 2),  # 保证金%
            'equity_after': round(self.equity, 2),
        }
        self.trades.append(trade)

        if self.verbose:
            mark = '+' if pnl_usd >= 0 else ''
            print(f"[CLOSE] {ts} | {pos['coin']:4s} {side:5s} | {reason:3s} | "
                  f"ep={ep:.2f} xp={price:.2f} | "
                  f"pnl={mark}{pnl_usd:.2f} ({margin_pnl_pct*100:+.1f}%) | "
                  f"eq={self.equity:.2f}")

        self.positions.remove(pos)

    # ================================================================
    # 内部: 辅助
    # ================================================================
    def _reset(self):
        self.equity = self.initial_capital
        self.positions = []
        self.trades = []
        self.equity_curve = []
        self._portfolio_curve = []
        self.cooldown = {}
        self.lock_until = -1
        self.losestreak = 0
        self._pyramid_count = 0
        self._last_entry_price = 0

    def _count_positions_before(self, bar_idx):
        """数一下指定 bar 之前的持仓数 (用于检测 just_closed)"""
        return len(self.positions)

    def _calc_total_equity(self, dfs, ts) -> float:
        """按已确认价格估算总权益 (严禁未来函数: 用上一根close或当前open)"""
        total = self.equity
        for pos in self.positions:
            coin = pos['coin']
            row = dfs[coin].loc[ts]
            # 用开盘价评估浮盈 (已确认, 非未闭合收盘价)
            px = float(row['open'])
            if pos['side'] == 'LONG':
                float_pnl = (px - pos['entry']) / pos['entry'] * self.leverage
            else:
                float_pnl = (pos['entry'] - px) / pos['entry'] * self.leverage
            total += pos['margin'] * float_pnl
            # 爆仓检测: 浮亏超过保证金 → 权益归零
            if pos['margin'] + pos['margin'] * float_pnl <= 0:
                return 0.0
        return max(total, 0.0)

    def _build_result(self, strategy, coins, index) -> Dict:
        closed = [t for t in self.trades if t['reason'] in ('TP', 'SL', 'EOD')]
        equity_arr = np.array([e['equity'] for e in self.equity_curve]) if self.equity_curve else np.array([self.initial_capital])

        return {
            'strategy': strategy.name,
            'coins': coins,
            'initial_capital': self.initial_capital,
            'leverage': self.leverage,
            'final_equity': round(self.equity, 2),
            'trades': self.trades,
            'closed_trades': closed,
            'equity_curve': self.equity_curve,
            'equity_array': equity_arr,
            'portfolio_curve': self._portfolio_curve,
            'data_bars': len(index),
            'data_start': str(index[0]),
            'data_end': str(index[-1]),
        }


# ============================================================
# 模块四: PerformanceAnalyzer — 绩效分析
# ============================================================
class PerformanceAnalyzer:
    """
    绩效分析模块。

    计算所有关键指标: 收益率、夏普、最大回撤、胜率、盈亏比。
    """

    @staticmethod
    def analyze(result: Dict) -> Dict:
        """
        分析回测结果。

        Args:
            result: BacktestEngine.run() 的返回值
        Returns:
            dict with all metrics
        """
        equity_arr = result.get('equity_array')
        trades = result.get('trades', [])
        initial = result['initial_capital']

        if equity_arr is None or len(equity_arr) < 2:
            return {'error': '权益数据不足'}

        metrics = {}

        # ---- 收益率 ----
        final = equity_arr[-1]
        metrics['total_return'] = round((final - initial) / initial * 100, 2)

        # 年化: 用真实时间跨度, 不用K线数估算
        start_str = result.get('data_start', '')
        end_str = result.get('data_end', '')
        try:
            start_dt = pd.to_datetime(start_str)
            end_dt = pd.to_datetime(end_str)
            total_days = (end_dt - start_dt).total_seconds() / 86400.0
            years = max(total_days / 365.25, 1 / 365.25)  # 至少1天
        except:
            # 降级: K线数估算
            n_bars = len(equity_arr)
            years = n_bars * 4 / (365 * 24)
        if years > 0 and initial > 0:
            total_ratio = final / initial
            if total_ratio > 0:
                metrics['annual_return'] = round((total_ratio ** (1 / years) - 1) * 100, 2)
            else:
                metrics['annual_return'] = -100.0
        else:
            metrics['annual_return'] = -100.0
        metrics['years'] = round(years, 2)

        # ---- 最大回撤 ----
        peak = np.maximum.accumulate(equity_arr)
        drawdown = (peak - equity_arr) / peak
        metrics['max_drawdown'] = round(np.max(drawdown) * 100, 2)

        # ---- 夏普比率 ----
        returns = np.diff(equity_arr) / equity_arr[:-1]
        if len(returns) > 1 and np.std(returns) > 0:
            # 年化夏普 (4H K线: 365*6 = 2190 根/年)
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(365 * 6)
            metrics['sharpe_ratio'] = round(sharpe, 3)
        else:
            metrics['sharpe_ratio'] = 0.0

        # ---- 交易统计 ----
        if trades:
            closed = [t for t in trades if t['reason'] in ('TP', 'SL', 'EOD')]
            if closed:
                wins = [t for t in closed if t['pnl'] > 0]
                losses = [t for t in closed if t['pnl'] <= 0]

                metrics['total_trades'] = len(closed)
                metrics['win_rate'] = round(len(wins) / len(closed) * 100, 2)
                metrics['avg_win'] = round(np.mean([t['pnl'] for t in wins]), 2) if wins else 0
                metrics['avg_loss'] = round(np.mean([t['pnl'] for t in losses]), 2) if losses else 0

                if metrics['avg_loss'] != 0:
                    metrics['profit_factor'] = round(abs(metrics['avg_win'] / metrics['avg_loss']), 2)
                else:
                    metrics['profit_factor'] = float('inf')

                metrics['total_pnl'] = round(sum(t['pnl'] for t in closed), 2)
            else:
                metrics['total_trades'] = 0
                metrics['win_rate'] = 0
        else:
            metrics['total_trades'] = 0

        # ---- 买入持有对比 ----
        metrics['buy_hold_return'] = round(
            (equity_arr[-1] / equity_arr[0] - 1) * 100, 2
        )

        return metrics

    @staticmethod
    def print_report(result: Dict, metrics: Dict):
        """打印格式化的回测报告"""
        print("\n" + "=" * 60)
        print(f"  回测报告: {result.get('strategy', 'Unknown')}")
        print("=" * 60)
        print(f"  数据区间: {result.get('data_start','?')} ~ {result.get('data_end','?')}")
        print(f"  数据量: {result.get('data_bars',0):,} 根K线")
        print(f"  初始资金: ${result['initial_capital']:,.0f}")
        print(f"  最终资金: ${result['final_equity']:,.2f}")

        if 'error' in metrics:
            print(f"  ERROR: {metrics['error']}")
            return

        print(f"\n  {'─' * 45}")
        print(f"  [核心指标]")
        print(f"  {'─' * 45}")

        tr = metrics['total_return']
        ar = metrics['annual_return']
        print(f"  总收益率:   {tr:+.2f}%")
        print(f"  年化收益:   {ar:+.2f}%")
        print(f"  最大回撤:   {metrics['max_drawdown']:.2f}%")
        print(f"  夏普比率:   {metrics['sharpe_ratio']:.3f}")

        print(f"\n  {'─' * 45}")
        print(f"  [交易统计]")
        print(f"  {'─' * 45}")
        print(f"  总交易数:   {metrics.get('total_trades', 0)}")
        print(f"  胜率:       {metrics.get('win_rate', 0):.1f}%")
        print(f"  平均盈利:   ${metrics.get('avg_win', 0):+.2f}")
        print(f"  平均亏损:   ${metrics.get('avg_loss', 0):+.2f}")
        print(f"  盈亏比:     {metrics.get('profit_factor', 0):.2f}")
        print(f"  累计盈亏:   ${metrics.get('total_pnl', 0):+.2f}")

        # 最近交易
        trades = result.get('trades', [])
        closed = [t for t in trades if t.get('reason') in ('TP', 'SL', 'EOD')][-8:]
        if closed:
            print(f"\n  {'─' * 45}")
            print(f"  [最近交易]")
            print(f"  {'─' * 45}")
            for t in reversed(closed):
                m = '+' if t['pnl'] >= 0 else ''
                print(f"  {t['close_time'][:16]} | {t['side']:5s} | "
                      f"{t['reason']:3s} | {m}{t['pnl']:.2f} ({t['pnl_pct']:+.2f}%)")

        print(f"\n  {'=' * 60}\n")


# ============================================================
# 模块五: 多Leg仓位与对冲系统
# ============================================================
from enum import Enum

class LegType(str, Enum):
    SPOT_LONG = "SPOT_LONG"
    FUTURES_LONG = "FUTURES_LONG"
    FUTURES_SHORT = "FUTURES_SHORT"

class StrategyMode(str, Enum):
    CLASSIC = "classic"           # 单向信号模式
    HEDGING = "hedging"          # Delta中性对冲
    UNLOCKING = "unlocking"      # 动能突破解封
    PYRAMIDING = "pyramiding"    # 分批加仓

class PositionLeg:
    """单条持仓腿"""
    def __init__(self, coin: str, leg_type: LegType, entry_price: float,
                 margin: float, leverage: int = 1):
        self.coin = coin
        self.leg_type = leg_type
        self.entry_price = entry_price
        self.margin = margin
        self.leverage = leverage
        self.notional = margin * leverage
        self.quantity = self.notional / entry_price
        self.open_time = None
        self.highest_price = entry_price   # trailing stop用
        self.lowest_price = entry_price
        self.trailing_activated = False

    @property
    def delta(self) -> float:
        """Delta暴露: 现货=正, 合约多头=正, 合约空头=负"""
        if self.leg_type == LegType.FUTURES_SHORT:
            return -self.notional
        return self.notional

    def update_extremes(self, high: float, low: float):
        self.highest_price = max(self.highest_price, high)
        self.lowest_price = min(self.lowest_price, low)

    def unrealized_pnl(self, current_price: float) -> float:
        if self.leg_type == LegType.FUTURES_SHORT:
            return (self.entry_price - current_price) / self.entry_price * self.notional
        return (current_price - self.entry_price) / self.entry_price * self.notional


class PortfolioManager:
    """
    多腿仓位管理器。

    支持:
      - 同账户 SPOT + FUTURES_LONG + FUTURES_SHORT 混合持仓
      - 实时 Delta 暴露计算
      - 分批建仓 (Pyramiding)
      - 状态机 (LOCKED/UNLOCKED/CLOSED)
      - ATR移动止损
    """

    def __init__(self, initial_capital: float, leverage: int = 3,
                 tp_pct: float = 0.10, sl_pct: float = 0.05,
                 mode: StrategyMode = StrategyMode.CLASSIC,
                 hedge_ratio: float = 0.5,
                 max_pyramid: int = 3,
                 pyramid_ratios: List[float] = None,
                 unlock_trigger: str = "price",
                 trailing_pct: float = 0.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.leverage = leverage
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.mode = mode
        self.hedge_ratio = hedge_ratio
        self.max_pyramid = max_pyramid
        self.pyramid_ratios = pyramid_ratios or [0.3, 0.3, 0.4]
        self.unlock_trigger = unlock_trigger
        self.trailing_pct = trailing_pct

        self.legs: List[PositionLeg] = []
        self.state = "UNLOCKED"   # LOCKED / UNLOCKED / CLOSED
        self.trades_log: List[Dict] = []
        self.pyramid_count = 0
        self._unlock_price = None

    # ---- Delta ----
    @property
    def net_delta(self) -> float:
        return sum(leg.delta for leg in self.legs)

    @property
    def is_hedged(self) -> bool:
        """Delta接近0 → 对冲锁定"""
        total = sum(abs(leg.notional) for leg in self.legs)
        if total == 0: return False
        return abs(self.net_delta) / total < 0.05

    @property
    def spot_value(self) -> float:
        return sum(leg.notional for leg in self.legs if leg.leg_type == LegType.SPOT_LONG)

    @property
    def futures_long_value(self) -> float:
        return sum(leg.notional for leg in self.legs if leg.leg_type == LegType.FUTURES_LONG)

    @property
    def futures_short_value(self) -> float:
        return sum(leg.notional for leg in self.legs if leg.leg_type == LegType.FUTURES_SHORT)

    # ---- 开仓 ----
    def open_leg(self, coin: str, leg_type: LegType, price: float,
                 alloc: float = 1.0, ts=None) -> Optional[PositionLeg]:
        """开仓一条腿 (分批模式下自动计算alloc)"""
        if self.cash <= 0:
            return None

        # 状态检查
        if self.mode == StrategyMode.HEDGING and self.state == "LOCKED":
            return None  # 对冲锁定状态不开新仓

        if self.mode == StrategyMode.PYRAMIDING and self.pyramid_count >= self.max_pyramid:
            return None

        if self.mode == StrategyMode.PYRAMIDING:
            idx = min(self.pyramid_count, len(self.pyramid_ratios) - 1)
            alloc = self.pyramid_ratios[idx] * alloc

        margin = self.cash * alloc
        lev = 1 if leg_type == LegType.SPOT_LONG else self.leverage

        leg = PositionLeg(coin, leg_type, price, margin, lev)
        leg.open_time = ts

        cost = leg.notional * (TAKER_FEE + SLIPPAGE)
        self.cash -= cost
        self.legs.append(leg)
        self.pyramid_count += 1

        return leg

    # ---- 平仓 ----
    def close_leg(self, leg: PositionLeg, price: float, reason: str = "manual", ts=None) -> float:
        """平掉指定腿, 返回PnL"""
        if leg not in self.legs:
            return 0.0

        pnl = leg.unrealized_pnl(price)
        exit_cost = leg.notional * (TAKER_FEE + SLIPPAGE)
        pnl -= exit_cost

        self.cash += leg.margin + pnl
        self.trades_log.append({
            'coin': leg.coin, 'type': leg.leg_type.value,
            'entry': leg.entry_price, 'exit': price,
            'margin': leg.margin, 'pnl': round(pnl, 2),
            'reason': reason, 'time': str(ts) if ts else '',
        })
        self.legs.remove(leg)
        return pnl

    def close_all(self, price: float, coin: str = None, reason: str = "close_all", ts=None) -> float:
        """平掉所有(或指定币种)的腿"""
        total_pnl = 0.0
        to_close = [l for l in self.legs if coin is None or l.coin == coin]
        for leg in to_close:
            total_pnl += self.close_leg(leg, price, reason, ts)
        return total_pnl

    def close_legs_by_type(self, leg_type: LegType, price: float, reason: str = "unlock", ts=None) -> float:
        """只平特定类型的腿 (如只平合约空单, 保留现货)"""
        return sum(self.close_leg(l, price, reason, ts)
                   for l in list(self.legs) if l.leg_type == leg_type)

    # ---- 动态止损 ----
    def check_trailing_stop(self, current_high: float, current_low: float,
                            current_close: float) -> List[PositionLeg]:
        """检查移动止损触发, 返回需要平仓的腿列表"""
        triggered = []
        if self.trailing_pct <= 0:
            return triggered

        for leg in self.legs:
            leg.update_extremes(current_high, current_low)
            if leg.leg_type == LegType.FUTURES_SHORT:
                # 空单: 价格新低 → 更新止损线
                trail_price = leg.lowest_price * (1 + self.trailing_pct)
                if current_close >= trail_price and leg.trailing_activated:
                    triggered.append(leg)
                elif leg.lowest_price < leg.entry_price:
                    leg.trailing_activated = True
            else:
                # 多单: 价格新高 → 更新止盈线
                trail_price = leg.highest_price * (1 - self.trailing_pct)
                if current_close <= trail_price and leg.trailing_activated:
                    triggered.append(leg)
                elif leg.highest_price > leg.entry_price:
                    leg.trailing_activated = True
        return triggered

    # ---- 状态机 ----
    def update_state(self, price: float, indicators: dict = None):
        """根据当前价格和指标更新状态机"""
        if self.mode == StrategyMode.UNLOCKING:
            if self.state == "LOCKED" and self._unlock_price:
                if price >= self._unlock_price:
                    self.state = "UNLOCKED"
                    # 解锁: 平掉对冲空单
                    self.close_legs_by_type(LegType.FUTURES_SHORT, price, "unlock")
            elif self.state == "UNLOCKED":
                if self.is_hedged:
                    self.state = "LOCKED"
                    self._unlock_price = price * 1.05  # 默认5%突破解锁

    # ---- 权益 ----
    def total_equity(self, current_price: float) -> float:
        return self.cash + sum(leg.unrealized_pnl(current_price) + leg.margin
                               for leg in self.legs)

    # ---- 重置 ----
    def reset(self):
        self.cash = self.initial_capital
        self.legs = []
        self.trades_log = []
        self.state = "UNLOCKED"
        self.pyramid_count = 0
        self._unlock_price = None


# ============================================================
# 快捷运行入口
# ============================================================
def run_backtest(
    coins: Union[str, List[str]] = 'ETH',
    strategy: StrategyBase = None,
    timeframe: str = '4h',
    initial_capital: float = 10000.0,
    leverage: int = 3,
    tp_pct: float = 10.0,          # 保证金止盈%
    sl_pct: float = 5.0,           # 保证金止损%
    bull_alloc: float = 1.0,
    range_alloc: float = 0.5,
    bear_alloc: float = 0.3,
    bear_ratio_limit: float = 0.5,
    verbose: bool = False,
) -> Tuple[Dict, Dict]:
    """
    一键运行回测 (使用 BacktestEngineV2 + 合约模式)。

    Args:
        coins: 币种或列表, 如 'ETH' 或 ['ETH','BTC','SOL']
        strategy: 策略对象
        timeframe: 时间框架
        initial_capital: 初始资金
        leverage: 杠杆倍数
        tp_pct: 止盈 (保证金%)
        sl_pct: 止损 (保证金%)
        bull_alloc: 牛市仓位比例
        range_alloc: 震荡仓位比例
        bear_alloc: 熊市仓位比例
        bear_ratio_limit: 空头比例上限 (超则空仓)
        verbose: 打印交易日志

    Example:
        >>> s = MACrossStrategy()
        >>> r, m = run_backtest(['ETH','BTC','SOL'], s, '4h', leverage=3)
    """
    if strategy is None:
        strategy = MACrossStrategy()
    if isinstance(coins, str):
        coins = [coins]

    de = DataEngine()
    dfs = {}
    for coin in coins:
        all_tf = de.get_multi_timeframe(coin)
        df = all_tf.get(timeframe, all_tf['4h'])
        if not de.validate(df):
            raise ValueError(f"{coin} {timeframe} 数据校验失败")
        dfs[coin] = df

    engine = BacktestEngineV2(
        initial_capital=initial_capital,
        leverage=leverage,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        max_positions=1,
        bull_alloc=bull_alloc,
        range_alloc=range_alloc,
        bear_alloc=bear_alloc,
        bear_ratio_limit=bear_ratio_limit,
        trailing_pct=0.0,
        verbose=verbose,
    )
    result = engine.run(dfs, strategy)
    metrics = PerformanceAnalyzer.analyze(result)
    return result, metrics


# ============================================================
# 自测
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("  量化回测核心引擎 v3.0 — 自测 (合约+轮动+动态仓位)")
    print("=" * 60)

    # 测试1: 三币轮动 (MACross + 3x 杠杆 + 动态仓位)
    print("\n[测试1] 三币轮动: MACross 5/20, 3x 杠杆, 动态仓位")
    s1 = MACrossStrategy(fast=5, slow=20)
    r1, m1 = run_backtest(
        coins=['ETH', 'BTC', 'SOL'], strategy=s1, timeframe='4h',
        leverage=3, tp_pct=10, sl_pct=5,
        bull_alloc=1.0, range_alloc=0.5, bear_alloc=0.3,
        verbose=False,
    )
    PerformanceAnalyzer.print_report(r1, m1)

    # 测试2: 单币 ETH 超跌反弹 (1x 现货)
    print("\n[测试2] 单币现货: 超跌反弹 on ETH 1H")
    s2 = OversoldBounceStrategy(lookback=12, threshold=-0.10)
    r2, m2 = run_backtest(
        coins='ETH', strategy=s2, timeframe='1h',
        leverage=1, tp_pct=8, sl_pct=4,
        bull_alloc=1.0, range_alloc=1.0, bear_alloc=1.0,
        verbose=False,
    )
    PerformanceAnalyzer.print_report(r2, m2)
