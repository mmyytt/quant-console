# P0修复前后逻辑变化报告

## 修复日期: 2026-08-10
## 修复范围: engine_core.py + app.py

---

## 一、代码修改清单

| # | 文件 | 修改位置 | 修改类型 |
|---|------|---------|---------|
| 1 | engine_core.py | `_try_rotate_entry()` L913-915 | 删除覆盖逻辑 |
| 2 | engine_core.py | `__init__()` L555-579 | 新增3个参数 |
| 3 | engine_core.py | `__init__()` L622-632 | 新增属性存储+杠杆上限保护 |
| 4 | engine_core.py | `run()` L670-682 | 新增策略参数加载+ATR预计算 |
| 5 | engine_core.py | `_try_rotate_entry()` L937-944 | 传递ATR值给_open() |
| 6 | engine_core.py | `_check_pyramiding()` L967-971 | 传递ATR值给_open() |
| 7 | engine_core.py | `_open()` L1104-1221 | 完整重写仓位/TP/SL/ATR逻辑 |
| 8 | engine_core.py | `_check_pyramiding()` L980-984 | TP重算尊重tp_mode |
| 9 | engine_core.py | `run_backtest()` L1941-1971 | 新增参数签名 |
| 10 | engine_core.py | `run_backtest()` L1991-2003 | 传递新参数 |
| 11 | app.py | L866-920 | UI重写: TP/SL模式选择+仓位参数说明 |
| 12 | app.py | L1399-1402 | 保存tp_mode/sl_mode到session |
| 13 | app.py | L1426-1438 | 传递新参数到engine |
| 14 | app.py | L1882-1896 | Walk Forward使用新参数 |
| 15 | engine_core.py | `_open()` ~L1130 | **阻塞项1**: Fixed Risk 公式插入 notional = units * fill_price |
| 16 | engine_core.py | `_open()` ~L1145 | **阻塞项3**: 新增 existing_notional 参数 + 累计上限检查 |
| 17 | engine_core.py | `_check_pyramiding()` ~L975 | **阻塞项3**: 传递 existing_notional=total_notional |
| 18 | engine_core.py | `_open()` ~L1170 | **阻塞项2**: ATR止损注释改为"入场时ATR止损" |
| 19 | app.py | L866-920 | **阻塞项2**: UI标签改为"ATR入场止损" |

---

## 二、逐模块修改前后对比

### 模块1: 仓位管理 (regime_alloc)

**修改前:**
```python
# _try_rotate_entry() L902-915
if regime == 'bull':    alloc = self.bull_alloc      # 1.0
elif regime == 'bear':  alloc = self.bear_alloc      # 0.3
else:                   alloc = self.range_alloc     # 0.5

# 经典模式首仓比例 (不受加仓开关影响)  ← 问题所在!
if self.strategy_mode == "classic" and hasattr(self, '_pyr_init_pct'):
    alloc = self._pyr_init_pct / 100.0  # 覆盖了regime_alloc!

self._open(...alloc...)  # alloc永远是0.3, 不管牛市震荡熊市
```

**修改后:**
```python
# _try_rotate_entry() L926-944
if regime == 'bull':    alloc = self.bull_alloc      # 1.0
elif regime == 'bear':  alloc = self.bear_alloc      # 0.3
else:                   alloc = self.range_alloc     # 0.5

# P0修复: regime_alloc 直接由市场状态决定，不受首仓比例覆盖
self._open(...alloc...)  # alloc由regime决定！
```

**影响分析:**
- 牛市: 之前固定30% → 现在100% ✅ 符合产品设计
- 震荡: 之前固定30% → 现在50% ✅ 符合产品设计
- 熊市: 之前固定30% → 现在30% 保持不变
- 不会引入未来函数 (regime值来自shift(1)计算)
- ⚠️ 历史回测公平性: **会改变** — 之前测试结果基于30%固定仓位，修复后仓位随市场状态变化

---

### 模块2: Fixed Risk 公式（含阻塞项1修复）

