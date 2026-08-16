# 量化回测平台技术说明文档 v2.1

> 编写日期: 2026-08-10 | 代码版本: P0修复后 (engine_core.py + app.py)
> 作者: 平台首席开发工程师 | 用途: 产品经理 + 外部架构审查

---

# 1. 平台整体架构

## 1.1 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| 前端 | Streamlit (Python) | 纯Python Web UI，单文件 `app.py` 约2000行 |
| 后端 | Python 3.10 | 单文件 `engine_core.py` 约2000行 |
| 数据存储 | Parquet + 本地CSV | ETH/BTC/SOL 5分钟/15分钟OHLCV |
| 科学计算 | Pandas + NumPy | 向量化指标计算, DataFrame操作 |
| 无数据库 | — | 所有状态存内存，回测结果存session |

## 1.2 核心模块结构 (单一文件 engine_core.py)

```
engine_core.py
├── 常量定义 (L57-60)
│   ├── TAKER_FEE = 0.0005      # 手续费 0.05%
│   ├── SLIPPAGE  = 0.0002      # 滑点 0.02%
│   └── TOTAL_COST = 0.0007     # 单边总成本 0.07%
│
├── MultiFactorRegime (L66-191)     # 三因子牛熊判断
│   ├── compute_adx()               # ADX(14) 计算
│   └── evaluate()                  # 综合评分 → bull/range/bear
│
├── DataEngine (L197-347)           # 数据加载引擎
│   ├── load_15min()                # 加载5m/15m parquet
│   ├── resample()                  # 重采样到4H/1D等
│   └── get_multi_timeframe()       # 一键获取多周期
│
├── StrategyBase (L348-378)         # 策略抽象基类
├── MACrossStrategy (L380-442)      # 双均线交叉策略
├── OversoldBounceStrategy (L448-491)# 超跌反弹策略
│
├── BacktestEngine (L497-516)       # 现货兼容包装 (v1)
├── BacktestEngineV2 (L522-1393)    # 主力合约回测引擎
│   ├── __init__()                  # 引擎初始化
│   ├── run()                       # 主回测循环入口
│   ├── _reset()                    # 状态重置
│   ├── _check_positions()          # TP/SL/爆仓/移动止损检查
│   ├── _try_rotate_entry()         # 多币轮动选币开仓
│   ├── _check_pyramiding()         # 金字塔加仓
│   ├── _open()                     # 开仓 (仓位计算核心)
│   ├── _close()                    # 平仓结算
│   ├── _hedge_state_machine()      # 对冲/解锁状态机
│   ├── _calc_total_equity()        # 估算含浮动盈亏的总权益
│   └── _build_result()             # 构建回测输出dict
│
├── PerformanceAnalyzer (L1395-1601)# 绩效指标计算
│
├── LegType / StrategyMode / PositionLeg / PortfolioManager (L1604-1900+)
│   # 多腿仓位管理系统 (较新, 部分功能未完全集成到主引擎)
│
└── run_backtest() (L1950-2010)     # 便捷函数: 一行回测
```

## 1.3 数据流向

```
[Parquet文件] → DataEngine.load_15min() → resample() → 多周期OHLCV
     ↓
Strategy.generate_signals(df)  → df['signal'] + df['regime'] + df['score']
     ↓
MultiFactorRegime.evaluate(df) → df['regime_mf'] + df['br_mf']
     ↓
ATR预计算 (shift(1)) → df['_atr_14']
     ↓
BacktestEngineV2.run() → 逐bar遍历 → 持仓检查 → 开仓/加仓 → 平仓
     ↓
_build_result() → {trades, equity_curve, portfolio_curve, ...}
     ↓
PerformanceAnalyzer.analyze() → {sharpe, max_dd, win_rate, ...}
     ↓
[Streamlit app.py] → 图表 + 指标面板
```

## 1.4 回测执行流程 (用户点击"开始回测"到生成曲线)

