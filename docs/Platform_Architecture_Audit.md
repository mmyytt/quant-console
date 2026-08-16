# 当前量化交易平台底层逻辑架构说明书

> **文档性质**: 代码实际实现审计（非理论设计）
> **审计日期**: 2026-08-10
> **审计范围**: engine_core.py / app.py / audit_engine.py / walk_forward.py
> **原则**: 一切以代码实际执行为准，不按产品文档推测

---

## 一、系统整体交易流程

### 完整执行链路（逐根K线遍历模式）

```
[每一根4H K线]
    │
    ├── 1. 断路器检查 ──── paused = i < self.lock_until
    │                       (engine_core.py:698)
    │
    ├── 2. 对冲状态机 ──── 仅hedging/unlocking模式
    │     (engine_core.py:700-701)   _hedge_state_machine()
    │
    ├── 3. 持仓TP/SL检查 ── _check_positions()
    │     (engine_core.py:704)       遍历所有持仓,Bar内High/Low触发
    │     ├── 先检查TP vs SL同bar双触发 → 取SL(保守原则)
    │     ├── 检查移动止损 trailing stop
    │     ├── 检查爆仓 liquidation
    │     └── 平仓后: 连亏计数+断路器
    │
    ├── 4. 轮动开仓 ────── _try_rotate_entry()
    │     (engine_core.py:710-712)   扫描所有币种signal
    │     ├── 过滤: 冷却期 / 空头比例超限 / 对冲LOCKED
    │     ├── 选币: score最高的币种
    │     ├── 定仓位: regime → alloc (bull=1.0/range=0.5/bear=0.3)
    │     └── _open() 执行开仓
    │
    ├── 5. 资金费率结算 ──── 每2根K线(=8h), 仅限杠杆>1
    │     (engine_core.py:720-727)   多头付/空头收
    │
    ├── 6. 金字塔加仓检测 ── _check_pyramiding()
    │     (engine_core.py:730-732)   浮盈达标→加仓
    │
    ├── 7. 权益快照 ──────── equity_curve.append()
    │     (engine_core.py:735-746)
    │
    └── → 下一根K线
```

### 每个步骤详细说明

| 步骤 | 负责模块 | 输入 | 输出 | 代码位置 |
|:---|:---|:---|:---|:---|
| 信号计算 | `DynamicStrategy.generate_signals()` | OHLCV DataFrame | df增加signal/regime/score列 | app.py:638 |
| 牛熊判断 | `MultiFactorRegime.evaluate()` | OHLCV (shift(1)) | regime_mf: bull/bear/range | engine_core.py:126 |
| 选币评分 | `_try_rotate_entry()` | 各币种signal+score | 最高分币种+方向 | engine_core.py:852 |
| 仓位计算 | `_try_rotate_entry()` → `_open()` | regime + alloc配置 | margin/notional/contracts | engine_core.py:902-917 |
| 开仓执行 | `_open()` | coin/side/price/alloc | 创建pos字典写入self.positions | engine_core.py:1080 |
| TP/SL检查 | `_check_positions()` | Bar High/Low + tp_price/sl_price | 触发则调用_close() | engine_core.py:759 |
| 平仓结算 | `_close()` | pos + exit_price | 更新equity, 写入trade记录 | engine_core.py:1164 |
| 资金更新 | `_close()` → `self.equity += pnl_usd` | pnl_usd | 新equity值 | engine_core.py:1191 |

---

## 二、市场状态判断模块审计

### 代码实际实现（MultiFactorRegime, engine_core.py:66-191）

#### 三因子加权评分模型

系统使用**3个因子 × 各自权重 = 综合评分**，然后根据阈值判定牛/熊/震荡。

**完整公式**:

