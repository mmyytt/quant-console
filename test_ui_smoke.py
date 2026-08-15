"""
UI 冒烟测试：研究闭环页面文字 / 存储层导出 / Streamlit 版本兼容 / 运行
覆盖：i18n 静态 key 完整 · 动态状态 key 解析 · research_storage 导出齐全 ·
      无 use_container_width / width="stretch" 残留（版本无关默认布局）·
      AppTest 完整启动 + 侧边栏按钮 + AI 研究仓页面加载 ·
      验证按钮唯一 key + 多验证按钮共存（防 auto-generated ID 冲突）·
      登录→AI 研究仓→create_session→输入目标→update_session（防 AttributeError）·
      创建任务→启动研究→重渲染（防 st.form 嵌套 button 的 StreamlitAPIException）
运行: python test_ui_smoke.py
"""
import os
import re
import sys
import tempfile

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

    # 5) 验证按钮必须带唯一 key（防 auto-generated ID 冲突）
    m = re.search(r'st\.button\(\s*t\(\s*"rl_verify_btn"\s*\)\s*,\s*key=', src)
    assert m, "验证按钮缺少唯一 key"
    print("[OK] 5. 验证按钮已绑定唯一 key（key=verify_{hypothesis_id}）")

    # 6) Streamlit AppTest：登录页渲染无异常（鉴权门禁先于业务 UI）
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("app.py", default_timeout=60)
    at.run()
    errs = list(getattr(at, "exception", []) or [])
    assert not errs, f"启动异常: {errs}"
    print("[OK] 6. AppTest 启动（登录页）无异常")

    # 7) 登录 → AI 研究仓：验证按钮唯一 key + create_session + 输入目标触发 update_session（无 AttributeError）
    _orig_path = db.DB_PATH
    try:
        tmp = tempfile.mkdtemp()
        db.DB_PATH = os.path.join(tmp, "smoke.db")
        db.init_db()
        seeded = [db.add_hypothesis(f"假设 {i}：EMA 金叉 + 量比确认",
                                    related_indicators=["EMA 双均线", "量比 Volume Ratio"])
                  for i in range(3)]
        at.session_state["logged_in"] = True
        at.session_state["active_tab"] = "AI 对话舱"
        at.run()
        errs = list(getattr(at, "exception", []) or [])
        assert not errs, f"AI 研究仓渲染异常: {errs}"

        # 验证按钮：多条假设同时渲染多个 key=verify_{id}，不重复 ID
        verify_keys = [b.key for b in at.button
                       if str(getattr(b, "key", "")).startswith("verify_")]
        assert len(verify_keys) >= 3, f"预期 ≥3 个验证按钮，实际 {len(verify_keys)}"

        # 进入研究仓自动 create_session（无 AttributeError）
        sessions = db.list_sessions(1)
        assert sessions, "进入研究仓未自动创建 session"
        sid = sessions[0]["id"]

        # 输入研究目标 → 触发 on_change → db.update_session（无 AttributeError 且生效）
        at.text_input(key="research_goal").set_value("寻找 ETH 趋势策略")
        at.run()
        errs = list(getattr(at, "exception", []) or [])
        assert not errs, f"输入研究目标触发 update_session 异常: {errs}"
        assert db.get_session(sid)["user_goal"] == "寻找 ETH 趋势策略", "update_session 未生效"

        print(f"[OK] 7. 登录→AI 研究仓：{len(verify_keys)} 个验证按钮 + create_session + update_session 均无 AttributeError")
    finally:
        db.DB_PATH = _orig_path

    # 8) 创建任务 + 启动研究：登录→AI研究仓→输入目标→点启动→重新渲染，无 StreamlitAPIException（form 嵌套回归）
    import pandas as pd
    import research_loop as rl
    import engine_core
    _orig_path2 = db.DB_PATH
    _orig_run = rl.run_research_task
    _orig_mtf = engine_core.DataEngine.get_multi_timeframe
    try:
        tmp2 = tempfile.mkdtemp()
        db.DB_PATH = os.path.join(tmp2, "smoke2.db")
        db.init_db()
        # 桩替换：smoke test 只验证 UI 渲染，不触发真实回测 + 网络数据刷新
        rl.run_research_task = lambda *a, **k: {"summary": "smoke stub", "ranked": [], "passed": 0}
        _idx = pd.to_datetime(["2024-01-01 00:00", "2024-01-01 04:00"])
        _fake = pd.DataFrame({"open": [100.0, 101.0], "high": [102.0, 103.0],
                              "low": [99.0, 98.0], "close": [101.0, 102.0],
                              "volume": [1000.0, 1100.0]}, index=_idx)
        engine_core.DataEngine.get_multi_timeframe = lambda self, asset: {tf: _fake for tf in ("15m", "1h", "4h", "1d")}

        at.session_state["logged_in"] = True
        at.session_state["active_tab"] = "AI 对话舱"
        at.run()
        assert not list(getattr(at, "exception", []) or []), "AI 研究仓渲染异常"

        at.text_input(key="rl_task_goal").set_value("寻找 ETH 趋势策略")
        at.run()
        assert not list(getattr(at, "exception", []) or []), "输入任务目标异常"

        at.button(key="rl_task_start").click()
        at.run()
        assert not list(getattr(at, "exception", []) or []), "启动研究触发 StreamlitAPIException"
        assert db.list_tasks(1), "启动研究未创建任务"
        print("[OK] 8. 创建任务 + 启动研究：登录→AI研究仓→输入目标→启动→重渲染，无 StreamlitAPIException")
    finally:
        db.DB_PATH = _orig_path2
        rl.run_research_task = _orig_run
        engine_core.DataEngine.get_multi_timeframe = _orig_mtf

    print("\nALL UI SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
