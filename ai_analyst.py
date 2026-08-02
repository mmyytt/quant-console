"""
DeepSeek AI 策略分析师模块
===========================
兼容 OpenAI SDK 格式, 使用 requests 调用
"""
import requests, json, time

DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """你是顶级量化对冲基金策略师"翔哥"。

你的任务是:
1. 基于提供的实时行情数据、技术指标状态和回测结果, 解读当前多空格局
2. 推荐最优的指标组合参数和止盈止损设置
3. 针对当前标的给出 Alpha 因子优化建议

规则:
- 用简洁专业的中文回答
- 直接给结论, 不啰嗦
- 如果回测胜率低于45%, 必须明确指出策略风险
- 给出的止盈止损建议必须是具体的百分比数字"""


def analyze(
    api_key: str,
    coin: str,
    current_price: float,
    indicators_state: dict,
    backtest_metrics: dict = None,
    timeframe: str = "4h",
    model: str = DEEPSEEK_MODEL,
    timeout: int = 30,
) -> dict:
    """
    发送分析请求给 DeepSeek。

    Args:
        api_key: DeepSeek API Key
        coin: 当前标的 (ETH/BTC/SOL)
        current_price: 最新价格
        indicators_state: 指标状态 {'EMA双均线': {'signal': '金叉', ...}, ...}
        backtest_metrics: 回测指标 {total_return, win_rate, max_drawdown, ...}
        timeframe: K线周期
        timeout: 请求超时秒数

    Returns:
        {"success": True, "content": "...", "model": "deepseek-chat"}
        或 {"success": False, "error": "..."}
    """
    if not api_key or not api_key.startswith("sk-"):
        return {"success": False, "error": "API Key 无效, 请填入以 sk- 开头的 DeepSeek Key"}

    # 构建上下文
    context = f"""【当前交易环境】
标的: {coin} | K线周期: {timeframe}
最新价格: ${current_price}

【技术指标状态】
"""
    for name, state in indicators_state.items():
        context += f"  {name}: {state}\n"

    if backtest_metrics and backtest_metrics.get("total_trades", 0) > 0:
        m = backtest_metrics
        context += f"""
【最近回测结果】
总收益: {m.get('total_return',0):+.1f}% | 年化: {m.get('annual_return',0):+.1f}%
最大回撤: {m.get('max_drawdown',0):.1f}% | 夏普: {m.get('sharpe_ratio',0):.3f}
交易数: {m.get('total_trades',0)} | 胜率: {m.get('win_rate',0):.1f}%
平均盈利: {m.get('avg_win',0):+.1f}% | 平均亏损: {m.get('avg_loss',0):+.1f}%
"""
    else:
        context += "\n【回测结果】尚未运行回测\n"

    context += "\n请给出你的分析报告(读盘 + 参数建议 + Alpha优化)。"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]

    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 1500,
                "temperature": 0.7,
            },
            timeout=timeout,
        )

        if resp.status_code != 200:
            err = resp.text[:300]
            return {"success": False, "error": f"API 返回 {resp.status_code}: {err}"}

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return {
            "success": True,
            "content": content,
            "model": data.get("model", model),
            "usage": data.get("usage", {}),
        }

    except requests.Timeout:
        return {"success": False, "error": "API 请求超时, 请稍后重试"}
    except Exception as e:
        return {"success": False, "error": f"请求异常: {type(e).__name__}: {str(e)[:200]}"}
