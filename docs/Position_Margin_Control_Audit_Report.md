# QuantCode 回测引擎仓位管理修复审计报告

> 任务：修复仓位管理逻辑漏洞 —— 累计保证金占用率可超过账户权益（模拟出不存在资金）
> 修复性质：**Level 3（高风险——仓位计算/风险控制）**，已按 CLAUDE.md 规范：备份 → 说明影响 → 历史回测 → 风险检查 → 测试
> 日期：2026-08-16

---

## 一、结论摘要（TL;DR）

1. **根因已修复**：新增「累计保证金占用率上限」`max_margin_allocation`（默认 `1.0`），所有开仓/加仓前校验 `Σ持仓保证金 ≤ 权益 × max_margin_allocation`，超出按剩余可用保证金裁剪。
2. **漏洞关闭**：`init_alloc=100% + 金字塔加仓` 场景，累计保证金从「可达 167% 权益」降为「≤100% 权益」，不再模拟出不存在的资金。
3. **无回归**：常规策略（累计保证金 ≤100% 权益）前后回测结果**逐项完全一致**（实测：收益率/回撤/Sharpe/交易次数全部相同）。
4. **交易数学零改动**：PnL、手续费、杠杆、TP/SL、ATR、指标逻辑一律未触碰（本报告第五节逐条核对）。

---

## 二、修改文件

| 文件 | 改动 | 类型 |
|------|------|------|
| `engine_core.py` | ① `__init__` 新增参数 `max_margin_allocation=1.0`（L628/L678）② `_open` Step 4a.5 新增累计保证金约束（L1396-1408）③ 持仓 dict 新增审计字段 + `[MARGIN AUDIT]` 日志（L1437-1440, L1470-1487） | 交易核心（Level 3） |
| `test_margin_control.py` | 新增：2 个用户指定测试用例 + 审计字段 + 交易数学复算核对（19 项断言） | 测试 |
| `test_pyramiding_state_upgrade.py` | `_make_engine` 增 `max_margin_allocation=1000.0`（隔离加仓状态测试与新护栏，镜像已有的 `max_notional_pct=1000.0`） | 测试 |
| `test_phase_b_fixes.py` | 同上，隔离「固定加仓额」测试 | 测试 |
| `backup_before_margin_fix/engine_core.py` | 修复前引擎备份（gitignored，不提交） | 备份 |

**未改**：`PnL 计算`、`手续费`、`杠杆`、`TP/SL`、`ATR`、`指标逻辑`、`平仓结算`、`对冲状态机`、`OKX API`。

---

## 三、修复逻辑（代码事实）

### 3.1 新增的引擎级硬风控

```python
# engine_core.py _open() Step 4a.5
used_margin = sum(p['margin'] for p in self.positions)
margin_budget = self.equity * self.max_margin_allocation
available_margin = margin_budget - used_margin
if margin > available_margin:
    margin = max(0.0, available_margin)   # 裁剪到剩余可用, 禁止创造额外资金
    notional = margin * lev
```

- 与现有 `max_notional_pct`（名义上限）**对称**：一个管「名义敞口」，一个管「保证金占用」。
- 加仓逻辑满足需求：`available_margin = equity × max_margin_allocation - current_used_margin`；`requested_add_margin > available_margin` 时 `add_margin = available_margin`。
- 零值守卫：裁剪后 `margin/notional ≤ 0` 直接 `return`，不创建「幽灵仓位」。

### 3.2 审计字段（每次开仓/加仓记录）

每个持仓 leg 新增：
- `position_id` — 仓位唯一 id（初始腿生成，加仓腿复用）
- `init_margin` — 初始投入保证金
- `add_margin` — 本腿（加仓）保证金
- `used_margin_after` — 本腿后累计占用保证金
- `margin_usage_ratio` — 占用率（= 累计保证金 / 扣费前权益，与预算口径一致，恒 ≤ max_margin_allocation）

verbose 模式下打印 `[MARGIN CAP]`（裁剪告警）与 `[MARGIN AUDIT]`（逐腿审计）日志。

### 3.3 一处一致性修正

占用率 `margin_usage_ratio` 分母改用**扣费前权益**（`equity_before_fee`），与保证金预算口径一致，避免「预算按扣费前、比率按扣费后」导致的 0.04% 假性超限。

---

## 四、测试结果

