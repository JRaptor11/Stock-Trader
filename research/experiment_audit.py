"""Tamper-evident audit records for research trials and holdout consumption.

The trial ledger records an accepted experiment before its worker starts.  It
is deliberately separate from result manifests: failed and superseded jobs are
still trials and therefore still count toward the researcher's search budget.

Holdouts are never inferred from prose.  A job consumes an untouched holdout
only when ``experiment.holdout_consumption`` contains the complete structured
declaration documented by :func:`holdout_consumption_from_job`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
UNENUMERATED_VARIANT = "JOB_LEVEL_UNENUMERATED"
_LOCK = threading.RLock()
_CONFIG_FIELDS = (
    "replay_config",
    "tier1_config",
    "intraday_config",
)
_DATASET_FIELDS = (
    "bars_csv",
    "security_master_csv",
    "market_events_csv",
    "fundamentals_csv",
)


class AuditIntegrityError(ValueError):
    """Raised when an existing audit ledger is malformed or tampered with."""


class HoldoutReuseError(ValueError):
    """Raised when a supposedly untouched holdout overlaps consumed evidence."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_timestamp(value: Any, field: str) -> str:
    normalized = _nonempty(value, field)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _nonempty(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _iso_date(value: Any, field: str) -> str:
    normalized = _nonempty(value, field)
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _chain_hash(row: dict) -> str:
    return _hash({key: value for key, value in row.items() if key != "chain_sha256"})


@contextmanager
def _ledger_guard(path: Path):
    """Serialize a complete read/validate/write cycle across processes.

    The coordinator already serializes API submissions, but this module is
    also used by local research tooling.  A sibling lock file keeps separate
    Python processes from reading the same chain tip and then overwriting one
    another.  The lock file contains no research data and may safely persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                with _LOCK:
                    yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                with _LOCK:
                    yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_ledger(path: Path, record_type: str, identity_fields: tuple[str, ...]) -> list[dict]:
    if not path.is_file():
        return []
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AuditIntegrityError(f"{path.name} cannot be read as UTF-8") from exc
    if not payload:
        return []
    if not payload.endswith(b"\n"):
        raise AuditIntegrityError(f"{path.name} has an incomplete final row")
    rows: list[dict] = []
    previous = None
    identities = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise AuditIntegrityError(f"{path.name} row {number} is blank")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditIntegrityError(f"{path.name} row {number} is not valid JSON") from exc
        if not isinstance(row, dict):
            raise AuditIntegrityError(f"{path.name} row {number} is not a JSON object")
        if row.get("schema_version") != SCHEMA_VERSION or row.get("record_type") != record_type:
            raise AuditIntegrityError(f"{path.name} row {number} has an invalid schema")
        if row.get("previous_chain_sha256") != previous:
            raise AuditIntegrityError(f"{path.name} row {number} has an invalid chain link")
        if not row.get("chain_sha256") or row["chain_sha256"] != _chain_hash(row):
            raise AuditIntegrityError(f"{path.name} row {number} has an invalid row hash")
        identity = tuple(row.get(field) for field in identity_fields)
        if identity in identities:
            raise AuditIntegrityError(f"{path.name} contains duplicate identity {identity}")
        identities.add(identity)
        rows.append(row)
        previous = row["chain_sha256"]
    return rows


def _append_rows(path: Path, rows: list[dict], previous: str | None) -> list[dict]:
    """Atomically replace the ledger with its verified prefix plus new rows.

    Direct append plus ``fsync`` can still leave a torn JSON row after process
    termination.  Writing a same-directory temporary and ``os.replace`` keeps
    either the old complete chain or the new complete chain visible.
    """
    if not rows:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    written = []
    encoded_rows = []
    for supplied in rows:
        row = {**supplied, "previous_chain_sha256": previous}
        row["chain_sha256"] = _chain_hash(row)
        encoded_rows.append(
            (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        )
        written.append(row)
        previous = row["chain_sha256"]
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            if path.is_file():
                with path.open("rb") as existing:
                    shutil.copyfileobj(existing, handle, length=1024 * 1024)
            for encoded in encoded_rows:
                handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(50):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Antivirus and OneDrive may briefly hold the destination on
                # Windows even though the ledger lock is ours.  Retry only
                # this transient sharing violation; other I/O failures remain
                # immediate and fail closed.
                if attempt == 49:
                    raise
                time.sleep(min(0.01 * (attempt + 1), 0.1))
        if os.name == "posix":
            try:
                directory_descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                # File durability has already been requested.  Some mounted
                # filesystems do not support fsync on directories.
                pass
    finally:
        temporary.unlink(missing_ok=True)
    return written


def _public_job(job: dict) -> dict:
    """Remove coordinator-only metadata before creating immutable identities."""
    return {key: value for key, value in job.items() if not str(key).startswith("_")}


def _shared_variant_definition(job: dict) -> dict:
    experiment = dict(job.get("experiment") or {})
    # Identity and evidence-use fields describe the run, not the strategy
    # variant.  Excluding them means an exact replication on another dataset
    # does not inflate the distinct-variant count.
    for key in (
        "trial_id", "tested_variants", "holdout_consumption", "holdout_policy",
    ):
        experiment.pop(key, None)
    definition = {
        "engine": str(job.get("engine") or "intraday_replay").strip().lower(),
        "experiment": experiment,
    }
    for field in _CONFIG_FIELDS:
        if field in job:
            definition[field] = job[field]
    return definition


def trial_records_from_job(job: dict, *, accepted_at: str | None = None) -> list[dict]:
    """Return one immutable trial record per explicitly enumerated variant.

    Jobs that do not enumerate ``experiment.tested_variants`` still receive a
    job-level record.  Such a record is marked incomplete so family-wide trial
    counts are honestly reported as lower bounds rather than exact totals.
    """
    job_id = _nonempty(job.get("job_id"), "job_id")
    recorded_at = _iso_timestamp(accepted_at or _utc_now(), "accepted_at")
    experiment = dict(job.get("experiment") or {})
    family_id = _nonempty(
        experiment.get("hypothesis_id") or "UNDECLARED", "hypothesis_id"
    ).upper()
    trial_id = _nonempty(experiment.get("trial_id") or job_id, "trial_id")
    variants = experiment.get("tested_variants")
    enumeration_complete = variants is not None
    if variants is None:
        variants = [{"variant_id": UNENUMERATED_VARIANT}]
    if not isinstance(variants, list) or not variants:
        raise ValueError("experiment.tested_variants must be a non-empty JSON list")

    shared = _shared_variant_definition(job)
    shared_sha256 = _hash(shared)
    job_sha256 = _hash(_public_job(job))
    dataset_hashes = job.get("dataset_sha256") or {}
    if not isinstance(dataset_hashes, dict):
        raise ValueError("dataset_sha256 must be a JSON object")
    unexpected_dataset_hashes = sorted(set(dataset_hashes).difference(_DATASET_FIELDS))
    if unexpected_dataset_hashes:
        raise ValueError(
            f"dataset_sha256 has unexpected fields: {unexpected_dataset_hashes}"
        )
    for field, value in dataset_hashes.items():
        digest = str(value or "").strip().lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(
                f"dataset_sha256.{field} must be a 64-character hexadecimal digest"
            )
    data_references = {
        field: {
            "filename": str(job[field]),
            "sha256": str(dataset_hashes.get(field) or "").strip().lower() or None,
        }
        for field in _DATASET_FIELDS if job.get(field)
    }
    seen = set()
    records = []
    for variant in variants:
        if not isinstance(variant, dict):
            raise ValueError("each tested variant must be a JSON object")
        variant_id = _nonempty(variant.get("variant_id"), "tested variant variant_id")
        if variant_id in seen:
            raise ValueError(f"duplicate tested variant id: {variant_id}")
        seen.add(variant_id)
        # The durable job object remains the human-readable source of truth.
        # Keep only its exact hashes here so the append-only ledger stays tiny
        # instead of repeating a large shared configuration for every variant.
        variant_definition = {"shared": shared, "variant": variant}
        strategy_id = str(
            variant.get("strategy_id") or variant.get("strategy") or ""
        ).strip() or None
        records.append({
            "schema_version": SCHEMA_VERSION,
            "record_type": "trial",
            "recorded_at": recorded_at,
            "job_id": job_id,
            "family_id": family_id,
            "trial_id": trial_id,
            "variant_id": variant_id,
            "variant_enumeration_complete": enumeration_complete,
            "strategy_id": strategy_id,
            "shared_definition_sha256": shared_sha256,
            "variant_sha256": _hash(variant_definition),
            "job_definition_sha256": job_sha256,
            "engine": shared["engine"],
            "data_references": data_references,
        })
    return records


def append_job_trials(path: str | Path, job: dict, *, accepted_at: str | None = None) -> dict:
    """Append all new variants from an accepted job, idempotently."""
    path = Path(path)
    candidates = trial_records_from_job(job, accepted_at=accepted_at)
    with _ledger_guard(path):
        existing = _read_ledger(path, "trial", ("job_id", "variant_id"))
        by_identity = {(row["job_id"], row["variant_id"]): row for row in existing}
        pending = []
        unchanged = 0
        for candidate in candidates:
            identity = (candidate["job_id"], candidate["variant_id"])
            prior = by_identity.get(identity)
            if prior:
                immutable = {
                    key: value for key, value in prior.items()
                    if key not in {"recorded_at", "previous_chain_sha256", "chain_sha256"}
                }
                compare = {
                    key: value for key, value in candidate.items()
                    if key != "recorded_at"
                }
                if immutable != compare:
                    raise AuditIntegrityError(
                        f"trial identity {identity} was reused for a different definition"
                    )
                unchanged += 1
                continue
            pending.append(candidate)
        written = _append_rows(
            path, pending, existing[-1]["chain_sha256"] if existing else None,
        )
        all_rows = existing + written
    return {
        "status": "appended" if written else "unchanged",
        "new_records": len(written),
        "unchanged_records": unchanged,
        "total_records": len(all_rows),
        "chain_sha256": all_rows[-1]["chain_sha256"] if all_rows else None,
        "job_definition_sha256": candidates[0]["job_definition_sha256"],
        "variant_enumeration_complete": all(
            row["variant_enumeration_complete"] for row in candidates
        ),
    }


def trial_family_summary(path: str | Path) -> dict:
    """Return multiple-testing counts and explicit lower-bound warnings."""
    path = Path(path)
    if path.is_file():
        with _ledger_guard(path):
            rows = _read_ledger(path, "trial", ("job_id", "variant_id"))
    else:
        rows = []
    families: dict[str, dict] = {}
    for row in rows:
        family = families.setdefault(row["family_id"], {
            "accepted_trial_records": 0,
            "distinct_variant_count": 0,
            "variant_count_is_lower_bound": False,
            "unenumerated_job_ids": set(),
            "_variants": set(),
        })
        family["accepted_trial_records"] += 1
        family["_variants"].add(row["variant_sha256"])
        if not row["variant_enumeration_complete"]:
            family["variant_count_is_lower_bound"] = True
            family["unenumerated_job_ids"].add(row["job_id"])
    for family in families.values():
        family["distinct_variant_count"] = len(family.pop("_variants"))
        family["unenumerated_job_ids"] = sorted(family["unenumerated_job_ids"])
    return {
        "schema_version": SCHEMA_VERSION,
        "accepted_trial_records": len(rows),
        "families": dict(sorted(families.items())),
        "chain_sha256": rows[-1]["chain_sha256"] if rows else None,
        "coverage_begins_at": rows[0]["recorded_at"] if rows else None,
        "pre_ledger_trials_not_reconstructed": True,
    }


def holdout_consumption_from_job(job: dict) -> dict | None:
    """Return a normalized explicit holdout declaration, or ``None``.

    Free-form ``holdout_policy`` text intentionally has no effect.  The
    required structure is::

        experiment.holdout_consumption = {
          "holdout_id": "...", "consumer_id": "...", "dataset_id": "...",
          "universe_id": "...", "frequency": "1d",
          "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD",
          "purpose": "locked validation"
        }
    """
    experiment = job.get("experiment") or {}
    supplied = experiment.get("holdout_consumption")
    if supplied is None:
        return None
    if not isinstance(supplied, dict):
        raise ValueError("experiment.holdout_consumption must be a JSON object")
    allowed_fields = {
        "holdout_id", "consumer_id", "dataset_id", "universe_id", "frequency",
        "start_date", "end_date", "purpose", "dataset_sha256",
    }
    unexpected = sorted(set(supplied).difference(allowed_fields))
    if unexpected:
        raise ValueError(
            f"experiment.holdout_consumption has unexpected fields: {unexpected}"
        )
    normalized = {
        "holdout_id": _nonempty(supplied.get("holdout_id"), "holdout_id").casefold(),
        "consumer_id": _nonempty(supplied.get("consumer_id"), "consumer_id").casefold(),
        "dataset_id": _nonempty(supplied.get("dataset_id"), "dataset_id").casefold(),
        "universe_id": _nonempty(supplied.get("universe_id"), "universe_id").casefold(),
        "frequency": _nonempty(supplied.get("frequency"), "frequency").lower(),
        "start_date": _iso_date(supplied.get("start_date"), "start_date"),
        "end_date": _iso_date(supplied.get("end_date"), "end_date"),
        "purpose": _nonempty(supplied.get("purpose"), "purpose"),
        "dataset_sha256": (
            str(supplied.get("dataset_sha256") or "").strip().lower() or None
        ),
    }
    if normalized["end_date"] < normalized["start_date"]:
        raise ValueError("holdout end_date precedes start_date")
    sha = normalized["dataset_sha256"]
    if sha and (len(sha) != 64 or any(character not in "0123456789abcdefABCDEF" for character in sha)):
        raise ValueError("dataset_sha256 must be a 64-character hexadecimal digest")
    return normalized


def _holdout_overlap(left: dict, right: dict) -> bool:
    dimensions = ("dataset_id", "universe_id", "frequency")
    return (
        all(left[field] == right[field] for field in dimensions)
        and left["start_date"] <= right["end_date"]
        and right["start_date"] <= left["end_date"]
    )


def append_holdout_consumption(
    path: str | Path, consumption: dict, *, consumed_at: str | None = None,
) -> dict:
    """Consume an untouched holdout, rejecting any prior overlapping use."""
    # Reuse the job parser's normalization by constructing the exact explicit
    # envelope.  This keeps direct callers and API-submitted jobs identical.
    normalized = holdout_consumption_from_job({
        "experiment": {"holdout_consumption": consumption},
    })
    assert normalized is not None
    path = Path(path)
    consumed_at = _iso_timestamp(consumed_at or _utc_now(), "consumed_at")
    with _ledger_guard(path):
        existing = _read_ledger(
            path, "holdout_consumption", ("holdout_id", "consumer_id"),
        )
        identity = (normalized["holdout_id"], normalized["consumer_id"])
        for row in existing:
            prior_identity = (row["holdout_id"], row["consumer_id"])
            prior_scope = {
                key: row.get(key) for key in normalized
            }
            if prior_identity == identity:
                if prior_scope != normalized:
                    raise AuditIntegrityError(
                        f"holdout identity {identity} was reused for a different scope"
                    )
                return {
                    "status": "unchanged", "new_records": 0,
                    "total_records": len(existing),
                    "chain_sha256": existing[-1]["chain_sha256"],
                }
            if _holdout_overlap(row, normalized):
                raise HoldoutReuseError(
                    "holdout scope overlaps evidence already consumed by "
                    f"{row['consumer_id']} ({row['holdout_id']}: "
                    f"{row['start_date']} through {row['end_date']})"
                )
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "holdout_consumption",
            "consumed_at": consumed_at,
            **normalized,
        }
        written = _append_rows(
            path, [record], existing[-1]["chain_sha256"] if existing else None,
        )
        rows = existing + written
    return {
        "status": "appended", "new_records": 1,
        "total_records": len(rows), "chain_sha256": rows[-1]["chain_sha256"],
    }


def holdout_registry_summary(path: str | Path) -> dict:
    """Validate the holdout chain and return compact durable-state metadata."""
    path = Path(path)
    if path.is_file():
        with _ledger_guard(path):
            rows = _read_ledger(
                path, "holdout_consumption", ("holdout_id", "consumer_id"),
            )
    else:
        rows = []
    return {
        "schema_version": SCHEMA_VERSION,
        "consumed_holdouts": len(rows),
        "chain_sha256": rows[-1]["chain_sha256"] if rows else None,
        "coverage_begins_at": rows[0]["consumed_at"] if rows else None,
    }


def audit_integrity_summary(
    *, trial_ledger: str | Path, holdout_registry: str | Path,
) -> dict:
    """Fail closed on either corrupt chain and report their current anchors."""
    return {
        "trials": trial_family_summary(trial_ledger),
        "holdouts": holdout_registry_summary(holdout_registry),
    }


def assert_audit_anchors(
    *,
    trial_ledger: str | Path,
    holdout_registry: str | Path,
    required_trial_anchors: list[str] | tuple[str, ...] | set[str] = (),
    required_holdout_anchors: list[str] | tuple[str, ...] | set[str] = (),
) -> dict:
    """Require durable status anchors to exist in the restored chains.

    A hash chain detects edits and interior deletion, but a valid prefix is
    also a valid chain.  Job status objects retain the chain tips observed at
    acceptance; checking every such tip makes accidental object rollback or
    truncation fail closed during coordinator recovery.
    """
    trial_path = Path(trial_ledger)
    holdout_path = Path(holdout_registry)
    if trial_path.is_file():
        with _ledger_guard(trial_path):
            trial_rows = _read_ledger(trial_path, "trial", ("job_id", "variant_id"))
    else:
        trial_rows = []
    if holdout_path.is_file():
        with _ledger_guard(holdout_path):
            holdout_rows = _read_ledger(
                holdout_path,
                "holdout_consumption",
                ("holdout_id", "consumer_id"),
            )
    else:
        holdout_rows = []

    available_trial = {str(row["chain_sha256"]) for row in trial_rows}
    available_holdout = {str(row["chain_sha256"]) for row in holdout_rows}
    required_trial = {str(value).strip().lower() for value in required_trial_anchors if value}
    required_holdout = {
        str(value).strip().lower() for value in required_holdout_anchors if value
    }
    for label, anchors in (
        ("trial", required_trial), ("holdout", required_holdout),
    ):
        malformed = sorted(
            anchor for anchor in anchors
            if len(anchor) != 64 or any(character not in "0123456789abcdef" for character in anchor)
        )
        if malformed:
            raise AuditIntegrityError(f"recorded {label} audit anchor is malformed")
    missing_trial = sorted(required_trial.difference(available_trial))
    missing_holdout = sorted(required_holdout.difference(available_holdout))
    if missing_trial:
        raise AuditIntegrityError(
            "trial ledger is missing durable status anchors; possible rollback or truncation: "
            + ", ".join(missing_trial)
        )
    if missing_holdout:
        raise AuditIntegrityError(
            "holdout registry is missing durable status anchors; possible rollback or truncation: "
            + ", ".join(missing_holdout)
        )
    return {
        "required_trial_anchors": len(required_trial),
        "required_holdout_anchors": len(required_holdout),
        "latest_trial_chain_sha256": trial_rows[-1]["chain_sha256"] if trial_rows else None,
        "latest_holdout_chain_sha256": (
            holdout_rows[-1]["chain_sha256"] if holdout_rows else None
        ),
    }


def register_job_audit(
    *, job: dict, trial_ledger: str | Path, holdout_registry: str | Path,
    accepted_at: str | None = None,
) -> dict:
    """Record accepted trials and any explicit untouched-holdout consumption."""
    accepted_at = _iso_timestamp(accepted_at or _utc_now(), "accepted_at")
    # Fully validate both declarations before mutating either ledger.  Holdout
    # writes deliberately happen first: an I/O failure between the two files
    # may conservatively consume evidence, but can never allow consumed
    # evidence to remain labeled untouched.
    trial_records_from_job(job, accepted_at=accepted_at)
    consumption = holdout_consumption_from_job(job)
    holdout = (
        append_holdout_consumption(
            holdout_registry, consumption, consumed_at=accepted_at,
        ) if consumption else {"status": "not_declared", "new_records": 0}
    )
    trials = append_job_trials(trial_ledger, job, accepted_at=accepted_at)
    return {"trials": trials, "holdout": holdout}
