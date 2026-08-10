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

        # 优先5m, 降级15m
        path_5m = os.path.join(self.data_dir, f"{coin}_5m.parquet")
        path_15m = os.path.join(self.data_dir, f"{coin}_15m.parquet")
        if os.path.exists(path_5m) and os.path.getsize(path_5m) > 100000:
            path = path_5m
            interval = "5m"
        elif os.path.exists(path_15m) and os.path.getsize(path_15m) > 100000:
            path = path_15m
            interval = "15m"
        else:
            path = path_15m
            interval = "15m"
            if not os.path.exists(path):
                if not ensure_data_ready(coin):
                    raise FileNotFoundError(f"数据文件不存在且下载失败: {path}")

        df = pd.read_parquet(path)
        # 检查数据是否过期 (>7天未更新 → 触发重新下载)
        time_col = df.columns[0]
        df[time_col] = pd.to_datetime(df[time_col])
        last_ts = df[time_col].max()
        days_stale = (datetime.now() - last_ts).days
        if days_stale > 7:
            print(f"[DataEngine] {coin} data is {days_stale}d stale, triggering refresh...", flush=True)
            from data_loader import ensure_data
            ensure_data(coin)
            # 清除本引擎缓存触发重读
            if coin in self._cache:
                del self._cache[coin]
            df = pd.read_parquet(path)
        print(f"[DataEngine] Loaded {coin} from {path}: {len(df):,} bars, last={df[time_col].max()}", flush=True)

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
                 tp_pct: float = 10.0,         # 止盈%（保证金收益率 或 价格百分比，取决于tp_mode）
                 sl_pct: float = 5.0,          # 止损%（保证金亏损率 或 价格百分比，取决于sl_mode）
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
                 spot_tp: float = 5.0,          # 现货止盈%
                 spot_sl: float = 2.0,          # 现货止损%
                 short_sl: float = 3.0,         # 空单止损%
                 verbose: bool = True,
                 tp_mode: str = 'margin_pct',   # P0新增: 'margin_pct'(保证金%) 或 'price_pct'(价格%)
                 sl_mode: str = 'margin_pct',   # P0新增: 'margin_pct'(保证金%) 或 'price_pct'(价格%)
                 max_notional_pct: float = 5.0, # P0新增: 最大名义仓位 = equity × 这个倍数
                 use_atr_sl: bool = False,       # 需求4: ATR入场止损开关
                 atr_period: int = 14,           # 需求4: ATR计算周期
                 atr_mult: float = 2.0,          # 需求4: ATR止损倍数
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
        self._last_closed_bar = -1
        self._bar_idx = 0
        self._enable_pyramiding = False
        self._pyr_init_pct = 30
        self._pyr_trigger_pct = 2.0
        self._pyr_add_pct = 0.5
        self._pyr_max = 3
        self._pyr_trail = False
        self._portfolio_curve = []
        # 对冲状态机
        self._hedge_state = "IDLE"
        self._hedge_entry_price = 0
        self._spot_leg = None
        self._short_leg = None
        self._hedge_open_time = None
        # 双腿独立风控参数 (UI可配置)
        self._spot_tp = spot_tp / 100.0    # 现货止盈 (价格%)
        self._spot_sl = spot_sl / 100.0    # 现货止损
        self._short_tp = spot_tp / 100.0   # 空单止盈
        self._short_sl = short_sl / 100.0  # 空单止损

        # P0新增: TP/SL模式 + 风控保护
        self.tp_mode = tp_mode              # 'margin_pct'(保证金%) 或 'price_pct'(价格%)
        self.sl_mode = sl_mode              # 'margin_pct'(保证金%) 或 'price_pct'(价格%)
        self.max_notional_pct = max_notional_pct  # 最大名义仓位 = equity × 倍数
        # 需求4: ATR入场止损参数
        self._use_atr_sl = use_atr_sl
        self._atr_period = atr_period
        self._atr_mult = atr_mult
        # 杠杆上限保护 (交易所最高125x)
        if self.leverage > 125:
            raise ValueError(f"杠杆 {self.leverage}x 超过交易所上限 125x")

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

        # 0. 读取金字塔参数 (从策略selected dict)
        try:
            sel = getattr(strategy, 'selected', {})
            self._enable_pyramiding = sel.get('_enable_pyramiding', False)
            self._pyr_init_pct = sel.get('_pyr_init_pct', 30)
            self._pyr_trigger_pct = sel.get('_pyr_trigger_pct', 2.0)
            self._pyr_add_pct = sel.get('_pyr_add_pct', 0.5)
            self._pyr_max = sel.get('_pyr_max', 3)
            self._pyr_trail = sel.get('_pyr_trail', False)
            self._pos_mode = sel.get('_pos_mode', 'fixed_capital')
            self._risk_pct = sel.get('_risk_per_trade', 1.0) / 100.0
            # 闭环重构: 从UI读取单笔建仓比例% + 宏观系数, 覆盖__init__默认值
            self._init_alloc_pct = sel.get('_init_alloc_pct', 30)
            # 用户自主控制: UI存原始百分比(如100=100%), 引擎除以100转为乘数
            self.bull_alloc = float(sel.get('_bull_alloc', 100.0)) / 100.0
            self.range_alloc = float(sel.get('_range_alloc', 50.0)) / 100.0
            self.bear_alloc = float(sel.get('_bear_alloc', 30.0)) / 100.0
            # P0新增: 从策略配置加载TP/SL模式
            self.tp_mode = sel.get('_tp_mode', 'margin_pct')
            self.sl_mode = sel.get('_sl_mode', 'margin_pct')
            # 需求4修复: ATR参数透传 (之前hasattr永远为False!)
            self._use_atr_sl = sel.get('_use_atr_sl', False)
            self._atr_period = sel.get('_atr_period', 14)
            self._atr_mult = sel.get('_atr_mult', 2.0)
        except Exception as e:
            # 参数读取失败不应静默吞掉 — 打印警告后用默认值继续
            import traceback
            print(f"[WARN] 策略参数读取异常: {e}")
            traceback.print_exc()

        # 1. 对每个币种计算信号 (对冲模式跳过, 不需要指标)
        coins = list(dfs.keys())
        dfs_with_sigs = {}
        for coin in coins:
            df = dfs[coin].copy()
            if self.strategy_mode != "hedging":  # 解锁模式仍需指标(EMA/RSI检测)
                df = strategy.generate_signals(df)
            dfs_with_sigs[coin] = df

        # 1.5. P0新增: 预计算 ATR(14) — 全序列计算, shift(1)防未来函数, 用于入场时止损定价
        for coin in coins:
            df = dfs_with_sigs[coin]
            tr = pd.concat([
                df['high'] - df['low'],
                abs(df['high'] - df['close'].shift(1)),
                abs(df['low'] - df['close'].shift(1))
            ], axis=1).max(axis=1)
            df['_atr_14'] = tr.ewm(span=14, adjust=False).mean().shift(1)

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
        # 需求4: 回测前输出风险配置报告
        self._print_risk_report()
        for i, ts in enumerate(common_index):
            self._bar_idx = i
            paused = i < self.lock_until
            # ---- 对冲/解锁状态机 (每bar调用, IDLE→首Bar自动建仓) ----
            if self.strategy_mode in ("hedging", "unlocking"):
                self._hedge_state_machine(ts, dfs_with_sigs, coins, i)

            # ---- 检查持仓 TP/SL ----
            self._check_positions(ts, dfs_with_sigs, i)

            # ---- 开仓: 轮动选币 (对冲LOCKED状态下彻底屏蔽!) ----
            hedge_blocked = (self.strategy_mode in ("hedging", "unlocking") and
                             self._hedge_state in ("LOCKED", "UNLOCKED"))
            just_closed = (i == self._last_closed_bar)
            if not paused and not just_closed and not hedge_blocked and \
               len(self.positions) < self.max_positions:
                self._try_rotate_entry(ts, dfs_with_sigs, coins, i)

            # 破产熔断
            if self.equity <= 0:
                self.equity = 0.0
                break

            # ---- 资金费率结算 (每8h, 模拟永续合约) ----
            if self.leverage > 1 and i % 2 == 0:  # 4H周期: 每2根=8h
                for pos in self.positions:
                    if pos.get('leg') == 'SPOT': continue  # 现货不收资金费
                    funding_fee = pos['notional'] * 0.0001  # 默认0.01%费率
                    if pos['side'] == 'LONG':
                        self.equity -= funding_fee
                    else:  # SHORT: 做空收资金费(牛市通常为正)
                        self.equity += funding_fee * 0.5  # 保守估计

            # ---- 金字塔加仓检测 (经典模式) ----
            if self._enable_pyramiding and self._pyramid_count > 0 and \
               self._pyramid_count < self._pyr_max and not paused:
                self._check_pyramiding(ts, dfs_with_sigs, coins)

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

            # 爆仓检测: 浮亏超过保证金 或 触及维持保证金率
            if exit_price is None and ep > 0:
                liq_px = pos.get('liq_price', 0)
                mmr = pos.get('mmr', 0.005)
                if side == 'LONG':
                    u_pnl_pct = (bl - ep) / ep * self.leverage
                    margin_ratio = (margin + margin * u_pnl_pct) / margin if margin > 0 else 0
                    if u_pnl_pct <= -1.0 or (liq_px > 0 and bl <= liq_px) or margin_ratio < mmr:
                        exit_price = max(bl, liq_px) if liq_px > 0 else bl
                        exit_reason = 'LIQUIDATED'
                else:
                    u_pnl_pct = (ep - bh) / ep * self.leverage
                    margin_ratio = (margin + margin * u_pnl_pct) / margin if margin > 0 else 0
                    if u_pnl_pct <= -1.0 or (liq_px > 0 and bh >= liq_px) or margin_ratio < mmr:
                        exit_price = min(bh, liq_px) if liq_px > 0 else bh
                        exit_reason = 'LIQUIDATED'

            if exit_price is not None:
                self._close(pos, exit_price, exit_reason, ts)

                # 冷却 + 连亏追踪 + 断路器
                self.cooldown[coin] = bar_idx + self.cooldown_bars

                # 计算保证金盈亏%
                if side == 'LONG':
                    margin_pnl = (exit_price - ep) / ep * self.leverage
                else:
                    margin_pnl = (ep - exit_price) / ep * self.leverage

                if margin_pnl < 0:  # 闭环重构: 任何亏损都计入连亏计数
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
            if regime == 'bear' or best['side'] == 'SHORT':
                return

        # 闭环重构: alloc = 单笔建仓比例% (来自UI), 宏观系数由_open()内部根据regime查表
        alloc = self._init_alloc_pct / 100.0

        if alloc <= 0:
            return

        # P0修复: 删除_pyr_init_pct对regime_alloc的覆盖
        # regime_alloc 直接由市场状态决定，不受首仓比例覆盖
        # P0新增: 传递ATR值和df行数据给_open()用于入场时ATR止损定价
        coin_df = dfs[best['coin']]
        entry_row = coin_df.loc[ts]
        atr_val = entry_row.get('_atr_14', 0) if hasattr(entry_row, 'get') else 0
        self._open(best['coin'], best['side'], best['price'], alloc, ts, regime,
                   best.get('resonance_score', 0), atr_value=atr_val, df_row=entry_row)
        self._pyramid_count += 1
        self._last_entry_price = best['price']

    def _check_pyramiding(self, ts, dfs, coins):
        """金字塔加仓: 持仓浮盈达标→追加仓位+加权均价+更新TP/SL"""
        for pos in self.positions:
            side = pos['side']; coin = pos['coin']
            row = dfs[coin].loc[ts]; px = float(row['open'])
            regime = pos.get('regime', 'range')
            lev = self.leverage

            # 当前同向持仓加权均价 (by notional)
            same_side = [p for p in self.positions if p['side'] == side and p['coin'] == coin]
            total_notional = sum(p['notional'] for p in same_side)
            if total_notional <= 0: continue
            avg_entry = sum(p['entry'] * p['notional'] for p in same_side) / total_notional

            trigger = self._pyr_trigger_pct / 100.0
            should_add = (side == 'LONG' and px >= avg_entry * (1 + trigger)) or \
                         (side == 'SHORT' and px <= avg_entry * (1 - trigger))

            if should_add:
                add_margin = self.equity * self._pyr_add_pct
                add_notional = add_margin * lev
                # P0新增: 传递ATR值 + 累计已有名义仓位给_open() (累计上限保护)
                pyr_atr = row.get('_atr_14', 0) if hasattr(row, 'get') else 0
                self._open(coin, side, px, self._pyr_add_pct, ts, regime, 0,
                           atr_value=pyr_atr, df_row=row,
                           existing_notional=total_notional)
                self._pyramid_count += 1

                # 加权均价: Σ(notional_i * entry_i) / Σ(notional_i)
                total_n = total_notional + add_notional
                new_avg = (total_notional * avg_entry + add_notional * px) / max(total_n, 1)
                for p in same_side + self.positions[-1:]:  # 包括刚开的
                    if p['side'] == side and p['coin'] == coin:
                        p['entry'] = new_avg
                        # P0: TP/SL 重算也尊重 tp_mode
                        if self.tp_mode == 'price_pct':
                            p['tp_price'] = new_avg * (1 + self.tp_pct) if side == 'LONG' else \
                                            new_avg * (1 - self.tp_pct)
                        else:
                            p['tp_price'] = new_avg * (1 + self.tp_pct / lev) if side == 'LONG' else \
                                            new_avg * (1 - self.tp_pct / lev)
                # 保本止损: 均价即新的止损线
                if self._pyr_trail:
                    for p in same_side + self.positions[-1:]:
                        if p['side'] == side and p['coin'] == coin:
                            p['sl_price'] = new_avg
                if self.verbose:
                    print(f"[PYR] {ts} | {coin} {side} +{self._pyr_add_pct*100:.0f}% | "
                          f"avg={new_avg:.2f} | n={self._pyramid_count}/{self._pyr_max}")

    # ================================================================
    # 双腿独立对冲系统 (仅 HEDGING / UNLOCKING 模式)
    # IDLE→LOCKED→(UNLOCKED|IDLE)→IDLE(循环重开)
    # ================================================================
    def _hedge_state_machine(self, ts, dfs, coins, bar_idx):
        coin = coins[0]; df = dfs[coin]; row = df.loc[ts]
        bh = float(row['high']); bl = float(row['low']); px = float(row['open'])
        # 安全初始化
        spot_ep = 0.0; spot_margin = 0.0; spot_notional = 0.0
        short_ep = 0.0; short_margin = 0.0; short_notional = 0.0

        # === IDLE or FLAT: 建/重建双腿仓 ===
        if self._hedge_state in ("IDLE", "FLAT"):
            if self.equity <= 0: return
            # 现货多头 (滑点入价+手续费按名义本金)
            spot_notional = self.equity * self.hedge_ratio
            spot_fill = px * (1 + SLIPPAGE)
            self.equity -= spot_notional * TAKER_FEE
            self._spot_leg = {
                'coin':coin,'side':'LONG','entry':spot_fill,
                'margin':spot_notional,'notional':spot_notional,
                'open_time':ts,'tp_pct':self._spot_tp,'sl_pct':self._spot_sl,
                'tp_price':spot_fill*(1+self._spot_tp),'sl_price':spot_fill*(1-self._spot_sl),
                'leg':'SPOT',
            }
            # 合约空头 (强制Delta中性: short_notional=spot_notional, 滑点入价)
            short_notional = spot_notional
            short_margin = short_notional / self.leverage
            short_fill = px * (1 - SLIPPAGE)
            self.equity -= short_notional * TAKER_FEE
            self._short_leg = {
                'coin':coin,'side':'SHORT','entry':short_fill,
                'margin':short_margin,'notional':short_notional,
                'open_time':ts,'tp_pct':self._short_tp,'sl_pct':self._short_sl,
                'tp_price':short_fill*(1-self._short_tp/self.leverage),
                'sl_price':short_fill*(1+self._short_sl/self.leverage),
                'leg':'FUTURES',
            }
            self._hedge_state = "LOCKED"; self._hedge_entry_price = px
            return

        # === LOCKED / UNLOCKED: 逐bar评估双腿 ===
        # -- 空单腿 --
        if self._short_leg is not None:
            short_ep = self._short_leg['entry']
            short_sl = short_ep * (1 + self._short_sl / self.leverage)
            unlock_triggered = False; reason = ""

            if self.strategy_mode == "unlocking":
                if px >= short_ep * (1 + self.unlock_pct):
                    unlock_triggered = True; reason = f"price_{self.unlock_pct*100:.0f}%"
                elif 'ema_fast' in row.index and row['ema_fast'] > row.get('ema_slow', 0):
                    unlock_triggered = True; reason = "ema_cross"
            if self.strategy_mode == "hedging" and bh >= short_sl:
                unlock_triggered = True; reason = "short_sl"

            if unlock_triggered:
                s_notional = self._short_leg.get('notional', 0)
                s_margin = self._short_leg.get('margin', 0)
                # 空单盈亏 = 名义本金 * (入场-出场)/入场 - 手续费
                short_pnl = s_notional * (short_ep - px) / short_ep if short_ep > 0 else 0
                if short_pnl < -s_margin: short_pnl = -s_margin
                short_pnl -= s_notional * TAKER_FEE  # 平仓手续费
                short_pnl += s_notional * SLIPPAGE   # 做空平仓=买贵
                self.equity += short_pnl  # 只加净PnL, margin从未离开账户
                self.trades.append({
                    'open_time':str(self._short_leg['open_time']),'close_time':str(ts),
                    'coin':coin,'side':'SHORT','entry':short_ep,'exit':px,
                    'reason':f'UNLOCK_{reason}','pnl':round(short_pnl,2),
                    'pnl_pct':round(short_pnl/s_margin*100,2) if s_margin > 0 else 0,
                })
                self._short_leg = None; self._hedge_state = "UNLOCKED"

        # -- 现货腿 --
        if self._spot_leg is not None:
            spot_ep = self._spot_leg['entry']; spot_margin = self._spot_leg['margin']
            spot_notional = self._spot_leg['notional']
            spot_tp = spot_ep * (1 + self._spot_tp)
            spot_sl = spot_ep * (1 - self._spot_sl)
            close_spot = False; spot_reason = ""; spot_px = px

            if bh >= spot_tp: close_spot = True; spot_reason = "SPOT_TP"; spot_px = spot_tp
            elif bl <= spot_sl: close_spot = True; spot_reason = "SPOT_SL"; spot_px = spot_sl

            if close_spot:
                sn_val = self._spot_leg.get('notional', spot_notional) if self._spot_leg else spot_notional
                sm_val = self._spot_leg.get('margin', spot_margin) if self._spot_leg else spot_margin
                spot_pnl = sn_val * (spot_px - spot_ep) / spot_ep if spot_ep > 0 else 0
                spot_pnl -= sn_val * TAKER_FEE  # 平仓手续费
                spot_pnl -= sn_val * SLIPPAGE   # 做多平仓=卖贱
                self.equity += spot_pnl  # 只加净PnL
                self.trades.append({
                    'open_time':str(self._spot_leg['open_time']),'close_time':str(ts),
                    'coin':coin,'side':'LONG','entry':spot_ep,'exit':spot_px,
                    'reason':spot_reason,'pnl':round(spot_pnl,2),
                    'pnl_pct':round(spot_pnl/sm_val*100,2) if sm_val > 0 else 0,
                })
                self._spot_leg = None
                # 现货平仓 → 如果空单还在, 也平掉 → 重置状态
                if self._short_leg is not None:
                    se = self._short_leg.get('entry', px)
                    sn = self._short_leg.get('notional', 0)
                    sm = self._short_leg.get('margin', 0)
                    short_pnl = sn * (se - px) / se if se > 0 else 0
                    short_pnl -= sn * TAKER_FEE
                    short_pnl += sn * SLIPPAGE
                    self.equity += short_pnl
                    self._short_leg = None
                self._hedge_state = "FLAT"  # 下一bar IDLE检测 → 自动re-hedge

    # ================================================================
    # 内部: 开仓 / 平仓
    # ================================================================
    def _open(self, coin: str, side: str, price: float, alloc: float, ts,
              regime: str = 'range', resonance_score: int = 0,
              atr_value: float = 0, df_row=None, existing_notional: float = 0):
        """
        开仓 (合约模式)。

        P0修复内容:
        - Fixed Risk 引入 regime_multiplier (市场状态影响风险预算)
        - TP/SL 支持 margin_pct(保证金%) 和 price_pct(价格%) 两种模式
        - ATR 使用每bar预计算值 (非静态_atr_val), 入场时设定止损, 持仓期间不再更新
        - 最大名义仓位保护
        """
        # 负权益拦截
        if self.equity <= 0:
            return

        fv = self.CONTRACT_FV.get(coin, 1.0)
        lev = self.leverage

        # 滑点计入成交价
        fill_price = price * (1 + SLIPPAGE) if side == 'LONG' else price * (1 - SLIPPAGE)

        # === 仓位计算 (Fixed Capital / Fixed Risk / Dynamic Stop) ===
        use_fixed_risk = getattr(self, '_pos_mode', 'fixed_capital') == 'fixed_risk'
        use_dynamic_stop = getattr(self, '_pos_mode', 'fixed_capital') == 'dynamic_stop'
        risk_pct = getattr(self, '_risk_pct', 0.01)
        init_alloc = alloc  # 来自_try_rotate_entry: 单笔建仓比例% / 100

        # Step 1: 获取 regime_multiplier (牛/震/熊宏观系数)
        if regime == 'bull':
            regime_mult = self.bull_alloc
        elif regime == 'bear':
            regime_mult = self.bear_alloc
        else:
            regime_mult = self.range_alloc

        # 用户自主权: 如果该市场状态乘数设为0, 直接拦截不开仓
        if regime_mult <= 0:
            if self.verbose:
                print(f"[BLOCKED] {regime}市场乘数={regime_mult:.0%}, 用户配置跳过开仓")
            return

        # Step 2: SL 价格 (dynamic_stop模式跳过, 仓位确定后再倒推)
        if not use_dynamic_stop:
            if self.sl_mode == 'price_pct':
                if side == 'LONG':
                    sl_price = fill_price * (1 - self.sl_pct)
                else:
                    sl_price = fill_price * (1 + self.sl_pct)
            else:
                if side == 'LONG':
                    sl_price = fill_price * (1 - self.sl_pct / lev)
                else:
                    sl_price = fill_price * (1 + self.sl_pct / lev)

            # Step 2b: ATR入场止损覆盖 (入场时一次性定价)
            if hasattr(self, '_use_atr_sl') and self._use_atr_sl:
                atr_val = atr_value if (atr_value and atr_value > 0) else fill_price * 0.01
                atr_mult = getattr(self, '_atr_mult', 2.0)
                if side == 'LONG':
                    sl_price = fill_price - atr_val * atr_mult
                else:
                    sl_price = fill_price + atr_val * atr_mult
                if self.verbose:
                    print(f"[ATR SL] Entry ATR(14)={atr_val:.2f} | SL_mult={atr_mult} | "
                          f"SL_price={sl_price:.2f} ({abs(sl_price-fill_price)/fill_price*100:.2f}%)")

            sl_distance = abs(fill_price - sl_price)

        # Step 3: 仓位计算
        if use_fixed_risk:
            # === Fixed Risk: risk_budget = equity × risk_pct × regime_mult × init_alloc ===
            risk_budget = self.equity * risk_pct * regime_mult * init_alloc
            position_units = risk_budget / max(sl_distance, 1e-6)
            notional = position_units * fill_price
            margin = notional / lev

            if self.verbose:
                # 需求4: Fixed Risk 实时解释模块 — 每笔开仓展示完整计算链
                stop_source = "ATR动态" if (hasattr(self, '_use_atr_sl') and self._use_atr_sl) else "固定SL"
                sl_pct_display = (sl_distance / fill_price * 100)
                print(f"\n  {'='*56}")
                print(f"  [Fixed Risk Calc] 仓位计算详解")
                print(f"  {'='*56}")
                print(f"  账户权益:        ${self.equity:>12,.2f}")
                print(f"  风险占比:        {risk_pct*100:>12.1f}%")
                print(f"  市场乘数:        {regime_mult*100:>12.0f}% ({regime})")
                print(f"  建仓比例:        {init_alloc*100:>12.0f}%")
                print(f"  {'-'*38}")
                print(f"  风险预算:        ${risk_budget:>12,.2f}  (=权益x风险%x乘数x建仓%)")
                print(f"  {'-'*38}")
                print(f"  止损来源:        {stop_source:>12s}")
                print(f"  止损距离:        ${sl_distance:>12,.2f} /ETH  ({sl_pct_display:.2f}%价格)")
                print(f"  {'-'*38}")
                print(f"  开仓数量:        {position_units:>12.4f} ETH  (=风险预算/止损距离)")
                print(f"  名义仓位:        ${notional:>12,.2f}  (=数量x价格)")
                print(f"  占用保证金:      ${margin:>12,.2f}  (=名义仓位/杠杆)")
                print(f"  保证金占比:      {margin/self.equity*100:>12.1f}% 权益")
                print(f"  {'-'*38}")
                print(f"  止损触发亏损:    ${position_units*sl_distance:>12,.2f}  (≈风险预算)")
                print(f"  {'='*56}\n")
                print(f"[TRADE LOG] Fixed Risk: Equity=${self.equity:.0f} | "
                      f"Regime={regime}(x{regime_mult:.0%}) | InitAlloc={init_alloc:.0%} | "
                      f"RiskBudget=${risk_budget:.2f} | SL_dist=${sl_distance:.2f} | "
                      f"Units={position_units:.4f} | Notional=${notional:.0f} | "
                      f"Margin=${margin:.0f} ({margin/self.equity*100:.0f}%)")
        else:
            # === Fixed Capital: margin = equity × regime_mult × init_alloc ===
            margin = self.equity * regime_mult * init_alloc
            notional = margin * lev

            if self.verbose:
                mode_label = "Dynamic Stop" if use_dynamic_stop else "Fixed Capital"
                print(f"[TRADE LOG] {mode_label}: Equity=${self.equity:.0f} | "
                      f"Regime={regime}(x{regime_mult:.0%}) | InitAlloc={init_alloc:.0%} | "
                      f"Margin=${margin:.0f} | Notional=${notional:.0f}")

        # Step 3.5: Dynamic Stop — 根据名义价值倒推止损线
        if use_dynamic_stop:
            risk_budget_usd = self.equity * risk_pct * regime_mult * init_alloc
            dyn_sl_pct = risk_budget_usd / max(notional, 1e-6)
            # 钳制在合理范围: 0.1% ~ 20% 价格波动
            dyn_sl_pct = max(0.001, min(0.20, dyn_sl_pct))
            if side == 'LONG':
                sl_price = fill_price * (1 - dyn_sl_pct)
            else:
                sl_price = fill_price * (1 + dyn_sl_pct)
            sl_distance = abs(fill_price - sl_price)
            if self.verbose:
                print(f"[DYNAMIC STOP] RiskBudget=${risk_budget_usd:.2f} | "
                      f"Notional=${notional:.0f} | DynSL%={dyn_sl_pct*100:.2f}% | "
                      f"SL_price=${sl_price:.2f}")

        # Step 4: 硬风控校验
        # 4a: 保证金不足保护
        if margin > self.equity:
            margin = self.equity
            notional = margin * lev

        # 4b: 累计名义仓位上限 (已有持仓 + 本次新增)
        max_notional = self.equity * self.max_notional_pct
        total_notional = existing_notional + notional
        if total_notional > max_notional:
            notional = max(0, max_notional - existing_notional)
            margin = notional / lev
            if self.verbose:
                print(f"[RISK CAP] 累计名义{total_notional:.0f}超上限{max_notional:.0f}, "
                      f"缩减至本次新增={notional:.0f}")

        # Step 5: TP 价格计算 (不参与仓位公式, 仅用于止盈触发)
        if self.tp_mode == 'price_pct':
            if side == 'LONG':
                tp_price = fill_price * (1 + self.tp_pct)
            else:
                tp_price = fill_price * (1 - self.tp_pct)
        else:
            if side == 'LONG':
                tp_price = fill_price * (1 + self.tp_pct / lev)
            else:
                tp_price = fill_price * (1 - self.tp_pct / lev)

        # 手续费
        fee = notional * TAKER_FEE
        self.equity -= fee

        # 维持保证金率 + 强平价格 (OKX标准: 维持保证金率0.5%)
        mmr = 0.005
        if side == 'LONG':
            liq_price = fill_price * (1 - 1.0 / lev + mmr)
        else:
            liq_price = fill_price * (1 + 1.0 / lev - mmr)

        pos = {
            'coin': coin, 'side': side, 'entry': fill_price,
            'margin': margin, 'notional': notional,
            'alloc': alloc, 'regime': regime,
            'resonance_score': resonance_score,
            'tp_price': tp_price, 'sl_price': sl_price,
            'liq_price': liq_price, 'mmr': mmr,
            'open_time': ts, 'cost': fee,
            'highest_price': fill_price, 'lowest_price': fill_price,
            'trailing_activated': False,
        }
        self.positions.append(pos)

        if self.verbose:
            fv = self.CONTRACT_FV.get(coin, 1.0)
            contracts = notional / (price * fv) if (price * fv) > 0 else 0.0
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

        # 平仓滑点: 做多平仓=卖贱, 做空平仓=买贵
        exit_slip = -SLIPPAGE if side == 'LONG' else SLIPPAGE
        pnl_usd += notional * exit_slip
        # 平仓手续费
        pnl_usd -= notional * TAKER_FEE

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

        self._last_closed_bar = self._bar_idx if hasattr(self, '_bar_idx') else -1
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
        self._last_closed_bar = -1  # 防同Bar重开
        self._last_entry_price = 0

    def _count_positions_before(self, bar_idx):
        """数一下指定 bar 之前的持仓数 (用于检测 just_closed)"""
        return len(self.positions)

    def _print_risk_report(self):
        """需求4: 回测前输出风险配置报告，明确各参数生效状态与覆盖关系"""
        pos_mode = getattr(self, '_pos_mode', 'fixed_capital')
        use_fixed_risk = pos_mode == 'fixed_risk'
        use_dynamic_stop = pos_mode == 'dynamic_stop'
        use_atr = getattr(self, '_use_atr_sl', False)
        atr_mult = getattr(self, '_atr_mult', 2.0)
        atr_period = getattr(self, '_atr_period', 14)
        risk_pct = getattr(self, '_risk_pct', 0.01)
        init_alloc = getattr(self, '_init_alloc_pct', 30)

        # 确定有效止损方式
        if use_dynamic_stop:
            active_stop = f"Dynamic Stop (风险预算={risk_pct*100:.1f}% → 倒推SL%)"
            ignored = "固定SL + ATR(均被覆盖)"
        elif use_atr:
            active_stop = f"ATR({atr_period}) × {atr_mult}"
            sl_mode = getattr(self, 'sl_mode', 'margin_pct')
            ignored = f"固定SL {self.sl_pct*100:.1f}% ({sl_mode})"
        else:
            sl_mode = getattr(self, 'sl_mode', 'margin_pct')
            active_stop = f"固定SL {self.sl_pct*100:.1f}% ({sl_mode})"
            ignored = "无"

        # 确定仓位模式标签
        if use_dynamic_stop:
            mode_label = "Dynamic Stop (仓位=Fixed Capital吃满 + 止损动态收紧)"
        elif use_fixed_risk:
            mode_label = "Fixed Risk (风险预算 → 倒推仓位)"
        else:
            mode_label = "Fixed Capital (保证金比例 → 仓位)"

        # 计算有效风险敞口
        regime_info = (
            f"牛={self.bull_alloc*100:.0f}% / "
            f"震={self.range_alloc*100:.0f}% / "
            f"熊={self.bear_alloc*100:.0f}%"
        )
        if use_fixed_risk:
            effective_risk = (
                f"单笔风险预算 = 权益 × {risk_pct*100:.1f}% × 市场乘数 × {init_alloc}%"
            )
        elif use_dynamic_stop:
            effective_risk = (
                f"仓位 = 权益 × 市场乘数 × {init_alloc}% (Fixed Capital), "
                f"SL = {risk_pct*100:.1f}%风险预算 / notional"
            )
        else:
            effective_risk = (
                f"保证金 = 权益 × 市场乘数 × {init_alloc}%"
            )

        print()
        print("=" * 64)
        print("  [Risk Report] 风险配置报告 (Risk Configuration Report)")
        print("=" * 64)
        print(f"  Position Mode:      {mode_label}")
        print(f"  Active Stop:        {active_stop}")
        print(f"  Ignored Params:     {ignored}")
        print(f"  Risk Formula:       {effective_risk}")
        print(f"  Regime Multipliers: {regime_info}")
        print(f"  Leverage:           {self.leverage}x")
        print(f"  Max Notional:       {self.max_notional_pct}× 权益")
        print(f"  Lock:               {self.lock_streak}笔连亏 → 锁{self.lock_bars}根K线")
        print(f"  Initial Equity:     ${self.initial_capital:,.0f}")
        print("=" * 64)
        print()

    def _calc_total_equity(self, dfs, ts) -> float:
        """按已确认价格估算总权益 (含对冲腿浮动盈亏)"""
        total = self.equity
        coin = list(dfs.keys())[0] if dfs else None
        px = float(dfs[coin].loc[ts]['open']) if coin else 0
        for pos in self.positions:
            if pos['side'] == 'LONG':
                float_pnl = (px - pos['entry']) / pos['entry'] * self.leverage
            else:
                float_pnl = (pos['entry'] - px) / pos['entry'] * self.leverage
            total += pos['margin'] * float_pnl
        # 对冲腿浮动盈亏
        if self._spot_leg is not None:
            spot_ep = self._spot_leg['entry']; spot_mg = self._spot_leg['margin']
            total += spot_mg * (px - spot_ep) / spot_ep if spot_ep > 0 else 0
        if self._short_leg is not None:
            short_ep = self._short_leg['entry']; short_mg = self._short_leg['margin']
            total += short_mg * (short_ep - px) / short_ep * self.leverage if short_ep > 0 else 0
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
            'data_start': str(index[0]) if len(index) > 0 else '',
            'data_end': str(index[-1]) if len(index) > 0 else '',
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

        # ---- 收益率 (NaN防护) ----
        final = equity_arr[-1]
        if initial > 0 and not np.isnan(final) and not np.isinf(final):
            total_ret = (final - initial) / initial * 100
            metrics['total_return'] = round(total_ret, 2) if abs(total_ret) < 1e12 else 999999.0
        else:
            metrics['total_return'] = -100.0

        # 年化 (NaN防护)
        start_str = result.get('data_start', '')
        end_str = result.get('data_end', '')
        try:
            start_dt = pd.to_datetime(start_str); end_dt = pd.to_datetime(end_str)
            total_days = (end_dt - start_dt).total_seconds() / 86400.0
            years = max(total_days / 365.25, 1 / 365.25)
        except:
            years = len(equity_arr) * 4 / (365 * 24)
        if years > 0 and initial > 0 and final > 0 and not np.isnan(final):
            ratio = final / initial
            if 0 < ratio < 1e12:
                metrics['annual_return'] = round((ratio ** (1 / years) - 1) * 100, 2)
            else:
                metrics['annual_return'] = -100.0
        else:
            metrics['annual_return'] = -100.0
        metrics['years'] = round(years, 2)

        # ---- 最大回撤 ----
        peak = np.maximum.accumulate(equity_arr)
        drawdown = (peak - equity_arr) / peak
        dd_val = np.max(drawdown) if len(drawdown) > 0 else 0
        metrics['max_drawdown'] = round(float(dd_val) * 100, 2) if not np.isnan(dd_val) and not np.isinf(dd_val) else 100.0

        # ---- 夏普比率 (动态年化因子) ----
        returns = np.diff(equity_arr) / equity_arr[:-1]
        if len(returns) > 1 and np.std(returns) > 0:
            # 根据数据时间跨度估算每日bar数
            try:
                start_dt = pd.to_datetime(result.get('data_start', ''))
                end_dt = pd.to_datetime(result.get('data_end', ''))
                total_days = max((end_dt - start_dt).days, 1)
                bars_per_day = len(equity_arr) / total_days
            except:
                bars_per_day = 6  # 降级: 假设4H
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(365 * max(bars_per_day, 1))
            metrics['sharpe_ratio'] = round(sharpe, 3)
        else:
            metrics['sharpe_ratio'] = 0.0

        # ---- 交易统计 ----
        if trades:
            closed = [t for t in trades if t.get('reason', '') in
                      ('TP', 'SL', 'EOD', 'TRAIL', 'LIQUIDATED',
                       'SPOT_TP', 'SPOT_SL', 'PORTFOLIO_STOP',
                       'UNLOCK_price_breakout', 'UNLOCK_ema_cross',
                       'UNLOCK_rsi', 'UNLOCK_volume') or
                      str(t.get('reason', '')).startswith('UNLOCK_')]
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
            (equity_arr[-1] / max(equity_arr[0], 1) - 1) * 100, 2
        )
        # ---- Sortino比率 (下行标准差) ----
        if len(returns) > 1:
            downside_returns = returns[returns < 0]
            if len(downside_returns) > 1 and np.std(downside_returns) > 0:
                sortino = np.mean(returns) / np.std(downside_returns) * np.sqrt(365 * max(bars_per_day, 1))
                metrics['sortino_ratio'] = round(sortino, 3)
            else:
                metrics['sortino_ratio'] = 0.0

            # ---- Calmar比率 (年化收益/最大回撤) ----
            dd_val = metrics.get('max_drawdown', 1.0)
            if dd_val > 0 and metrics.get('annual_return', 0) > -100:
                metrics['calmar_ratio'] = round(metrics['annual_return'] / dd_val, 3)
            else:
                metrics['calmar_ratio'] = 0.0

            # ---- 最大连续亏损 ----
            pnl_sequence = [t.get('pnl', 0) for t in closed] if 'closed' in dir() else []
            if not pnl_sequence and trades:
                pnl_sequence = [t.get('pnl', 0) for t in trades if t.get('pnl') is not None]
            max_consecutive_losses = 0; current_streak = 0
            for pnl in pnl_sequence:
                if pnl <= 0: current_streak += 1
                else: current_streak = 0
                max_consecutive_losses = max(max_consecutive_losses, current_streak)
            metrics['max_consecutive_losses'] = max_consecutive_losses

            # ---- Recovery Factor (总收益/最大回撤金额) ----
            total_pnl = sum(pnl_sequence) if pnl_sequence else 0
            max_dd_amount = metrics.get('max_drawdown', 0) / 100.0 * initial
            if max_dd_amount > 0:
                metrics['recovery_factor'] = round(abs(total_pnl) / max_dd_amount, 2)
            else:
                metrics['recovery_factor'] = 0.0

        # 最终NaN清理
        for k in list(metrics.keys()):
            v = metrics[k]
            if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                metrics[k] = 0.0

        return metrics

    # ================================================================
    # 收益质量审计 (2026-08-11 新增)
    # 所有数据来自真实 trade history, 禁止根据收益曲线估算
    # ================================================================

    @staticmethod
    def _get_closed_trades(result: Dict):
        """提取已平仓交易 (统一过滤逻辑)"""
        trades = result.get('trades', [])
        return [t for t in trades if t.get('reason', '') in
                ('TP', 'SL', 'EOD', 'TRAIL', 'LIQUIDATED',
                 'SPOT_TP', 'SPOT_SL', 'PORTFOLIO_STOP') or
                str(t.get('reason', '')).startswith('UNLOCK_')]

    @staticmethod
    def _parse_dt(ts_str):
        """安全解析时间字符串"""
        try:
            return pd.to_datetime(ts_str)
        except:
            return None

    @staticmethod
    def quality_audit(result: Dict, metrics: Dict) -> Dict:
        """
        收益质量审计 — 所有数据来自真实 trade history。

        Returns dict with keys:
          - annual_table: list of {year, return_pct, max_dd, trades, win_rate, pf}
          - contribution: {top1_pct, top5_pct, top10_pct, level, warning}
          - extreme_removal: [{removed, new_return, new_annual, new_maxdd}, ...]
          - risk_contrib: {max_loss, max_consec_losses, max_consec_period, top5_loss_pct}
          - trade_stats: {avg_win, avg_loss, max_win, max_loss, avg_hold_h, max_hold_h}
        """
        trades = result.get('trades', [])
        closed = PerformanceAnalyzer._get_closed_trades(result)
        initial = result['initial_capital']
        equity_arr = result.get('equity_array')

        audit = {}

        # ── 1. 年度表现明细表 ──
        annual_rows = []
        if closed:
            df = pd.DataFrame(closed)
            df['year'] = pd.to_datetime(df['close_time']).dt.year
            for year, grp in df.groupby('year'):
                yr_pnl = grp['pnl'].sum()
                yr_return = yr_pnl / initial * 100
                yr_wins = (grp['pnl'] > 0).sum()
                yr_trades = len(grp)
                yr_wr = yr_wins / yr_trades * 100 if yr_trades > 0 else 0
                yr_wins_pnl = grp[grp['pnl'] > 0]['pnl']
                yr_loss_pnl = grp[grp['pnl'] <= 0]['pnl']
                avg_w = yr_wins_pnl.mean() if len(yr_wins_pnl) > 0 else 0
                avg_l = abs(yr_loss_pnl.mean()) if len(yr_loss_pnl) > 0 else 0
                pf = abs(avg_w / avg_l) if avg_l != 0 else (float('inf') if avg_w > 0 else 0)

                # 年度最大回撤 (从权益曲线截取该年区间)
                yr_dates = pd.to_datetime(grp['close_time'])
                yr_start = yr_dates.min(); yr_end = yr_dates.max()
                yr_dd = 0.0
                if equity_arr is not None and len(equity_arr) > 1:
                    try:
                        eq_df = pd.DataFrame({
                            'ts': pd.to_datetime([e['timestamp'] for e in result.get('equity_curve', [])]),
                            'eq': equity_arr
                        })
                        yr_eq = eq_df[(eq_df['ts'] >= yr_start) & (eq_df['ts'] <= yr_end)]
                        if len(yr_eq) > 1:
                            peak = np.maximum.accumulate(yr_eq['eq'].values)
                            dd = (peak - yr_eq['eq'].values) / peak
                            yr_dd = float(np.max(dd) * 100) if len(dd) > 0 else 0.0
                    except:
                        yr_dd = 0.0

                annual_rows.append({
                    'year': int(year), 'return_pct': round(yr_return, 2),
                    'max_dd': round(yr_dd, 2), 'trades': yr_trades,
                    'win_rate': round(yr_wr, 1), 'profit_factor': round(pf, 2) if pf != float('inf') else 999.0,
                })
        audit['annual_table'] = sorted(annual_rows, key=lambda x: x['year'])

        # ── 2. 交易贡献分析 ──
        contribution = {}
        if closed:
            sorted_by_pnl = sorted(closed, key=lambda t: t['pnl'], reverse=True)
            total_profit = sum(t['pnl'] for t in closed if t['pnl'] > 0)
            for n, label in [(1, 'top1'), (5, 'top5'), (10, 'top10')]:
                top_n = sorted_by_pnl[:n]
                top_n_profit = sum(t['pnl'] for t in top_n if t['pnl'] > 0)
                pct = (top_n_profit / total_profit * 100) if total_profit > 0 else 0
                contribution[f'{label}_amount'] = round(top_n_profit, 2)
                contribution[f'{label}_pct'] = round(pct, 2)

            # 集中度风险评级 (用top5)
            top5_pct = contribution.get('top5_pct', 0)
            if top5_pct < 30:
                contribution['level'] = '收益分散'; contribution['warning'] = 'green'
            elif top5_pct < 50:
                contribution['level'] = '中等集中'; contribution['warning'] = 'yellow'
            else:
                contribution['level'] = '高度依赖少数交易'; contribution['warning'] = 'red'
        else:
            for n in ['top1', 'top5', 'top10']:
                contribution[f'{n}_amount'] = 0; contribution[f'{n}_pct'] = 0
            contribution['level'] = '无交易数据'; contribution['warning'] = 'grey'
        audit['contribution'] = contribution

        # ── 3. 极端收益剔除测试 ──
        removal_tests = []
        if closed and equity_arr is not None and len(equity_arr) > 1:
            sorted_by_pnl = sorted(closed, key=lambda t: t['pnl'], reverse=True)
            final_eq = equity_arr[-1]
            years = metrics.get('years', 1)

            # 构建权益曲线时间轴
            eq_curve = result.get('equity_curve', [])
            eq_ts_list = []
            try:
                eq_ts_list = [pd.to_datetime(e['timestamp']) for e in eq_curve]
            except:
                eq_ts_list = list(range(len(equity_arr)))

            for n, label in [(1, '剔除最大1笔盈利'), (5, '剔除最大5笔盈利'), (10, '剔除最大10笔盈利')]:
                removed_trades = sorted_by_pnl[:min(n, len(sorted_by_pnl))]
                removed_pnl = sum(t['pnl'] for t in removed_trades)
                new_final = final_eq - removed_pnl
                new_ret = (new_final - initial) / initial * 100
                new_annual = ((max(new_final, 0.01) / initial) ** (1 / max(years, 0.01)) - 1) * 100

                # 构建调整后的权益曲线: 在每笔被剔除交易的平仓时间点扣减其PnL
                # idx -> 累计扣减金额
                adjustments = {}  # {equity_curve_index: cumulative_deduction}
                for t in removed_trades:
                    try:
                        close_dt = pd.to_datetime(t['close_time'])
                        pnl = t['pnl']
                        # 找到权益曲线中 >= close_time 的第一个时间点
                        for j, ets in enumerate(eq_ts_list):
                            if isinstance(ets, (int, float)):
                                if j >= close_dt:
                                    adjustments[j] = adjustments.get(j, 0) + pnl
                                    break
                            elif ets >= close_dt:
                                adjustments[j] = adjustments.get(j, 0) + pnl
                                break
                    except:
                        pass

                # 应用调整: 从每个调整点开始，后续所有权益点都减去累计扣减
                adjusted_eq = equity_arr.copy().astype(float)
                cumulative_deduction = 0.0
                sorted_adjustments = sorted(adjustments.items())
                adj_idx = 0
                for i in range(len(adjusted_eq)):
                    while adj_idx < len(sorted_adjustments) and sorted_adjustments[adj_idx][0] <= i:
                        cumulative_deduction += sorted_adjustments[adj_idx][1]
                        adj_idx += 1
                    adjusted_eq[i] = max(adjusted_eq[i] - cumulative_deduction, 0.01)

                # 基于调整后的曲线重新计算最大回撤
                try:
                    peak2 = np.maximum.accumulate(adjusted_eq)
                    dd2 = (peak2 - adjusted_eq) / peak2
                    new_maxdd = round(float(np.max(dd2) * 100), 2) if len(dd2) > 0 else 0.0
                except:
                    new_maxdd = 0.0

                removal_tests.append({
                    'label': label, 'removed_amount': round(removed_pnl, 2),
                    'new_return': round(new_ret, 2), 'new_annual': round(new_annual, 2),
                    'new_maxdd': new_maxdd,
                })
        audit['extreme_removal'] = removal_tests

        # ── 4. 风险贡献分析 ──
        risk = {}
        if closed:
            # 最大单笔亏损
            losses = [t for t in closed if t['pnl'] < 0]
            risk['max_single_loss'] = round(min(t['pnl'] for t in losses), 2) if losses else 0
            # 最大连续亏损 (使用metrics中已计算的)
            risk['max_consecutive_losses'] = metrics.get('max_consecutive_losses', 0)
            # 最大连续亏损周期 (时间段)
            pnl_seq = [(t['pnl'], t.get('close_time', '')) for t in closed]
            max_streak = 0; cur_streak = 0
            streak_start = None; max_streak_start = None; max_streak_end = None
            for i, (pnl, ct) in enumerate(pnl_seq):
                if pnl <= 0:
                    if cur_streak == 0: streak_start = ct
                    cur_streak += 1
                    if cur_streak > max_streak:
                        max_streak = cur_streak
                        max_streak_start = streak_start
                        max_streak_end = ct
                else:
                    cur_streak = 0; streak_start = None
            risk['max_consecutive_period'] = f"{str(max_streak_start)[:10]} ~ {str(max_streak_end)[:10]}" if max_streak_start else 'N/A'
            # 最大5笔亏损占比
            sorted_losses = sorted(losses, key=lambda t: t['pnl'])
            top5_loss = sum(t['pnl'] for t in sorted_losses[:5])
            total_loss = sum(t['pnl'] for t in losses)
            risk['top5_loss_amount'] = round(abs(top5_loss), 2)
            risk['top5_loss_pct'] = round(abs(top5_loss / total_loss * 100), 2) if total_loss != 0 else 0
        else:
            risk['max_single_loss'] = 0; risk['max_consecutive_losses'] = 0
            risk['max_consecutive_period'] = 'N/A'
            risk['top5_loss_amount'] = 0; risk['top5_loss_pct'] = 0
        audit['risk_contrib'] = risk

        # ── 5. 交易统计 ──
        stats = {}
        if closed:
            wins = [t for t in closed if t['pnl'] > 0]
            losses = [t for t in closed if t['pnl'] < 0]
            stats['avg_win'] = round(np.mean([t['pnl'] for t in wins]), 2) if wins else 0
            stats['avg_loss'] = round(np.mean([t['pnl'] for t in losses]), 2) if losses else 0
            stats['max_win'] = round(max(t['pnl'] for t in wins), 2) if wins else 0
            stats['max_loss'] = round(min(t['pnl'] for t in losses), 2) if losses else 0

            # 持仓时间 (小时)
            hold_hours = []
            for t in closed:
                ot = PerformanceAnalyzer._parse_dt(t.get('open_time', ''))
                ct = PerformanceAnalyzer._parse_dt(t.get('close_time', ''))
                if ot and ct:
                    hold_hours.append((ct - ot).total_seconds() / 3600)
            stats['avg_hold_hours'] = round(np.mean(hold_hours), 1) if hold_hours else 0
            stats['max_hold_hours'] = round(max(hold_hours), 1) if hold_hours else 0
        else:
            stats = {'avg_win': 0, 'avg_loss': 0, 'max_win': 0, 'max_loss': 0,
                     'avg_hold_hours': 0, 'max_hold_hours': 0}
        audit['trade_stats'] = stats

        return audit

    # ================================================================
    # 增强审计方法 (2026-08-11)
    # ================================================================

    @staticmethod
    def trading_frequency(result: Dict) -> Dict:
        """交易频率分析 — 从真实交易时间计算"""
        closed = PerformanceAnalyzer._get_closed_trades(result)
        if not closed:
            return {'total_trades': 0, 'avg_per_year': 0, 'avg_per_month': 0, 'level': '无交易'}

        df = pd.DataFrame(closed)
        df['close_dt'] = pd.to_datetime(df['close_time'])
        start = df['close_dt'].min()
        end = df['close_dt'].max()
        total_years = max((end - start).days / 365.25, 0.1)
        total_months = max((end - start).days / 30.44, 0.1)
        n = len(closed)

        avg_yr = n / total_years
        avg_mo = n / total_months

        if avg_yr < 6: level = '极低频策略'
        elif avg_yr < 24: level = '低频策略'
        elif avg_yr < 100: level = '中频策略'
        else: level = '高频策略'

        return {
            'total_trades': n,
            'period': f"{str(start)[:10]} ~ {str(end)[:10]}",
            'total_years': round(total_years, 1),
            'avg_per_year': round(avg_yr, 1),
            'avg_per_month': round(avg_mo, 1),
            'level': level,
        }

    @staticmethod
    def market_attribution(result: Dict) -> Dict:
        """市场状态归因分析 — 按牛/震/熊拆分收益"""
        closed = PerformanceAnalyzer._get_closed_trades(result)
        if not closed:
            return {'bull_pnl': 0, 'range_pnl': 0, 'bear_pnl': 0, 'bull_trades': 0, 'range_trades': 0, 'bear_trades': 0,
                    'conclusion': '无交易数据'}

        bull_pnl = sum(t['pnl'] for t in closed if t.get('regime') == 'bull')
        range_pnl = sum(t['pnl'] for t in closed if t.get('regime') == 'range')
        bear_pnl = sum(t['pnl'] for t in closed if t.get('regime') == 'bear')
        bull_n = len([t for t in closed if t.get('regime') == 'bull'])
        range_n = len([t for t in closed if t.get('regime') == 'range'])
        bear_n = len([t for t in closed if t.get('regime') == 'bear'])
        total_pnl = bull_pnl + range_pnl + bear_pnl

        # 各市场贡献占比
        total_abs = abs(bull_pnl) + abs(range_pnl) + abs(bear_pnl)
        bull_pct = bull_pnl / total_abs * 100 if total_abs > 0 else 0
        range_pct = range_pnl / total_abs * 100 if total_abs > 0 else 0
        bear_pct = bear_pnl / total_abs * 100 if total_abs > 0 else 0

        # 结论
        if bull_pct > 60 and bear_pct < 20:
            conclusion = '策略主要依赖牛市趋势行情，熊市贡献有限'
        elif bull_pct > 40 and range_pct > 30:
            conclusion = '收益来源较均衡，牛市和震荡市均有贡献'
        elif bear_pct > 50:
            conclusion = '策略在熊市中表现突出（可能依赖做空或对冲）'
        else:
            conclusion = '收益来源多元，各市场状态均有表现'

        # 各市场胜率
        bull_wr = len([t for t in closed if t.get('regime') == 'bull' and t['pnl'] > 0]) / max(bull_n, 1) * 100
        range_wr = len([t for t in closed if t.get('regime') == 'range' and t['pnl'] > 0]) / max(range_n, 1) * 100
        bear_wr = len([t for t in closed if t.get('regime') == 'bear' and t['pnl'] > 0]) / max(bear_n, 1) * 100

        return {
            'bull_pnl': round(bull_pnl, 2), 'range_pnl': round(range_pnl, 2), 'bear_pnl': round(bear_pnl, 2),
            'bull_trades': bull_n, 'range_trades': range_n, 'bear_trades': bear_n,
            'bull_pct': round(bull_pct, 1), 'range_pct': round(range_pct, 1), 'bear_pct': round(bear_pct, 1),
            'bull_wr': round(bull_wr, 1), 'range_wr': round(range_wr, 1), 'bear_wr': round(bear_wr, 1),
            'conclusion': conclusion,
        }

    @staticmethod
    def generate_strategy_summary(result: Dict, metrics: Dict, audit: Dict) -> str:
        """自动生成策略评价报告"""
        freq = PerformanceAnalyzer.trading_frequency(result)
        attr = PerformanceAnalyzer.market_attribution(result)
        contrib = audit.get('contribution', {})
        removal = audit.get('extreme_removal', [])

        lines = []
        lines.append(f"策略类型：{result.get('strategy', 'Unknown')} — {freq.get('level', '未知')}")

        # 收益特征
        total_ret = metrics.get('total_return', 0)
        max_dd = metrics.get('max_drawdown', 0)
        sharpe = metrics.get('sharpe_ratio', 0)
        lines.append(f"收益特征：总收益{total_ret:+.1f}%，最大回撤{max_dd:.1f}%，夏普{sharpe:.2f}")

        # 频率特征
        lines.append(f"交易频率：{freq.get('avg_per_year',0):.1f}笔/年，{freq.get('avg_per_month',0):.1f}笔/月 — {freq.get('level','')}")

        # 收益来源
        lines.append(f"收益来源：牛市{attr.get('bull_pct',0):.0f}%（{attr.get('bull_trades',0)}笔，胜率{attr.get('bull_wr',0):.0f}%），"
                     f"震荡{attr.get('range_pct',0):.0f}%（{attr.get('range_trades',0)}笔，胜率{attr.get('range_wr',0):.0f}%），"
                     f"熊市{attr.get('bear_pct',0):.0f}%（{attr.get('bear_trades',0)}笔，胜率{attr.get('bear_wr',0):.0f}%）")
        lines.append(f"收益来源判断：{attr.get('conclusion', '')}")

        # 集中度
        top5_pct = contrib.get('top5_pct', 0)
        level = contrib.get('level', '未知')
        lines.append(f"收益集中度：前5笔占{top5_pct:.0f}% — {level}")

        # 极端依赖
        if removal:
            r10 = removal[-1] if len(removal) >= 3 else removal[0]
            orig_ret = total_ret
            new_ret = r10.get('new_return', 0)
            decay = abs(new_ret - orig_ret)
            if decay > abs(orig_ret) * 0.5:
                lines.append(f"极端行情依赖：删除10笔最大盈利后收益从{orig_ret:+.1f}%降至{new_ret:+.1f}%，策略高度依赖少数交易")
            else:
                lines.append(f"极端行情依赖：删除10笔最大盈利后收益变化{decay:.1f}%，策略收益来源较稳定")

        # 风险
        risk = audit.get('risk_contrib', {})
        lines.append(f"风险特征：最大单笔亏损${risk.get('max_single_loss',0):+.0f}，最大连续亏损{risk.get('max_consecutive_losses',0)}笔")

        # 综合建议
        suggestions = []
        if attr.get('bull_pct', 0) > 70:
            suggestions.append("关注熊市过滤有效性，考虑降低熊市风险暴露")
        if top5_pct > 40:
            suggestions.append("收益集中度高，建议分散入场时机或降低单笔风险")
        if freq.get('avg_per_year', 0) < 6:
            suggestions.append("交易频率极低，样本量不足可能导致统计偏差")
        if max_dd > 30:
            suggestions.append("最大回撤偏高，建议收紧止损或降低杠杆")
        if sharpe < 0.5:
            suggestions.append("风险调整后收益偏低，考虑优化入场信号质量")
        if not suggestions:
            suggestions.append("当前策略表现均衡，持续监控市场状态变化即可")

        lines.append(f"优化建议：{'；'.join(suggestions)}")

        return '\n'.join(lines)

    @staticmethod
    def param_audit_report(result: Dict, metrics: Dict) -> Dict:
        """后台参数一致性审计 — 验证所有UI参数真正影响回测"""
        trades = result.get('trades', [])
        closed = PerformanceAnalyzer._get_closed_trades(result)

        report = {
            'total_params': 0,
            'ui_params': [],
            'engine_params': [],
            'used_params': [],
            'overridden_params': [],
            'anomalies': [],
        }

        # 从result推断实际使用的参数
        if closed:
            # 检查是否有不同regime的交易 -> 验证市场系数生效
            regimes = set(t.get('regime', '?') for t in closed)
            report['engine_params'].append(f'市场系数生效: {regimes}')

            # 检查保证金是否变化 -> 验证alloc参数生效
            margins = [t.get('margin', 0) for t in closed if t.get('margin')]
            if len(set(round(m, 2) for m in margins)) > 1:
                report['engine_params'].append('仓位大小有变化 -> alloc参数生效')
            else:
                report['anomalies'].append('所有仓位大小相同 -> alloc参数可能未生效')

            # 检查exit_reason -> 验证TP/SL生效
            reasons = set(t.get('reason', '?') for t in closed)
            if 'SL' in str(reasons) or 'TP' in str(reasons):
                report['engine_params'].append(f'TP/SL触发: {reasons}')
            if 'LIQUIDATED' in str(reasons):
                report['engine_params'].append('爆仓检测生效')

        # UI参数清单
        report['ui_params'] = [
            '_pos_mode', '_risk_per_trade', '_init_alloc_pct',
            '_bull_alloc', '_range_alloc', '_bear_alloc',
            '_tp_mode', '_sl_mode', '_use_atr_sl', '_atr_period', '_atr_mult',
        ]
        report['total_params'] = len(report['ui_params'])
        report['used_params'] = report['engine_params']

        # 覆盖参数
        if closed:
            atr_trades = [t for t in closed if 'ATR' in str(t.get('reason', ''))]
            if not atr_trades and any('ATR' in str(r) for r in reasons):
                report['overridden_params'].append('ATR动态止损覆盖固定SL')

        return report

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
        closed = [t for t in trades if t.get('reason', '') in
                  ('TP','SL','EOD','TRAIL','LIQUIDATED','SPOT_TP','SPOT_SL','PORTFOLIO_STOP') or
                  str(t.get('reason','')).startswith('UNLOCK_')][-8:]
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

        self.cash += pnl  # 只加净PnL, margin从未离开账户
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
# Walk Forward 验证 + 审计报告
# ============================================================
def walk_forward_validation(strategy, coin: str, timeframe: str = '4h',
                             train_years: tuple = (2017, 2022),
                             val_year: int = 2023,
                             test_years: tuple = (2024, 2026),
                             engine_kwargs: dict = None) -> dict:
    """
    前向滚动验证: 训练→验证→测试 三段独立评估。

    Returns:
        {'train': metrics, 'val': metrics, 'test': metrics, 'robustness': score}
    """
    de = DataEngine()
    df = de.get_multi_timeframe(coin).get(timeframe)
    if df is None or len(df) < 500:
        return {'error': '数据不足'}

    kw = engine_kwargs or {}
    results = {}

    for label, (start_yr, end_yr) in [
        ('train', train_years), ('val', (val_year, val_year)),
        ('test', test_years)
    ]:
        mask = (df.index.year >= start_yr) & (df.index.year <= end_yr)
        df_seg = df[mask].copy()
        if len(df_seg) < 200: continue
        engine = BacktestEngineV2(**kw)
        result = engine.run({coin: df_seg}, strategy)
        metrics = PerformanceAnalyzer.analyze(result)
        results[label] = {
            'total_return': metrics.get('total_return', -100),
            'annual_return': metrics.get('annual_return', -100),
            'max_drawdown': metrics.get('max_drawdown', 100),
            'sharpe': metrics.get('sharpe_ratio', 0),
            'sortino': metrics.get('sortino_ratio', 0),
            'win_rate': metrics.get('win_rate', 0),
            'trades': metrics.get('total_trades', 0),
        }

    # 鲁棒性评分: 验证集和测试集表现
    if 'train' in results and 'test' in results:
        train_ann = results['train']['annual_return']
        test_ann = results['test']['annual_return']
        if train_ann > 0 and test_ann > 0:
            robustness = min(test_ann / max(train_ann, 1), 2.0) * 100
        elif test_ann > 0:
            robustness = 80
        else:
            robustness = max(0, 50 + test_ann)
        results['robustness'] = round(robustness, 1)
    else:
        results['robustness'] = 0

    return results


