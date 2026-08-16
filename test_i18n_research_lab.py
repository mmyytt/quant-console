"""
自动量化研究实验室 i18n 测试: rl_lab_* key 在 zh/en 语言包完整且 t() 正确解析
============================================================================
覆盖: key 存在性 (zh + en) · t() 返回实际文本 (非原始 key) · 带占位符 key 格式化
运行: python test_i18n_research_lab.py
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

    import i18n

    KEYS = [
        "rl_lab_title", "rl_lab_hint", "rl_lab_mode_label",
        "rl_lab_mode_standard", "rl_lab_mode_deep", "rl_lab_btn",
        "rl_lab_running", "rl_lab_parse_fail", "rl_lab_retry_note",
        "rl_lab_no_direction", "rl_lab_diag", "rl_lab_report_title",
    ]

    # 1) key 同时存在于 zh 与 en 语言包
    zh, en = i18n._TRANSLATIONS["zh"], i18n._TRANSLATIONS["en"]
    for k in KEYS:
        assert k in zh, f"zh 语言包缺少 {k}"
        assert k in en, f"en 语言包缺少 {k}"
    print(f"[OK] 1. {len(KEYS)} 个 rl_lab_* key 均在 zh/en 语言包中")

    # 2) t() 返回实际文本，而非原始 key（中文环境）
    i18n.set_lang("zh")
    assert i18n.t("rl_lab_title") == "🔬 自动量化研究实验室", i18n.t("rl_lab_title")
    assert i18n.t("rl_lab_btn") == "🚀 启动研究实验室"
    for k in KEYS:
        assert i18n.t(k) != k, f"zh.{k} 返回原始 key 而非翻译"
    print("[OK] 2. zh 环境: t('rl_lab_title') = 自动量化研究实验室 (非原始 key)")

    # 3) 英文环境返回英文标题
    i18n.set_lang("en")
    assert i18n.t("rl_lab_title") == "🔬 Auto Quant Research Lab", i18n.t("rl_lab_title")
    assert i18n.t("rl_lab_btn") == "🚀 Launch Research Lab"
    for k in KEYS:
        assert i18n.t(k) != k, f"en.{k} 返回原始 key 而非翻译"
    print("[OK] 3. en 环境: t('rl_lab_title') = Auto Quant Research Lab (非原始 key)")

    # 4) 带占位符 key 正确格式化
    i18n.set_lang("zh")
    assert "第 2 次" in i18n.t("rl_lab_retry_note", n=2), i18n.t("rl_lab_retry_note", n=2)
    print("[OK] 4. 占位符 key rl_lab_retry_note 正确格式化 (n=2)")

    print("\nALL I18N RESEARCH LAB TESTS PASSED")


if __name__ == "__main__":
    main()
