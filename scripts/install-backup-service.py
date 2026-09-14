#!/usr/bin/env python3
"""Install the daily 03:15 local-time backup as a macOS user launch agent."""
from datetime import datetime, timezone
import os
from pathlib import Path
import plistlib
import subprocess
import sys


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("This installer requires macOS launchd.")
    root = Path(__file__).resolve().parent.parent
    python = root / ".venv/bin/python"
    if not python.exists():
        raise SystemExit("Run uv sync first.")
    data = Path(os.environ.get("CORRESPONDENCE_DATA_DIR", "~/Library/Application Support/Correspondence")).expanduser().resolve()
    if not (data / "board.sqlite3").is_file():
        raise SystemExit(f"No existing board database in {data}")
    logs = data / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    label = "org.bwbrook.correspondence.backup"
    launch_file = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    launch_file.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": label,
        "ProgramArguments": [str(python), str(root / "scripts/backup.py")],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": {"Hour": 3, "Minute": 15},
        "EnvironmentVariables": {"CORRESPONDENCE_DATA_DIR": str(data), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(logs / "backup.log"),
        "StandardErrorPath": str(logs / "backup-error.log"),
    }
    if launch_file.exists() and plistlib.loads(launch_file.read_bytes()) != config:
        previous = launch_file.with_name(launch_file.name + ".previous-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        with previous.open("xb") as handle:
            previous.chmod(0o600)
            handle.write(launch_file.read_bytes())
    launch_file.write_bytes(plistlib.dumps(config))
    launch_file.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(launch_file)], check=True)
    print(f"Installed {label}: daily at 03:15 local time; 30-day automatic-backup retention.")


if __name__ == "__main__":
    main()
