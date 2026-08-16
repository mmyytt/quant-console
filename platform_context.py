"""
平台能力地图（Platform Context）
============================================================
从 indicator_schema.INDICATOR_SCHEMA 自动读取指标元数据，汇总平台能力，
生成 platform_context.json 结构，供 Quant Research Agent 每次对话自动加载。

外挂模块：只读，不 import app.py（避免触发 Streamlit 副作用），不触碰交易核心。
"""
import json
import os

from indicator_schema import INDICATOR_SCHEMA

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _indicator_list():
    """从 INDICATOR_SCHEMA 自动提取指标能力（不含 compute lambda，仅元数据）。"""
    out = []
    for key, s in INDICATOR_SCHEMA.items():
        out.append({
            "key": key,
            "name": s["name"],
            "category": s["category"],
            "desc": s["desc"],
            "params": {
                pk: {"label": pv["label"], "default": pv["default"], "min": pv["min"], "max": pv["max"]}
                for pk, pv in s["params"].items()
            },
        })
    return out


# 平台稳定能力（镜像 config.json / data_loader / app.py 侧栏，非交易逻辑）
_ASSETS = ["ETH", "BTC", "SOL"]
_TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]
_CATEGORIES = sorted({s["category"] for s in INDICATOR_SCHEMA.values()})


def build_platform_context() -> dict:
    return {
        "indicators": _indicator_list(),
        "strategies": {
            "combination_modes": ["AND（全部满足）", "OR（任一满足）", "加权（weighted）"],
            "directions": ["双向", "仅做多", "仅做空"],
            "categories": _CATEGORIES,
        },
        "risk_controls": {
            "position_modes": ["fixed_risk（按单笔风险比例）", "fixed_capital（按资金比例）", "dynamic_stop（动态止损）"],
            "tp_sl_modes": ["margin_pct（保证金收益率）", "price_pct（价格百分比）"],
            "parameters": [
                "leverage 杠杆", "tp_pct 止盈", "sl_pct 止损", "trailing_pct 移动止盈",
                "ATR 止损 (use_atr_sl / atr_period / atr_mult)",
                "max_notional_pct 最大名义敞口", "lock_streak/lock_bars 连亏锁仓",
                "cooldown_bars 冷却",
                "初始建仓比例 _init_alloc_pct (0~100%)",
                "加仓开关 _enable_pyramiding (true/false)",
                "加仓比例 _pyr_add_pct (0.1~1.0)", "最大加仓次数 _pyr_max (1~5)",
                "牛/震/熊仓位系数 _bull_alloc/_range_alloc/_bear_alloc (0~100%)",
            ],
        },
        "backtest_capabilities": {
            "assets": _ASSETS,
            "timeframes": _TIMEFRAMES,
            "validation": [
                "IS/OOS 样本内外切分", "Walk-Forward 滚动验证", "Monte Carlo 自助抽样",
                "参数敏感性扫描", "组合网格寻优",
            ],
            "safety": [
                "FutureLeakDetector 未来函数检测", "真实 OKX 资金费率", "taker 手续费 + 滑点",
                "多周期重采样 (15m/1h/4h/1d)",
            ],
        },
    }


def get_platform_context_json() -> str:
    return json.dumps(build_platform_context(), ensure_ascii=False, indent=2)


def format_context_text() -> str:
    """把平台能力地图格式化为可注入 AI 提示的文本块（研究助手 V1 的 quant_context）。"""
    ctx = build_platform_context()
    caps = ctx["backtest_capabilities"]
    rc = ctx["risk_controls"]
    lines = ["## 平台能力（quant_context，必须以此为准）",
             f"- 可交易资产: {'、'.join(caps['assets'])}",
             f"- 回测周期: {'、'.join(caps['timeframes'])}",
             f"- 指标组合方式: {'、'.join(ctx['strategies']['combination_modes'])}",
             f"- 方向: {'、'.join(ctx['strategies']['directions'])}",
             f"- 验证手段: {'、'.join(caps['validation'])}",
             "",
             "## 风控能力",
             f"- 仓位模式: {'、'.join(rc['position_modes'])}",
             f"- 止盈止损模式: {'、'.join(rc['tp_sl_modes'])}",
             f"- 风控参数: {'、'.join(rc['parameters'])}",
             "",
             "## 可用指标清单（按分类）"]
    by_cat = {}
    for ind in ctx["indicators"]:
        by_cat.setdefault(ind["category"], []).append(ind)
    for cat, inds in by_cat.items():
        lines.append(f"### {cat}")
        for ind in inds:
            params = ind["params"]
            if params:
                plist = ", ".join(
                    f"{pv['label']}={pv['default']}({pv['min']}~{pv['max']})"
                    if ("min" in pv and "max" in pv) else f"{pv['label']}={pv['default']}"
                    for pv in params.values()
                )
            else:
                plist = "无参数"
            lines.append(f"- {ind['name']}: {ind['desc']}；参数: {plist}")
    return "\n".join(lines)


def refresh_platform_context_file(path=None):
    """将能力地图落盘为 platform_context.json（供交付/调试可见）。"""
    path = path or os.path.join(_BASE_DIR, "platform_context.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(get_platform_context_json())
    return path


if __name__ == "__main__":
    print(refresh_platform_context_file())
