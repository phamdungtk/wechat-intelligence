"""Small in-process job registry for article downloads and live progress."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException

logger = logging.getLogger("uvicorn.error")
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wechat-article")
lock = Lock()
jobs: dict[str, dict] = {}
MAX_JOBS = 100
MAX_PENDING = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(job: dict, message: str, level: str = "info") -> None:
    with lock:
        job["events"].append({"time": _now(), "level": level, "message": message})
    if level == "error":
        logger.error("job=%s %s", job["id"], message)
    else:
        logger.info("job=%s %s", job["id"], message)


def start_job(operation: str, work) -> dict:
    with lock:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        for key, value in list(jobs.items()):
            if value["state"] in ("done", "error") and datetime.fromisoformat(value["created_at"]) < cutoff:
                del jobs[key]
        pending = sum(value["state"] in ("queued", "running") for value in jobs.values())
        if pending >= MAX_PENDING:
            raise HTTPException(status_code=429, detail="Đang có nhiều bài viết được tải. Vui lòng thử lại sau.")
        if len(jobs) >= MAX_JOBS:
            completed = [value for value in jobs.values() if value["state"] in ("done", "error")]
            if completed:
                jobs.pop(min(completed, key=lambda value: value["created_at"])["id"])
        job = {"id": uuid4().hex, "operation": operation, "state": "queued", "created_at": _now(),
               "events": [], "result": None, "error": None}
        jobs[job["id"]] = job

    _event(job, "Đã nhận yêu cầu. Đang chờ lượt tải bài viết.")

    def run() -> None:
        with lock:
            job["state"] = "running"
        try:
            result = work(lambda message: _event(job, message))
            _event(job, "Hoàn tất. Bài viết đã được lưu và phân tích.")
            with lock:
                job["result"] = result
                job["state"] = "done"
        except HTTPException as exc:
            message = str(exc.detail)
            _event(job, message, "error")
            with lock:
                job["state"] = "error"
                job["error"] = message
            logger.exception("Article job failed: %s", job["id"])
        except Exception:
            message = "Có lỗi khi tải hoặc xử lý bài viết. Xem nhật ký máy chủ để biết chi tiết."
            _event(job, message, "error")
            with lock:
                job["state"] = "error"
                job["error"] = message
            logger.exception("Article job failed: %s", job["id"])

    executor.submit(run)
    return {"job_id": job["id"]}


def get_job(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Không tìm thấy phiên tải bài viết.")
        return {"id": job["id"], "operation": job["operation"], "state": job["state"],
                "events": list(job["events"]), "result": job["result"], "error": job["error"]}