```
composite_score = EMA_score × 0.40 + ADX_score × 0.35 + Funding_score × 0.25

其中:
  EMA_score     ∈ [-1, 1]   (EMA斜率方向+强度)
  ADX_score     ∈ [-1, 1]   (DI方向 × ADX强度)
  Funding_score ∈ [-1, 1]   (资金费率情绪, 无数据时=0)
```

**阈值判定**:
```
composite >  +0.30  →  'bull'   (牛市)
composite <  -0.30  →  'bear'   (熊市)
其他                →  'range'  (震荡)
```

权重/阈值均为**硬编码默认值**，可通过构造函数覆盖:
- `ema_weight=0.40`, `adx_weight=0.35`, `funding_weight=0.25`
- `bull_threshold=0.30`, `bear_threshold=-0.30`

#### 各因子详细计算

**Factor 1: EMA斜率 (权重40%)**
```python
ema = close.ewm(span=50).mean()
slope = (ema - ema.shift(20)) / ema.shift(20)    # 20根K线内EMA变化率
ema_score = (slope / 0.03).clip(-1, 1)            # ±3% 归一化到 ±1
```
- EMA参数: 50周期，斜率回看20根K线
- 使用 `shift(1)` 防未来函数
- 斜率归一化基准: ±3%
- **没有与价格均线关系判断，没有高低点结构，没有成交量因子**

**Factor 2: ADX趋势强度+方向 (权重35%)**
```python
adx, plus_di, minus_di = compute_adx(high, low, close, period=14)
di_diff = (plus_di - minus_di) / (plus_di + minus_di)     # DI方向 [-1,1]
adx_strength = (adx / (25 * 2)).clip(0, 1)                 # ADX强度 [0,1]
adx_score = di_diff * adx_strength                          # 组合 [-1,1]
```
- ADX周期: 14
- ADX阈值: 25（用于归一化强度，不是用于过滤震荡）
- `adx_score` 为正=多头趋势强，为负=空头趋势强
- **ADX低于阈值不会自动判定为震荡**，而是降低adx_score的绝对值

**Factor 3: 资金费率情绪 (权重25%)**
```python
fr = funding_rate.shift(1)                           # 上一期费率, 防未来
funding_score = -(fr / 0.0005).clip(-1, 1)           # 取反: 正费率→偏空
```
- 多头拥挤(正费率>0) → 偏空信号(负分)
- 空头拥挤(负费率<0) → 偏多信号(正分)
- 无数据时 funding_score = 0（不参与评分）

#### 震荡市场判断逻辑（重要）

**震荡不是"非牛非熊"的独立判断，而是"综合评分落在(-0.30, +0.30)区间"的默认结果。**

这意味着震荡可能由以下组合导致:
- EMA斜率接近0（无明显方向）+ ADX方向不明 + funding中立
- EMA略看多但ADX极弱+funding偏空 → 相互抵消
- 三者信号矛盾 → 总分落在中间地带

**不存在ADX独立阈值过滤**（如 "ADX<20=震荡" 这种规则不存在）。

---

## 三、交易信号模块说明

### 指标配置确认

| 指标 | 代码Key | 参数 | 与实际一致？ |
|:---|:---|:---|:---|
| EMA 双均线 | `'EMA 双均线'` | short=15, long=60 | ✅ 已确认 |
| 成交量突破 | `'成交量突破'` | VOL_ma=20, VOL_mult=2 | ✅ 已确认 |
| 斐波那契回调 | `'斐波那契回调'` | FIB_lookback=200 | ✅ 已确认 |

代码位置: `INDICATOR_SCHEMA` (app.py:114-359), `INDICATOR_REGISTRY` (app.py:361-381)

### 信号组合逻辑 (DynamicStrategy.generate_signals, app.py:638-734)

**多头开仓条件**:
```
所有启用指标的 _long 列同时为 True (AND模式)
AND 不在熊市 regime (regime_filter启用时)
AND 不在"仅做空"模式
```

