"""
AI 研究舱 单一入口意图判断 回归测试
====================================
覆盖：
  1. research_loop.intent_prompt 存在且可调用（不 AttributeError）
  2. intent_prompt(goal) 返回非空 prompt（含 JSON 输出指令）
  3. parse_intent 正确解析 explore / verify（JSON 直解 + markdown 容错 + 关键词回退）
运行: python test_intent_prompt_exists.py
"""
import os
import sys


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)

    import research_loop as rl

    # 1) intent_prompt 存在且可调用（不 AttributeError）
    assert hasattr(rl, "intent_prompt"), "research_loop 缺少 intent_prompt"
    p = rl.intent_prompt("研究ETH策略")
    assert isinstance(p, str) and p.strip(), "intent_prompt 应返回非空 prompt"
    print("[OK] 1. intent_prompt 存在且可调用（无 AttributeError）")

    # 2) prompt 应指导 LLM 输出 JSON（intent + goal）
    assert "intent" in p and "explore" in p and "verify" in p, "prompt 应含 intent/explore/verify 指令"
    print("[OK] 2. intent_prompt 输出 JSON 格式指令（intent/explore/verify）")

    # 3) parse_intent：JSON 直解 / markdown 容错 / 关键词回退
    assert rl.parse_intent('{"intent": "explore", "goal": "x"}') == "explore"
    assert rl.parse_intent('{"intent": "verify", "goal": "x"}') == "verify"
    assert rl.parse_intent('```json\n{"intent": "explore", "goal": "x"}\n```') == "explore"
    assert rl.parse_intent("verify") == "verify"
    assert rl.parse_intent("随便一段话") == "explore"  # 解析失败默认 explore（更安全）
    print("[OK] 3. parse_intent：JSON / markdown / 关键词 三种路径解析正确，失败默认 explore")

    print("\nALL INTENT-PROMPT TESTS PASSED")


if __name__ == "__main__":
    main()
