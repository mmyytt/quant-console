"""
后台研究任务持久化测试（Research Job State）
============================================
覆盖：
  1. 任务文件往返 + 状态转移（RUNNING/COMPLETED/FAILED/STOPPED）
  2. find_running_job 刷新恢复 RUNNING 任务
  3. run_research_pipeline 新参数：pool / stage_cb / should_stop（提前停止不崩溃）
  4. on_error：单候选回测异常不中断整轮
  5. run_job 端到端持久化：刷新后 get_job 恢复 COMPLETED + 报告
  6. list_jobs 历史研究
运行: python test_research_jobs.py
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


def _synthetic_df(n=400):
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2022-01-01", periods=n, freq="1D")
    rng = np.random.default_rng(7)
    t = np.arange(n)
    close = 100 + 0.05 * t + 5 * np.sin(t / 30.0) + np.cumsum(rng.normal(0, 0.4, n))
    return pd.DataFrame({
        "open": close - np.abs(rng.normal(0, 0.3, n)),
        "high": close + np.abs(rng.normal(0.3, 0.6, n)),
        "low": close - np.abs(rng.normal(0.3, 0.6, n)),
        "close": close,
        "vol": rng.uniform(500, 2000, n),
    }, index=idx)


def main():
    _fix_encoding()
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    sys.path.insert(0, root)

    import research_jobs as rjobs
    import research_loop as rl

    assert "app" not in sys.modules, "测试前提：research_jobs/research_loop 不应 import app.py"
    rjobs.JOBS_DIR = tempfile.mkdtemp()  # 隔离：不污染真实 research_jobs/

    # 1) 任务文件往返 + 状态转移
    jid = rjobs.create_job("测试任务", 500, asset="ETH", timeframe="4h", mode="standard")
    j = rjobs.get_job(jid)
    assert j and j["status"] == rjobs.STATUS_RUNNING and j["candidate_total"] == 500, j
    assert j["job_id"] and j["target"] and j["started_time"], "缺少关键字段"
    rjobs.update_job(jid, current_index=86, passed=12, failed=74)
    j = rjobs.get_job(jid)
    assert j["current_index"] == 86 and j["passed"] == 12 and j["failed"] == 74, j
    rjobs.set_status(jid, rjobs.STATUS_COMPLETED)
    j = rjobs.get_job(jid)
    assert j["status"] == rjobs.STATUS_COMPLETED and j["finished_time"], j
    print("[OK] 1. 任务文件往返 + 状态转移（RUNNING→COMPLETED，进度字段持久化）")

    # 2) find_running_job 刷新恢复
    jid2 = rjobs.create_job("运行中任务", 100)
    rj = rjobs.find_running_job()
    assert rj and rj["job_id"] == jid2, "应找回最近的 RUNNING 任务"
    rjobs.set_status(jid2, rjobs.STATUS_STOPPED)
    print("[OK] 2. find_running_job 刷新后从文件恢复 RUNNING 任务")

    df = _synthetic_df()
    directions = [{"hypothesis": "h", "indicators": ["EMA 双均线"], "params": {},
                   "risk": {"leverage": 2, "tp_pct": 8.0, "sl_pct": 4.0},
                   "position": {"_init_alloc_pct": 50.0}}]
    pool = rl.build_search_space(directions, mode="standard")

    # 3) run_research_pipeline 新参数：pool / stage_cb / should_stop
    stages, stop_n = [], {"n": 0}

    def _stage_cb(stage, i, n, passed, failed):
        stages.append(stage)

    def _should_stop():
        stop_n["n"] += 1
        return stop_n["n"] > 5  # 第 5 次检查后请求停止

    res = rl.run_research_pipeline(directions, df, "ETH", mode="standard", pool=pool,
                                   stage_cb=_stage_cb, should_stop=_should_stop)
    assert "候选生成" in stages and "快速回测" in stages, f"stage_cb 应上报阶段, 实际 {stages}"
    assert res["pool_size"] == len(pool), "pool 参数应生效（候选总数一致）"
    print("[OK] 3. run_research_pipeline 支持 pool/stage_cb/should_stop（提前停止不崩溃）")

    # 4) on_error：单候选异常不中断整轮
    _orig_quick = rl._quick_backtest

    def _boom(*a, **k):
        raise RuntimeError("模拟回测异常")

    rl._quick_backtest = _boom
    try:
        errs = []
        res2 = rl.run_research_pipeline(directions, df, "ETH", mode="standard", pool=pool,
                                        on_error=lambda i, c, e: errs.append((i, e)))
        assert errs, "应通过 on_error 记录候选异常"
        assert res2["elimination"].get("回测异常", 0) >= 1, "异常候选应计入「回测异常」淘汰"
        assert isinstance(res2["top"], list), "整轮不应因候选异常而中断"
    finally:
        rl._quick_backtest = _orig_quick
    print("[OK] 4. on_error 记录候选异常 + 计数「回测异常」，整轮不中断")

    # 5) run_job 端到端持久化：刷新后 get_job 恢复 COMPLETED + 报告
    rjobs.JOBS_DIR = tempfile.mkdtemp()
    jid3 = rjobs.create_job("端到端", len(pool), asset="ETH", timeframe="4h")
    rjobs.run_job(jid3, directions, df, "ETH", mode="standard", plan={"goal": "端到端"})
    j3 = rjobs.get_job(jid3)  # 模拟刷新后重读
    assert j3["status"] == rjobs.STATUS_COMPLETED, f"状态应为 COMPLETED, 实际 {j3['status']}"
    assert j3["result"] and "report" in j3["result"], "应持久化结果 + 报告"
    assert j3["finished_time"], "应记录完成时间"
    print("[OK] 5. run_job 端到端持久化：刷新后恢复 COMPLETED + 报告")

    # 6) list_jobs 历史
    jobs = rjobs.list_jobs(10)
    assert any(x["job_id"] == jid3 for x in jobs), "list_jobs 应返回历史任务"
    print("[OK] 6. list_jobs 历史研究（关闭浏览器后仍可查看）")

    print("\nALL RESEARCH JOB TESTS PASSED")


if __name__ == "__main__":
    main()