```
Step 1: app.py 收集 UI 参数
  ├── 仓位模式: pos_mode (fixed_capital / fixed_risk)
  ├── 策略模式: strategy_mode (classic / hedging / unlocking)
  ├── 杠杆, TP/SL百分比, TP/SL模式 (margin_pct / price_pct)
  ├── 牛/震/熊 alloc, 连亏锁仓参数
  ├── ATR止损参数, 金字塔加仓参数
  └── 输出: strategy.selected dict → 传给 engine

Step 2: 数据加载
  ├── DataEngine 从 parquet 文件加载 ETH_5m.parquet
  ├── resample() 到目标周期 (如 4H)
  ├── 校验完整性: 时间戳排序, 无重复, 无大缺口
  └── 输出: {'ETH': df} dict

Step 3: 信号生成 (run()内部)
  ├── df = strategy.generate_signals(df)    # 指标 + 交易信号
  ├── df = regime.evaluate(df)              # 牛熊判断
  ├── ATR(14) 预计算全序列, shift(1)防未来
  └── 多币种时间对齐 (如果多币)

Step 4: 逐bar回测循环 (for ts in common_index:)
  每次迭代:
  ├── a. 断路器检查 (lock_until > bar_idx? → 跳过开仓)
  ├── b. _check_positions(): 检查所有持仓
  │     ├── TP/SL同bar触发 → 保守取SL
  │     ├── 移动止损 trailing_pct
  │     ├── 爆仓检测 (保证金率 < 维持保证金率)
  │     └── 触发 → _close() → 更新losestreak
  ├── c. _try_rotate_entry(): 扫描所有币种信号
  │     ├── 过滤: 冷却期 / bear_ratio超限 / regime=range且无信号
  │     ├── 评分排序, 选最高分
  │     ├── 确定 regime → 查表获取 alloc
  │     └── _open() 开仓
  ├── d. 资金费率结算 (每2根4H = 8h, 默认0.01%费率)
  ├── e. _check_pyramiding(): 金字塔加仓检测
  └── f. 记录权益曲线

Step 5: 回测结束
  ├── 强制平仓所有持仓 (EOD)
  ├── _build_result() 组装数据
  └── PerformanceAnalyzer.analyze() 计算指标
```

---

# 2. 回测引擎逻辑

## 2.1 K线数据读取

**数据源**: 本地 parquet 文件 (ETH/BTC/SOL, 5分钟粒度)
**文件路径**: `C:\Users\myt\Desktop\eth_all\ETH_5m.parquet` (941,297 bars)
**数据截止**: 2026-08-03 (距当前约7天)

```python
# DataEngine.load_15min() → engine_core.py:L212
# 优先读取5m数据, 降级到15m
path_5m = os.path.join(self.data_dir, f"{coin}_5m.parquet")
df = pd.read_parquet(path)
```

**重采样逻辑** (`engine_core.py:L260+`):
```python
# 5min → 4H 重采样
resampled = df.resample('4h', closed='left', label='left').agg({
    'open': 'first', 'high': 'max', 'low': 'min',
    'close': 'last', 'vol': 'sum'
})
```
- 使用 `closed='left'` 确保 K线在标准时间边界闭合 (00:00/04:00/08:00...)
- 当前数据: 19,625根4H K线, 2017-08-17 ~ 2026-08-03 (约8.69年)

**过期检测**: 数据超过7天未更新 → 自动触发重新下载 (`data_loader.ensure_data()`)

## 2.2 指标计算 (MACrossStrategy示例)

```python
# engine_core.py:L400-442
fast_ema  = close.ewm(span=5).mean().shift(1)       # 快线, shift(1)防未来
slow_ema  = close.ewm(span=20).mean().shift(1)       # 慢线
regime_ema = close.ewm(span=50).mean().shift(1)      # 趋势均线

# 交叉信号: 确认上一根已闭合的交叉
cross_up   = (fast_ema > slow_ema) & (fast_ema.shift(1) <= slow_ema.shift(1))
cross_down = (fast_ema < slow_ema) & (fast_ema.shift(1) >= slow_ema.shift(1))
```

**关键防未来函数措施**:
- 所有 `ewm().mean()` 后紧跟 `.shift(1)` — 均线用上一根收盘价计算
- 交叉信号用 `shift(1)` 确认历史状态
- 成交量均线: `vol.rolling(20).mean().shift(1)`

## 2.3 信号生成

**信号列**: `df['signal']` ∈ {1 (做多), -1 (做空), 0 (观望)}
**评分列**: `df['score']` = ma_up(0/1) + vol_up(0/1) + trend_up(0/1) ∈ {0,1,2,3}
**牛熊列**: `df['regime']` ∈ {'bull', 'range', 'bear'}

**超跌反弹策略**: 只用 `close.shift(1)` 计算N根跌幅, 超阈值 → signal=1, 严禁使用当前根数据。

## 2.4 开仓流程

```python
# engine_core.py _try_rotate_entry() L876-946
# 每个bar执行:
1. 扫描所有币种, 过滤:
   - 冷却期内 (cooldown >= bar_idx)
   - signal=0/NaN
   - bear_ratio > bear_ratio_limit (空头比例超限→空仓)
2. 按 score 排序, 选最高分
3. 查表获取 alloc (仓位比例):
   - bull  → self.bull_alloc   (默认 1.0 = 100%)
   - bear  → self.bear_alloc   (默认 0.3 = 30%)
   - range → self.range_alloc  (默认 0.5 = 50%)
4. 调用 _open(coin, side, price=row['open'], alloc, ...)
```

