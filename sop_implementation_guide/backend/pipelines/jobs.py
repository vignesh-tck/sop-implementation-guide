"""
Background work on a pipeline (an agent turn, a run, a record re-run) and the
planner's Stop.

Each job runs in a daemon thread with its own stop flag. Stop clears the
pipeline's busy flag at once, so the panel is usable again even while the job
is stuck in a slow model or network call, and sets the flag; the job checks it
between model calls, before tool calls and at each run progress report, and
ends there without writing anything more. A stopped job never clears busy
itself, since by then a new job may hold it.
"""

import threading
from typing import Callable

from backend.db.session import get_supabase
from backend.repositories.pipelines import PipelineRepository


class Stopped(BaseException):
    """
    The planner pressed Stop. A BaseException, like KeyboardInterrupt, so the
    `except Exception` handlers that turn a failing step or tool into an error
    message let it through and the job actually ends.
    """


_jobs: dict[int, threading.Event] = {}
_lock = threading.Lock()
_local = threading.local()


def start(pipeline_id: int, target: Callable, *args) -> None:
    """Run target(*args) in the background for a pipeline already claimed busy; release it when done."""
    flag = threading.Event()
    with _lock:
        _jobs[pipeline_id] = flag

    def body() -> None:
        _local.flag = flag
        try:
            target(*args)
        except Stopped:
            pass
        finally:
            with _lock:
                if _jobs.get(pipeline_id) is flag:
                    del _jobs[pipeline_id]
            if not flag.is_set():
                PipelineRepository(get_supabase()).release(pipeline_id)

    threading.Thread(target=body, daemon=True).start()


def stop(pipeline_id: int) -> bool:
    """Tell the pipeline's job to stop. False when this server runs none (e.g. it restarted mid-job)."""
    with _lock:
        flag = _jobs.pop(pipeline_id, None)
    if flag:
        flag.set()
    return flag is not None


def checker() -> Callable[[], None]:
    """
    A check bound to the calling job, to hand to code that reports from worker
    threads (where the job's flag is not visible). Raises Stopped once the job
    is stopped; never raises outside a job.
    """
    flag = getattr(_local, "flag", None)

    def check() -> None:
        if flag is not None and flag.is_set():
            raise Stopped()
    return check


def check() -> None:
    checker()()