**空头开仓条件**:
```
所有启用指标的 _short 列同时为 True (AND模式)
AND 不在牛市 regime (regime_filter启用时)
AND 不在"仅做多"模式
```

**退出条件**:
- **没有独立"平仓信号"**: 当前策略没有exit signal指标
- 平仓仅由 TP/SL/爆仓/移动止损 触发
- 强制平仓: 回测结束时的 EOD 平仓

### 各指标作用（代码实际）

| 指标 | 计算内容 | 输出的_long/_short含义 |
|:---|:---|:---|
| EMA 双均线 | 快线(15)上穿慢线(60) | _long=金叉, _short=死叉 |
| 成交量突破 | 成交量 > 20周期均量 × 2 | _long=放量上涨, _short=放量下跌 |
| 斐波那契回调 | 价格触及FIB关键位 | _long=支撑位反弹, _short=阻力位回落 |

三个指标用 **AND** 组合，即**三者同时触发才开仓**。

---

## 四、仓位管理模块审计

### 两种模式的核心区别

代码位置: `_open()` (engine_core.py:1088-1114)

**关键变量**:
- `_pos_mode`: 从 `strategy.selected['_pos_mode']` 读取, 默认 `'fixed_capital'`
- `_risk_pct`: 从 `strategy.selected['_risk_per_trade']` 读取, 默认 `1.0` (即1%)

### Fixed Capital 模式

```python
margin = self.equity × alloc           # alloc来自regime: bull=1.0/range=0.5/bear=0.3
notional = margin × leverage           # 名义价值
```

**实际含义**: `alloc` 控制的是**保证金占用比例**，即使用账户权益的多少比例作为保证金。

| 市场 | alloc | 10000U账户的保证金 | 3x杠杆的名义价值 |
|:---|:---|:---|:---|
| bull | 1.0 | 10000U | 30000U |
| range | 0.5 | 5000U | 15000U |
| bear | 0.3 | 3000U | 9000U |

**重要**: Fixed Capital模式**不考虑止损距离、不考虑ATR、不考虑市场波动率**。

### Fixed Risk 模式

```python
sl_distance = fill_price × (sl_pct / leverage)              # 每股止损金额(价格距离)
max_risk_amount = self.equity × risk_pct                     # 最大风险金额
position_units = max_risk_amount / sl_distance               # 开仓股数
position_value = position_units × fill_price                 # 仓位名义价值
margin = position_value / leverage                           # 所需保证金
```

**实际公式**:
```
仓位名义价值 = (equity × risk%) / (sl_pct / leverage)
```

**10000U, 5%风险, 3x杠杆, sl_pct=5%的例子**:
```
sl_distance = fill_price × 0.05 / 3 = fill_price × 0.0167  (价格跌1.67%止损)
max_risk = 10000 × 0.05 = 500U
position_value = 500 / 0.0167 = 29940U
margin = 29940 / 3 = 9980U
```
→ 几乎全仓！因为止损距离太窄。

**Fixed Risk 的致命问题**: 止损距离 `sl_pct/leverage` 在高杠杆下极窄，导致仓位公式算出巨大仓位，可能超过账户可用保证金。

代码中的保护 (line 1100-1101):
```python
if margin > self.equity:
    margin = self.equity
```
这个保护只是cap保证金不超过equity，但没有cap名义价值。

---

## 五、牛熊仓位比例与风险比例关系

### 关键冲突点

**Fixed Capital 模式下**:
- `alloc` (bull_alloc/range_alloc/bear_alloc) 控制**保证金占用比例**
- 震荡 `range_alloc=0.5` → 只用 50% 资金做保证金

**震荡50%仓位 = 情况B: 只降低资金使用量**

```python
margin = self.equity × 0.5        # 5000U保证金(对10000U账户)
notional = 5000 × leverage        # 杠杆放大后的名义价值
```
- 风险金额不一定减半！TP/SL触发取决于价格波动，不是按比例减半的