**撮合价格**: 使用**当前bar开盘价** `row['open']` (无未来函数)
- 信号基于 t-1 已闭合数据计算
- 撮合使用 t 时刻开盘价 + 滑点
- 做多: `fill_price = open * (1 + SLIPPAGE)`  (买贵0.02%)
- 做空: `fill_price = open * (1 - SLIPPAGE)`  (卖贱0.02%)

## 2.5 仓位选择

**对于 Fixed Capital% 和 Fixed Risk% 两种模式，详见第3节。**

## 2.6 加仓流程 (金字塔)

```python
# engine_core.py _check_pyramiding() L948-996
触发条件:
  - _enable_pyramiding = True
  - 每个 position 独立维护 pyramid_count < max_pyramid_count (最大加仓次数, 默认3)
  - 已持仓浮盈 >= _pyr_trigger_pct (默认2%)

加仓金额:
  - add_margin = equity × _pyr_add_pct (默认50%)
  - add_notional = add_margin × leverage

加仓后:
  - 重新计算加权均价: Σ(notional_i × entry_i) / Σ(notional_i)
  - 更新所有同向仓位的 entry 为加权均价
  - 重新计算 TP/SL (基于新均价, 尊重tp_mode/sl_mode)
  - 如果 _pyr_trail=True: 均价即新止损线
```

## 2.7 平仓流程

```python
# engine_core.py _check_positions() L783-872
每根bar检查:
  做多:
    t_hit = bar_high >= tp_price   # 止盈触发
    s_hit = bar_low <= sl_price    # 止损触发
    同bar双触发 → 保守原则: 取SL (worst-case)

  做空:
    t_hit = bar_low <= tp_price
    s_hit = bar_high >= sl_price

  移动止损 (仅trailing_pct>0时):
    做多: trail_stop = highest_price × (1 - trailing_pct)
          需要 trailing_activated=True 才触发
    做空: trail_stop = lowest_price × (1 + trailing_pct)

  爆仓:
    浮亏超过100%保证金 或 价格触及liq_price 或 保证金率 < 维持保证金率(0.5%)
    → 强制平仓, 归零
```

**平仓价格**: 用TP/SL限价 (模拟触发后立即成交)
**平仓成本**: PnL中已扣除平仓滑点 + 平仓手续费

## 2.8 止盈止损价格计算

```python
# engine_core.py _open() L1205-1230
# 两种模式, 在开仓时一次性计算:

保证金%模式 (margin_pct):
  做多: tp_price = fill × (1 + tp_pct/lev)
  做多: sl_price = fill × (1 - sl_pct/lev)

价格%模式 (price_pct):
  做多: tp_price = fill × (1 + tp_pct)     # tp_pct已是小数
  做多: sl_price = fill × (1 - sl_pct)

# 高杠杆下的差异:
# 3x, tp_pct=10%保证金: tp距离 = 10%/3 = 3.33%价格
# 3x, tp_pct=10%价格:   tp距离 = 10%价格
```

## 2.9 未来函数审计

| 检查项 | 使用数据 | 未来函数? | 说明 |
|--------|---------|-----------|------|
| EMA均线 | ewm().shift(1) | ✅ 安全 | 用上一根收盘价 |
| 交叉信号 | shift(1)确认 | ✅ 安全 | 已闭合K线的状态 |
| 成交量均线 | rolling(20).shift(1) | ✅ 安全 | |
| 牛熊判断 | EMA slope shift(1) | ✅ 安全 | 全链路shift(1) |
| ADX | ewm(alpha=1/14).shift(1)隐含 | ✅ 安全 | 用已闭合数据 |
| ATR(14) | ewm(span=14).shift(1) | ✅ 安全 | 明确shift(1) |
| 开仓撮合 | row['open'] (当前bar) | ✅ 安全 | 基于t-1信号,t时执行 |
| TP/SL触发 | bar High/Low | ✅ 安全 | bar内价格路径 |
| 平仓价格 | TP/SL限价 | ✅ 安全 | 模拟限价单成交 |
| 资金费率 | shift(1) | ✅ 安全 | 上一周期费率 |

**结论: 当前代码不存在未来函数泄露。所有预测性计算基于已闭合数据, 撮合基于当前bar开盘价。**

---

# 3. 仓位管理系统（重点）

## 3.1 两种仓位模式对比

