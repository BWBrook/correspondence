"""Resolve one server's machine label from its launch environment and aliases."""
from __future__ import annotations

import os
import re
import socket
import tomllib
from pathlib import Path

ALIAS_CONFIG = Path(__file__).with_name("machine-aliases.toml")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def short_name(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\r\n"):
        raise ValueError("Machine must be a short hostname or label")
    parts = value.strip().rstrip(".").lower().split(".")
    if not parts or any(not LABEL.fullmatch(part) for part in parts):
        raise ValueError("Machine must be a 1–63 character hostname label")
    return parts[0]


def resolve_machine(config: Path | None = None) -> str:
    with (config or ALIAS_CONFIG).open("rb") as handle:
        aliases = tomllib.load(handle).get("aliases", {})
    if not isinstance(aliases, dict):
        raise ValueError("Machine aliases must be a table")
    mapped = {}
    for source, target in aliases.items():
        key, label = short_name(source), short_name(target)
        if key in mapped and mapped[key] != label:
            raise ValueError("Conflicting aliases for the same short hostname")
        mapped[key] = label
    supplied = os.environ.get("CORRESPONDENCE_MACHINE")
    name = short_name(socket.gethostname() if supplied is None else supplied)
    return mapped.get(name, name)
