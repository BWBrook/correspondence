#!/usr/bin/env python3
"""Install the loopback browser service as a user launch agent on macOS."""
import os
import plistlib
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent.parent
python = root / ".venv/bin/python"
if not python.exists() or not (root / "web/dist/index.html").exists():
    raise SystemExit("Run uv sync and npm ci --prefix web && npm run build --prefix web first.")
label = "org.bwbrook.correspondence"
data = Path(os.environ.get("CORRESPONDENCE_DATA_DIR", "~/Library/Application Support/Correspondence")).expanduser().resolve()
data.mkdir(parents=True, exist_ok=True, mode=0o700)
logs = data / "logs"
logs.mkdir(exist_ok=True, mode=0o700)
launch_file = Path.home() / "Library/LaunchAgents" / (label + ".plist")
launch_file.parent.mkdir(parents=True, exist_ok=True)
config = {
    "Label": label,
    "ProgramArguments": [str(python), "-m", "correspondence.web"],
    "WorkingDirectory": str(root),
    "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
    "EnvironmentVariables": {"CORRESPONDENCE_DATA_DIR": str(data), "CORRESPONDENCE_PORT": "8765", "PYTHONUNBUFFERED": "1"},
    "StandardOutPath": str(logs / "web.log"), "StandardErrorPath": str(logs / "web-error.log"),
}
if launch_file.exists() and plistlib.loads(launch_file.read_bytes()) != config:
    backup = data / "backups" / "previous-launch-agent.plist"
    backup.parent.mkdir(exist_ok=True)
    if backup.exists():
        raise SystemExit(f"Existing configuration differs and backup already exists: {backup}")
    backup.write_bytes(launch_file.read_bytes())
launch_file.write_bytes(plistlib.dumps(config))
launch_file.chmod(0o600)
target = f"gui/{os.getuid()}"
subprocess.run(["launchctl", "bootout", f"{target}/{label}"], capture_output=True)
subprocess.run(["launchctl", "bootstrap", target, str(launch_file)], check=True)
print("Correspondence installed: http://127.0.0.1:8765")
