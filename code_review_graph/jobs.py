"""Background graph builds for the MCP server.

``build_or_update_graph_tool`` never builds inside the server process: it runs
``code-review-graph build|update --progress-file <f>`` as a detached child
(own session, stdin from ``/dev/null``, output to a log under the data dir).
One job runs per repository root; a second request while it runs gets the
same job back. The child reports through the progress file, which also
carries its final result, so ``status_only`` reads real progress and exit.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

RUNNING = "running"
WAITING_FOR_LOCK = "waiting_for_lock"
_ACTIVE = frozenset({RUNNING, WAITING_FOR_LOCK})

# Default time the MCP tool waits for a job before answering ``building``.
DEFAULT_WAIT_SECONDS = 25.0
_POLL_SECONDS = 0.2
_LOG_TAIL_LINES = 20
_PROGRESS_THROTTLE_SECONDS = 0.5


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Child side: the CLI writes its progress here
# ---------------------------------------------------------------------------


class ProgressFile:
    """A JSON progress record, replaced atomically on every update."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.state: dict[str, Any] = {"pid": os.getpid(), "started_at": _now()}
        self._lock = threading.Lock()

    def update(self, **fields: Any) -> None:
        with self._lock:
            self.state.update(fields)
            self.state["updated_at"] = _now()
            try:
                _write_json_atomic(self.path, self.state)
            except OSError as exc:
                logger.warning("Cannot write progress file %s: %s", self.path, exc)


class _ProgressLogHandler(logging.Handler):
    """Mirror INFO log lines into the progress file as ``message``."""

    def __init__(self, progress: ProgressFile) -> None:
        super().__init__(level=logging.INFO)
        self._progress = progress
        self._last = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        now = time.monotonic()
        if now - self._last < _PROGRESS_THROTTLE_SECONDS:
            return
        self._last = now
        try:
            self._progress.update(message=record.getMessage()[:500])
        except Exception:  # noqa: BLE001 - a log handler must never raise
            self.handleError(record)


@contextmanager
def progress_reporting(
    path: Optional[str | Path], command: str,
) -> Iterator[Optional[ProgressFile]]:
    """Report one CLI write command into *path*; a no-op when *path* is None.

    The caller records success with ``progress.update(status="ok", ...)``.
    ``SystemExit`` and exceptions are recorded with their exit code.
    """
    if not path:
        yield None
        return
    progress = ProgressFile(path)
    progress.update(status=RUNNING, command=command, phase="starting")
    handler = _ProgressLogHandler(progress)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        yield progress
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if progress.state.get("status") in _ACTIVE:
            from .locking import EXIT_LOCK_BUSY

            status = {0: "ok", EXIT_LOCK_BUSY: "lock_busy"}.get(code, "error")
            progress.update(status=status, exit_code=code, finished_at=_now())
        raise
    except BaseException as exc:
        progress.update(
            status="error", exit_code=1, message=str(exc)[:2000], finished_at=_now(),
        )
        raise
    else:
        if progress.state.get("status") in _ACTIVE:
            progress.update(status="ok", exit_code=0, finished_at=_now())
    finally:
        root_logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Server side: start, track and report jobs
# ---------------------------------------------------------------------------


@dataclass
class _Job:
    job_id: str
    root: str
    record_path: Path
    progress_path: Path
    log_path: Path
    proc: Optional[subprocess.Popen[bytes]] = None


_jobs_lock = threading.Lock()
_jobs: dict[str, _Job] = {}


def _paths(root: Path) -> tuple[Path, Path, Path]:
    from .incremental import get_data_dir

    jobs_dir = get_data_dir(root) / "jobs"
    return jobs_dir / "build.json", jobs_dir / "build.progress.json", jobs_dir / "build.log"


def _pid_alive(pid: int) -> bool:
    from .daemon import pid_alive

    return pid_alive(pid)


def _log_tail(path: Path) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16384))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    return lines[-_LOG_TAIL_LINES:]


def build_command(
    root: Path,
    progress_path: Path,
    *,
    full_rebuild: bool,
    base: Optional[str],
    postprocess: str,
    embedding_provider: Optional[str],
    embedding_model: Optional[str],
) -> list[str]:
    cmd = [
        sys.executable, "-m", "code_review_graph",
        "build" if full_rebuild else "update",
        "--repo", str(root), "--progress-file", str(progress_path),
        "--if-locked", "wait",
    ]
    if base and not full_rebuild:
        cmd += ["--base", base]
    if postprocess == "none":
        cmd.append("--skip-postprocess")
    elif postprocess == "minimal":
        cmd.append("--skip-flows")
    elif postprocess != "full":
        raise ValueError(f"postprocess must be full, minimal or none, not {postprocess!r}")
    if embedding_provider or embedding_model:
        if not (embedding_provider and embedding_model):
            raise ValueError("embedding_provider and embedding_model must be supplied together")
        cmd += ["--embedding-provider", embedding_provider, "--embedding-model", embedding_model]
    return cmd


