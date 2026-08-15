"""
UI 冒烟测试：研究闭环页面文字 / 存储层导出 / Streamlit 版本兼容 / 运行
覆盖：i18n 静态 key 完整 · 动态状态 key 解析 · research_storage 导出齐全 ·
      无 use_container_width / width="stretch" 残留（版本无关默认布局）·
      AppTest 完整启动 + 侧边栏按钮 + AI 研究仓页面加载 ·
      验证按钮唯一 key + 多验证按钮共存（防 auto-generated ID 冲突）·
      AI 研究舱 V1：登录→研究仓→目标输入框→验证按钮（无 AttributeError）·
      API Key 持久化 save/load 往返 + 环境变量回退
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

    # 5b) 侧边栏非表单按钮必须走 `with st.sidebar:`，禁止 st.sidebar.button()（form 误判）
    # 且禁止 with st.sidebar.container(): 嵌套（会触发 StreamlitDuplicateElementKey）
    bad_sb = re.findall(r'st\.sidebar\.button\s*\(', src)
    assert not bad_sb, f"仍存在 st.sidebar.button 直接调用（易触发 form 误判）: {bad_sb}"
    bad_cont = re.findall(r'with\s+st\.sidebar\.container\s*\(\s*\)\s*:', src)
    assert not bad_cont, f"存在 with st.sidebar.container(): 嵌套（易触发 StreamlitDuplicateElementKey）: {bad_cont}"
    print("[OK] 5b. 侧边栏按钮走 with st.sidebar: 结构，无 container 嵌套（规避 form 误判 + DuplicateElementKey）")

    # 6) Streamlit AppTest：登录页渲染无异常（鉴权门禁先于业务 UI）
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("app.py", default_timeout=60)
    at.run()
    errs = list(getattr(at, "exception", []) or [])
    assert not errs, f"启动异常: {errs}"
    print("[OK] 6. AppTest 启动（登录页）无异常")

    # 7) 登录 → AI 研究仓：验证按钮唯一 key + 研究目标输入框（V1 单目标入口）
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

        # 侧边栏 4 个按钮均带唯一 key 且渲染正常（无 StreamlitAPIException / 无 auto-ID 冲突）
        sb_keys = [str(b.key) for b in at.sidebar.button]
        for _k in ("clear_cache_btn", "all_history_btn", "apply_date_btn", "logout_btn"):
            assert _k in sb_keys, f"侧边栏按钮缺少 key={_k}（现有 {sb_keys}）"

        # 研究目标输入框（V1 单目标入口 key=rl_goal），set_value 触发正常 rerun
        at.text_input(key="rl_goal").set_value("寻找 ETH 趋势策略")
        at.run()
        errs = list(getattr(at, "exception", []) or [])
        assert not errs, f"输入研究目标异常: {errs}"

        # 策略搜索区块（V2）：目标输入框 + 开始搜索按钮，渲染无异常且 key 唯一
        assert "rl_search_goal" in [str(ti.key) for ti in at.text_input], \
            f"缺少策略搜索目标输入框（现有 {[str(ti.key) for ti in at.text_input]}）"
        assert any(str(getattr(b, "key", "")) == "rl_search" for b in at.button), \
            "缺少策略搜索按钮（key=rl_search）"

        print(f"[OK] 7. 登录→AI 研究仓：{len(verify_keys)} 个验证按钮 + 目标输入框 + 策略搜索区块 均无异常")
    finally:
        db.DB_PATH = _orig_path

    # 7b) API Key 持久化：save → load 往返一致 + 环境变量回退
    import api_config
    _orig_cfg_path = api_config._CONFIG_PATH
    _orig_env = os.environ.get("AI_API_KEY")
    try:
        api_config._CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "ai_config.json")
        api_config.save("sk-test-123", "DeepSeek-V3 (推荐)")
        cfg = api_config.load()
        assert cfg["key"] == "sk-test-123", f"load 未返回已保存 key: {cfg}"
        assert cfg["model"] == "DeepSeek-V3 (推荐)", "load 未返回已保存 model"
        api_config._CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "missing.json")
        os.environ["AI_API_KEY"] = "sk-env-456"
        assert api_config.load()["key"] == "sk-env-456", "环境变量回退失败"
        print("[OK] 7b. API Key 持久化：save/load 往返 + 环境变量回退")
    finally:
        api_config._CONFIG_PATH = _orig_cfg_path
        if _orig_env is None:
            os.environ.pop("AI_API_KEY", None)
        else:
            os.environ["AI_API_KEY"] = _orig_env

    # 9) DuplicateElementKey 回归：登录→AI研究仓→点击验证→rerun，无 StreamlitDuplicateElementKey
    import pandas as pd
    import research_loop as rl
    import engine_core
    _orig_verify = rl.verify_hypothesis
    _orig_path3 = db.DB_PATH
    _orig_mtf2 = engine_core.DataEngine.get_multi_timeframe
    try:
        tmp3 = tempfile.mkdtemp()
        db.DB_PATH = os.path.join(tmp3, "smoke3.db")
        db.init_db()
        hid = db.add_hypothesis("测试假设：EMA 金叉", related_indicators=["EMA 双均线"],
                                status="new", asset="ETH", timeframe="4h", leverage=3,
                                tp_pct=5.0, sl_pct=2.0)
        rl.verify_hypothesis = lambda hyp, df, coin, strategy_factory=None: {
            "passed": True, "failures": [],
            "score": {"total": 82, "grade": "B", "return": 12.0, "sharpe": 1.5, "mdd": 9.0,
                      "oos": 6.0, "param_stability": 60, "monte_carlo": 70},
            "metrics": {"sharpe": 1.5, "total_return": 12.0, "max_drawdown": 9.0,
                        "win_rate": 0.52, "trade_count": 35, "oos_return": 6.0},
            "indicators": ["EMA 双均线"], "params": {}, "coin": "ETH",
            "leverage": 3, "tp_pct": 5.0, "sl_pct": 2.0,
            "fingerprint": "", "experiment_id": 1, "report": "smoke stub",
        }
        _idx3 = pd.to_datetime(["2024-01-01 00:00", "2024-01-01 04:00"])
        _fake3 = pd.DataFrame({"open": [100.0, 101.0], "high": [102.0, 103.0],
                               "low": [99.0, 98.0], "close": [101.0, 102.0],
                               "volume": [1000.0, 1100.0]}, index=_idx3)
        engine_core.DataEngine.get_multi_timeframe = lambda self, asset: {tf: _fake3 for tf in ("15m", "1h", "4h", "1d")}

        at.session_state["logged_in"] = True
        at.session_state["active_tab"] = "AI 对话舱"
        at.run()
        assert not list(getattr(at, "exception", []) or []), "AI 研究仓渲染异常"

        verify_btns = [b for b in at.button if str(getattr(b, "key", "")).startswith("verify_")]
        assert verify_btns, "未找到验证按钮（key=verify_{id}）"
        verify_btns[0].click()
        at.run()
        errs9 = list(getattr(at, "exception", []) or [])
        assert not errs9, f"点击验证→rerun 触发 DuplicateElementKey/异常: {errs9}"
        print("[OK] 9. 点击验证→rerun 回归：无 StreamlitDuplicateElementKey / StreamlitAPIException")
    finally:
        db.DB_PATH = _orig_path3
        rl.verify_hypothesis = _orig_verify
        engine_core.DataEngine.get_multi_timeframe = _orig_mtf2

    print("\nALL UI SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
