"""Fail-closed audit for every maintained prospective research track.

The guard never advances a ledger.  It verifies freshness, hash continuity,
configuration continuity, broker isolation, and the remote prospective-shadow
cursor before and after the maintenance workflow does its work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def audit_ledger(spec: dict, workspace_root: Path, required_session: str | None) -> dict:
    path = Path(str(spec["path"]).format(workspace_root=workspace_root))
    rows = _read_rows(path)
    errors: list[str] = []
    previous = None
    config_hashes = set()
    dates = []
    for index, row in enumerate(rows, 1):
        if row.get("previous_chain_sha256") != previous:
            errors.append(f"row {index} has broken previous-chain linkage")
        stored = row.get("chain_sha256")
        unsigned = {key: value for key, value in row.items() if key != "chain_sha256"}
        if not stored or stored != hashlib.sha256(_canonical(unsigned)).hexdigest():
            errors.append(f"row {index} has an invalid record hash")
        previous = stored
        if row.get("config_sha256"):
            config_hashes.add(row["config_sha256"])
        if row.get("broker_orders_enabled") is True:
            errors.append(f"row {index} enables broker orders")
        if row.get("paper_trading_approved") is True or row.get("live_trading_approved") is True:
            errors.append(f"row {index} contains trading approval")
        value = row.get(spec["date_field"])
        if value:
            dates.append(str(value))
    if not rows:
        errors.append("ledger is missing or empty")
    latest = max(dates) if dates else None
    if required_session and latest != required_session:
        errors.append(f"latest session is {latest or 'missing'}; required {required_session}")
    if len(config_hashes) > 1:
        errors.append("multiple configuration hashes are present")
    state_path = spec.get("state_path")
    if state_path:
        state_file = Path(str(state_path).format(workspace_root=workspace_root))
        if not state_file.is_file():
            errors.append("rolling state is missing")
        else:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if state.get("last_session") != latest:
                errors.append("rolling-state session disagrees with ledger")
            if rows and state.get("chain_sha256") != rows[-1].get("chain_sha256"):
                errors.append("rolling-state chain hash disagrees with ledger")
            if state.get("broker_orders_enabled") is True:
                errors.append("rolling state enables broker orders")
    return {
        "id": spec["id"], "path": str(path), "rows": len(rows),
        "latest_session": latest, "required_session": required_session,
        "config_sha256": next(iter(config_hashes)) if len(config_hashes) == 1 else None,
        "chain_sha256": previous, "ok": not errors, "errors": errors,
    }


def fetch_service_status(url: str, token: str, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(
        url.rstrip("/") + "/api/shadow/status",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def build_report(policy: dict, workspace_root: Path, completed_session: str,
                 weekly_session: str | None, service_status: dict | None) -> dict:
    ledgers = []
    for spec in policy["ledgers"]:
        required = completed_session if spec["cadence"] == "daily" else weekly_session
        ledgers.append(audit_ledger(spec, workspace_root, required))
    service_errors = []
    if service_status is None:
        service_errors.append("service status was not supplied")
    else:
        if service_status.get("last_session") != completed_session:
            service_errors.append(
                f"remote shadow is at {service_status.get('last_session')}; "
                f"required {completed_session}"
            )
        if service_status.get("broker_orders_enabled") is not False:
            service_errors.append("remote shadow does not explicitly disable broker orders")
        readiness = service_status.get("readiness") or {}
        if readiness.get("status") not in {"completed", "legacy_completed"}:
            service_errors.append(
                f"remote activation readiness is {readiness.get('status') or 'unknown'}"
            )
    return {
        "policy_id": policy["policy_id"],
        "completed_session": completed_session,
        "weekly_required_session": weekly_session,
        "service": {"ok": not service_errors, "errors": service_errors,
                    "last_session": (service_status or {}).get("last_session"),
                    "expected_next_session": (service_status or {}).get("expected_next_session")},
        "ledgers": ledgers,
        "ok": not service_errors and all(item["ok"] for item in ledgers),
        "broker_orders_enabled": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path,
                        default=Path(__file__).with_name("forward-maintenance-policy-001.json"))
    parser.add_argument("--workspace-root", type=Path,
                        default=Path(__file__).resolve().parents[2])
    parser.add_argument("--completed-session", required=True)
    parser.add_argument("--weekly-required-session")
    parser.add_argument("--service-url", default="https://stock-trader-9du8.onrender.com")
    parser.add_argument("--service-status-json", type=Path)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    if args.service_status_json:
        status = json.loads(args.service_status_json.read_text(encoding="utf-8"))
    else:
        token = os.getenv("RESEARCH_API_TOKEN")
        if not token:
            raise SystemExit("RESEARCH_API_TOKEN is required")
        status = fetch_service_status(args.service_url, token)
    report = build_report(
        policy, args.workspace_root.resolve(), args.completed_session,
        args.weekly_required_session, status,
    )
    json.dump(report, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
