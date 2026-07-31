from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import threading
from typing import Any

from layers.layer_csv import layer_csv_path


_LOCK = threading.Lock()
FIELDS = [
    "timestamp", "event", "symbol", "scope", "lifecycle_id",
    "old_state", "new_state", "trigger_reason", "entry_price_source",
    "entry_price", "trigger_price", "observed_loss_percent", "position_qty",
    "order_id", "order_status", "filled_qty", "remaining_broker_position_qty",
    "retry_count", "next_retry_at_epoch", "reentry_block_until_epoch",
    "error", "details",
]


def append_fail_safe_transition(
    event: str,
    lifecycle: dict,
    *,
    old_state: str | None = None,
    error: Any = None,
    details: Any = None,
) -> None:
    path = layer_csv_path("fail_safe_lifecycle.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {field: lifecycle.get(field) for field in FIELDS}
    row.update({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "old_state": old_state,
        "new_state": lifecycle.get("lifecycle_state"),
        "error": str(error) if error is not None else lifecycle.get("last_error"),
        "details": (
            json.dumps(details, sort_keys=True, default=str)
            if isinstance(details, (dict, list, set, tuple)) else details
        ),
    })
    with _LOCK:
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)
