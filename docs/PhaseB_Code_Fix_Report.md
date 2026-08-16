# QuantCode Phase B 代码级修复报告（Upgrade Report）

日期：2026-08-16
范围：按最新全平台审计报告，对 P0-1 / P0-2 / P1-1 / P1-2 / P1-3 / P2 六项做代码级修复。
原则：不重设计交易逻辑，不改变已验证的开仓/加仓/收益/手续费公式，仅修复上述问题。

---

## 一、已修复问题

| 编号 | 问题 | 修复文件 | 修改内容 |
|------|------|----------|----------|
| P0-1 | AI 无法控制仓位/加仓/牛熊系数 | `research_phase1.py` | `RISK_CONFIG` 新增 4 个仓位参数；新增 `POSITION_PARAM_KEYS`；`make_engine_kwargs` 返回 `_position_params`；`run_single` 剥离后注入 `strategy.selected`（引擎 `run()` 从 selected 读取同一通道） |
| P0-2 | 不同仓位模型被判为重复实验 | `research_loop.py` | `full_fingerprint` 新增 `position_params` 参数，7 个仓位键纳入指纹；`run_hypothesis_backtest` / `verify_hypothesis` / `expand_parameter_grid` / `expand_refinement_grid` / `_run_experiment` / `run_parameter_search` 全程透传 |
| P1-1 | 5m 静默回退到 4h / 15m | `data_loader.py` `engine_core.py` `app.py` `walk_forward.py` `i18n.py` | 修复 `load_15min` 休眠 5m 分支（原 15m 实际加载的是 5m 数据，已改为只读 15m）；新增 `load_5min`；`get_multi_timeframe` 条件加入 `5m`；`ensure_data`/`repair_gaps`/下载接口均支持 `interval`；三处 UI 入口 + walk_forward 在无 5m 时显式报错 `err_5m_missing`，不再静默回退 |
| P1-2 | 交易报告缺持仓级聚合 | `engine_core.py` | `__init__` 新增 `_position_seq`；`_open` 生成/复用 `position_id`；`_check_pyramiding` 透传 `position_id`；`_close` 记录 `position_id`/`init_margin`/`pyramid_count`；新增 `_aggregate_positions`，`_build_result` 输出 `position_trades` |
| P1-3 | Walk Forward UI 未传 param_grid | `app.py` | WF 调用补 `param_grid={"leverage": [1, 2, 3]}`（训练窗 IS-only 选杠杆，测试窗只验证，无泄露） |
| P2 | Monte Carlo bars_per_year 硬编码 4h | `research_phase1.py` | 新增 `_TIMEFRAME_BARS_PER_YEAR` + `_infer_timeframe`（按中位 bar 间隔推断），`_monte_carlo` 按周期自动计算 |

---

## 二、修改前后逻辑

### P1-1（关键修正）
- **修改前**：`load_15min` 内存在休眠 5m 分支，实际把 5m parquet 当作「15m」数据加载 → 15m 回测实际跑在 5m 数据上（误标）。
- **修改后**：`load_15min` 只读 `{coin}_15m.parquet`；`5m` 独立通过 `load_5min` 加载并显式返回；无 5m 时 UI 报错而非回退。

### P1-2（纯新增，不改 PnL）
- 逐腿 trade 结构原样保留；新增 `position_trades` 为只读聚合视图（按 `position_id` 合并初始腿 + 加仓腿），字段：`position_id / initial_margin / add_margin / total_margin / average_entry / pyramid_count / realized_pnl / entry_time / exit_time`。不参与任何收益/回撤/交易次数计算。

---

## 三、测试结果

| 测试 | 结果 |
|------|------|
| `python -m py_compile`（7 个修改文件） | ✅ 通过 |
| 固定加仓金额 10000/3x/50%/50%/max2 → 5000+2500+2500=10000 | ✅ 通过 |
| position_id 共享 + position_trades 聚合 | ✅ 通过 |
| ETH 5m ≠ 4h（5m=941297 / 15m=313692 / 4h=19625 根） | ✅ 通过 |
| full_fingerprint 纳入仓位参数，不同仓位模型指纹不同 | ✅ 通过 |
| `test_pyramiding_state_upgrade.py`（加仓状态回归） | ✅ 全过 |
| `test_engine_fix.py`（引擎回归） | ✅ 41 passed, 0 failed |
| `test_p0_blockers.py`（加仓上限） | ✅ 3 项通过 |
| `test_research_search.py`（研究搜索） | ✅ 全过 |
| `test_no_ui_import.py`（无 UI 导入） | ✅ 全过 |

> 注：`test_research_search.py` 内 `_fake_backtest` mock 签名因 `run_hypothesis_backtest` 新增 `position_params` 形参，已同步补 `position_params=None`（测试适配，非业务逻辑改动）。

---

## 四、未修复问题

无。六项审计问题均已代码级修复。

---

## 五、风险说明

1. **P1-3 杠杆 IS 优化会改变 Walk Forward 结果**：训练窗现会枚举 leverage∈{1,2,3} 选 IS-Sharpe 最优者用于测试窗。这是 P1-3 的目标行为（train 优化 → test 验证），但对比修复前（固定用户杠杆）WF 分数会变化。若希望关闭，将 `param_grid` 传 `None` 即可恢复原行为。
2. **P1-1 数据语义修正可能暴露历史误标**：此前「15m」实为 5m 数据，修复后 15m 回测改用真实 15m 数据，收益/回撤/交易次数结果可能与旧记录不一致。此为纠正错误，非回归。
3. **未触碰交易核心公式**：开仓/加仓/收益/手续费/TP/SL/ATR 均未改动；P1-2 的 `position_trades` 为只读聚合，不影响 `trades`/`equity_curve`/绩效指标。
4. **5m 数据边界**：`load_5min` 依赖本地 `{coin}_5m.parquet`（当前 ETH/BTC/SOL 已存在）；其他币种首次 5m 回测会触发 `ensure_data(interval="5m")` 下载（上限近 2 年），无数据则报错而非回退。
