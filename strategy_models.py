"""
策略模型（从 app.py 抽离）
============================================================
DynamicStrategy: 从指标注册表动态组装信号的策略类（StrategyBase 子类）。

独立成模块的原因：Streamlit 入口 app.py 不得被业务模块 import，
否则会重跑整个页面、重复创建 widget 触发 StreamlitDuplicateElementKey。
业务模块一律 `from strategy_models import DynamicStrategy`，绝不 `from app import ...`。
"""
import numpy as np
import pandas as pd

from engine_core import StrategyBase, MultiFactorRegime
from indicator_schema import INDICATOR_REGISTRY


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
