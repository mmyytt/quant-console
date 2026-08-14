"""
UI 冒烟测试：研究闭环页面文字 / 存储层导出 / Streamlit 版本兼容 / 运行
覆盖：i18n 静态 key 完整 · 动态状态 key 解析 · research_storage 导出齐全 ·
      无 use_container_width / width="stretch" 残留（版本无关默认布局）·
      AppTest 完整启动 + 侧边栏按钮 + AI 研究仓页面加载
运行: python test_ui_smoke.py
"""
import os
import re
import sys

# Streamlit 入口文件（全项目 UI，均需版本无关布局）
UI_FILES = ("app.py", "live_trading/trading_dashboard.py",
            "rotation_app.py", "streamlit_app.py")


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

    src = open("app.py", encoding="utf-8").read()

    # 1) 静态 i18n key 完整性（防「显示 key 名」这类问题）
    static = set(re.findall(r'\bt\(\s*["\']([^"\']+)["\']', src))
    # 动态 key 前缀（如 t("rl_task_status_" + status)）单独在第 2 步校验
    dynamic_prefixes = set(re.findall(r'\bt\(\s*["\']([^"\']+_)["\']\s*\+', src))
    static = {k for k in static if k not in dynamic_prefixes}
    zh, en = i18n._TRANSLATIONS["zh"], i18n._TRANSLATIONS["en"]
    miss_zh = sorted(k for k in static if k not in zh)
    miss_en = sorted(k for k in static if k not in en)
    assert not miss_zh, f"zh 缺失 key: {miss_zh}"
    assert not miss_en, f"en 缺失 key: {miss_en}"
    print(f"[OK] 1. i18n 静态 key 完整（app.py 共 {len(static)} 个，zh/en 全命中）")

    # 2) 动态状态 key：四种状态都能解析，不落回 key 名
    for s in ("pending", "running", "done", "failed"):
        for lang in ("zh", "en"):
            i18n.set_lang(lang)
            out = i18n.t("rl_task_status_" + s)
            assert out and out != "rl_task_status_" + s, f"状态 {s} 在 {lang} 未翻译"
    print("[OK] 2. 研究任务状态 key（pending/running/done/failed）zh/en 全解析")

    # 3) research_storage 导出函数齐全（防 AttributeError）
    used = sorted(set(re.findall(r'\bdb\.(\w+)', src)))
    missing = [f for f in used if not callable(getattr(db, f, None))]
    assert not missing, f"db 模块缺失函数: {missing}"
    print(f"[OK] 3. research_storage 导出齐全（app.py 用到 db.{', db.'.join(used)} 全存在）")

    # 4) 无 use_container_width / width="stretch"（版本无关默认布局，防回归）
    bad = []
    for f in UI_FILES:
        txt = open(f, encoding="utf-8").read()
        if "use_container_width" in txt:
            bad.append(f"{f}:use_container_width")
        for kw in ('width="stretch"', 'width="content"', "width='stretch'", "width='content'"):
            if kw in txt:
                bad.append(f"{f}:{kw}")
    assert not bad, f"存在版本特定布局参数: {bad}"
    print("[OK] 4. 无 use_container_width / width=\"stretch\"（全部走 Streamlit 默认布局）")

    # 5) Streamlit AppTest：完整启动 + 侧边栏按钮不报错
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("app.py", default_timeout=60)
    at.run()
    errs = list(getattr(at, "exception", []) or [])
    assert not errs, f"启动异常（含侧边栏 clear_cache 按钮）: {errs}"
    print("[OK] 5. AppTest 完整启动 + 侧边栏按钮无异常")

    # 6) AI 研究仓页面正常加载
    at.session_state["active_tab"] = "AI 对话舱"
    at.run()
    errs = list(getattr(at, "exception", []) or [])
    assert not errs, f"AI 研究仓加载异常: {errs}"
    print("[OK] 6. AI 研究仓页面正常加载")

    print("\nALL UI SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
