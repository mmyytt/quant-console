# QuantCode Phase B Final Verification Report（最终验收报告）

日期：2026-08-16
范围：六部分自查（数据层 / 交易引擎 / 仓位模型 / AI研究仓 / Walk Forward·OOS / 运行验证）。
原则：不重构引擎、不改已验证交易数学、不改开仓/加仓/收益公式、不为过测改测试。

---

## 一、当前平台状态

**评级：A — 可以进入策略研究阶段。**

当前版本满足「可信回测引擎 + 可进行策略研究」两项门槛：

- 回测引擎（开仓/加仓/收益/手续费/多空）逐行核对，数学与设计一致，无未来函数、无数据泄露。
- 数据链路 5m/15m/1h/4h/1d 各自真实对应，主路径无静默降级，升序/去重/缺口修复齐备。
- AI 研究仓搜索空间已包含 指标参数 / 杠杆 / TP / SL / 初始仓位 / 加仓开关 / 加仓比例 / 最大加仓次数 / 牛熊仓位系数，fingerprint 不折叠不同仓位模型。

**当前版本可以进入策略研究阶段。**

---

## 二、已修复问题（本次验收中发现并修复）

| 编号 | 级别 | 问题 | 修复 |
|------|------|------|------|
| V-1 | High | P0-1 搜索空间未真正暴露仓位参数：`platform_context` 用旧键名（max_pyramid/pyramid_step/bull_alloc），`search_prompt`/`hypothesis_prompt` JSON 无仓位字段，`run_parameter_search` 构建 direction 时丢弃仓位参数 | `platform_context.py` 改为 7 个精确键 + 取值范围；双 prompt 增加 `position` 子对象 + 约束；`_position_params_from` 支持嵌套 `position`；`run_parameter_search` 粗/精搜均透传仓位参数；`build_report` 修正旧「固定单仓」文案为实际配置 |

> 说明：P0-1 的底层管道（`make_engine_kwargs`→`run_single`→`strategy.selected`、`full_fingerprint`、`_position_params_from`）在上一轮已就绪，本次验收发现「AI 实际被告知可搜哪些仓位参数」这一环缺失，已补齐。此修复不触碰任何交易数学。

---

## 三、测试结果（真实运行）

| 测试 | 结果 |
|------|------|
| `python -m py_compile`（engine_core / research_phase1 / research_loop / data_loader / walk_forward / app / i18n / platform_context） | ✅ 通过 |
| 固定加仓 10000/3x/50%初始/50%加仓/max2 → 5000+2500+2500=10000 | ✅ 通过 |
| ETH 5m=941297 / 15m=313692 / 4h=19625（K线数不同，数据语义正确） | ✅ 通过 |
| position_id 共享 + position_trades 聚合 | ✅ 通过 |
| full_fingerprint 纳入仓位参数（不同仓位模型指纹不同） | ✅ 通过 |
| 仓位参数已暴露给 AI（context + 双 prompt + 嵌套提取） | ✅ 通过 |
| `test_pyramiding_state_upgrade.py`（加仓状态回归 5 项） | ✅ 通过 |
| `test_engine_fix.py`（引擎回归） | ✅ 41 passed, 0 failed |
| `test_p0_blockers.py`（加仓上限 3 项） | ✅ 通过 |
| `test_research_search.py`（研究搜索全链路） | ✅ 通过 |
| `test_no_ui_import.py`（无 UI 导入） | ✅ 通过 |

---

## 四、逐部分核对结论

### 1. 数据层（data_loader.py）
- **周期真实对应**：5m 独立 parquet（ETH/BTC/SOL 已存在，941k/941k/628k 根）；15m 为基座；1h/4h/1d 由 15m 标准 OHLCV 重采样（open=first/high=max/low=min/close=last/vol=sum）。✔
- **无静默降级（主路径）**：app.py 三处 + walk_forward 一处已改为「无 5m 时显式报错 `err_5m_missing`」。✔
- **升序/去重/缺口**：`sort_index` + `~duplicated` 贯穿；`repair_gaps` 按 interval_map（5m/15m/1h/4h/1d）检测并修复断层。✔

### 2. 交易引擎（engine_core.py）
- **开仓**：Fixed Capital `margin = equity × regime_mult × init_alloc`（L1360）；Fixed Risk `risk_budget = equity × risk_pct × regime_mult`。✔
- **加仓**：`add_margin = init_margin × _pyr_add_pct`（L1108，固定不复利）。✔
- **加仓进账户**：新 leg 进入 `positions`；开仓扣费、平仓结算 `pnl_usd = margin × 保证金收益率`，均汇入 `self.equity` 与最终收益。✔
- **多空对称**：触发/SL/成交滑点/盈亏公式 LONG 与 SHORT 完全镜像。✔
- **无未来函数**：指标统一 `shift(1)`；信号 bar i 生成 → bar i 开盘撮合；`FutureLeakDetector` 全序列扫描（只读诊断，结果进 `leak_warnings`）。✔

### 3. 仓位模型
- 唯一生效模型：`_init_alloc_pct`（初始建仓）、`_pyr_add_pct`/`_pyr_max`/`_enable_pyramiding`（顺势加仓）、`_bull_alloc`/`_range_alloc`/`_bear_alloc`（市场状态系数）。
- 7 个参数全部在引擎中真实被读取使用，无死参数、无「UI 显示但代码不生效」。构造参数 `max_pyramid` 已接线为 `_pyr_max` 回退默认值，非死参数。✔

### 4. AI 研究仓
- 搜索空间现包含：指标参数、杠杆、TP、SL、初始仓位、加仓开关、加仓比例、最大加仓次数、牛熊仓位系数。✔
- `full_fingerprint` 纳入全部 7 个仓位参数，仅仓位不同的策略不会被判为重复。✔
- 不可执行参数：指标参数被约束在 min~max、TP/SL 保证 tp>sl、杠杆在 1~20，引擎侧另有零值/超上限守卫。✔

### 5. Walk Forward / OOS
- `_search_train_params` 仅在训练窗（IS）枚举 `param_grid` 选最优，测试窗（OOS）用最优参数只跑一次——测试集不参与选参，无参数泄露。✔
- UI 已接入 `param_grid={"leverage": [1, 2, 3]}`（此前漏接）。✔
- 单假设验证 OOS 按年份严格切分（IS 截止年之前训练、之后样本外），无未来数据。✔

---

## 五、仍存在风险（不影响评级）

| 级别 | 位置 | 描述 | 建议 |
|------|------|------|------|
| Low | `engine_core.py:2754` `run_backtest()` | 模块级便捷函数 `all_tf.get(timeframe, all_tf['4h'])` 仍有潜在 5m→4h 静默回退。不在主 UI 路径；对 ETH/BTC/SOL（均有 5m）不触发 | 若后续脚本化调用 5m 回测，加同样的显式报错 |
| Low | `expand_parameter_grid` | 仓位参数现由 LLM 在每个方向中显式提出并被透传测试，但尚未加入自动网格扫描（杠杆/TP·SL/指标主参数仍是网格扫描轴） | 如需「仓位参数自动网格寻优」，下一阶段再扩展，勿在本阶段重构 |

---

## 六、结论

不存在 Critical / High 未解决项。数据链路、交易数学、多空对称、未来函数防护、仓位模型、Walk Forward/OOS 均通过核对与真实测试。

**当前版本可以进入策略研究阶段。** 建议进入下一阶段人工测试（真实 API 数据读取 / 策略研究端到端走查）。