def _status_of(job: _Job) -> dict[str, Any]:
    progress = _read_json(job.progress_path) or {}
    record = _read_json(job.record_path) or {}
    returncode = job.proc.poll() if job.proc is not None else None
    pid = record.get("pid")
    alive = (
        returncode is None
        if job.proc is not None
        else isinstance(pid, int) and _pid_alive(pid)
    )
    status = progress.get("status", RUNNING)
    if not alive and status in _ACTIVE:
        # Killed before it could report (SIGKILL, OOM): never read as success.
        status = "error"
        if progress.get("message"):
            progress["last_message"] = progress["message"]
        progress["message"] = "build process exited without reporting a result"
        progress["exit_code"] = returncode
    response: dict[str, Any] = {
        "job_id": job.job_id,
        "status": "building" if status in _ACTIVE else status,
        "phase": progress.get("phase") or status,
        "started_at": record.get("started_at") or progress.get("started_at"),
        "updated_at": progress.get("updated_at"),
        "pid": pid,
        "log_file": str(job.log_path),
        "progress_file": str(job.progress_path),
    }
    for key in ("message", "last_message"):
        if progress.get(key):
            response[key] = progress[key]
    if status in _ACTIVE:
        response["summary"] = (
            f"Graph build job {job.job_id} is running; call "
            "build_or_update_graph(status_only=True) for progress."
        )
        return response
    response["exit_code"] = progress.get("exit_code", returncode)
    if status == "ok":
        result = progress.get("result") if isinstance(progress.get("result"), dict) else {}
        return {**result, **response, "status": result.get("status", "ok")}
    response["error_code"] = {
        "lock_busy": "lock_busy", "rebuild_required": "rebuild_required",
    }.get(status, "build_failed")
    response["status"] = "error"
    response.setdefault("message", f"graph build job ended with status {status}")
    response["summary"] = response["message"]
    response["log_tail"] = _log_tail(job.log_path)
    return response


def _job_from_record(root: Path) -> Optional[_Job]:
    record_path, progress_path, log_path = _paths(root)
    record = _read_json(record_path)
    if record is None:
        return None
    return _Job(
        job_id=str(record.get("job_id", "unknown")), root=str(root),
        record_path=record_path, progress_path=progress_path, log_path=log_path,
    )


def _current(root: Path) -> Optional[_Job]:
    job = _jobs.get(str(root))
    return job if job is not None else _job_from_record(root)


def start_build_job(root: str | Path, **options: Any) -> dict[str, Any]:
    """Start a build job for *root*, or return the one already running."""
    root = Path(root).resolve()
    recurse = options.pop("recurse_submodules", None)
    with _jobs_lock:
        current = _current(root)
        if current is not None:
            status = _status_of(current)
            if status["status"] == "building":
                return {**status, "already_running": True}
        record_path, progress_path, log_path = _paths(root)
        cmd = build_command(root, progress_path, **options)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        job = _Job(
            job_id=uuid.uuid4().hex[:12], root=str(root), record_path=record_path,
            progress_path=progress_path, log_path=log_path,
        )
        _write_json_atomic(progress_path, {
            "status": RUNNING, "phase": "queued", "started_at": _now(), "updated_at": _now(),
        })
        env = dict(os.environ)
        # The server never holds the writer lock, so the child must not inherit a token.
        env.pop("CRG_WRITER_LOCK_TOKEN", None)
        if recurse is not None:
            env["CRG_RECURSE_SUBMODULES"] = "1" if recurse else "0"
        log_fd = open(log_path, "ab")  # noqa: SIM115 - handed to the child
        try:
            popen_kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0,
                )
            else:
                popen_kwargs["start_new_session"] = True
            job.proc = subprocess.Popen(
                cmd, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                stdout=log_fd, stderr=subprocess.STDOUT, **popen_kwargs,
            )
        finally:
            log_fd.close()
        _write_json_atomic(record_path, {
            "job_id": job.job_id, "root": str(root), "pid": job.proc.pid,
            "argv": cmd, "started_at": _now(),
        })
        _jobs[str(root)] = job
        logger.info("Started graph build job %s (pid %d) for %s", job.job_id, job.proc.pid, root)
        return _status_of(job)


def build_job_status(root: str | Path) -> dict[str, Any]:
    """The latest job for *root*: running progress, or its final result."""
    root = Path(root).resolve()
    with _jobs_lock:
        job = _current(root)
        if job is None:
            return {"status": "idle", "summary": "no build job has run for this root"}
        return _status_of(job)


def wait_for_build_job(root: str | Path, timeout: float) -> dict[str, Any]:
    """Poll the job for *root* until it finishes or *timeout* elapses."""
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        status = build_job_status(root)
        if status["status"] != "building" or time.monotonic() >= deadline:
            return status
        time.sleep(_POLL_SECONDS)


def run_build_job(
    root: str | Path,
    *,
    full_rebuild: bool = False,
    base: Optional[str] = None,
    postprocess: str = "full",
    recurse_submodules: Optional[bool] = None,
    embedding_provider: Optional[str] = None,
    embedding_model: Optional[str] = None,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
) -> dict[str, Any]:
    """Start (or join) the job for *root* and wait up to *wait_seconds* for it."""
    started = start_build_job(
        root, full_rebuild=full_rebuild, base=base, postprocess=postprocess,
        recurse_submodules=recurse_submodules,
        embedding_provider=embedding_provider, embedding_model=embedding_model,
    )
    if started["status"] != "building":
        return started
    return wait_for_build_job(root, wait_seconds)
