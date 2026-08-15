"""
AI API Key 持久化（研究助手 V1）
============================================================
用户第一次输入 DeepSeek / OpenAI API Key 后安全保存到本地 .ai_config.json（gitignored），
以后进入 AI 研究舱无需重复输入。回退顺序：本地文件 → 环境变量 AI_API_KEY。

外挂模块：无第三方依赖，不 import app.py / engine_core.py。
安全约定：Key 只落盘 .ai_config.json（gitignored），禁止写入代码或提交 GitHub。
"""
import json
import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_BASE_DIR, ".ai_config.json")

_DEFAULT_MODEL = "DeepSeek-V3 (推荐)"


def load() -> dict:
    """读取已保存的 {key, model}；文件不存在时回退环境变量 AI_API_KEY。"""
    cfg = {"key": "", "model": _DEFAULT_MODEL}
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in ("key", "model")})
        except Exception:
            pass
    if not cfg.get("key"):
        cfg["key"] = os.environ.get("AI_API_KEY", "")
    return cfg


def save(key: str, model: str = _DEFAULT_MODEL) -> None:
    """保存 {key, model} 到 .ai_config.json（覆盖旧值）。"""
    data = {"key": key or "", "model": model or _DEFAULT_MODEL}
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