**Fixed Risk 模式下**:
- `alloc` **完全被跳过**！(line 913-915 有经典模式首仓覆盖)
- 仓位由 `risk_pct / sl_distance` 公式决定
- `range_alloc=0.5` 在此模式下**不起作用**

**代码实际流程** (engine_core.py:902-917):
```python
# 第1步: regime决定alloc
if regime == 'bull':    alloc = self.bull_alloc      # 1.0
elif regime == 'bear':  alloc = self.bear_alloc      # 0.3
else:                   alloc = self.range_alloc     # 0.5

# 第2步: 经典模式首仓覆盖 (如果有_pyr_init_pct)
if self.strategy_mode == "classic" and hasattr(self, '_pyr_init_pct'):
    alloc = self._pyr_init_pct / 100.0   # 默认30 → 0.3   ← 覆盖了上一步!

# 第3步: _open()内部再判断Fixed Risk
# Fixed Risk模式完全不使用alloc参数！
```

---

## 六、资金管理逻辑冲突检查

### 问题1: Fixed Risk与牛熊仓位冲突

**结论: 存在设计冲突。**

Fixed Risk模式:
- 仓位 = `(equity × risk%) / (sl_pct / leverage)`
- 仓位由止损距离公式决定，**不受alloc参数控制**
- 震荡/牛市/熊市的仓位**完全一样**

但实际上Fixed Risk的公式间接包含了杠杆效应 — `sl_pct/leverage` 在高杠杆时更窄 → 仓位更大。

### 问题2: 首仓比例与牛熊仓位冲突

**结论: 存在代码覆盖冲突。**

代码执行顺序 (engine_core.py:902-917):
```python
# T1: regime决定alloc (如bull=1.0)
alloc = self.bull_alloc

# T2: 经典模式首仓覆盖
alloc = self._pyr_init_pct / 100.0   # 默认0.3 → 覆盖了T1!
```

T2完全覆盖了T1！所以**经典模式下 bull_alloc/range_alloc/bear_alloc 实际不生效**，只有 `_pyr_init_pct` 生效。

### 问题3: 杠杆的影响（已验证）

从实测数据确认:
```
杠杆 → 改变止盈止损价格距离 (tp_price = fill * (1 + tp_pct/lev))
     → 改变触发概率 (高杠杆=更容易触发TP/SL)
     → 改变交易频率 (1x=89笔 → 5x=148笔)
     → 改变仓位大小 (Fixed Risk模式)
     → 改变回撤幅度
```

**杠杆不是纯粹的收益放大器。**

---

## 七、风险控制模块说明

### ATR 动态止损

代码位置: engine_core.py:1128-1135

```python
if hasattr(self, '_use_atr_sl') and self._use_atr_sl:
    atr_val = self._atr_val              # 静态预设值, 非每bar计算
    atr_mult = self._atr_mult             # 默认2.0
    if side == 'LONG':
        sl_price = fill_price - atr_val * atr_mult   # 覆盖保证金止损!
    else:
        sl_price = fill_price + atr_val * atr_mult
```

**关键发现**:
- `_atr_val` 是一个固定值，在回测开始前设置，**并不随每根K线动态更新**
- ATR止损**完全覆盖**保证金止损 sl_pct
- 实际效果: 开仓时设定一个基于当前ATR的止损价，后续不变

### 单笔最大风险5%（开仓前计算）

- Fixed Risk模式: 在开仓时通过仓位公式控制 → `position_value = risk_amount / sl_distance`
- Fixed Capital模式: **无单笔风险控制**，只有仓位比例控制
- 无论如何，**没有"亏损后限制"机制** — 亏损发生后追踪的是连亏断路器

### 连亏3笔锁仓

代码位置: engine_core.py:841-845

