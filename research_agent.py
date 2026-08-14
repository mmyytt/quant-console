"""
Quant Research Agent 系统提示与记忆加载
============================================================
把「平台能力地图 + 研究记忆」注入 AI 系统提示，让对话舱升级为研究智能体。
纯组装逻辑：只读 platform_context + research_storage，不调回测、不碰交易核心。
"""
import json

from platform_context import build_platform_context
from research_storage import db


def _fmt_indicator_ctx(platform_ctx) -> str:
    lines = []
    by_cat = {}
    for ind in platform_ctx["indicators"]:
        by_cat.setdefault(ind["category"], []).append(ind)
    for cat, inds in by_cat.items():
        lines.append(f"【{cat}】")
        for ind in inds:
            params = ", ".join(f"{pv['label']}={pv['default']}" for pv in ind["params"].values()) or "无参数"
            lines.append(f"  - {ind['name']}（{ind['desc']}）参数: {params}")
    return "\n".join(lines)


def _fmt_memory_ctx(memory) -> str:
    lines = []
    hyp = memory.get("hypotheses") or []
    if hyp:
        lines.append("【已研究假设】（避免重复研究）")
        for h in hyp[:15]:
            rel = ""
            if h.get("related_indicators"):
                try:
                    rel = " 指标: " + ", ".join(json.loads(h["related_indicators"]))
                except Exception:
                    pass
            lines.append(f"  - [{h['status']}] {h['hypothesis_text']}{rel}")
    exp = memory.get("experiments") or []
    if exp:
        lines.append("【已测试策略实验】")
        for e in exp[:10]:
            lines.append(
                f"  - {e['strategy_name'] or '未命名'} | {e['asset']} {e['timeframe']} | "
                f"收益 {_f(e['total_return'])}% | Sharpe {_f(e['sharpe'])} | MDD {_f(e['max_drawdown'])}% | "
                f"评级 {e['final_rating'] or '-'}"
            )
    strat = memory.get("strategies") or []
    if strat:
        lines.append("【策略库】")
        for s in strat[:5]:
            lines.append(f"  - {s['name']} [{s['status']}]")
    fail = memory.get("failure_memory") or []
    if fail:
        lines.append("【失败研究记忆】（禁止重复这些方向）")
        for f in fail[:10]:
            ic = ""
            if f.get("indicator_combination"):
                try:
                    ic = " 指标: " + ", ".join(json.loads(f["indicator_combination"]))
                except Exception:
                    pass
            lines.append(f"  - {f.get('fingerprint') or f.get('strategy_name') or '未命名'}{ic}"
                         f"｜失败原因: {f.get('failure_reason') or '-'}")
    if not (hyp or exp or strat or fail):
        lines.append("（尚无研究记录。首次研究请从用户目标出发提出假设。）")
    return "\n".join(lines)


def _f(v, nd=2):
    return "0" if v is None else f"{v:.{nd}f}"


def build_system_prompt(platform_ctx=None, memory=None, trading_notes=""):
    """生成 Quant Research Agent 系统提示。"""
    platform_ctx = platform_ctx or build_platform_context()
    memory = memory or db.memory_summary()

    assets = "、".join(platform_ctx["backtest_capabilities"]["assets"])
    timeframes = "、".join(platform_ctx["backtest_capabilities"]["timeframes"])
    modes = "、".join(platform_ctx["strategies"]["combination_modes"])
    validations = "、".join(platform_ctx["backtest_capabilities"]["validation"])

    prompt = f"""你是 QuantCode 的量化研究智能体（Quant Research Agent），协助用户在平台内完成策略研究。

## 平台能力（必须以此为准，不要臆造平台不支持的指标/能力）
- 可交易资产: {assets}
- 回测周期: {timeframes}
- 指标组合方式: {modes}
- 验证手段: {validations}

## 可用指标库（从 INDICATOR_SCHEMA 自动读取）
{_fmt_indicator_ctx(platform_ctx)}

## 研究记忆（历史已做过的研究，禁止重复研究相同假设）
{_fmt_memory_ctx(memory)}

## 工作准则
1. 数据负责证明，你负责解释：所有量化结论必须来自平台回测/验证结果，禁止编造数字。
2. 禁止为了得到漂亮结果而不断改参数（参数偷窥）；如需寻优，必须说明多重检验风险。
3. 提出新假设前，先核对上方「研究记忆」，避免重复研究相同假设。
4. 建议的假设格式：明确「指标/逻辑 + 资产 + 周期 + 方向」，并说明为什么可能有效。
{trading_notes}
"""
    return prompt


def load_memory_summary() -> dict:
    """加载研究记忆聚合（供页面展示 + 系统提示）。"""
    return db.memory_summary()


def memory_stats(memory) -> dict:
    counts = memory.get("hypothesis_counts", {})
    return {
        "hypotheses": sum(counts.values()),
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0),
        "pending": counts.get("new", 0) + counts.get("testing", 0),
        "experiments": len(memory.get("experiments", [])),
        "strategies": len(memory.get("strategies", [])),
    }
