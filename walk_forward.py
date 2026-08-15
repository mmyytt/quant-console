"""
================================================================
 Walk Forward 滚动样本外分析引擎 v1.0
================================================================
 设计原则:
   - 与 engine_core.py 完全解耦, 独立模块
   - 严禁未来函数: 每窗口独立回测, 信号用 shift(1)
   - 自动切分滚动窗口 (train_years + test_years)
   - ADX 市场环境分析: 策略是否过度依赖趋势行情
   - 所有数字由程序计算，AI只负责解释

 使用方法:
   from walk_forward import WalkForwardAnalyzer
   wf_result = WalkForwardAnalyzer.analyze(
       coin='ETH', timeframe='4h',
       start_year=2020, end_year=2026,
       strategy_config=selected_indicators,
       engine_kwargs=strat_kwargs,
   )
================================================================
"""
import pandas as pd
import numpy as np
import re
import itertools
from typing import Dict, List, Optional
from datetime import datetime

from engine_core import DataEngine, BacktestEngineV2, PerformanceAnalyzer, MultiFactorRegime
from i18n import t as _t, trend_dep_label
# DynamicStrategy 在 app.py 中, 通过参数注入


# ============================================================
# WalkForwardAnalyzer — 滚动样本外测试引擎
# ============================================================
class WalkForwardAnalyzer:
    """
    滚动窗口 Walk Forward 分析器。

    对任意策略进行滚动样本外测试, 验证策略泛化能力:
      - 自动切分训练/测试窗口
      - 每窗口独立回测
      - ADX 趋势依赖分析
      - 综合 Walk Forward Score
    """

    @staticmethod
    def analyze(
        coin: str,
        timeframe: str,
        start_year: int,
        end_year: int,
        strategy_config: dict,
        engine_kwargs: dict,
        mf_params: dict = None,
        use_and: bool = True,
        train_years: int = 2,
        test_years: int = 1,
        strategy_class=None,  # DynamicStrategy 类引用
        param_grid: Optional[Dict] = None,  # P2-7: 训练窗参数搜索网格 {engine_kwarg: [values]}
    ) -> Dict:
        """
        执行滚动 Walk Forward 分析。

        Args:
            coin: 币种, 'ETH'/'BTC'/'SOL'
            timeframe: 时间框架, '4h'/'1h'/'1d'等
            start_year: 数据起始年份
            end_year: 数据结束年份
            strategy_config: 策略配置 (selected_indicators dict)
            engine_kwargs: BacktestEngineV2 参数字典
            mf_params: 多因子牛熊参数
            use_and: 信号组合模式 (AND/OR)
            train_years: 每窗口训练年数
            test_years: 每窗口测试年数
            strategy_class: DynamicStrategy 类 (从 app.py 传入)

        Returns:
            {
                "windows": [...],        # 每窗口详细结果
                "score": {...},          # Walk Forward 综合评分
                "adx_analysis": {...},   # ADX 趋势依赖分析
                "summary": str,          # 文本摘要
                "overfitting_risk": str, # 过拟合风险评级
            }
        """
        # 1) 加载数据
        de = DataEngine()
        all_tf = de.get_multi_timeframe(coin)
        df = all_tf.get(timeframe, all_tf['4h'])
        if df is None or len(df) < 500:
            return {"error": _t('wf_err_insufficient'), "windows": [], "score": {}}

        # 确保 index 是 datetime
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        # 2) 生成滚动窗口
        windows_spec = WalkForwardAnalyzer._generate_windows(
            start_year, end_year, train_years, test_years
        )

        if not windows_spec:
            return {"error": _t('wf_err_no_window'), "windows": [], "score": {}}

        # 3) 逐窗口回测
        window_results = []
        # 过滤 mf_params: 只保留 MultiFactorRegime 接受的参数
        _valid_mf_keys = {'ema_span','slope_lookback','adx_period','adx_threshold',
                          'ema_weight','adx_weight','funding_weight','bull_threshold','bear_threshold'}
        _mf_kwargs = {k: v for k, v in (mf_params or {}).items() if k in _valid_mf_keys}
        mf = MultiFactorRegime(**_mf_kwargs)

        for i, ((train_start, train_end), (test_start, test_end)) in enumerate(windows_spec):
            win_result = WalkForwardAnalyzer._run_window(
                df=df,
                coin=coin,
                timeframe=timeframe,
                train_range=(train_start, train_end),
                test_range=(test_start, test_end),
                strategy_config=strategy_config,
                engine_kwargs=engine_kwargs,
                mf_params=mf_params,
                use_and=use_and,
                strategy_class=strategy_class,
                window_index=i + 1,
                param_grid=param_grid,
            )
            window_results.append(win_result)

        # 过滤掉失败的窗口
        valid_windows = [w for w in window_results if w.get("train") and w.get("test")]

        if not valid_windows:
            return {"error": _t('wf_err_all_failed'), "windows": window_results, "score": {}}

        # 4) ADX 市场环境分析
        adx_analysis = WalkForwardAnalyzer._analyze_adx(df, valid_windows, mf)

        # 5) 综合评分
        score = WalkForwardAnalyzer._compute_score(valid_windows, adx_analysis)

        # 6) 文本摘要
        summary = WalkForwardAnalyzer._build_summary(valid_windows, score, adx_analysis)

        return {
            "windows": window_results,
            "score": score,
            "adx_analysis": adx_analysis,
            "summary": summary,
            "overfitting_risk": score.get("overfitting_risk", "unknown"),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "config": {
                "coin": coin, "timeframe": timeframe,
                "start_year": start_year, "end_year": end_year,
                "train_years": train_years, "test_years": test_years,
            },
        }

    @staticmethod
    def _generate_windows(
        start_year: int, end_year: int, train_years: int, test_years: int
    ) -> List[tuple]:
        """
        生成滚动窗口时间范围。

        示例 (2020-2026, train=2, test=1):
          Window 1: train=(2020,2022), test=(2023,2023)
          Window 2: train=(2021,2023), test=(2024,2024)
          Window 3: train=(2022,2024), test=(2025,2025)
          Window 4: train=(2023,2025), test=(2026,2026)
        """
        windows = []
        total_span = end_year - start_year + 1
        window_span = train_years + test_years

        if total_span < window_span:
            return windows

        num_windows = total_span - window_span + 1

        for i in range(num_windows):
            train_start = start_year + i
            train_end = train_start + train_years - 1
            test_start = train_end + 1
            test_end = test_start + test_years - 1

            # 修正格式: (start_year, end_year) 元组
            windows.append((
                (train_start, train_end),
                (test_start, test_end),
            ))

        return windows

    @staticmethod
    def _slice_window(df: pd.DataFrame, train_range: tuple, test_range: tuple,
                      warmup_bars: int = 600) -> tuple:
        """
        切片训练/测试窗口 (P3-7 / P3-8)。

        - P3-7: 训练切片前置 warmup_bars 根K线作为指标预热, 返回的 df_train
          含缓冲段; core_train_bars 为去掉缓冲后的真实训练期 bar 数。
        - P3-8: 最后一个测试窗的年度若数据在年末之前截断, partial_year=True。
        Returns: (df_train, df_test, core_train_bars, partial_year)
        """
        train_start, train_end = train_range
        test_start, test_end = test_range

        test_mask = (df.index.year >= test_start) & (df.index.year <= test_end)
        df_test = df[test_mask].copy()

        core_train_mask = (df.index.year >= train_start) & (df.index.year <= train_end)
        core_train_bars = int(core_train_mask.sum())
        train_mask = core_train_mask
        if core_train_bars > 0:
            train_first = df.index[core_train_mask][0]
            pos = df.index.get_loc(train_first)
            buffer_start = df.index[max(0, pos - warmup_bars)]
            train_mask = (df.index >= buffer_start) & (df.index.year <= train_end)
        df_train = df[train_mask].copy()

        partial_year = False
        if len(df_test) > 0:
            data_last = df.index[-1]
            if data_last.year == test_end and data_last.month < 12:
                partial_year = True

        return df_train, df_test, core_train_bars, partial_year

    @staticmethod
    def _run_window(
        df: pd.DataFrame,
        coin: str,
        timeframe: str,
        train_range: tuple,
        test_range: tuple,
        strategy_config: dict,
        engine_kwargs: dict,
        mf_params: dict,
        use_and: bool,
        strategy_class,
        window_index: int,
        param_grid: Optional[Dict] = None,
    ) -> Dict:
        """
        对单个窗口执行: 训练窗参数搜索(可选) → 训练集回测 → 测试集回测。

        P2-7: 若传入 param_grid, 在训练窗做 IS-only 参数搜索, 用最优参数
        在测试窗跑一次样本外验证 (严禁测试窗参与选参)。
        """
        train_start, train_end = train_range
        test_start, test_end = test_range

        # 切片数据 (P3-7 warmup 缓冲 + P3-8 partial_year 检测)
        df_train, df_test, core_train_bars, partial_year = \
            WalkForwardAnalyzer._slice_window(df, train_range, test_range)

        result = {
            "window": window_index,
            "train_range": f"{train_start}-{train_end}",
            "test_range": f"{test_start}-{test_end}",
            "train_bars": core_train_bars,
            "test_bars": len(df_test),
            "partial_year": partial_year,
        }

        if core_train_bars < 200:
            result["error"] = _t("wf_train_insufficient", n=core_train_bars)
            result["train"] = None
            result["test"] = None
            return result

        if len(df_test) < 50:
            result["error"] = _t("wf_test_insufficient", n=len(df_test))
            result["train"] = None
            result["test"] = None
            return result

        # 创建策略 (需要 DynamicStrategy 类)
        if strategy_class is None:
            # 尝试动态导入
            try:
                from strategy_models import DynamicStrategy
                strategy_class = DynamicStrategy
            except ImportError:
                result["error"] = _t("wf_import_failed")
                result["train"] = None
                result["test"] = None
                return result

        # 训练集回测 (P2-7: 若传 param_grid, 先做训练窗 IS-only 参数搜索)
        best_kwargs = engine_kwargs
        if param_grid:
            best_kwargs, grid_results = WalkForwardAnalyzer._search_train_params(
                df_train=df_train,
                coin=coin,
                strategy_config=strategy_config,
                engine_kwargs=engine_kwargs,
                mf_params=mf_params,
                use_and=use_and,
                strategy_class=strategy_class,
                param_grid=param_grid,
            )
            result["param_grid"] = param_grid
            result["grid_size"] = len(grid_results)
            result["best_params"] = {k: best_kwargs.get(k) for k in param_grid}
            result["train_grid"] = grid_results

        try:
            train_strategy = strategy_class(
                selected=strategy_config.copy(),
                use_and=use_and,
                mf_params=mf_params or {},
            )
            train_engine = BacktestEngineV2(**best_kwargs)
            train_result = train_engine.run({coin: df_train}, train_strategy)
            train_metrics = PerformanceAnalyzer.analyze(train_result)
            result["train"] = WalkForwardAnalyzer._extract_metrics(train_metrics)
            # 保存训练集交易记录 (用于ADX分析)
            result["_train_trades"] = train_result.get("closed_trades", train_result.get("trades", []))
        except Exception as e:
            result["train"] = None
            result["train_error"] = str(e)
            result["test"] = None
            return result

        # 测试集回测 (用训练窗选出的最优参数, 只跑一次)
        try:
            test_strategy = strategy_class(
                selected=strategy_config.copy(),
                use_and=use_and,
                mf_params=mf_params or {},
            )
            test_engine = BacktestEngineV2(**best_kwargs)
            test_result = test_engine.run({coin: df_test}, test_strategy)
            test_metrics = PerformanceAnalyzer.analyze(test_result)
            result["test"] = WalkForwardAnalyzer._extract_metrics(test_metrics)
            # 保存测试集交易记录
            result["_test_trades"] = test_result.get("closed_trades", test_result.get("trades", []))
        except Exception as e:
            result["test"] = None
            result["test_error"] = str(e)

        return result

    @staticmethod
    def _search_train_params(
        df_train: pd.DataFrame,
        coin: str,
        strategy_config: dict,
        engine_kwargs: dict,
        mf_params: dict,
        use_and: bool,
        strategy_class,
        param_grid: Dict,
        min_trades: int = 3,
    ) -> tuple:
        """
        训练窗参数搜索 (P2-7): 在训练窗枚举 param_grid, 仅用 IS 指标选最优。

        - 选参标准: IS Sharpe 最高 (NaN/None 视为 -inf), 且满足最小交易数
          (min_trades), 防止无交易/偶然高 Sharpe 的过拟合。
        - 严禁用测试窗任何指标参与选参 (测试窗只在 _run_window 里跑一次)。
        - 返回 (best_kwargs, grid_results); 若全部组合失败, best_kwargs 回退原值。
        """
        if not param_grid:
            return engine_kwargs, []

        keys = list(param_grid.keys())
        combos = list(itertools.product(*[param_grid[k] for k in keys]))
        best_kwargs = engine_kwargs
        best_score = float("-inf")
        grid_results = []

        for combo in combos:
            trial_kwargs = dict(engine_kwargs)
            trial_kwargs.update(dict(zip(keys, combo)))
            try:
                st = strategy_class(
                    selected=strategy_config.copy(),
                    use_and=use_and,
                    mf_params=mf_params or {},
                )
                eng = BacktestEngineV2(**trial_kwargs)
                res = eng.run({coin: df_train}, st)
                m = PerformanceAnalyzer.analyze(res)
            except Exception:
                continue

            n_trades = m.get("total_trades", 0) or 0
            sharpe = m.get("sharpe_ratio")
            sharpe = float(sharpe) if sharpe is not None and sharpe == sharpe else float("-inf")
            total_ret = m.get("total_return") or 0.0

            grid_results.append({
                "params": dict(zip(keys, combo)),
                "sharpe": (None if sharpe == float("-inf") else round(sharpe, 3)),
                "total_return": round(float(total_ret), 1),
                "trades": int(n_trades),
            })

            if n_trades < min_trades:
                continue
            # IS-only: 优先 Sharpe, 其次已由 best_score 单调比较
            if sharpe > best_score:
                best_score = sharpe
                best_kwargs = trial_kwargs

        return best_kwargs, grid_results

    @staticmethod
    def _extract_metrics(metrics: Dict) -> Dict:
        """从 PerformanceAnalyzer 结果提取关键指标 (None 安全, P2-6)。"""

        def _r(v, nd=1):
            try:
                return round(float(v), nd)
            except (TypeError, ValueError):
                return 0.0

        # P2-6: years<1.0 时 annual_return=None; 短窗(≈1年)退化为 total_return,
        # 避免 None→0.0 掩盖真实 OOS 表现。
        annual = metrics.get("annual_return")
        if annual is None:
            annual = metrics.get("total_return", 0)

        return {
            "total_return": _r(metrics.get("total_return", 0)),
            "annual_return": _r(annual),
            "max_drawdown": _r(metrics.get("max_drawdown", 0)),
            "sharpe": _r(metrics.get("sharpe_ratio", 0), 3),
            "sortino": _r(metrics.get("sortino_ratio", 0), 3),
            "calmar": _r(metrics.get("calmar_ratio", 0), 3),
            "win_rate": _r(metrics.get("win_rate", 0)),
            "trades": metrics.get("total_trades", 0) or 0,
            "profit_factor": _r(metrics.get("profit_factor", 0), 2),
        }

    @staticmethod
    def _analyze_adx(
        df: pd.DataFrame, windows: List[Dict], mf: MultiFactorRegime
    ) -> Dict:
        """
        ADX 市场环境分析: 策略在不同趋势强度下的表现差异。

        利用 MultiFactorRegime.compute_adx() 计算每根K线的ADX,
        然后统计盈利交易和亏损交易发生时的平均ADX。
        """
        # 计算全周期 ADX (用 shift(1) 防未来函数)
        df_adx = df.copy()
        high_s = df_adx['high'].shift(1)
        low_s = df_adx['low'].shift(1)
        close_s = df_adx['close'].shift(1)

        adx_series, _, _ = mf.compute_adx(high_s, low_s, close_s, period=14)

        # 收集所有窗口的测试集交易
        all_win_trades = []
        all_loss_trades = []

        for w in windows:
            test_trades = w.get("_test_trades", [])
            for t in test_trades:
                pnl = t.get("pnl", 0)
                open_time = t.get("open_time", "")
                if not open_time:
                    continue
                try:
                    # 解析入场时间, 查找对应ADX
                    ot = pd.to_datetime(re.sub(r'\.\d+$', '', str(open_time)))
                    # 找到最近的一根K线
                    if ot in adx_series.index:
                        adx_val = adx_series.loc[ot]
                    else:
                        # 找最近的 (异步对齐)
                        idx = adx_series.index.get_indexer([ot], method='ffill')[0]
                        if idx >= 0:
                            adx_val = adx_series.iloc[idx]
                        else:
                            continue

                    if pd.isna(adx_val):
                        continue

                    if pnl > 0:
                        all_win_trades.append(adx_val)
                    else:
                        all_loss_trades.append(adx_val)
                except Exception:
                    continue

        avg_adx_win = float(np.mean(all_win_trades)) if all_win_trades else 0
        avg_adx_loss = float(np.mean(all_loss_trades)) if all_loss_trades else 0
        avg_adx_all = float(np.mean(all_win_trades + all_loss_trades)) if (all_win_trades + all_loss_trades) else 0

        # 判断趋势依赖度
        # 如果盈利ADX > 亏损ADX * 1.3 → 策略高度依赖趋势
        adx_ratio = avg_adx_win / (avg_adx_loss + 1e-9)

        if avg_adx_win > avg_adx_loss * 1.3 and avg_adx_win > 20:
            trend_dependency = "high"
            dependency_detail = _t('wf_dep_high')
        elif avg_adx_win > avg_adx_loss * 1.1:
            trend_dependency = "medium"
            dependency_detail = _t('wf_dep_medium')
        else:
            trend_dependency = "low"
            dependency_detail = _t('wf_dep_low')

        return {
            "avg_adx_winning": round(avg_adx_win, 1),
            "avg_adx_losing": round(avg_adx_loss, 1),
            "avg_adx_all": round(avg_adx_all, 1),
            "adx_ratio": round(adx_ratio, 2),
            "trend_dependency": trend_dependency,
            "dependency_detail": dependency_detail,
            "winning_trade_count": len(all_win_trades),
            "losing_trade_count": len(all_loss_trades),
        }

    @staticmethod
    def _compute_score(windows: List[Dict], adx_analysis: Dict) -> Dict:
        """
        计算 Walk Forward 综合评分。

        评分维度 (满分 100):
          窗口稳定性:  40分 — OOS收益率标准差越小越好
          OOS平均收益: 30分 — 样本外平均年化收益
          OOS盈利窗口: 20分 — 盈利窗口比例
          趋势独立性:  10分 — ADX依赖度越低越好
        """
        # 提取 OOS 指标
        oos_returns = []
        oos_sharpes = []
        oos_drawdowns = []
        train_returns = []
        profitable_windows = 0
        total_windows = 0

        for w in windows:
            if w.get("test") is None:
                continue
            # P3-8: 不完整年度测试窗年化收益失真, 不计入 OOS 聚合
            if w.get("partial_year"):
                continue
            test = w["test"]
            train = w.get("train", {})

            oos_returns.append(test.get("annual_return", 0))
            oos_sharpes.append(test.get("sharpe", 0))
            oos_drawdowns.append(test.get("max_drawdown", 100))
            if train:
                train_returns.append(train.get("annual_return", 0))
            if test.get("annual_return", 0) > 0:
                profitable_windows += 1
            total_windows += 1

        if total_windows == 0:
            return {"walk_forward_score": 0, "overfitting_risk": "insufficient"}

        avg_oos_return = np.mean(oos_returns) if oos_returns else 0
        avg_oos_sharpe = np.mean(oos_sharpes) if oos_sharpes else 0
        avg_oos_drawdown = np.mean(oos_drawdowns) if oos_drawdowns else 100
        oos_std = np.std(oos_returns) if len(oos_returns) > 1 else abs(avg_oos_return) * 0.5

        # 1) 窗口稳定性 (40分)
        # OOS收益率变异系数: cv = std / |mean|, 越小越稳定
        cv = oos_std / (abs(avg_oos_return) + 1e-9)
        if cv < 0.5:
            stability_score = 40
        elif cv < 1.0:
            stability_score = 30
        elif cv < 2.0:
            stability_score = 20
        else:
            stability_score = 10

        # 2) OOS 平均收益 (30分)
        if avg_oos_return > 20:
            return_score = 30
        elif avg_oos_return > 10:
            return_score = 22
        elif avg_oos_return > 0:
            return_score = 15
        elif avg_oos_return > -10:
            return_score = 8
        else:
            return_score = 3

        # 3) OOS 盈利窗口比例 (20分)
        profit_ratio = profitable_windows / total_windows
        if profit_ratio >= 0.75:
            profit_score = 20
        elif profit_ratio >= 0.5:
            profit_score = 14
        elif profit_ratio >= 0.25:
            profit_score = 8
        else:
            profit_score = 3

        # 4) 趋势独立性 (10分)
        dependency = adx_analysis.get("trend_dependency", "medium")
        if dependency == "low":
            independence_score = 10
        elif dependency == "medium":
            independence_score = 6
        else:
            independence_score = 2

        total_score = stability_score + return_score + profit_score + independence_score

        # 过拟合风险判定
        if avg_oos_return < 0 and avg_oos_return < (np.mean(train_returns) * 0.5 if train_returns else 0):
            of_risk = "high"
        elif profit_ratio >= 0.75 and avg_oos_return > 0:
            of_risk = "low"
        elif profit_ratio >= 0.5:
            of_risk = "medium"
        else:
            of_risk = "high"

        # OOS/训练收益衰减
        avg_train_return = np.mean(train_returns) if train_returns else 0
        if avg_train_return > 0 and avg_oos_return != 0:
            oos_decay = round((1 - avg_oos_return / avg_train_return) * 100, 1)
        else:
            oos_decay = 100.0 if avg_oos_return < 0 else 0.0

        return {
            "walk_forward_score": total_score,
            "max_score": 100,
            "stability_score": stability_score,
            "return_score": return_score,
            "profit_score": profit_score,
            "independence_score": independence_score,
            "avg_oos_return": round(avg_oos_return, 1),
            "avg_oos_sharpe": round(avg_oos_sharpe, 3),
            "avg_oos_drawdown": round(avg_oos_drawdown, 1),
            "oos_std": round(oos_std, 1),
            "profitable_windows": profitable_windows,
            "total_windows": total_windows,
            "profit_ratio": round(profit_ratio * 100, 1),
            "overfitting_risk": of_risk,
            "oos_decay": oos_decay,
            "avg_train_return": round(avg_train_return, 1),
        }

    @staticmethod
    def _build_summary(windows: List[Dict], score: Dict, adx: Dict) -> str:
        """
        生成 Walk Forward 文本摘要 (用于 AI 分析)。
        """
        n = score.get("total_windows", 0)
        profitable = score.get("profitable_windows", 0)
        avg_ret = score.get("avg_oos_return", 0)
        of_risk = score.get("overfitting_risk", "unknown")
        trend_dep = adx.get("trend_dependency", "unknown")
        adx_win = adx.get("avg_adx_winning", 0)
        adx_loss = adx.get("avg_adx_losing", 0)

        parts = []

        # 窗口统计
        parts.append(_t('wf_summary_windows', n=n, profitable=profitable,
                        ratio=score.get('profit_ratio', 0), avg_ret=avg_ret))

        # 过拟合风险
        if of_risk == "low":
            parts.append(_t('wf_summary_of_low'))
        elif of_risk == "medium":
            parts.append(_t('wf_summary_of_medium'))
        else:
            parts.append(_t('wf_summary_of_high', decay=score.get('oos_decay', 0),
                            profitable=profitable, n=n))

        # ADX 趋势依赖
        parts.append(_t('wf_summary_trend', trend_dep=trend_dep_label(trend_dep),
                        adx_win=adx_win, adx_loss=adx_loss))
        parts.append(adx.get("dependency_detail", ""))

        return " ".join(parts)


# ============================================================
# 快捷入口
# ============================================================
def run_walk_forward(
    coin: str = "ETH",
    timeframe: str = "4h",
    start_year: int = 2020,
    end_year: int = 2026,
    strategy_config: dict = None,
    engine_kwargs: dict = None,
    **kwargs,
) -> Dict:
    """
    一键运行 Walk Forward 分析。

    示例:
        result = run_walk_forward(
            coin='ETH', timeframe='4h',
            start_year=2020, end_year=2026,
            strategy_config=selected_indicators,
            engine_kwargs=strat_kwargs,
        )
    """
    return WalkForwardAnalyzer.analyze(
        coin=coin,
        timeframe=timeframe,
        start_year=start_year,
        end_year=end_year,
        strategy_config=strategy_config or {},
        engine_kwargs=engine_kwargs or {},
        **kwargs,
    )


if __name__ == "__main__":
    print("=" * 60)
    print(" Walk Forward 滚动样本外分析引擎 v1.0")
    print("=" * 60)
    print("使用方法:")
    print("  from walk_forward import WalkForwardAnalyzer")
    print("  result = WalkForwardAnalyzer.analyze(...)")