**修改前 (P0初版):**
```python
# _open() L1094-1101 (旧)
sl_distance = fill_price * (self.sl_pct / lev)       # 高杠杆=窄止损
max_risk_amount = self.equity * risk_pct              # 不考虑市场状态
position_units = max_risk_amount / sl_distance
position_value = position_units * fill_price
margin = position_value / lev
if margin > self.equity: margin = self.equity
notional = margin * lev
```

**P0修复后 (存在Bug):**
```python
# P0第一版修复 — 有单位一致性Bug!
risk_budget = self.equity * risk_pct * regime_mult
position_units = risk_budget / sl_distance
margin = position_units / lev        # ← BUG: position_units是币数不是USD!
# 缺少: notional = position_units * fill_price
```

**阻塞项1修复后 (当前版本):**
```python
# Step 1: 市场状态乘数
regime_mult = ...
# Step 2: 风险预算
risk_budget = self.equity * risk_pct * regime_mult
# Step 3: 止损距离
sl_distance = fill_price * sl_pct  # (价格%) 或 fill_price * (sl_pct/lev) (保证金%)
# Step 4: 仓位单位数 (Block 1 fix — 明确命名 + 插入转换)
position_units = risk_budget / sl_distance   # 单位: 币数 (e.g. 多少个ETH)
# Step 5: 名义价值 (Block 1 fix — 币数→USD的转换)
notional = position_units * fill_price       # ← 关键修复! 单位=USDT
# Step 6: 保证金
margin = notional / lev
# Step 7: 上限保护
max_notional = self.equity * self.max_notional_pct
if notional > max_notional:
    notional = max_notional
    margin = notional / lev
```

**公式对比:**
| 项目 | 修改前 | P0初版(Bug) | 修复后 |
|------|--------|-------------|--------|
| 风险预算 | equity × risk_pct | equity × risk_pct × regime_mult | equity × risk_pct × regime_mult |
| 单位计算 | 混合 | units = risk_budget / sl_distance | units = risk_budget / sl_distance |
| 名义价值 | 无明确转换 | **缺失!** units当USD用了 | notional = units × fill_price |
| 市场状态影响 | 无 | bull=100%, range=50%, bear=30% | bull=100%, range=50%, bear=30% |
| 最大仓位限制 | margin ≤ equity | 无 | notional ≤ equity × 5 |

**Block 1 数值验证 (用户指定示例):**
```
参数: equity=10000U, risk=1%, bull=100%, entry=2000, SL=5%价格, 3x杠杆

手工推导:
  risk_budget  = 10000 × 1% × 100% = 100.00 USDT
  sl_distance  = 2000 × (1+0.02%) × 5% = 100.02 USDT/单位
  position_units = 100.00 / 100.02 = 0.9998 (单位: ETH个数)
  notional     = 0.9998 × 2000.40 = 2000.00 USDT  ← 关键: units→USD转换
  margin       = 2000.00 / 3 = 666.67 USDT
  最大亏损     = 666.67 × 5% × 3 = 100.00 USDT ✓ (等于risk_budget)

结论: position_units是币数, notional是美元名义价值, margin是保证金
      公式正确，单位一致性验证通过。
```

**影响分析:**
- 牛市: 风险预算不变 (regime_mult=1.0)
- 震荡: 风险预算减半 (regime_mult=0.5) — 更保守
- 熊市: 风险预算降至30% (regime_mult=0.3) — 更保守
- 最大仓位保护始终生效，防止极端情况
- ⚠️ 历史回测公平性: **会改变** — Fixed Risk模式下震荡/熊市仓位显著降低

---

### 模块3: ATR入场止损（阻塞项2：命名与行为澄清）

**阻塞项2背景**: 代码中ATR止损在`_open()`入场时一次性计算sl_price并存入position字典。`_check_positions()`只读取`pos['sl_price']`检查触发，不按每根K线重新计算ATR止损价。持仓期间唯一动态调整止损的是`trailing_pct`移动止损机制。因此不能称为"动态ATR止损"。

