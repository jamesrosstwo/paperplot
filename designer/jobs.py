"""Background jobs for the plot designer (contract section 5).

Long tasks (a full paper compile) run on a single worker thread so that only one LaTeX job runs at a time; concurrent
users' compile requests queue behind it (and a request while one is still *queued* joins that job, see dedupe_queued).
Each job has an id and a status `queued | running | done | error`; `get(job_id)` returns a JSON-able dict that also
holds `position` (how many jobs are ahead of a queued one).

    job_id = jobs.submit("compile", fn, *args, **kwargs)
    jobs.get(job_id) -> {id, kind, status, created, started, finished, elapsed, result, error}

A job whose function raises ends in `error` with the message (and a traceback tail in `detail`); a job whose function
returns normally ends in `done` with its return value as `result` (even when that result says e.g. `ok: false`).
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

STATUSES = ("queued", "running", "done", "error")
_MAX_KEEP = 200            # finished jobs kept in memory; the oldest finished ones are dropped beyond this


class JobRunner:
    """A thread pool of `workers` (default 1: one LaTeX job at a time) with job bookkeeping."""

    def __init__(self, workers: int = 1, name: str = "designer-job"):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    # ------------------------------------------------------------------ submit / query
    def submit(self, kind: str, fn: Callable[..., Any], *args, dedupe_queued: bool = False, **kwargs) -> str:
        """Queue `fn(*args, **kwargs)`; return the job id.

        dedupe_queued: if a job of the same kind is still *queued* (not yet running), return its id instead of queuing
        another one — it has not started, so it will pick up the latest state anyway.
        """
        with self._lock:
            if dedupe_queued:
                for job in self._jobs.values():
                    if job["kind"] == kind and job["status"] == "queued":
                        return job["id"]
            job_id = uuid.uuid4().hex[:12]
            self._jobs[job_id] = {
                "id": job_id, "kind": kind, "status": "queued",
                "created": time.time(), "started": None, "finished": None,
                "result": None, "error": None, "detail": None,
            }
            self._prune_locked()
        self._pool.submit(self._run, job_id, fn, args, kwargs)
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            out = self._public(job)
            out["position"] = self._position_locked(job)
            return out

    def _position_locked(self, job: dict) -> int:
        """0 = running or finished; n = n jobs (queued or running) are ahead of this queued one."""
        if job["status"] != "queued":
            return 0
        ahead = 0
        for j in self._jobs.values():          # insertion order = submission order (timestamps can tie on Windows)
            if j is job:
                break
            if j["status"] in ("queued", "running"):
                ahead += 1
        return ahead

    def list(self, kind: str | None = None) -> list[dict]:
        with self._lock:
            jobs = [self._public(j) for j in self._jobs.values() if kind is None or j["kind"] == kind]
        return sorted(jobs, key=lambda j: j["created"], reverse=True)

    def active(self, kind: str | None = None) -> list[dict]:
        """Jobs that are queued or running."""
        return [j for j in self.list(kind) if j["status"] in ("queued", "running")]

    def wait(self, job_id: str, timeout: float | None = None, poll: float = 0.05) -> dict | None:
        """Block until the job is done/error (or timeout); return its dict. For tests and scripts."""
        t0 = time.time()
        while True:
            job = self.get(job_id)
            if job is None or job["status"] in ("done", "error"):
                return job
            if timeout is not None and time.time() - t0 > timeout:
                return job
            time.sleep(poll)

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=True)

    # ------------------------------------------------------------------ internals
    def _run(self, job_id: str, fn, args, kwargs) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "running"
            job["started"] = time.time()
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 - a job must never kill the worker thread
            tb = traceback.format_exc()
            with self._lock:
                job["status"] = "error"
                job["error"] = f"{type(e).__name__}: {e}"
                job["detail"] = tb[-4000:]
                job["finished"] = time.time()
            return
        with self._lock:
            job["status"] = "done"
            job["result"] = result
            job["finished"] = time.time()

    @staticmethod
    def _public(job: dict) -> dict:
        out = dict(job)
        start = job["started"]
        end = job["finished"] or time.time()
        out["elapsed"] = round(end - start, 3) if start else 0.0
        return out

    def _prune_locked(self) -> None:
        if len(self._jobs) <= _MAX_KEEP:
            return
        finished = sorted((j for j in self._jobs.values() if j["status"] in ("done", "error")),
                          key=lambda j: j["created"])
        for j in finished[: len(self._jobs) - _MAX_KEEP]:
            self._jobs.pop(j["id"], None)


# Module-level runner used by the server: one worker = one LaTeX job at a time.
runner = JobRunner(workers=1, name="designer-latex")


def submit(kind: str, fn: Callable[..., Any], *args, **kwargs) -> str:
    return runner.submit(kind, fn, *args, **kwargs)


def get(job_id: str) -> dict | None:
    return runner.get(job_id)


def list_jobs(kind: str | None = None) -> list[dict]:
    return runner.list(kind)
