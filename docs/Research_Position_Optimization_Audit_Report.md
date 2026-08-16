# Research Position Optimization 审计报告

> 主题：修复 QuantCode 平台 AI Research Loop 与 Robustness Lab 的策略研究闭环问题
> 目标：让 AI Research 成为完整策略研究系统，联合优化「指标参数 + 仓位管理 + 风控参数」，而非只优化指标
> 日期：2026-08-17
> 风险等级：Level 2（指标/参数优化 + 研究模块 + 报告系统）— 未触碰交易核心数学

---

## 一、背景与问题

交易引擎已完成仓位真实性修复（`max_margin_allocation` 已加入、加仓不会再导致累计保证金超过账户权益）。但 AI 研究舱虽然已经暴露 `position` 参数，却没有真正完成「指标参数 + 仓位管理 + 风控参数」的**联合研究**：

- 指标参数能进入回测，但仓位参数（初始建仓比例、加仓开关、加仓比例、加仓次数、移动止损）没有真正进入引擎；
- 报告会误把「满仓 + 高杠杆导致的收益虚高」归因为「指标优秀」，缺乏仓位/杠杆/风险贡献的分离；
- LLM 输出的多种 JSON 包装结构（顶层数组 / `candidates` / `list` / `strategies`）未被健壮解析。

---

## 二、修改范围与安全边界

### 已修改（本任务范围）

| 文件 | 修改内容 |
|------|---------|
| `research_loop.py` | LLM 解析健壮化、仓位参数进入回测核心、`_position_metrics` 仓位指标重建、`contribution_analysis` 收益归因、`validate_research_strategy` 校验、报告增强 |
| `research_phase1.py` | `RISK_CONFIG` 新增 `_pyr_trail`、`POSITION_PARAM_KEYS` 纳入 `_pyr_trail` |
| `platform_context.py` | 风控参数清单补充「移动止损 _pyr_trail」 |
| `robustness_lab.py` | 新增 Position 维度扫描（初始仓位/加仓/加仓比例/最大加仓/移动止损） |
| `test_research_search.py` | 候选/方向补 `position` 字段（适配新的仓位参数强制校验） |
| `test_research_parser_position.py` | 新增：Phase 1 解析测试 |
| `test_research_position_pipeline.py` | 新增：Phase 6 端到端全链路测试 |

### 未修改（铁律遵守）

- **`engine_core.py`**：未做任何改动。
- **PnL 计算 / TP·SL 数学 / 手续费 / 杠杆计算 / 指标计算**：全部未触碰。
- 仓位参数通过 `strategy.selected` 注入引擎（与 UI 主路径同一通道），引擎 `run()` 时按既有接线读取，**不改变已验证的开仓/加仓/收益/手续费公式**。

---

## 三、分阶段改动详情

### Phase 1 — LLM 输出解析健壮化

`parse_hypothesis_array` 现支持：
- 顶层 `list[dict]` 数组；
- `{"candidates": [...]}` / `{"list": [...]}` / `{"strategies": [...]}` 包装结构；
- markdown 代码围栏 + 解释文字混合输出；
- 字段顺序无关（dict 按键名读取）。

`_spec_to_hyp` 完整携带 `position` / `risk` / `move_stop` 字段，并从嵌套 `risk` 对象兜底读取 `leverage` / `tp_pct` / `sl_pct`。

### Phase 2 — 仓位参数进入 AI 研究核心

- 每个候选策略都携带三组参数：
  - **指标参数** `indicator_params`；
  - **风控参数** `risk_params`（`leverage` / `tp_pct` / `sl_pct`）；
  - **仓位参数** `position_params`（`_init_alloc_pct` / `_enable_pyramiding` / `_pyr_add_pct` / `_pyr_max` / `_pyr_trail`）。
- **关键修复**：研究路径 `_make_strategy` 强制 `_pos_mode = "fixed_capital"`。原 `fixed_risk` 模式下 `_init_alloc_pct` 是**死参数**（仓位由风险预算倒推），无法研究「初始仓位比例」这一仓位管理维度。切到 `fixed_capital` 后 `_init_alloc_pct` 真正生效。
- **语义别名**：`move_stop` → `_pyr_trail`（引擎真实 key「加仓后移动止损至均价」），同时支持顶层与嵌套 `position` 两种写法。
- **禁止「只优化指标不优化仓位」**：无仓位参数的候选在回测前被直接拒绝。

### Phase 3 — Robustness Lab 扩展 Position 维度

新增 5 个 `category: "position"` 扫描维度，均记入位置指纹：

| 维度 | 取值 |
|------|------|
| 初始建仓比例 | 30% / 50% / 70% / 100% |
| 加仓开关 | 关 / 开 |
| 加仓比例 | 25% / 50% / 75% |
| 最大加仓次数 | 1 / 2 / 3 次 |
| 移动止损 | 关 / 开 |