**当前实现:**
```python
# run() — 预计算 ATR(14) 全序列（每bar用shift(1)防未来函数）
for coin in coins:
    df = dfs_with_sigs[coin]
    tr = pd.concat([...]).max(axis=1)
    df['_atr_14'] = tr.ewm(span=14, adjust=False).mean().shift(1)

# _open() — 入场时用当前bar的ATR值一次性设定止损价
if hasattr(self, '_use_atr_sl') and self._use_atr_sl:
    atr_val = atr_value if (atr_value and atr_value > 0) else fill_price * 0.01
    if side == 'LONG': sl_price = fill_price - atr_val * atr_mult
    else:              sl_price = fill_price + atr_val * atr_mult
    # sl_price 存入 position dict, 持仓期间不再更新

# _check_positions() — 只读取固定sl_price检查触发
if pos['sl_price']:  # 只读不写
    if side == 'LONG' and low <= pos['sl_price']: → 止损
    if side == 'SHORT' and high >= pos['sl_price']: → 止损

# trailing_pct — 独立机制，不依赖ATR，是唯一的持仓动态止损
if pos.get('trailing_pct'):
    # 盈利超阈值后，向有利方向移动止损位
```

**Block 2 代码溯源验证结果:**
| 检查项 | 结果 |
|--------|------|
| `_check_positions()` 中引用 sl_price | 1次 (只读) |
| `_check_positions()` 中涉及 ATR | 否 |
| `_open()` 中 sl_price 赋值 | 6次 (入场设定) |
| 持仓期间是否更新 ATR 止损价 | 否 |
| 唯一动态止损机制 | trailing_pct (独立) |

**命名修正（代码+UI+文档）:**

| 位置 | 修改前 | 修改后 |
|------|--------|--------|
| engine_core.py `_open()` 注释 | ATR动态止损 | 入场时ATR止损 (不随K线更新) |
| engine_core.py verbose日志 | 动态 | 入场 |
| app.py UI标签 | ATR动态止损 | ATR入场止损 |
| Platform_Logic_Manual_v2.md | ATR动态止损 | ATR入场止损 |

**关键差异:**
| 项目 | 修改前 | 修改后 |
|------|--------|--------|
| ATR值来源 | 静态预设或每bar动态 | 每bar动态（不变） |
| 止损更新方式 | 标注为"动态" | 明确为入场一次性设定 |
| 持仓动态止损 | 无独立说明 | trailing_pct 独立负责 |
| 未来函数 | 无 | 无 (shift(1)防护) |
| 历史回测公平性 | — | **不变** — 仅命名/注释修正 |

---

### 模块4: TP/SL 定义

**修改前:**
- 只有一个 `tp_pct` 参数，永远是保证金%
- UI标签: "止盈%" (不区分)
- 注释写着"保证金止盈%"但用户界面看不到

**修改后:**
- 两个参数: `tp_mode` + `sl_mode`
- tp_mode: `'margin_pct'` (保证金收益率%) 或 `'price_pct'` (价格%)
- sl_mode: `'margin_pct'` (保证金亏损率%) 或 `'price_pct'` (价格%)
- UI 新增两个 radio 选择器，用户可选

**TP公式:**
| 模式 | 代码值 | LONG公式 | 含义 |
|------|--------|---------|------|
| 保证金% | margin_pct | fill × (1 + tp_pct/lev) | 杠杆收益达到tp_pct%止盈 |
| 价格% | price_pct | fill × (1 + tp_pct/100) | 价格上涨tp_pct%止盈 |

**影响分析:**
- 默认行为不变 (margin_pct模式 = 旧行为)
- 新增 price_pct 模式：止盈距离与杠杆无关
- ⚠️ 历史回测公平性: **不变** — 默认模式保持旧逻辑

---

### 模块5: 风险保护机制

**新增保护:**
| # | 保护项 | 位置 | 逻辑 |
|---|--------|------|------|
| 1 | 杠杆上限 | `__init__()` | leverage > 125 → 抛异常 |
| 2 | 最大名义仓位 | `_open()` | notional > equity × 5 → cap |
| 3 | 保证金不足 | `_open()` | margin > equity → margin = equity |
| 4 | 爆仓保护 | `_close()` | margin_pnl_pct ≤ -1.0 → 归零 (已有,不变) |

**影响分析:**
- 杠杆上限：仅极端情况生效，不影响正常回测
- 最大仓位限制：仅高杠杆Fixed Risk时可能触发
- ⚠️ 历史回测公平性: **会改变** — 之前可能存在超仓交易被纠正

