"""Small runtime helpers safe for the always-resident API coordinator.

Keep this module dependency-free.  Importing the replay engine from the web
process defeats worker isolation because Render accounts both processes in the
same 512 MiB cgroup.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def write_json_atomic(path: str | Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        for attempt in range(50):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(min(0.01 * (attempt + 1), 0.1))
    finally:
        temporary.unlink(missing_ok=True)


def drop_file_cache(path: str | Path) -> None:
    """Best-effort eviction of file pages charged to Render's memory cgroup."""
    if os.name != "posix" or not hasattr(os, "posix_fadvise"):
        return
    try:
        with Path(path).open("rb") as handle:
            os.posix_fadvise(
                handle.fileno(), 0, 0, getattr(os, "POSIX_FADV_DONTNEED", 4)
            )
    except OSError:
        pass


def cgroup_memory_snapshot() -> dict:
    """Return whole-service Linux cgroup memory, including OOM history."""
    snapshot = {}
    for key, candidates in {
        "service_memory_current_bytes": (
            "/sys/fs/cgroup/memory.current",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        ),
        "service_memory_limit_bytes": (
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ),
    }.items():
        for candidate in candidates:
            try:
                value = Path(candidate).read_text(encoding="utf-8").strip()
                if value != "max":
                    snapshot[key] = int(value)
                break
            except (OSError, ValueError):
                continue
    current = snapshot.get("service_memory_current_bytes")
    limit = snapshot.get("service_memory_limit_bytes")
    if current is not None and limit:
        snapshot["service_memory_pct"] = round(current / limit * 100.0, 2)
    try:
        for line in Path("/sys/fs/cgroup/memory.events").read_text(
            encoding="utf-8"
        ).splitlines():
            name, value = line.split(maxsplit=1)
            if name in {"high", "max", "oom", "oom_kill"}:
                snapshot[f"service_memory_events_{name}"] = int(value)
    except (OSError, ValueError):
        pass
    return snapshot
