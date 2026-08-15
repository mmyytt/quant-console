"""
防回归测试：禁止业务模块 import app.py（Streamlit 入口）
============================================================
背景：research_loop.py 曾 `from app import DynamicStrategy`，导致验证流程
重新 import app.py、重跑整个 Streamlit 页面、重复创建 widget，
触发 StreamlitDuplicateElementKey（StreamlitAPIException 子类）。

架构铁律：app.py 只做 UI，只能被 import；业务模块禁止反向 import app.py。

本测试两层校验：
1) 静态扫描：业务模块不得出现 `from app import` / `import app`。
2) 运行时：真实跑 verify_hypothesis（不 mock），断言 app.py 全程未 import。

运行: python test_no_ui_import.py
"""
import os
import re
import sys
import tempfile
import glob

ROOT = os.path.dirname(os.path.abspath(__file__))

# 需要扫描的业务模块（含用户指定 + 平台其余纯逻辑模块）
SCAN_FILES = [
    "research_loop.py",
    "engine_core.py",
    "research_phase1.py",
    "indicator_schema.py",
    "platform_context.py",
    "api_config.py",
    "strategy_models.py",
    "llm_client.py",
    "audit_engine.py",
    "research_storage/db.py",
]
SCAN_GLOBS = ["strategy*.py", "verify*.py"]

# 匹配真实 import 语句（不匹配注释里的「不 import app.py」）
IMPORT_RE = re.compile(r"^\s*(from\s+app\s+import|import\s+app\b)", re.M)


def _fix_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def static_scan():
    offenders = []
    targets = list(SCAN_FILES)
    for g in SCAN_GLOBS:
        targets.extend(glob.glob(os.path.join(ROOT, g)))
    for f in targets:
        path = os.path.join(ROOT, f) if not os.path.isabs(f) else f
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        for m in IMPORT_RE.finditer(src):
            line_no = src[:m.start()].count("\n") + 1
            offenders.append(f"{os.path.basename(f)}:{line_no}: {m.group(0).strip()}")
    return offenders


def runtime_verify_no_app_import():
    """真实跑 verify_hypothesis（不 mock），断言全程不 import app.py。"""
    import numpy as np
    import pandas as pd
    import research_storage.db as db
    import research_loop as rl
    from indicator_schema import INDICATOR_REGISTRY

    assert "app" not in sys.modules, "测试前提：app 不应已加载"

    # 小样本合成 OHLCV（列名须为 vol，与 engine_core / 指标一致；避免真实数据陈旧触发网络刷新）
    n = 900
    idx = pd.date_range("2023-01-01", periods=n, freq="1h")
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0.03, 1.2, n))
    df = pd.DataFrame({
        "open": close - np.abs(rng.normal(0, 0.3, n)),
        "high": close + np.abs(rng.normal(0.3, 0.5, n)),
        "low": close - np.abs(rng.normal(0.3, 0.5, n)),
        "close": close,
        "vol": rng.uniform(500, 2000, n),
    }, index=idx)

    # 临时 db
    tmp = tempfile.mkdtemp()
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    ind_name = next(iter(INDICATOR_REGISTRY.keys()))  # 取一个真实存在的指标
    db.add_hypothesis("防回归测试假设", related_indicators=[ind_name],
                      status="new", asset="ETH", timeframe="1h",
                      leverage=2, tp_pct=8.0, sl_pct=4.0)
    hyp = db.list_hypotheses(1)[0]  # 完整 dict（含 id）

    # 真实 verify：verify_hypothesis → run_hypothesis_backtest → run_single
    #   → engine_core → DynamicStrategy(strategy_models)
    verdict = rl.verify_hypothesis(hyp, df, "ETH")

    # 核心断言：整条链路从未 import app.py
    assert "app" not in sys.modules, "verify_hypothesis 意外 import 了 app.py！"
    assert isinstance(verdict, dict) and "metrics" in verdict, "verify 未返回 metrics"
    return verdict


def main():
    _fix_encoding()
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)

    offenders = static_scan()
    if offenders:
        print("[FAIL] 业务模块存在 import app.py：")
        for o in offenders:
            print("  " + o)
        raise SystemExit(1)
    print(f"[OK] 1. 静态扫描：业务模块（{len(SCAN_FILES)} 个 + strategy*/verify*）无 import app.py")

    verdict = runtime_verify_no_app_import()
    print("[OK] 2. 真实 verify_hypothesis 链路跑通（不 mock），app.py 全程未 import")
    print(f"       passed={verdict.get('passed')}  experiment_id={verdict.get('experiment_id')}  "
          f"trade_count={verdict['metrics'].get('trade_count')}")

    print("\nALL NO-UI-IMPORT TESTS PASSED")


if __name__ == "__main__":
    main()
