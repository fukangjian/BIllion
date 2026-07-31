"""
Invest FastAPI 服务 — 盘前流程触发、状态查询、定时调度
"""
import json
import logging
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import (
    DAILY_REPORT_OUTPUT_DIR,
    MARKET_SCAN_OUTPUT_DIR,
    MONTHLY_REVIEW_TIME,
    SCHEDULER_ENABLED,
    SCHEDULER_TIME,
    SERVER_HOST,
    SERVER_PORT,
    WATCHLIST,
    WEEKLY_REVIEW_TIME,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("invest_server")

# 任务超时（秒）
TASK_TIMEOUT = 600

# 全局运行状态
_run_state: dict[str, Any] = {
    "last_pre_market": None,
    "last_pipeline": None,
    "last_research": None,
    "last_weekly_review": None,
    "last_monthly_review": None,
    "running": False,
    "current_task": None,
}

_executor = ThreadPoolExecutor(max_workers=1)
_scheduler = None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _latest_file(directory: Path, pattern: str = "*.md") -> Optional[Path]:
    if not directory.exists():
        return None
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _list_output_files(directory: Path, limit: int = 10) -> list[dict]:
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": f.name,
            "path": str(f),
            "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
        }
        for f in files[:limit]
    ]


def _read_file_safe(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {"path": None, "content": None, "exists": False}
    try:
        content = path.read_text(encoding="utf-8")
        return {"path": str(path), "content": content, "exists": True}
    except OSError as e:
        return {"path": str(path), "content": None, "exists": False, "error": str(e)}


def _run_with_timeout(func, timeout: int = TASK_TIMEOUT) -> Any:
    """在线程池中运行任务并设置超时"""
    future = _executor.submit(func)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeout:
        raise TimeoutError(f"任务执行超时（>{timeout}s）")


def _do_pipeline(skip_fetch: bool = False, symbols: list[str] | None = None) -> dict:
    from run_all import run_pipeline

    t0 = time.time()
    path = run_pipeline(skip_fetch=skip_fetch, symbols=symbols)
    elapsed = round(time.time() - t0, 1)
    return {"output": str(path), "elapsed_seconds": elapsed}


def _do_research(watchlist: list[str] | None = None, sectors: list[str] | None = None) -> dict:
    from run_all import run_research

    t0 = time.time()
    path = run_research(watchlist=watchlist, sectors=sectors)
    elapsed = round(time.time() - t0, 1)
    return {"output": str(path), "elapsed_seconds": elapsed}


def _do_pre_market(skip_fetch: bool = False, symbols: list[str] | None = None) -> dict:
    scan = _do_pipeline(skip_fetch=skip_fetch, symbols=symbols)
    report = _do_research()
    return {"scan": scan, "report": report}


def _do_weekly_review() -> dict:
    """生成周报（review.report_generator，写入 vault 每周复盘目录）"""
    from review.report_generator import generate_weekly_report

    t0 = time.time()
    path = generate_weekly_report()
    elapsed = round(time.time() - t0, 1)
    return {"output": str(path), "elapsed_seconds": elapsed}


def _do_monthly_review() -> dict:
    """生成月报（review.report_generator，写入 vault 每月复盘目录）"""
    from review.report_generator import generate_monthly_report

    t0 = time.time()
    path = generate_monthly_report()
    elapsed = round(time.time() - t0, 1)
    return {"output": str(path), "elapsed_seconds": elapsed}


def _execute_task(task_name: str, func, state_key: str) -> dict:
    """串行执行任务，更新全局状态"""
    if _run_state["running"]:
        raise HTTPException(
            status_code=409,
            detail=f"已有任务运行中: {_run_state['current_task']}",
        )

    _run_state["running"] = True
    _run_state["current_task"] = task_name
    record = {"task": task_name, "started_at": _now_iso(), "status": "running"}

    try:
        result = _run_with_timeout(func)
        record.update(status="ok", finished_at=_now_iso(), result=result)
        _run_state[state_key] = record
        return record
    except TimeoutError as e:
        record.update(status="timeout", finished_at=_now_iso(), error=str(e))
        _run_state[state_key] = record
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        record.update(
            status="error",
            finished_at=_now_iso(),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        _run_state[state_key] = record
        logger.error("任务 %s 失败: %s\n%s", task_name, e, record["traceback"])
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _run_state["running"] = False
        _run_state["current_task"] = None


# --- FastAPI 应用 ---

app = FastAPI(
    title="Invest API",
    description="Invest 盘前数据管道与研究日报服务",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录每个请求的 method、path、耗时"""
    t0 = time.time()
    logger.info("→ %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
        elapsed = round((time.time() - t0) * 1000)
        logger.info("← %s %s %dms", request.method, request.url.path, elapsed)
        return response
    except Exception as e:
        elapsed = round((time.time() - t0) * 1000)
        logger.error("✗ %s %s %dms — %s", request.method, request.url.path, elapsed, e)
        raise


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"status": "error", "detail": exc.detail})
    logger.error("未捕获异常 %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": str(exc)},
    )


@app.get("/")
def root():
    return {
        "service": "Invest API",
        "endpoints": [
            "POST /pre-market",
            "POST /pipeline",
            "POST /research",
            "GET /status",
            "GET /latest-scan",
            "GET /latest-report",
        ],
    }


@app.post("/pre-market")
def pre_market(skip_fetch: bool = False):
    """运行完整盘前流程（pipeline + research）"""
    return _execute_task(
        "pre-market",
        lambda: _do_pre_market(skip_fetch=skip_fetch, symbols=WATCHLIST),
        "last_pre_market",
    )


@app.post("/pipeline")
def pipeline(skip_fetch: bool = False):
    """仅运行数据管道"""
    return _execute_task(
        "pipeline",
        lambda: _do_pipeline(skip_fetch=skip_fetch, symbols=WATCHLIST),
        "last_pipeline",
    )


@app.post("/research")
def research():
    """仅运行研究日报"""
    return _execute_task(
        "research",
        lambda: _do_research(),
        "last_research",
    )


@app.get("/status")
def status():
    """返回最近运行状态和输出文件列表"""
    return {
        "running": _run_state["running"],
        "current_task": _run_state["current_task"],
        "last_pre_market": _run_state["last_pre_market"],
        "last_pipeline": _run_state["last_pipeline"],
        "last_research": _run_state["last_research"],
        "last_weekly_review": _run_state["last_weekly_review"],
        "last_monthly_review": _run_state["last_monthly_review"],
        "outputs": {
            "market_scans": _list_output_files(MARKET_SCAN_OUTPUT_DIR),
            "daily_reports": _list_output_files(DAILY_REPORT_OUTPUT_DIR),
        },
        "scheduler": {
            "enabled": SCHEDULER_ENABLED,
            "time": SCHEDULER_TIME,
            "weekly_review_time": WEEKLY_REVIEW_TIME,
            "monthly_review_time": MONTHLY_REVIEW_TIME,
        },
    }


@app.get("/latest-scan")
def latest_scan():
    """返回最新 market_scan 内容（附带扫描 JSON 中的持仓监控结果）"""
    latest = _latest_file(MARKET_SCAN_OUTPUT_DIR)
    data = _read_file_safe(latest)
    position_monitor = None
    if latest is not None:
        json_file = latest.with_suffix(".json")
        if json_file.exists():
            try:
                scan_data = json.loads(json_file.read_text(encoding="utf-8"))
                position_monitor = scan_data.get("position_monitor")
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("读取扫描 JSON 失败（position_monitor 降级为 None）: %s", e)
    return {"status": "ok", **data, "position_monitor": position_monitor}


@app.get("/latest-report")
def latest_report():
    """返回最新日报内容"""
    latest = _latest_file(DAILY_REPORT_OUTPUT_DIR)
    data = _read_file_safe(latest)
    return {"status": "ok", **data}


def _parse_schedule_time(time_str: str) -> tuple[int, int]:
    """解析 HH:MM 格式的调度时间"""
    parts = time_str.strip().split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    return hour, minute


def _scheduled_pre_market():
    """定时任务回调"""
    logger.info("定时任务触发: 盘前流程")
    if _run_state["running"]:
        logger.warning("跳过定时任务：已有任务运行中")
        return
    try:
        _execute_task(
            "scheduled-pre-market",
            lambda: _do_pre_market(skip_fetch=False, symbols=WATCHLIST),
            "last_pre_market",
        )
        logger.info("定时盘前流程完成")
    except HTTPException as e:
        logger.error("定时盘前流程失败: %s", e.detail)


def _scheduled_weekly_review():
    """定时任务回调：每周五生成周报（失败仅记日志，不影响其他 job）"""
    logger.info("定时任务触发: 周报生成")
    if _run_state["running"]:
        logger.warning("跳过周报生成：已有任务运行中")
        return
    try:
        _execute_task("scheduled-weekly-review", _do_weekly_review, "last_weekly_review")
        logger.info("定时周报生成完成")
    except HTTPException as e:
        logger.error("定时周报生成失败: %s", e.detail)


def _scheduled_monthly_review():
    """定时任务回调：每月最后一天生成月报（失败仅记日志，不影响其他 job）"""
    logger.info("定时任务触发: 月报生成")
    if _run_state["running"]:
        logger.warning("跳过月报生成：已有任务运行中")
        return
    try:
        _execute_task("scheduled-monthly-review", _do_monthly_review, "last_monthly_review")
        logger.info("定时月报生成完成")
    except HTTPException as e:
        logger.error("定时月报生成失败: %s", e.detail)


def _start_scheduler():
    """启动 APScheduler 定时调度（每日盘前 + 每周五周报 + 每月末月报）"""
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("未安装 apscheduler，定时调度不可用")
        return

    hour, minute = _parse_schedule_time(SCHEDULER_TIME)
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _scheduled_pre_market,
        CronTrigger(hour=hour, minute=minute),
        id="pre_market_daily",
        replace_existing=True,
    )

    weekly_hour, weekly_minute = _parse_schedule_time(WEEKLY_REVIEW_TIME)
    _scheduler.add_job(
        _scheduled_weekly_review,
        CronTrigger(day_of_week="fri", hour=weekly_hour, minute=weekly_minute),
        id="weekly_review_fri",
        replace_existing=True,
    )

    monthly_hour, monthly_minute = _parse_schedule_time(MONTHLY_REVIEW_TIME)
    _scheduler.add_job(
        _scheduled_monthly_review,
        CronTrigger(day="last", hour=monthly_hour, minute=monthly_minute),
        id="monthly_review_last_day",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("定时调度已启动: 每日 %02d:%02d 运行盘前流程", hour, minute)
    logger.info("定时调度已启动: 每周五 %02d:%02d 生成周报", weekly_hour, weekly_minute)
    logger.info("定时调度已启动: 每月最后一天 %02d:%02d 生成月报", monthly_hour, monthly_minute)


@app.on_event("startup")
def on_startup():
    logger.info("Invest 服务启动 — host=%s port=%d", SERVER_HOST, SERVER_PORT)
    if SCHEDULER_ENABLED:
        # 延迟启动调度器，避免阻塞 startup
        threading.Thread(target=_start_scheduler, daemon=True).start()


@app.on_event("shutdown")
def on_shutdown():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("定时调度已停止")
    _executor.shutdown(wait=False)


def main():
    import uvicorn

    uvicorn.run(
        "server:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