```python
if margin_pnl <= -self.sl_pct:      # 保证金亏损≥sl_pct → 算一次连亏
    self.losestreak += 1
    if self.losestreak >= self.lock_streak:   # 默认3
        self.lock_until = bar_idx + self.lock_bars  # 默认12根K线(=2天4H)
        self.losestreak = 0
else:
    self.losestreak = 0              # 盈利或未触发SL → 重置连亏计数
```

**关键细节**: 只有触发止损(SL)才计入连亏，TP平仓不算亏，手动/爆仓也不一定触发计数。

---

## 八、历史市场状态切换分析

无法在此文档中内嵌完整时间序列，但基于代码逻辑可确认:

### MultiFactorRegime 的滞后性问题

**存在滞后**，原因:
1. EMA(50) 平滑 → 趋势变化滞后约25根K线
2. 斜率回看20根K线 → 额外20根滞后
3. ADX(14) 本身有滞后
4. 阈值 ±0.30 → 需要足够强的信号才切换状态

**典型滞后场景**: 
- 价格从顶部急跌 → EMA斜率转负需要时间 → 可能已跌10%仍在"bull"状态
- 震荡市 → EMA斜率在0附近反复 → 频繁在bull/range/bear间切换

---

## 九、《当前交易系统逻辑一致性审计》

### 1. 产品设计理念 vs 代码实际

| 产品预期 | 代码实际 | 一致性 |
|:---|:---|:---|
| "牛市100%仓位" | alloc=1.0 → margin=equity×1.0 | ✅ 一致 (但被_pyr_init_pct覆盖) |
| "震荡50%仓位" | alloc=0.5 → margin=equity×0.5 | ✅ 一致 (但被_pyr_init_pct覆盖) |
| "Fixed Risk单笔风险5%" | position_value = (equity×5%) / sl_distance | ✅ 公式一致 |
| "ATR 14周期动态止损" | _atr_val是固定值，不动态更新 | ❌ 名不副实 |
| "牛熊过滤" | 3因子加权评分+阈值 | ✅ 一致 |
| "止盈15点" | 保证金收益率15%，非价格15% | ⚠️ 命名歧义 |

### 2. 存在的逻辑冲突

| # | 冲突 | 严重程度 | 影响 |
|:---|:---|:---|:---|
| 1 | `_pyr_init_pct` 覆盖 `bull_alloc/range_alloc/bear_alloc` | 🔴 高 | 经典模式下牛熊仓位比例不生效 |
| 2 | Fixed Risk与alloc参数互斥 | 🟡 中 | 两种仓位逻辑不能同时使用 |
| 3 | ATR止损名为"动态"实为"静态" | 🟡 中 | _atr_val预设不变，不随行情调整 |
| 4 | 保证金止盈 vs 价格止盈 命名歧义 | 🟡 中 | "止盈15%"在不同杠杆下含义完全不同 |
| 5 | Fixed Risk高杠杆仓位可能超过equity | 🟠 中低 | 有margin cap保护但不完备 |

### 3. 需要后续优化的模块

| 优先级 | 模块 | 问题 | 建议 |
|:---|:---|:---|:---|
| P0 | 仓位管理 | `_pyr_init_pct`覆盖牛熊alloc | 分开两个独立参数，不要互相覆盖 |
| P0 | TP/SL | 保证金模式 vs 价格模式 | 增加价格百分比止盈模式选项 |
| P1 | ATR止损 | 静态_atr_val | 改为每bar动态计算ATR(14) |
| P1 | Fixed Risk | 高杠杆下仓位暴增 | 增加max_position_pct上限 |
| P2 | 牛熊判断 | EMA+ADX滞后 | 考虑加入价格结构(高低点)判断 |
| P2 | 资金费率 | 无数据时权重虚设 | 无funding数据时重新分配权重 |

---

> **审计结论**: 系统核心回测逻辑（无未来函数、Bar内撮合、滑点+手续费）是正确的。仓位管理模块存在参数覆盖冲突和模式互斥问题，需要后续优化但不影响回测结果的准确性。