| 维度 | Fixed Capital % | Fixed Risk % |
|------|:---:|:---:|
| UI标签 | "固定资金比例" | "固定风险比例" |
| 核心思想 | 按市场状态分配保证金 | 控制每笔最大亏损金额 |
| 仓位决定因素 | 权益 × regime_alloc | 风险预算 / 止损距离 |
| 杠杆影响 | 线性: margin×lev = notional | 反比: 高杠杆→窄止损→大仓位 |
| 市场状态影响 | margin直接乘regime_alloc | risk_budget乘regime_mult |
| 上限保护 | margin ≤ equity | notional ≤ equity × 5 |

## 3.2 Fixed Capital % (固定资金比例)

**代码位置**: `engine_core.py` `_open()` L1191-1199

```python
# 公式:
margin  = equity × alloc           # alloc由regime决定
notional = margin × leverage       # 名义仓位

# 示例 (10000U账户, 3x杠杆):
# 牛市 alloc=1.0: margin=10000, notional=30000
# 震荡 alloc=0.5: margin=5000,  notional=15000
# 熊市 alloc=0.3: margin=3000,  notional=9000
```

**特点**:
- 简单直接，仓位不受价格波动影响
- 不考虑止损距离和单笔亏损
- 杠杆只影响名义仓位大小

## 3.3 Fixed Risk % (固定风险比例)

**代码位置**: `engine_core.py` `_open()` L1145-1189

```python
# 完整公式链:
# Step 1: 风险预算
regime_mult = bull_alloc/range_alloc/bear_alloc  # 市场状态乘数
risk_budget = equity × risk_pct × regime_mult

# Step 2: 止损距离 (根据sl_mode)
if sl_mode == 'price_pct':
    sl_distance = fill_price × sl_pct              # 价格%止损
else:
    sl_distance = fill_price × (sl_pct / leverage) # 保证金%止损

# Step 3: 仓位单位数
position_units = risk_budget / sl_distance        # 单位: 币数

# Step 4: 名义价值
notional = position_units × fill_price            # 单位转换为USDT

# Step 5: 保证金
margin = notional / leverage

# Step 6: 累计上限保护 (Block 3修复)
max_notional = equity × max_notional_pct          # 默认5倍
total_notional = existing_notional + notional
if total_notional > max_notional:
    notional = max(0, max_notional - existing_notional)
    margin = notional / leverage
```

**数值验证** (Block 1修复确认):
```
参数: equity=10000, risk=1%, bull=100%, entry=2000, SL=5%价格, 3x

risk_budget  = 10000 × 1% × 100%   = 100.00 USDT
sl_distance  = 2000.40 × 5%        = 100.02 USDT/unit
position_units = 100.00 / 100.02   = 0.9998 ETH
notional     = 0.9998 × 2000.40    = 2000.00 USDT  ← units→USD转换
margin       = 2000.00 / 3         = 666.67 USDT
最大亏损     = 666.67 × 5% × 3     = 100.00 USDT ← 等于risk_budget ✓
```

**与P0初版的关键差异**: P0第一版重写时漏了 `notional = position_units * fill_price` 这一步, 导致 `position_units`(币数)被直接当作美元金额用了。Block 1修复已纠正。

## 3.4 关于"单笔风险占比% 锁定为1%"的问题

### 直接答案: **这是UI设计如此, 不是Bug。**

**问题定位链**:

1. **前端文件**: `app.py` L906-908
```python
risk_per_trade = c1.number_input("单笔风险占比%", 0.5, 5.0, 1.0, 0.5,
                                  disabled=not use_fixed_risk,  # ← 关键!
                                  help="Fixed Risk模式: 每笔最大亏损占账户的%...")
```

2. **状态来源**: `app.py` L869-872
```python
pos_mode = st.radio("仓位模式", ["固定资金比例 (Fixed Capital %)",
                                   "固定风险比例 (Fixed Risk %)"], index=0)
use_fixed_risk = "Risk" in pos_mode  # ← 选择Fixed Capital时 = False
```

3. **后端读取**: `engine_core.py` `run()` L670-671
```python
self._pos_mode = sel.get('_pos_mode', 'fixed_capital')
self._risk_pct = sel.get('_risk_per_trade', 1.0) / 100.0
```

4. **后端使用**: `engine_core.py` `_open()` L1134-1135
```python
use_fixed_risk = getattr(self, '_pos_mode', 'fixed_capital') == 'fixed_risk'
risk_pct = getattr(self, '_risk_pct', 0.01)
# 只有 use_fixed_risk=True 时, risk_pct 才会被使用
```