---

### 模块6: 金字塔加仓累计限制（阻塞项3修复）

**问题**: P0修复中在`_open()`添加了`max_notional_pct`上限检查，但检查的是单次`_open()`调用的notional，不是已有仓位+新增仓位的累计值。金字塔加仓场景下，每次`_open()`独立检查导致总仓位可能超过5倍上限。

**修复方案**: `_open()`新增`existing_notional`参数，检查`existing_notional + notional ≤ max_notional`。

**修改代码:**
```python
# _open() 签名变更
def _open(self, ..., existing_notional: float = 0):
    ...
    # Step 7: 累计上限保护 (Block 3 fix)
    max_notional = self.equity * self.max_notional_pct
    total_notional = existing_notional + notional
    if total_notional > max_notional:
        notional = max(0, max_notional - existing_notional)  # 只缩减新增部分
        margin = notional / lev

# _check_pyramiding() 传递累额
total_notional = sum(...)  # 同向已有仓位之和
self._open(..., existing_notional=total_notional)
```

**Block 3 数值验证:**
```
参数: equity=10000U, max_notional_pct=5.0, 上限=50000U

场景1: 已有48000U + 新增5000U = 53000U > 50000U
       → 新增缩减至 50000-48000 = 2000U ✓

场景2: 连续加仓测试
       首仓20000U → 加仓1+15000U(累计35000U) → 加仓2+15000U(累计50000U=上限) ✓

场景3: 第三次加仓 (剩余空间=0)
       → 新增被完全拒绝 (缩减至0) ✓

结论: 累计限制机制正确工作，每次加仓检查"已有+新增"总和而不是仅新增。
```

**关键差异:**
| 项目 | 修改前 | 修改后 |
|------|--------|--------|
| 检查范围 | 单次_open()的notional | 已有+新增的累计notional |
| _open()签名 | (..., ) | (..., existing_notional: float=0) |
| 超限处理 | 每次独立5倍 | 累计不超过5倍 |

---

## 三、未来函数风险检查

| 检查项 | 状态 | 说明 |
|--------|------|------|
| regime 计算 | ✅ 安全 | MultiFactorRegime.evaluate() 全程shift(1) |
| ATR预计算 | ✅ 安全 | ewm(span=14).shift(1) 排除当前bar |
| 信号生成 | ✅ 安全 | DynamicStrategy信号均用shift(1) |
| TP/SL 计算 | ✅ 安全 | 基于fill_price，已包含滑点 |
| 开仓撮合 | ✅ 安全 | 用下一根开盘价 + 滑点 |

---

## 四、阻塞项修复追加（2026-08-10）

| 阻塞项 | 问题 | 修复 | 验证 |
|--------|------|------|------|
| Block 1 | Fixed Risk公式缺少 units×fill_price 转换，units被直接当USD | 插入 notional=units×fill_price | 数值测试: units=0.9998, notional=2000, margin=666.67, max_loss=100 ✓ |
| Block 2 | "动态ATR止损"实际不动态，sl_price入场后不变 | 全部重命名为"ATR入场止损"，明确trailing_pct为唯一动态止损 | 源码检查: _check_positions()只读sl_price，不涉及ATR ✓ |
| Block 3 | 金字塔累计仓位检查每次_open()独立，非累计 | _open()新增existing_notional参数，累计检查 | 数值测试: 48000+5000→缩减至2000; 连续加仓不超5倍 ✓ |

---

## 五、总结

| 维度 | 评估 |
|------|------|
| 修改模块数 | 2个文件, 19处修改 (含3阻塞项) |
| 是否影响回测公平性 | 是 — Block 1 bug修复会改变仓位计算 |
| 是否引入未来函数 | 否 — 所有计算均shift(1)防护 |
| 是否改变策略逻辑 | 否 — 参数不变(EMA=15/60等核心策略不变) |
| 是否改变仓位逻辑 | 是 — Fixed Risk公式修复 + 累计上限 |
| 默认行为兼容 | 是 — TP/SL默认margin_pct模式保持旧行为 |
| 3个阻塞项状态 | **全部通过** ✅ |