def generate_audit_report(result: dict, metrics: dict, coin: str, timeframe: str) -> str:
    """生成回测审计报告文本"""
    m = metrics
    closed = result.get('closed_trades', result.get('trades', []))
    liq_count = sum(1 for t in closed if t.get('reason', '') == 'LIQUIDATED')
    total_fees = sum(abs(t.get('pnl', 0)) * 0.0005 * 2 for t in closed if t.get('pnl'))
    funding_cost_est = len(result.get('equity_curve', [])) / 2 * 0.0001 * 10000  # 粗略估算

    lines = [
        "=" * 50, "  BACKTEST AUDIT REPORT", "=" * 50,
        f"  Strategy: {result.get('strategy', '?')}",
        f"  Coin: {coin} | Timeframe: {timeframe}",
        f"  Period: {result.get('data_start','?')} ~ {result.get('data_end','?')}",
        f"  Leverage: {result.get('leverage','?')}x",
        "",
        f"  --- Returns ---",
        f"  Total Return: {m.get('total_return',0):+.1f}%",
        f"  Annual Return: {m.get('annual_return',0):+.1f}%",
        f"  Sortino Ratio: {m.get('sortino_ratio',0):.3f}",
        f"  Calmar Ratio: {m.get('calmar_ratio',0):.3f}",
        f"  Recovery Factor: {m.get('recovery_factor',0):.2f}",
        "",
        f"  --- Risk ---",
        f"  Max Drawdown: {m.get('max_drawdown',0):.1f}%",
        f"  Sharpe Ratio: {m.get('sharpe_ratio',0):.3f}",
        f"  Max Consecutive Losses: {m.get('max_consecutive_losses',0)}",
        f"  Liquidation Count: {liq_count}",
        "",
        f"  --- Trading ---",
        f"  Total Trades: {m.get('total_trades',0)}",
        f"  Win Rate: {m.get('win_rate',0):.1f}%",
        f"  Avg Win: ${m.get('avg_win',0):+.2f} | Avg Loss: ${m.get('avg_loss',0):+.2f}",
        f"  Profit Factor: {m.get('profit_factor',0):.2f}",
        "",
        f"  --- Costs (Est.) ---",
        f"  Trading Fees: ${total_fees:.0f}",
        f"  Funding Cost: ${funding_cost_est:.0f}",
        f"  Slippage Rate: {SLIPPAGE*100:.2f}%",
        "",
        f"  --- REALISM SCORE ---",
    ]

    # 真实性评分
    score = 85  # 基础分
    if m.get('sortino_ratio', 0) > 0: score += 3
    if m.get('calmar_ratio', 0) > 0: score += 2
    if liq_count > 0: score -= 5  # 有强平说明风控严格
    if total_fees > 0: score += 5
    if funding_cost_est > 0: score += 5

    if score >= 90: grade = "A: 接近真实交易环境"
    elif score >= 75: grade = "B: 存在轻微理想化(如固定滑点假设)"
    else: grade = "C: 结果参考价值有限, 需加强摩擦成本模拟"

    lines.append(f"  Grade: {grade} (Score: {score}/100)")
    lines.append("=" * 50)
    return "\n".join(lines)