**流程解释**:
- 用户选择 `Fixed Capital` → `use_fixed_risk=False`
- `risk_per_trade` input 的 `disabled=True` → **灰色不可编辑, 显示默认值1.0**
- 这个1.0仍然通过 `strategy.selected["_risk_per_trade"]` 传到后端
- 但引擎检测到 `_pos_mode='fixed_capital'` → **不使用 `_risk_pct` 变量**
- 所以显示1%但实际不影响Fixed Capital计算

### 评价: **这是一个UX缺陷, 不是逻辑错误。**

**建议修复**:
```python
# 方案A: Fixed Capital模式下隐藏该input
if use_fixed_risk:
    risk_per_trade = c1.number_input("单笔风险占比%", ...)
else:
    risk_per_trade = 1.0  # 不使用, 隐藏

# 方案B: 显示 "N/A (仅Fixed Risk模式使用)"
c1.metric("单笔风险占比%", "N/A" if not use_fixed_risk else f"{risk_per_trade}%")
```

---

# 4. 风控系统

## 4.1 五层风控架构

| 层级 | 机制 | 触发条件 | 行为 | 代码位置 |
|------|------|---------|------|---------|
| L1 | 连亏锁仓 | 连续触发止损N次 | 锁仓M根K线不开新仓 | L866-869 |
| L2 | 单笔止损 | 价格触及sl_price | 平仓, 计入losestreak | L798-814 |
| L3 | 移动止损 | 浮盈≥2×trailing_pct触发激活 | 止损位跟涨保护 | L817-834 |
| L4 | 爆仓保护 | 保证金率<维持保证金率(0.5%) | 强制归零平仓 | L837-851 |
| L5 | 仓位上限 | notional > equity × 5 | 缩减至上限 | L1171-1179 |

## 4.2 ATR入场止损

**确认: ATR止损在开仓瞬间固定, 持仓期间不按每根K线重新计算。**

```python
# engine_core.py _open() L1232-1245
# 入场时一次性计算:
if hasattr(self, '_use_atr_sl') and self._use_atr_sl:
    atr_val = atr_value if (atr_value and atr_value > 0) else fill_price * 0.01
    atr_mult = getattr(self, '_atr_mult', 2.0)
    if side == 'LONG':
        sl_price = fill_price - atr_val * atr_mult  # 一次定价
    else:
        sl_price = fill_price + atr_val * atr_mult
    # sl_price 存入 position dict, 不再更新

# engine_core.py _check_positions() L793
# 持仓期间只读取固定sl_price:
sl_px = pos['sl_price']   # 只读不写
# ATR不参与 _check_positions 任何逻辑

# 代码溯源结果:
# _check_positions() 中引用 sl_price: 1次 (只读)
# _check_positions() 中涉及 ATR:    否
# _open() 中 sl_price 赋值:         6次 (入场设定)
```

**ATR参数**:
- 周期: 14 (ATR(14))
- 倍数: 默认2.0
- 计算方式: `tr.ewm(span=14).mean().shift(1)` (不包含当前bar)
- 备选: `atr_period_val` + `atr_mult_val` 均可在UI修改

**如果用户想要"跟随ATR变化自动调整止损"**: 系统不支持。替代方案是使用 `trailing_pct` (移动止损), 这是当前唯一持仓期间动态调整止损的机制。

## 4.3 移动止损

```python
# 激活条件:
# LONG: highest_price > entry × (1 + trailing_pct × 2)
# SHORT: lowest_price < entry × (1 - trailing_pct × 2)

# 触发条件:
# LONG: bar_low <= highest_price × (1 - trailing_pct) 且 trailing_activated
# SHORT: bar_high >= lowest_price × (1 + trailing_pct) 且 trailing_activated
```

**风险**: 默认 `trailing_pct=0` → 移动止损关闭。需要用户显式设置。

## 4.4 连亏锁仓 (断路器)

```python
# engine_core.py L860-869
# 只计算 "触发止损" 的亏损:
if margin_pnl <= -self.sl_pct:   # margin_pnl是保证金收益率
    self.losestreak += 1
    if self.losestreak >= self.lock_streak:  # 默认3次
        self.lock_until = bar_idx + self.lock_bars  # 默认12根4H=2天
        self.losestreak = 0
else:
    self.losestreak = 0  # 盈利(包括TP/EOD/微亏) → 重置计数
```

**已知问题**: 只有 `margin_pnl ≤ -sl_pct` 才计入连亏。一笔微亏(EOD平仓-2%)不会触发计数累加。这意味着可能出现连续小亏但不触发断路器的场景。是否需要将"任何亏损"都计入连亏？这是一个产品决策。

## 4.5 最大回撤限制

**当前状态: 没有硬性最大回撤限制。** 系统只有 `max_drawdown` 计算（事后统计），没有事前拦截。

## 4.6 仓位上限计算