`full_fingerprint` 现纳入 `position_params`，两个仅仓位不同的实验不再被判为重复。

### Phase 4 — 研究报告增强

- **`_position_metrics`（纯函数，不改引擎）**：基于 trades + equity_curve 事件回放重建仓位指标 —— 最大/平均保证金占用率、最大有效杠杆、平均持仓比例、加仓次数。
- **`contribution_analysis`（4 轮消融归因）**：分解指标贡献 / 杠杆贡献 / 仓位贡献 / 风险贡献，满足恒等式 `实际收益 = 指标 + 杠杆 + 仓位 + 风险`。
- **报告新增区块**：
  - 「仓位与杠杆真实性」：展示最大保证金占用率、平均保证金占用率、最大有效杠杆、平均持仓比例、加仓次数统计；
  - 「收益归因（指标/仓位/杠杆/风险）」：当指标贡献 < 总收益 50% 时给出 ⚠️ 提示，避免「满仓+杠杆收益虚高但报告认为指标优秀」的误判。

### Phase 5 — Research Validation（自动校验）

`validate_research_strategy` 对每个 AI 策略执行：
1. 仓位参数存在（禁止只优化指标不优化仓位）；
2. 仓位参数 key 全部 ∈ `POSITION_PARAM_KEYS`（无未接线/死参数）；
3. 指标参数 key 全部 ∈ schema（无死参数，避免被 `build_selected` 静默丢弃）。

校验失败 → 直接拒绝进入回测（记录原因），从源头阻断无效/虚假策略。保证金占用率 ≤ 权益、有效杠杆不超限由引擎护栏保证，回测后经 `_position_metrics` 复验。

---

## 四、测试结果

| 测试 | 结果 |
|------|------|
| `py_compile`（7 个文件） | ✅ PY_COMPILE OK |
| `test_engine_fix.py` | ✅ 41 passed, 0 failed |
| `test_margin_control.py` | ✅ 19 PASS, 0 FAIL（含加仓累计保证金 ≤ 权益） |
| `test_research_search.py` | ✅ ALL SEARCH TESTS PASSED |
| `test_research_parser_position.py`（新增） | ✅ 8 项全过 |
| `test_research_position_pipeline.py`（新增） | ✅ 全链路通过 |

### 端到端全链路验证（`test_research_position_pipeline.py`）

输入 AI 输出 spec（EMA 双均线 + 斐波那契回调 + 量比 Volume Ratio，`_init_alloc_pct=50%`、`_enable_pyramiding=True`、`_pyr_add_pct=0.5`、`_pyr_max=2`、`move_stop=True`），验证：

```
AI 输出 → parse → _spec_to_hyp → _position_params_from → make_engine_kwargs → engine → backtest
```

- ✅ 仓位参数全部到达引擎：`init_alloc=50%` `pyramiding=True` `add=0.5` `max_add=2` `move_stop=True`
- ✅ `_pos_mode = fixed_capital`（`_init_alloc_pct` 真正生效）
- ✅ `move_stop` 别名正确映射到 `_pyr_trail`
- ✅ 仓位真实性：最大保证金占用率 **73.88% ≤ 100%**（引擎护栏 + 事件回放复验一致）
- ✅ 加仓真实发生：10 笔交易、9 次加仓
- ✅ 生产路径 `run_hypothesis_backtest` 产出 `position_metrics`

---

## 五、风险检查

| 检查项 | 结论 |
|--------|------|
| 未来函数 / 数据泄露 | 无新增（复用既有 `verify_hypothesis` 的 IS/OOS/Walk-Forward 切分） |
| 交易核心数学 | 未触碰 PnL / TP·SL / 手续费 / 杠杆 / 指标计算 |
| 保证金占用率 ≤ 权益 | 引擎护栏保证，`_position_metrics` 复验通过 |
| 死参数 | 校验拒绝未接线 key 与 schema 外指标参数 |
| 收益归因真实性 | 4 轮消融法，指标贡献 < 50% 时报告显式告警 |

---

## 六、结论

AI Research 已从「只优化指标」升级为「指标参数 + 仓位管理 + 风控参数」联合研究系统：
- 仓位参数（初始仓位 / 加仓 / 移动止损）真实进入回测引擎；
- 报告区分指标 / 仓位 / 杠杆 / 风险四类贡献，杜绝收益虚高误判；
- Robustness Lab 可对仓位维度做敏感性扫描；
- 每个 AI 策略自动通过「仓位参数完整 + 无死参数」校验，失败即拒绝回测。

交易核心（`engine_core.py`）零改动，仓位真实性由引擎既有护栏 + 事件回放双重保证。
