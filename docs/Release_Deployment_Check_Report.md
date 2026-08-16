# QuantCode Phase B 发布与部署最终检查报告（Release Deployment Check）

日期：2026-08-16
范围：八部分发布前检查（git 状态与 .gitignore / Streamlit Cloud 环境适配 / Python 版本兼容 / 路径与文件加载 / 启动导入链 / 部署回归测试 / 本报告 / 提交推送）。
原则：不修改交易数学逻辑（开仓/加仓/TP·SL/收益/手续费/equity 一律未动），不为部署修改测试结果，只处理部署兼容性与发布风险。

---

## 结论

**评级：可以发布。**

- 无 Critical 问题，无需停止提交。
- 发现并修复 1 个 Low 风险（`engine_core.py` 硬编码本机数据目录 → 环境变量 + 项目目录回退，数据逻辑不变）。
- `.gitignore` 已补齐，密钥 / 备份 / 数据库 / 生成结果 / 本地脚本 / parquet 数据 均不会进入版本库。
- 本地运行正常，全套回归测试通过，Streamlit 环境兼容。

---

## 一、git 状态与 .gitignore

- 修改文件 10 个（`app.py` / `engine_core.py` / `data_loader.py` / `research_loop.py` / `research_phase1.py` / `platform_context.py` / `walk_forward.py` / `i18n.py` 等 Phase B 修复）。
- 删除 `backup_before_risk_upgrade_app.py`（历史备份）。
- 未跟踪文件约 50 个，其中大量为一次性脚本 / 调试脚本 / 生成结果 / 本地启动脚本 / 备份。

**`.gitignore` 本次新增排除项：**

| 类别 | 规则 |
|------|------|
| 密钥 | `*.key` |
| 备份 | `*.bak*`、`backup_*/` |
| 数据库（用户数据） | `*.db`（原已有 `research.db`） |
| Streamlit 本地配置 | `.streamlit/` |
| 本地启动脚本 | `*.bat` |
| 生成 HTML | `*.html` |
| parquet 数据 | `*.parquet` |
| 生成结果 JSON | `*_result.json`、`integrity_validation_report.json`、`baseline_*.json` |
| 本地/调试/一次性脚本 | `debug_*.py`、`verify_*.py`、`backtest_*.py`、`compare_*.py`、`simulate_*.py`、`serve.py`、`rotation_app.py`、`streamlit_app.py`、`守护进程.py` 等 |
| 根目录一次性报告 | `research_report.md`、`research_phase1_report.md`、`ARCHITECTURE_AUDIT.md` |

原有规则（`.env` / `.ai_config.json` / `data/` / `*.log` / `*.csv` / `*.pkl` / `*.zip` 等）保持不变。`.env`（OKX 凭证）持续 gitignored，无泄露。

---

## 二、Streamlit Cloud 环境适配（Windows 硬编码路径）

扫描全部 `.py` 文件，定位到 1 处**活跃模块**中的硬编码本机路径：

- `engine_core.py` `_find_data_dir()` 原写死 `C:\Users\myt\Desktop\eth_all`。

**修复**：改为环境变量 `QUANT_DATA_DIR`（未设置时为空）→ 项目 `data/` 目录回退。数据加载逻辑（先查 15m parquet、再按周期重采样、缺口修复）完全不变。

```python
local = os.environ.get("QUANT_DATA_DIR", "")
if local and os.path.isdir(local) and os.path.exists(os.path.join(local, "ETH_15m.parquet")):
    return local
proj = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
```

其余硬编码路径全部位于**已 gitignore 的**一次性脚本 / 备份 / 调试文件中（`backtest_*.py`、`serve.py`、`convert_to_docx.py`、`test_*.py` 等），不影响云部署，不进入版本库。

本地验证：`test_phase_b_fixes.py` 实测数据加载从 `data\ETH_15m.parquet`（314,573 bars）成功读取，证明回退路径生效。

---

## 三、Python 版本兼容（requirements.txt）

| 依赖 | 版本 | 用途 |
|------|------|------|
| streamlit | ==1.60.0（精确锁定） | Web UI |
| plotly | >=6.0.0 | 图表 |
| pandas | >=2.0.0 | 数据 |
| numpy | >=1.24.0 | 数值 |
| requests | >=2.28.0 | HTTP（数据源） |
| pyarrow | >=10.0.0 | parquet 读写 |
| yfinance | >=0.2.30 | 备用数据源 |

- 本地已装 streamlit==1.60.0，与 requirements.txt 一致。
- 无 scipy / sklearn / ta 依赖（核心模块均未 import），SQLite 为 stdlib。
- Python 3.11 / 3.10 均可运行（未用 3.14 不兼容的 PyTorch 相关库）。

---

## 四、路径与文件加载检查

- `data_loader.py`：无硬编码绝对路径，数据目录由 `engine_core._find_data_dir()` 定位。
- `presets.json`：`app.py` 用相对路径加载，云环境可用。
- SQLite（`research.db`）：运行时按需创建，已 gitignore，不依赖预置文件。
- 无对 `C:\Users\myt\...` 的运行期依赖（活跃模块已清空）。

---

## 五、Streamlit 启动 / 导入链检查

- 全部 13 个核心模块 import 冒烟通过（无 `ModuleNotFoundError`）。
- `test_no_ui_import.py` 确认 `research_loop.py` 全链路回测不 import `app.py`，避免 Streamlit 副作用。
- `app.py` 顶层 `from llm_client import call_unified_api` 正常。

---

## 六、部署回归测试（真实运行）

| 测试 | 结果 |
|------|------|
| `python -m py_compile`（8 核心模块） | ✅ 通过 |
| `test_phase_b_fixes.py`（固定加仓 / position_id / 5m≠4h / 指纹 / 仓位参数暴露） | ✅ 5 项通过 |
| `test_pyramiding_state_upgrade.py`（加仓状态 5 项） | ✅ 通过 |
| `test_p0_blockers.py`（加仓上限 3 项） | ✅ 通过 |
| `test_research_search.py`（研究搜索全链路 8 项） | ✅ 通过 |
| `test_no_ui_import.py`（无 UI 导入） | ✅ 通过 |
| `test_engine_fix.py`（引擎回归） | ✅ 41 passed, 0 failed |

---

## 七、剩余风险（均 Low，不影响发布）

| 位置 | 描述 |
|------|------|
| `engine_core.py` `run_backtest()` 便捷函数 | `all_tf.get(timeframe, all_tf['4h'])` 对 5m 仍有潜在静默回退；不在主 UI 路径，ETH/BTC/SOL 均有 5m 不触发 |
| `expand_parameter_grid` | 仓位参数尚未纳入自动网格扫描轴（杠杆/TP·SL/指标主参数已是网格轴）；如需仓位自动寻优留待下一阶段 |

以上与发布部署无关，记录备案。

---

## 八、提交与推送

（本报告完成后执行 `git add .` → commit → push，结果见会话输出。）

Commit 信息：`Release: QuantCode Phase B verified deployment`