```python
# _open() L1171-1179 (Block 3修复)
max_notional = self.equity * self.max_notional_pct  # 默认5倍
total_notional = existing_notional + notional        # 已有+新增
if total_notional > max_notional:
    notional = max(0, max_notional - existing_notional)
    margin = notional / lev
```

**重要**: `existing_notional` 由 `_check_pyramiding()` 计算（同向所有持仓的名义价值之和），传递给 `_open()`。确保每次金字塔加仓检查的是累计仓位而非单次。

---

# 5. 金字塔加仓系统

## 5.1 触发条件

```python
# engine_core.py _check_pyramiding() L948-974
必须同时满足:
  1. _enable_pyramiding = True         # UI开关
  2. 每个 position 独立维护 pyramid_count < max_pyramid_count  # 未达最大加仓次数(默认3)
  3. 同向持仓浮盈 ≥ _pyr_trigger_pct   # 默认2%加权均价涨幅
  4. 不在锁仓期内

浮盈判断:
  LONG:  current_price >= avg_entry × (1 + trigger_pct/100)
  SHORT: current_price <= avg_entry × (1 - trigger_pct/100)
```

## 5.2 加仓比例

```python
add_margin = self.equity * self._pyr_add_pct   # 默认50%权益
add_notional = add_margin * lev                # 3x杠杆 → 150%权益
```

**问题**: `_pyr_add_pct` 默认0.5(50%)过大。3x杠杆下每次加仓名义价值是权益的150%。初始仓位在牛市100%→300%名义, 第一次加仓150%→累计450%, 第二次150%→600%（超出5倍上限, 会被Block 3机制缩减到500%）。

## 5.3 最大加仓次数

- `_pyr_max`: 默认3次 (UI可配置)
- 包括初始开仓, 总共最多 `1 + _pyr_max` = 4个仓位段

## 5.4 加仓后成本计算

```python
# L976-978: 按名义价值加权平均
total_n = total_notional + add_notional
new_avg = (total_notional * avg_entry + add_notional * px) / max(total_n, 1)
# 所有同向仓位(包括旧的+刚开的)都更新为 new_avg
```

**问题**: 加仓后的TP/SL基于 `new_avg` 重新计算, 但使用的是初始的 `tp_pct` 和 `sl_pct`。如果初始仓位已经浮盈较多, 加仓后的新均价会使得止损距离相对初始仓位变小——更紧的止损可能更容易触发。

## 5.5 是否可能无限增加仓位?

**不会。** 三层限制:
1. `_pyr_max` 限制加仓次数 (默认3次)
2. `max_notional_pct` 限制累计名义仓位 (默认5倍权益)
3. 当 `notional` 达到上限后, `_open()` 将 `notional` 缩减至 `max(0, max_notional - existing_notional)`, 如果已满则归零

---

# 6. 回测真实性审计

## 6.1 已修复问题

| 问题 | 严重度 | 修复日期 | 修复内容 |
|------|--------|---------|---------|
| Fixed Risk 单位一致性 | P0 | 2026-08-10 | 插入 `notional = units × fill_price`, 修复units被当USD的bug |
| _pyr_init_pct覆盖regime_alloc | P0 | 2026-08-10 | 删除覆盖语句, regime_alloc直接由市场状态决定 |
| TP/SL定义模糊 | P0 | 2026-08-10 | 新增 margin_pct/price_pct 双模式 + UI选择器 |
| ATR静态预设值 | P0 | 2026-08-10 | ATR(14)每bar预计算 + shift(1)防未来 |
| 仓位无累计上限 | P0 | 2026-08-10 | `existing_notional`参数, 累计检查 |
| 杠杆无上限保护 | P0 | 2026-08-10 | leverage>125时抛异常 |
| ATR"动态止损"命名误导 | P1 | 2026-08-10 | 全部重命名为"ATR入场止损", 明确trailing_pct为唯一动态机制 |

## 6.2 仍然存在的问题

### 6.2.1 手续费 (真实性: 中)

```python
TAKER_FEE = 0.0005  # 0.05%, 固定值
# 开仓收一次, 平仓收一次 → 双边0.10%
```

**问题**:
- 真实交易所费率因VIP等级/交易量而异 (0.02%~0.05%)
- 没有Maker费率 (限价单成交=更低费率)
- 资金费率固定0.01% (真实费率动态变化)
- **影响**: 对高频策略影响大; 对4H低频策略影响有限

### 6.2.2 滑点模型 (真实性: 低)

```python
SLIPPAGE = 0.0002  # 0.02%, 固定值
```

