# Fibonacci 回看周期参数链路检查报告

> 任务：排查「schema 已扩到 500，但 Streamlit 页面仍限制 200」的遗漏。
> 约束：不改变任何交易数学逻辑。

---

## 一、核心结论

**代码中不存在硬编码 200 限制。** 全链路（Schema → Registry → UI → 鲁棒性实验室 → AI 研究）均已正确使用 `max=500`。页面仍显示 200 的**最可能原因是陈旧部署/缓存**（上一个 commit `d3dea4a` 已把上限改为 500，但 Streamlit Cloud 或本地会话尚未重载新代码）。

已做一处防漂移加固：鲁棒性实验室的 Fibonacci 扫描值从「硬编码列表」改为「从 Schema 派生」，确保未来再调整 Schema 时扫描范围自动同步，杜绝此类「Schema 与页面/网格不一致」的隐患。

---

## 二、逐项检查结果

### 1. app.py（UI 输入控件）— ✅ 无硬编码 200

指标参数输入控件位于 [app.py:516-521](app.py#L516-L521)（主指标）与 [app.py:550-553](app.py#L550-L553)（共振因子）：

```python
val = cols[i % 2].number_input(
    label, pdef["min"], pdef["max"],   # ← 直接读 schema/registry，非硬编码
    sel[name]["params"].get(pk, pdef["default"]),
    pdef["step"], key=f"p_{name}_{pk}", ...)
```

- `pdef` 来自 `INDICATOR_REGISTRY`，而 Registry 由 `indicator_schema.py:524-544` 从 `INDICATOR_SCHEMA` 自动生成（复制 `min/max/step`）。
- 运行时实测 `INDICATOR_REGISTRY['斐波那契回调']['params']['FIB_lookback']['max'] == 500`。
- 全文件 `grep '\b200\b'` 仅命中：仓位分配 `0,200`（bull/range/bear 分配）、图表高度 200、warmup=200、`len(df)<200` 判据、`_get_max_period` 的 200 下限 —— **均与 Fibonacci 输入无关**。
- 无 `number_input/slider/selectbox` 对 Fibonacci 回看做 200 上限。

### 2. robustness_lab.py — ✅ 已改为统一 Schema（本次加固）

- `SWEEP_DIMENSIONS['fibonacci']['values']` 原为硬编码 `[50,...,500]`（本就含 300/400/500，无 200 上限）。
- 本次改为从 Schema 派生：`list(range(FIB_min, FIB_max+1, FIB_step))` → `[50,100,...,500]`，**值不变、来源统一**。
- `PARAM_COMBO_GRID['fibonacci']['values'] = [50, 200, 300, 500]` 为**精心挑选的性能子集**（非上限），含 300/500，无 200 天花板。
- `volume_ma` 维度 `[10,20,30,50,70,100]` 亦在 Schema 10~100 范围内。

### 3. research_loop.py / AI 研究仓 — ✅ 可产生 300/400/500

- `_param_sample_values` / `expand_parameter_grid` 直接读 `INDICATOR_SCHEMA` 的 `min/max`，范围扩展后 AI 采样自动覆盖 50~500。
- 上一轮已修复 `_coerce` / `_param_type_int`：整型参数保持整型，`rolling(300/400/500)` 不再因 `300.0` 崩溃被 `try/except` 静默吞掉。
- 实测采样值落 50~500 且为整型。

### 4. 全项目搜索 — ✅ 无隐藏 200 限制

关键词 `200 / fib / fibonacci / lookback / FIB_lookback` 全量搜索（排除 backup/测试脚本），仅命中：
- `indicator_schema.py:425` FIB_lookback max=500（唯一定义点）；
- 其他 `200` 均为仓位分配、EMA_long 上限、CCI 阈值、空头比例 200 根、蒙特卡洛 n_boot=200 等**无关用途**。

---

## 三、修改文件

| 文件 | 改动 |
|------|------|
| `robustness_lab.py` | Fibonacci 扫描值改为从 `INDICATOR_SCHEMA` 派生（`_FIB_SWEEP`），消除硬编码列表 |
| `test_fib_range.py` | 新增：FIB=200/300/400/500 全链路验证 |

未改：`app.py`（本已正确）、`indicator_schema.py`（本已 max=500）、`research_loop.py`、交易数学逻辑。

---

## 四、测试结果

### 新增 `test_fib_range.py` — **14 PASS, 0 FAIL**
```
[PASS] schema FIB_lookback.max == 500
[PASS] registry FIB_lookback.max == 500
[PASS] lookback=200 真实计算, 信号数=508 (非静默跳过)
[PASS] lookback=300 真实计算, 信号数=442 (非静默跳过)
[PASS] lookback=400 真实计算, 信号数=386 (非静默跳过)
[PASS] lookback=500 真实计算, 信号数=300 (非静默跳过)
[PASS] 不同 lookback 信号数不同
[PASS] FIB=200 回测成功 (ret=+4.48%)
[PASS] FIB=300 回测成功 (ret=-23.86%)
[PASS] FIB=400 回测成功 (ret=-17.27%)
[PASS] FIB=500 回测成功 (ret=-12.58%)
[PASS] FIB 200/300/400/500 结果不同
[PASS] fibonacci sweep 派生自 schema
[PASS] sweep 最大值为 500
```

### 回归测试
| 测试 | 结果 |
|------|------|
| `py_compile`（5 个模块） | ✅ OK |
| `test_param_extension.py` | ✅ 14 PASS |
| `test_engine_fix.py` | ✅ PASS |
| `test_pyramiding_state_upgrade.py` | ✅ PASS |
| `test_research_search.py` | ✅ PASS |
| `test_no_ui_import.py` | ✅ PASS |

---

## 五、确认项

| 项 | 结论 |
|----|------|
| Fibonacci=200 可运行 | ✅ 信号 508，ret +4.48% |
| Fibonacci=300 可运行 | ✅ 信号 442，ret -23.86% |
| Fibonacci=400 可运行 | ✅ 信号 386，ret -17.27% |
| Fibonacci=500 可运行 | ✅ 信号 300，ret -12.58% |
| 指标真实计算 | ✅ 信号数随 lookback 单调递减（回看越长信号越少，符合逻辑） |
| 回测结果变化 | ✅ 四组收益互不相同 |
| 无静默跳过 | ✅ 每组均产生 >0 信号，未被 `try/except` 吞掉 |

---

## 六、给用户的行动建议（页面仍显示 200 时）

代码已正确。若页面仍显示 200，请：
1. **Streamlit Cloud**：在应用面板点「Redeploy / 重启应用」拉取最新 commit `d3dea4a`（及本次提交）。
2. **本地运行**：Ctrl+C 停止后重新 `python -m streamlit run app.py`（模块级 `INDICATOR_REGISTRY` 在进程启动时构建，需重启进程才生效，浏览器刷新不够）。
3. 若仍异常，清浏览器缓存 / 无痕窗口。
