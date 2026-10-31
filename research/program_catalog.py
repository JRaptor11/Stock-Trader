"""Deterministic inventory of the research program's declared experiments.

The live trial ledger records newly accepted jobs, but it began after much of
the historical research had already been performed.  This module reconstructs
the *declared* pre-ledger lineage from version-controlled JSON files without
pretending that their original acceptance timestamps or exact search order are
known.  The resulting catalog is an honest lower-bound inventory, not a
replacement for the append-only prospective trial ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _family_id(payload: dict) -> str | None:
    experiment = payload.get("experiment") or {}
    value = (
        experiment.get("hypothesis_id")
        or payload.get("hypothesis_id")
        or payload.get("study_id")
    )
    normalized = str(value or "").strip().upper()
    return normalized or None


def _evidence_class(payload: dict, family_id: str) -> str:
    evaluation = payload.get("research_evaluation") or {}
    experiment = payload.get("experiment") or {}
    trial_id = str(experiment.get("trial_id") or "").lower()
    status = str(payload.get("status") or "").lower()
    if (
        evaluation.get("forward_start_date")
        or "forward" in family_id.lower()
        or "prospective" in trial_id
        or "prospective" in status
    ):
        return "prospective_forward"
    return "retrospective_consumed"


def _job_record(path: Path, payload: dict, root: Path) -> dict:
    family_id = _family_id(payload)
    if not family_id:
        raise ValueError(f"{path.name} has a job_id but no hypothesis_id")
    config = (
        payload.get("tier1_config")
        or payload.get("replay_config")
        or payload.get("intraday_config")
        or {}
    )
    evaluation = payload.get("research_evaluation") or {}
    experiment = payload.get("experiment") or {}
    strategies = config.get("strategy_names") or []
    variants = experiment.get("tested_variants")
    return {
        "source_path": path.relative_to(root).as_posix(),
        "source_sha256": _sha256(payload),
        "job_id": str(payload["job_id"]),
        "family_id": family_id,
        "trial_id": str(experiment.get("trial_id") or payload["job_id"]),
        "engine": str(payload.get("engine") or "intraday_replay"),
        "evidence_class": _evidence_class(payload, family_id),
        "dataset": str(payload.get("bars_csv") or "") or None,
        "data_source": str(payload.get("data_source") or "") or None,
        "universe": config.get("universe_name"),
        "market_state_universe": config.get("market_state_universe_name"),
        "strategies": list(strategies),
        "strategy_count": len(strategies),
        "variant_enumeration_complete": variants is not None,
        "enumerated_variant_count": len(variants) if isinstance(variants, list) else None,
        "required_common_start_date": config.get("required_common_start_date"),
        "discovery_end_date": config.get("discovery_end_date"),
        "holdout_start_date": config.get("holdout_start_date"),
        "forward_start_date": evaluation.get("forward_start_date"),
        "cost_ladder_bps": config.get("cost_ladder_bps"),
        "primary_cost_bps": config.get("primary_cost_bps"),
        "router_authority": bool(evaluation.get("router", False)),
    }


def _declaration_record(path: Path, payload: dict, root: Path) -> dict:
    family_id = _family_id(payload)
    if not family_id:
        raise ValueError(f"{path.name} has no research family identifier")
    return {
        "source_path": path.relative_to(root).as_posix(),
        "source_sha256": _sha256(payload),
        "family_id": family_id,
        "status": payload.get("status"),
        "evidence_class": _evidence_class(payload, family_id),
    }


def build_program_catalog(
    repo_root: str | Path,
    *,
    as_of_date: str,
    consumed_through: str,
) -> dict:
    """Build an exact catalog from tracked-style research JSON definitions.

    ``consumed_through`` applies to new or revised hypotheses.  Existing
    frozen forward declarations may continue appending later sessions without
    turning those observations back into an untouched optimization set.
    """
    date.fromisoformat(as_of_date)
    date.fromisoformat(consumed_through)
    root = Path(repo_root).resolve()
    research_root = root / "research"
    jobs = []
    declarations = []
    skipped = []
    for path in sorted(research_root.glob("*.json")):
        if ".example." in path.name or path.name.endswith("job.example.json"):
            skipped.append(path.relative_to(root).as_posix())
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot catalog {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            continue
        if payload.get("job_id") and _family_id(payload):
            jobs.append(_job_record(path, payload, root))
        elif _family_id(payload):
            declarations.append(_declaration_record(path, payload, root))

    families: dict[str, dict] = {}
    for record in declarations:
        family = families.setdefault(record["family_id"], {
            "family_id": record["family_id"], "declarations": [], "jobs": [],
        })
        family["declarations"].append(record)
    for record in jobs:
        family = families.setdefault(record["family_id"], {
            "family_id": record["family_id"], "declarations": [], "jobs": [],
        })
        family["jobs"].append(record)
    family_rows = []
    for family in families.values():
        family["declarations"].sort(key=lambda row: row["source_path"])
        family["jobs"].sort(key=lambda row: (row["job_id"], row["source_path"]))
        family["job_count"] = len(family["jobs"])
        family["declared_job_variants_lower_bound"] = sum(
            row["enumerated_variant_count"] or row["strategy_count"] or 1
            for row in family["jobs"]
        )
        family["contains_unenumerated_jobs"] = any(
            not row["variant_enumeration_complete"] for row in family["jobs"]
        )
        family_rows.append(family)
    family_rows.sort(key=lambda row: row["family_id"])

    payload = {
        "schema_version": SCHEMA_VERSION,
        "catalog_id": "RESEARCH_PROGRAM_CATALOG_001",
        "as_of_date": as_of_date,
        "new_or_revised_hypotheses_consumed_through": consumed_through,
        "policy": {
            "preledger_lineage_is_reconstructed_lower_bound": True,
            "original_acceptance_order_is_unknown": True,
            "historical_results_already_viewed_are_not_untouched": True,
            "existing_frozen_forward_ledgers_may_continue": True,
            "forward_observations_may_not_be_reused_to_retune_the_same_strategy": True,
        },
        "family_count": len(family_rows),
        "job_count": len(jobs),
        "declaration_count": len(declarations),
        "families": family_rows,
        "excluded_examples": sorted(skipped),
    }
    payload["catalog_sha256"] = _sha256(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--consumed-through", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = build_program_catalog(
        args.repo_root,
        as_of_date=args.as_of_date,
        consumed_through=args.consumed_through,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "family_count": payload["family_count"],
        "job_count": payload["job_count"],
        "catalog_sha256": payload["catalog_sha256"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
