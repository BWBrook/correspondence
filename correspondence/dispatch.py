"""Deliver a prompt Barry sent from the board to a project's main agent session.

Dispatch starts only from a browser request; agents have no tool that creates
one. Codex prompts go into the Codex queue for the pinned thread, which the
Codex desktop app picks up and runs. Claude delivery needs a messenger session
and is not available yet.
"""
from __future__ import annotations

import os
import shutil
import subprocess


def codex_binary() -> str:
    return os.environ.get("CORRESPONDENCE_CODEX") or shutil.which("codex") or "/opt/homebrew/bin/codex"


def deliver_codex(session_id: str, body: str) -> tuple[bool, str]:
    try:
        result = subprocess.run([codex_binary(), "queue", "--thread", session_id, "--message", body],
                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"{type(error).__name__}: {error}"
    detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return result.returncode == 0, detail