**问题**:
- 真实滑点随市场波动/深度/订单大小变化
- 没有区分不同币种的流动性差异 (BTC vs SOL)
- 大仓位时0.02%滑点严重低估
- Fixed Risk模式在牛市可能算出大仓位 → 实际滑点远超0.02%
- **影响**: 牛市大仓位回测结果过于乐观

### 6.2.3 资金费率 (真实性: 低)

```python
# L743-751: 固定费率, 简单结算
if self.leverage > 1 and i % 2 == 0:  # 每2根4H=8h
    funding_fee = pos['notional'] * 0.0001  # 固定0.01%
    if side == 'LONG':
        self.equity -= funding_fee
    else:
        self.equity += funding_fee * 0.5  # 保守估计
```

**问题**:
- 费率固定0.01%, 真实费率每8h动态变化 (-0.1% ~ +0.5%)
- 做空收入打5折 (`* 0.5`) 是不精确的估计
- 没有历史资金费率数据 → 无法准确回测
- **影响**: 永续合约策略的holding成本被简化, 极端行情下费率吃掉所有利润的场景未被测试

### 6.2.4 流动性假设 (真实性: 低)

**问题**:
- 假设任何仓位都能以 fill_price 成交
- 没有订单簿深度检查
- 没有市场冲击成本模型
- 大仓位 (>50000U notional) 在低流动性时段可能无法全部成交
- **影响**: 大资金回测结果的可靠性存疑

### 6.2.5 样本外测试 (OOS)

**状态**: UI有开关, 但当前测试未启用
```python
# 每次回测使用全部数据 (2017-2026)
# 没有固定的训练/测试集分割
```

**问题**:
- 8.69年全部用于训练/回测 → 存在参数过拟合风险
- 两个策略(MACross/OversoldBounce)的参数都在全数据集上优化过
- **影响**: 夏普比率和收益可能高估了真实表现

### 6.2.6 参数过拟合

**当前状态**: 大量可调参数, 但没有系统的参数敏感性分析:
- EM快线(5)/慢线(20)/趋势(50)
- regime权重(EMA 40%/ADX 35%/Funding 25%)
- 牛/震/熊阈值(±30%)
- TP/SL 模式 + 百分比
- ATR周期/倍数
- 金字塔参数(触发/加仓比例/最大次数)
- lock_streak/lock_bars/cooldown_bars

**问题**: 没有Walk-Forward分析, 没有参数稳定性测试。641笔交易的结果可能是参数过拟合的产物。

### 6.2.7 连亏锁仓的逻辑缺陷

```python
# L865: 只有 margin_pnl ≤ -sl_pct 才累积losestreak
if margin_pnl <= -self.sl_pct:  # 触发止损
    self.losestreak += 1
```

**问题**: 微亏平仓(如EOD -1%)不触发计数。连续10笔微亏不会触发断路器。
**建议**: 改为 `if margin_pnl < 0` 或可配置的阈值。

### 6.2.8 金字塔加仓的 regime 不一致

**问题**: `_check_pyramiding()` 传递 `alloc=self._pyr_add_pct` 给 `_open()`, 这个值不随市场状态变化。
- 初始仓位: regime=range → alloc=0.5 (50%保证金)
- 第一笔加仓: 无视regime → add_margin = 50%权益 (始终0.5)
- **影响**: 震荡/熊市期间加仓行为与初始仓位不成比例

---

# 7. 当前版本风险评级

## P0 (严重影响回测真实性 — 必须修复才能使用)

| # | 问题 | 影响 | 状态 |
|---|------|------|:---:|
| 1 | Fixed Risk 单位一致性 | units被当作USD, 保证金计算错误 | ✅ 已修复 |
| 2 | _pyr_init_pct覆盖regime_alloc | Fixed Capital下仓位不随市场状态变化 | ✅ 已修复 |
| 3 | 金字塔累计仓位缺乏上限 | 多次加仓可能无限制膨胀 | ✅ 已修复 |
| 4 | 滑点模型过于简单 | 大仓位回测结果过于乐观 | ❌ 待修复 |
| 5 | 资金费率使用固定值 | 永续合约成本模拟失真 | ❌ 待修复 |

## P1 (影响实盘可靠性 — 建议修复后再实盘)

| # | 问题 | 影响 | 状态 |
|---|------|------|:---:|
| 1 | 无样本外测试流程 | 可能存在参数过拟合 | ❌ 待修复 |
| 2 | 连亏锁仓只统计触发SL的亏损 | 微亏不触发断路器 | ❌ 待修复 |
| 3 | 金字塔加仓不尊重regime | 震荡/熊市加仓比例与初始仓位不一致 | ❌ 待修复 |
| 4 | ATR止损固定, 无法跟K线更新 | 入场后波动放大→止损保护不足 | ⚠️ 设计如此 |
| 5 | 无最大回撤硬限制 | 回撤可达90%+ | ❌ 待修复 |
| 6 | 流动性假设: 任何仓位都能成交 | 大资金实盘不可靠 | ❌ 待修复 |

