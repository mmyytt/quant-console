"""
统一 LLM API 客户端（从 app.py 抽离）
============================================================
call_unified_api: DeepSeek / OpenAI / Anthropic / Gemini 统一调用。
抽离原因同 strategy_models.py：业务模块禁止 import app.py。
"""
from i18n import t


def call_unified_api(messages: list, api_key: str, model_name: str, trading_notes: str) -> dict:
    import requests
    if trading_notes.strip():
        np = {"role": "system", "content": t("unified_api_prompt", notes=trading_notes.strip())}
        hs = any(m["role"] == "system" for m in messages)
        if hs:
            for m in messages:
                if m["role"] == "system": m["content"] = np["content"] + "\n\n" + m["content"]
        else: messages.insert(0, np)
    if "DeepSeek" in model_name:
        mdl = "deepseek-chat" if "V3" in model_name else "deepseek-reasoner"
        r = requests.post("https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": mdl, "messages": messages, "max_tokens": 2000, "temperature": 0.7}, timeout=45)
        if r.status_code == 200: d = r.json(); return {"success": True, "content": d["choices"][0]["message"]["content"], "model": d.get("model", mdl)}
        return {"success": False, "error": f"DeepSeek {r.status_code}: {r.text[:200]}"}
    if "OpenAI" in model_name or "GPT" in model_name:
        mdl = "gpt-4o" if "mini" not in model_name else "gpt-4o-mini"
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": mdl, "messages": messages, "max_tokens": 2000}, timeout=45)
        if r.status_code == 200: d = r.json(); return {"success": True, "content": d["choices"][0]["message"]["content"], "model": d.get("model", mdl)}
        return {"success": False, "error": f"OpenAI {r.status_code}: {r.text[:200]}"}
    if "Claude" in model_name or "Anthropic" in model_name:
        sm = next((m for m in messages if m["role"] == "system"), None)
        cm = [m for m in messages if m["role"] != "system"]
        body = {"model": "claude-sonnet-4-20250514", "max_tokens": 2000, "messages": cm}
        if sm: body["system"] = sm["content"]
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=body, timeout=45)
        if r.status_code == 200: d = r.json(); return {"success": True, "content": d["content"][0]["text"], "model": d.get("model", "claude")}
        return {"success": False, "error": f"Claude {r.status_code}: {r.text[:200]}"}
    if "Gemini" in model_name:
        mdl = "gemini-2.0-flash"
        contents = [{"role": "user" if m["role"] != "assistant" else "model", "parts": [{"text": m["content"]}]} for m in messages]
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"}, json={"contents": contents}, timeout=45)
        if r.status_code == 200:
            d = r.json(); txt = d["candidates"][0]["content"]["parts"][0]["text"]
            return {"success": True, "content": txt, "model": mdl}
        return {"success": False, "error": f"Gemini {r.status_code}: {r.text[:200]}"}
    return {"success": False, "error": f"Unknown model: {model_name}"}
