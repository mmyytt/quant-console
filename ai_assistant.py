"""
AI 策略助手模块 — 多模型 OpenAI 兼容协议
===========================================
支持: DeepSeek / ChatGPT / Claude / 任意 OpenAI 兼容 API
"""
import requests, json, time
from i18n import t as _t

# 模型预设
MODEL_PRESETS = {
    "DeepSeek-V3": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "endpoint": "/v1/chat/completions",
    },
    "DeepSeek-R1": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-reasoner",
        "endpoint": "/v1/chat/completions",
    },
    "OpenAI GPT-4o-mini": {
        "base_url": "https://api.openai.com",
        "model": "gpt-4o-mini",
        "endpoint": "/v1/chat/completions",
    },
    "自定义 (OpenAI兼容)": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "endpoint": "/v1/chat/completions",
    },
}

# 翔哥默认交易心法
DEFAULT_TRADING_NOTES = """我是马总，我倾向顺大势逆小势的交易风格。
我信任的指标信号：EMA金叉/死叉、成交量异动、布林带收窄突破。
我偏好的行情：震荡市高抛低吸，牛市只做多。
我厌恶的信号：无量假突破、单边市逆势抄底。
止损必须严格，亏损5%无条件离场。"""


def get_default_trading_notes() -> str:
    """返回当前语言的默认交易心法"""
    return _t("trading_notes")


def chat(
    messages: list,
    api_key: str,
    model_name: str = "DeepSeek-V3",
    custom_base_url: str = "",
    custom_model: str = "",
    trading_notes: str = "",
    timeout: int = 45,
) -> dict:
    """
    OpenAI 兼容协议对话。

    Args:
        messages: [{"role": "user"/"assistant"/"system", "content": "..."}]
        api_key: API Key
        model_name: 模型预设名称
        custom_base_url: 自定义 Base URL (仅"自定义"时使用)
        custom_model: 自定义模型名 (仅"自定义"时使用)
        trading_notes: 个人交易心法, 自动注入 System Prompt
        timeout: 超时秒数

    Returns:
        {"success": True, "content": "...", "model": "..."}
        或 {"success": False, "error": "..."}
    """
    if not api_key or len(api_key) < 10:
        return {"success": False, "error": _t("chat_need_key")}

    preset = MODEL_PRESETS.get(model_name, MODEL_PRESETS["DeepSeek-V3"])
    base_url = custom_base_url if model_name == "自定义 (OpenAI兼容)" and custom_base_url else preset["base_url"]
    model = custom_model if model_name == "自定义 (OpenAI兼容)" and custom_model else preset["model"]
    url = base_url.rstrip("/") + preset["endpoint"]

    # 注入交易心法到 system prompt
    if trading_notes.strip():
        notes_prompt = {
            "role": "system",
            "content": _t("assistant_system_prompt", notes=trading_notes.strip())
        }
        # 插入到消息列表开头 (放在已有 system prompt 之后)
        has_system = any(m.get("role") == "system" for m in messages)
        if has_system:
            # 追加到现有 system prompt
            for m in messages:
                if m["role"] == "system":
                    m["content"] = notes_prompt["content"] + "\n\n" + m["content"]
                    break
        else:
            messages.insert(0, notes_prompt)

    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 2000,
                "temperature": 0.7,
            },
            timeout=timeout,
        )

        if resp.status_code != 200:
            err_text = resp.text[:400]
            return {"success": False, "error": f"API {resp.status_code}: {err_text}"}

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return {
            "success": True,
            "content": content,
            "model": data.get("model", model),
            "usage": data.get("usage", {}),
        }

    except requests.Timeout:
        return {"success": False, "error": _t("chat_timeout", timeout=timeout)}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def build_context(coin: str, timeframe: str, price: float,
                  indicators: dict, backtest: dict = None) -> str:
    """构建发送给 AI 的实时上下文"""
    ctx = _t("ctx_market", coin=coin, timeframe=timeframe, price=price)
    for name, state in indicators.items():
        ctx += f"  {name}: {state}\n"

    if backtest and backtest.get("total_trades", 0) > 0:
        m = backtest
        ctx += _t("ctx_backtest",
                  total_return=m.get('total_return',0),
                  annual_return=m.get('annual_return',0),
                  max_drawdown=m.get('max_drawdown',0),
                  sharpe_ratio=m.get('sharpe_ratio',0),
                  win_rate=m.get('win_rate',0),
                  total_trades=m.get('total_trades',0))
    return ctx


# 快捷提问模板
QUICK_PROMPTS = [
    ("💡 行情解读", "帮我解读当前行情是否符合我的交易心法。"),
    ("🎯 共振策略", "结合我选的指标，帮我设计一套三体共振策略并讲解原理。"),
    ("❓ 假突破检测", "现在的指标数据里，有没有出现'无量洗盘'或假突破的信号？"),
    ("📊 参数优化", "根据回测结果，给出3条止盈止损参数优化建议。"),
    ("🔍 风险诊断", "扫描我当前的策略配置，指出最大的3个风险点。"),
]


def get_quick_prompts() -> list:
    """返回当前语言的快捷提问模板 [(按钮, 内容)]"""
    return [
        (_t("quick_market"), _t("quick_market_prompt")),
        (_t("quick_resonance"), _t("quick_resonance_prompt")),
        (_t("quick_fake_break"), _t("quick_fake_break_prompt")),
        (_t("quick_param_opt"), _t("quick_param_opt_prompt")),
        (_t("quick_risk_diag"), _t("quick_risk_diag_prompt")),
    ]