## P2 (优化体验 — 不影响功能正确性)

| # | 问题 | 影响 | 状态 |
|---|------|------|:---:|
| 1 | Fixed Capital模式下"单笔风险占比%"锁定为1%显示 | UX混淆 | ❌ 待修复 |
| 2 | 没有回测进度条 | 长回测(>3s)用户体验差 | ❌ 待修复 |
| 3 | GBK编码问题 | 中文终端输出乱码 | ❌ 待修复 |
| 4 | 没有参数敏感性分析工具 | 调参效率低 | ❌ 待修复 |
| 5 | 没有Walk-Forward自动化 | 无法验证参数稳定性 | ❌ 待修复 |
| 6 | PortfolioManager模块未完成集成 | 多腿仓位管理功能不完整 | ⚠️ 开发中 |

---

# 8. 产品经理总结

## 8.1 当前平台阶段

**平台处于: B阶段 (研究阶段) — 可用于策略研究，尚未达到实盘准备。**

具体特征:
- ✅ 回测引擎核心逻辑正确 (8.69年历史数据, 防未来函数到位)
- ✅ 两种仓位模式工作正常 (Fixed Capital + Fixed Risk)
- ✅ 风控框架完整 (止损/爆仓/锁仓/移动止损/仓位上限)
- ✅ P0阻塞项全部修复, 单元测试通过
- ⚠️ 交易成本模拟过于简化 (固定手续费+固定滑点+固定资金费率)
- ⚠️ 无样本外验证框架
- ❌ 无流动性模型
- ❌ 无实盘接口
- ❌ 无订单管理系统

## 8.2 距离真实交易还缺少什么

| 缺失项 | 重要性 | 预估工作量 | 说明 |
|--------|:---:|:---:|------|
| 历史资金费率数据 | 高 | 2天 | 导入历史funding rate, 替代固定0.01% |
| 动态滑点模型 | 高 | 3天 | 基于波动率+仓位的滑点估计 |
| Walk-Forward分析 | 高 | 5天 | 样本外测试 + 参数稳定性验证 |
| 实盘交易接口 | 必须 | 10天+ | OKX API v5 对接 |
| 订单管理系统 | 必须 | 10天+ | 限价单/市价单/冰山单/止损单 |
| 实时风控 | 必须 | 5天 | 与回测风控一致, 增加实时监控 |
| 仓位同步 | 必须 | 3天 | 回测信号→实盘执行→状态同步 |

## 8.3 回测数据实况

```
P0修复后 正式回测结果 (ETH 4H, 2017.08~2026.08, 3x杠杆):

MACrossStrategy Fixed Capital:
  交易641笔, 总收益-68.76%, 最大回撤90.93%, 夏普-0.12
  → 无法实盘使用。最大回撤90%不可接受。

MACrossStrategy Fixed Risk (1%风险/笔):
  交易641笔, 总收益-12.30%, 最大回撤35.89%, 夏普-0.14
  → 回撤显著改善但仍为负收益, 不可实盘使用。

OversoldBounceStrategy Fixed Capital:
  交易274笔, 总收益-11.92%, 最大回撤54.11%, 夏普0.04
  → 夏普接近正值, 但回撤过大。
```

## 8.4 诚实评估

**当前平台的设计缺陷和局限性 (不隐藏)**:

1. **策略本身盈利能力未验证**: 两个内置策略在8年回测中均无法实现正收益。这可能是参数问题, 也可能是策略逻辑本身在4H周期上无alpha。

2. **成本模型乐观**: 固定0.02%滑点 + 0.05%手续费低估了真实交易成本。在扣除更真实的成本后, 回测结果会更差。

3. **无参数稳定性证据**: 没有Walk-Forward分析, 无法证明当前参数不是过拟合产物。

4. **仓位模型缺少实盘考量**: Fixed Risk在大波动时会给出极大仓位, 虽然5倍上限保护存在, 但在真实市场中的执行可行性未验证。

5. **没有多币种联合回测**: 当前只测试了ETH单币。BTC/SOL的数据已加载, 但多币轮动的实际表现未评估。

**总结**: 平台作为**策略研究工具是合格的**, 核心回测逻辑正确且防未来函数。但距离**实盘交易**, 还需要在成本模型、样本外验证、实盘接口三个方向上完成建设。当前任何回测结果不应直接用于实盘资金决策。
