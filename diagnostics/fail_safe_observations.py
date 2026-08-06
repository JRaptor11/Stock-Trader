from __future__ import annotations

import csv
from datetime import datetime, timezone
import threading
from typing import Any

from layers.layer_csv import layer_csv_path


_LOCK = threading.Lock()
THRESHOLDS = (2, 3, 4, 5)
FIELDS = [
    "timestamp",
    "symbol",
    "position_qty",
    "entry_price",
    "current_price",
    "loss_percent",
    "configured_threshold_percent",
    "required_confirmations",
    *[
        field
        for threshold in THRESHOLDS
        for field in (
            f"breach_{threshold}_percent",
            f"confirmation_count_{threshold}_percent",
            f"confirmed_crossing_{threshold}_percent",
        )
    ],
]


def append_position_loss_observation(observation: dict[str, Any]) -> None:
    """Persist one broker-position loss observation for threshold analysis."""
    path = layer_csv_path("fail_safe_position_observations.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {field: observation.get(field) for field in FIELDS}
    row["timestamp"] = (
        observation.get("timestamp")
        or datetime.now(timezone.utc).isoformat()
    )

    with _LOCK:
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=FIELDS,
                extrasaction="ignore",
            )
            if write_header:
                writer.writeheader()
            writer.writerow(row)
