"""Process-start diagnostics and quiet, timestamped HTTP access logging."""
from __future__ import annotations

import logging
import os
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def capture_process_info(repository=None):
    """Snapshot source once; later checkout changes must not relabel loaded code."""
    root = repository or Path(__file__).resolve().parent.parent
    revision = dirty = None
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {"revision": revision, "dirty": dirty,
            "started_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}


# Shared across imports of web as __main__ and correspondence.web in one process.
PROCESS_INFO = capture_process_info()


def board_info(participant):
    return {"service": "correspondence", "participant": participant, **PROCESS_INFO}


class PollAccessFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's access record: client, method, full path, HTTP version, status.
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            _, method, path, _, status = args
            if method == "GET" and isinstance(status, int) and 200 <= status < 300:
                return urlsplit(path).path not in {"/api/projects", "/api/threads"}
        return True


def access_log_config():
    from uvicorn.config import LOGGING_CONFIG

    config = deepcopy(LOGGING_CONFIG)
    config["filters"] = {"polls": {"()": PollAccessFilter}}
    config["handlers"]["access"]["filters"] = ["polls"]
    for formatter in config["formatters"].values():
        formatter["fmt"] = "%(asctime)s %(levelprefix)s " + (
            '%(client_addr)s - "%(request_line)s" %(status_code)s'
            if formatter["()"] == "uvicorn.logging.AccessFormatter" else "%(message)s"
        )
        formatter["datefmt"] = "%Y-%m-%dT%H:%M:%S%z"
    return config
