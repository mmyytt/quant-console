"""
AI 研究舱 双模块清晰度 UI 测试
==============================
验证产品重构目标（用户进入 AI 研究舱后第一眼能区分）：
  1. 两个模块标题（策略发现 / 策略验证）均渲染，且文字不同
  2. 两个输入框作用不同：搜索目标 key=rl_search_goal，验证假设 key=rl_goal，label 不同且非空
  3. 每个模块顶部有引导文案（caption），帮助用户区分「找策略」vs「验证策略」
运行: python test_ui_two_modules.py
"""
import os
import sys
import tempfile


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

    import i18n
    import research_storage.db as db

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("app.py", default_timeout=60)

    _orig_path = db.DB_PATH
    try:
        tmp = tempfile.mkdtemp()
        db.DB_PATH = os.path.join(tmp, "two_modules.db")
        db.init_db()
        at.session_state["logged_in"] = True
        at.session_state["active_tab"] = "AI 对话舱"
        at.run()
        errs = list(getattr(at, "exception", []) or [])
        assert not errs, f"AI 研究舱渲染异常: {errs}"

        # 1) 两个模块标题均渲染且文字不同
        subs = [str(s.value) for s in getattr(at, "subheader", []) or []]
        d_title = i18n.t("rl_module_discovery")
        v_title = i18n.t("rl_module_verify")
        assert d_title in subs, f"缺少「策略发现」模块标题（现有 subheader: {subs}）"
        assert v_title in subs, f"缺少「策略验证」模块标题（现有 subheader: {subs}）"
        assert d_title != v_title, "两个模块标题应不同"
        print(f"[OK] 1. 双模块标题渲染：{d_title} / {v_title}")

        # 2) 两个输入框作用不同：key 不同、label 不同且非空
        ti_keys = {str(ti.key): ti for ti in getattr(at, "text_input", []) or []}
        assert "rl_search_goal" in ti_keys, f"缺少策略发现输入框 key=rl_search_goal（现有 {list(ti_keys)}）"
        assert "rl_goal" in ti_keys, f"缺少策略验证输入框 key=rl_goal（现有 {list(ti_keys)}）"
        search_label = str(ti_keys["rl_search_goal"].label)
        verify_label = str(ti_keys["rl_goal"].label)
        assert search_label and verify_label, "两个输入框 label 均应为非空"
        assert search_label != verify_label, \
            f"两个输入框 label 应不同，避免作用混淆（当前均为 {search_label!r}）"
        assert search_label == i18n.t("research_goal_label"), \
            f"策略发现输入框 label 应为研究目标，实际 {search_label!r}"
        assert verify_label == i18n.t("rl_verify_input_label"), \
            f"策略验证输入框 label 应为策略假设，实际 {verify_label!r}"
        print(f"[OK] 2. 输入框作用区分：搜索={search_label!r}(rl_search_goal) / 验证={verify_label!r}(rl_goal)")

        # 3) 每个模块顶部有引导文案（caption），帮助区分「找」vs「验证」
        caps = [str(c.value) for c in getattr(at, "caption", []) or []]
        assert i18n.t("rl_module_discovery_hint") in caps, "缺少「策略发现」引导文案"
        assert i18n.t("rl_module_verify_hint") in caps, "缺少「策略验证」引导文案"
        print("[OK] 3. 双模块引导文案（caption）均渲染")

        print("\nALL TWO-MODULE CLARITY UI TESTS PASSED")
    finally:
        db.DB_PATH = _orig_path


if __name__ == "__main__":
    main()