# ============================================================
# 快捷运行入口
# ============================================================
def run_backtest(
    coins: Union[str, List[str]] = 'ETH',
    strategy: StrategyBase = None,
    timeframe: str = '4h',
    initial_capital: float = 10000.0,
    leverage: int = 3,
    tp_pct: float = 10.0,           # 止盈%（保证金收益率 或 价格百分比，取决于tp_mode）
    sl_pct: float = 5.0,            # 止损%（保证金亏损率 或 价格百分比，取决于sl_mode）
    bull_alloc: float = 1.0,
    range_alloc: float = 0.5,
    bear_alloc: float = 0.3,
    bear_ratio_limit: float = 0.5,
    verbose: bool = False,
    tp_mode: str = 'margin_pct',     # P0: 'margin_pct' | 'price_pct'
    sl_mode: str = 'margin_pct',     # P0: 'margin_pct' | 'price_pct'
    max_notional_pct: float = 5.0,   # P0: 最大名义仓位 = equity × 倍数
) -> Tuple[Dict, Dict]:
    """
    一键运行回测 (使用 BacktestEngineV2 + 合约模式)。

    Args:
        coins: 币种或列表, 如 'ETH' 或 ['ETH','BTC','SOL']
        strategy: 策略对象
        timeframe: 时间框架
        initial_capital: 初始资金
        leverage: 杠杆倍数
        tp_pct: 止盈%（保证金收益率 或 价格百分比）
        sl_pct: 止损%（保证金亏损率 或 价格百分比）
        bull_alloc: 牛市仓位比例
        range_alloc: 震荡仓位比例
        bear_alloc: 熊市仓位比例
        bear_ratio_limit: 空头比例上限 (超则空仓)
        verbose: 打印交易日志
        tp_mode: 'margin_pct'(保证金%止盈) | 'price_pct'(价格%止盈)
        sl_mode: 'margin_pct'(保证金%止损) | 'price_pct'(价格%止损)
        max_notional_pct: 最大名义仓位(equity的倍数)

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
        tp_mode=tp_mode,
        sl_mode=sl_mode,
        max_notional_pct=max_notional_pct,
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