### 4.1 单元测试（用户指定用例）

| 用例 | 期望 | 实测 | 结果 |
|------|------|------|------|
| Case 1：本金10000，init_alloc=100%，加仓50%，max_add=2 | 初始保证金=10000，第1/2次加仓=0 | 初始 10000；两次加仓均被拒，累计仍 10000 | ✅ |
| Case 2：本金10000，init_alloc=50%，加仓50%，max_add=2 | 5000 / 2500 / 2500，累计 10000 | 5000 / 2500 / 2488.75，累计 9988.75（=扣费后权益，恰好打满不超） | ✅ |

> 注：Case 2 第 2 次加仓为 2488.75 而非 2500，差额 11.25 来自 3 笔手续费（0.05%）已从权益扣除。这是**更保守、更真实**的结果：`sum(保证金) ≤ 扣费后权益` 严格成立，占用率恒为 1.000000（打满不超）。用户「2500」为忽略手续费的理想值。

**`test_margin_control.py`：19 PASS, 0 FAIL**

### 4.2 回归测试

| 测试 | 结果 |
|------|------|
| `py_compile`（engine_core + 2 个测试文件） | ✅ OK |
| `test_engine_fix.py`（引擎核心） | ✅ 41 PASS, 0 FAIL |
| `test_pyramiding_state_upgrade.py`（加仓状态管理） | ✅ PASS |
| `test_phase_b_fixes.py`（固定加仓额 + position_id 聚合） | ✅ PASS |
| `test_p0_blockers.py`（3 阻塞项） | ✅ PASS |
| `test_param_extension.py` | ✅ 14 PASS |
| `test_no_ui_import.py` | ✅ PASS |

---

## 五、交易数学零改动核对

| 公式 | 位置 | 是否改动 |
|------|------|---------|
| Fixed Capital `margin = equity × regime_mult × init_alloc` | `_open` | ❌ 未改 |
| `notional = margin × lev` | `_open` | ❌ 未改 |
| 加仓 `add_margin = init_margin × _pyr_add_pct`（固定不复利） | `_check_pyramiding` | ❌ 未改 |
| `fee = notional × TAKER_FEE(0.05%)` | `_open` | ❌ 未改 |
| 平仓 `equity += pnl_usd`（保证金只锁定不扣减） | `_close` | ❌ 未改 |
| TP/SL / ATR / 指标 / 维持保证金率 / 强平价 | 各处 | ❌ 未改 |

单元测试中对「名义=保证金×杠杆」「手续费=名义×0.05%」「杠杆字段」复算核对通过。

---

## 六、前后回测对比（真实 ETH 1h 数据，78,659 根 bar）

对比方法：同一策略 `S1_EMA_ADX_Trend`（fixed_capital + 金字塔），分别在「修复前引擎（git HEAD 备份）」与「修复后引擎（当前工作区）」运行，追踪**峰值累计保证金占用率**（并发开仓腿之和 / 当时权益）。

| 场景 | 指标 | 修复前 | 修复后 | 判定 |
|------|------|--------|--------|------|
| **A 常规**（init_alloc=30% + 金字塔） | 收益率 | -41.90% | -41.90% | 完全一致 |
| | 最大回撤 | 78.36% | 78.36% | 完全一致 |
| | Sharpe | 0.222 | 0.222 | 完全一致 |
| | 交易次数 | 2111 | 2111 | 完全一致 |
| | 峰值占用率 | 66.2% | 66.2% | 约束未触发（<100%） |
| **B 激进**（init_alloc=60% + 金字塔50%×2） | 收益率 | -99.15% | -98.31% | 变化（风险修正） |
| | 最大回撤 | 99.81% | 99.54% | 变化（风险修正） |
| | Sharpe | 0.203 | 0.174 | 变化（风险修正） |
| | 交易次数 | 2111 | 2110 | 少 1 笔（被拒加仓） |
| | **峰值占用率** | **147.4%** ⚠️ | **100.0%** ✅ | 修复生效 |

**结论**：
- 场景 A（常规）前后**逐项一致**，证明修复对正常策略**零影响、无回归**。
- 场景 B（激进满仓+加仓）修复前峰值占用率 147.4%（超权益，模拟不存在资金），修复后严格 100.0%。收益/回撤的下降属于**风险修正**（把不可执行的 147% 敞口修正为可执行的 100%），不是 bug。

