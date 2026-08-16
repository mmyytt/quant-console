"""
研究任务状态管理（Research Job State）
=====================================
解决 Streamlit 长时间回测任务在 rerun / 刷新 / websocket 断连时进度丢失的问题。

核心思想：
  - 回测在**后台线程**运行，不依赖页面生命周期。
  - 任务状态以**本地 JSON 文件**（research_jobs/job_<id>.json）为唯一真相，
    st.session_state 仅作缓存；页面刷新后靠扫描文件找回任务。
  - 原子写（临时文件 + os.replace），避免读到半截 JSON。

本模块只做文件 I/O + 后台 runner，不 import streamlit / engine_core，
不改变任何回测 / 过滤 / 仓位 / 风控逻辑。
"""
import os
import json
import time
import uuid
from datetime import datetime

JOBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research_jobs")

# 任务状态
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_STOPPED = "STOPPED"

# 四阶段展示名（对应 UI 需求：候选生成 / 快速回测 / 样本外验证 / 风险审查）
STAGES = ["候选生成", "快速回测", "样本外验证", "风险审查"]
_STAGE_INDEX = {s: i for i, s in enumerate(STAGES)}


def _ensure_dir():
    os.makedirs(JOBS_DIR, exist_ok=True)


def _job_path(job_id):
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(job):
    _ensure_dir()
    path = _job_path(job["job_id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, default=str)
    os.replace(tmp, path)  # 原子替换，避免读到半截文件


def create_job(target, candidate_total, asset="ETH", timeframe="4h", mode="standard"):
    """创建新任务，落盘初始状态，返回 job_id。"""
    _ensure_dir()
    job_id = "job_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    job = {
        "job_id": job_id,
        "target": target,
        "asset": asset,
        "timeframe": timeframe,
        "mode": mode,
        "candidate_total": candidate_total,
        "current_index": 0,
        "passed": 0,
        "failed": 0,
        "status": STATUS_RUNNING,
        "started_time": _now(),
        "finished_time": None,
        "stage": STAGES[0],
        "stage_index": 0,
        "eta_seconds": None,
        "errors": [],
        "elimination": {},
        "result": None,
    }
    _write(job)
    return job_id


def get_job(job_id):
    """读取任务状态；文件不存在或损坏返回 None。"""
    if not job_id:
        return None
    path = _job_path(job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def update_job(job_id, **fields):
    """增量更新任务字段（不改 status，避免与终止状态互相覆盖）。"""
    job = get_job(job_id)
    if job is None:
        return
    job.update(fields)
    _write(job)


def set_status(job_id, status):
    """设置终止/进行中状态并记完成时间。"""
    job = get_job(job_id)
    if job is None:
        return
    job["status"] = status
    if status != STATUS_RUNNING:
        job["finished_time"] = _now()
    _write(job)


def list_jobs(limit=100):
    """历史研究列表（按开始时间倒序）。"""
    _ensure_dir()
    jobs = []
    for fn in os.listdir(JOBS_DIR):
        if not fn.endswith(".json"):
            continue
        j = get_job(fn[:-5])
        if j:
            jobs.append(j)
    jobs.sort(key=lambda j: j.get("started_time") or "", reverse=True)
    return jobs[:limit]


def find_running_job():
    """刷新后恢复：返回最近一个仍在 RUNNING 的任务（无则 None）。"""
    for j in list_jobs(200):
        if j.get("status") == STATUS_RUNNING:
            return j
    return None


def run_job(job_id, directions, df, coin, mode="standard", plan=None):
    """在调用方线程内运行研究漏斗，实时写回 job 文件（供后台线程调用）。

    职责：
      - 通过 stage_cb 把「阶段 / 进度 / 通过 / 淘汰 / ETA」持久化到 job 文件。
      - 通过 on_error 记录每条候选回测异常（不中断整轮）。
      - 通过 should_stop 响应外部「停止」请求。
    结束后置状态为 COMPLETED / FAILED / STOPPED。
    """
    import research_loop as rl

    _t0 = time.time()

    def _stage_cb(stage, i, n, passed, failed):
        eta = None
        if stage == "快速回测" and i > 0 and n > 0:
            rate = i / (time.time() - _t0)
            if rate > 0:
                eta = int((n - i) / rate)
        update_job(job_id, stage=stage, stage_index=_STAGE_INDEX.get(stage, 1),
                   current_index=i, current_total=n, passed=passed, failed=failed,
                   eta_seconds=eta)

    def _on_error(i, combo, err):
        j = get_job(job_id) or {}
        errs = list(j.get("errors") or [])
        errs.append({"candidate_id": i, "label": (combo or {}).get("label"),
                     "params": (combo or {}).get("param_overrides"), "error": str(err)})
        update_job(job_id, errors=errs)

    def _should_stop():
        j = get_job(job_id)
        return j is None or j.get("status") != STATUS_RUNNING

    try:
        result = rl.run_research_pipeline(directions, df, coin, mode=mode, plan=plan,
                                          stage_cb=_stage_cb, on_error=_on_error,
                                          should_stop=_should_stop)
    except Exception as e:  # 兜底：任何意外异常都不让任务「卡死」，标记 FAILED
        j = get_job(job_id) or {}
        errs = list(j.get("errors") or [])
        errs.append({"candidate_id": None, "label": "(致命错误)", "params": None, "error": str(e)})
        update_job(job_id, errors=errs)
        set_status(job_id, STATUS_FAILED)
        return

    if _should_stop():
        set_status(job_id, STATUS_STOPPED)
        return

    sc = result.get("stage_counts") or {}
    elim = result.get("elimination") or {}
    update_job(job_id, status=STATUS_COMPLETED, finished_time=_now(),
               current_index=sc.get("pool", 0),
               passed=sc.get("stage1_pass", 0),
               failed=sum(elim.values()),
               eta_seconds=0,
               elimination=elim,
               result=result)