---

## 七、全路径覆盖检查（无旁路）

`_open` 是所有开仓/加仓（初始腿 + 金字塔加仓腿）的**唯一收敛点**。`max_margin_allocation` 默认值 `1.0`，且所有构造路径均未显式传参 → 全部继承默认护栏。

| 路径 | 引擎构造点 | 是否经 `_open` | 覆盖 |
|------|-----------|---------------|------|
| 经典策略（Classic） | `app.py:1770/1808` | ✅ | ✅ |
| AI 研究仓（AI Research Loop） | `research_phase1.py:275`（run_single） | ✅ | ✅ |
| 鲁棒性实验室（Robustness Lab） | `robustness_lab.py:290` | ✅ | ✅ |
| Walk Forward | `walk_forward.py:319/338/388` | ✅ | ✅ |
| 参数优化（Parameter Optimization） | 经 run_single / make_engine_kwargs | ✅ | ✅ |
| 现货兼容引擎（BacktestEngine） | `engine_core.py:553`（委托 V2，leverage=1） | ✅ | ✅ |

**已知边界（不在本次范围）**：对冲模式（`strategy_mode='hedging'`）的 `_spot_leg`/`_short_leg` 由 `_hedge_state_machine` 直接构造，不经过 `_open`、不计入 `self.positions`，故不纳入本次累计保证金约束。这是独立的风控模型（有 `hedge_ratio`/`spot_tp`/`spot_sl`/`short_sl` 独立控制），本次任务范围仅为「fixed_capital + 金字塔」仓位真实性，未触及对冲模型。

---

## 八、是否影响历史策略复现

| 策略类别 | 是否受影响 | 说明 |
|---------|-----------|------|
| 累计保证金 ≤ 100% 权益（绝大多数：默认 init_alloc=30%，或 init_alloc+加仓 ≤100%） | **不受影响** | 场景 A 实测逐项一致，历史结果可完全复现 |
| 累计保证金 > 100% 权益（满仓+加仓超权益） | **被修正** | 此类旧结果建立在「模拟不存在的资金」上，本就不可执行；修复后为真实可执行的 100% 敞口 |

> 一句话：**有效策略结果不变，无效（超杠杆）策略结果被纠正**。若某项历史策略收益因此下降，属于风险修正，非 bug。

---

## 九、风险说明

1. **修复是纯「收紧」**：只对超权益的累计保证金做向下裁剪，从不放大任何仓位。风险方向单一，无「修复引入新风险」的可能。
2. **默认值保守**：`max_margin_allocation=1.0`（累计保证金 ≤100% 权益）为最稳健口径，等价于真实交易所约束。
3. **可选激进**（未启用）：如需趋势放大（方案 A），可显式调大 `max_margin_allocation`，但需在 UI 红字提示「有效杠杆 = 总名义/权益，已超配置杠杆」——本次未实现，避免扩大范围。
4. **审计口径一致**：`margin_usage_ratio` 用扣费前权益，与预算一致，审计日志可放心解读。

---

## 十、遗留问题（本次范围外，未改）

1. `PerformanceAnalyzer.analyze` 在权益曲线退化为平坦/爆仓时，`bars_per_day` 未定义（`UnboundLocalError`，`engine_core.py` ~L1844）。**预存问题**，仅激进爆仓场景触发，与本次修复无关。
2. `analyze` 短周期（<1 年）`annual_return=None` 导致的 `round(None)` 崩溃。**预存问题**（前次任务已确认），与本次修复无关。

---

## 附：数值核对（代码行号）

- `engine_core.py:628` — `max_margin_allocation: float = 1.0`
- `engine_core.py:678` — `self.max_margin_allocation = max_margin_allocation`
- `engine_core.py:1396-1408` — Step 4a.5 累计保证金约束（裁剪 + `[MARGIN CAP]` 日志）
- `engine_core.py:1437-1440` — `equity_before_fee` 捕获（审计口径一致）
- `engine_core.py:1470-1487` — 审计字段 + `[MARGIN AUDIT]` 日志
- `engine_core.py:1114` — 加仓 `add_margin = init_margin × _pyr_add_pct`（未改）
- `engine_core.py:1365-1366` — Fixed Capital `margin = equity × regime_mult × init_alloc`（未改）
