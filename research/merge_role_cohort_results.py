"""Merge compact, independently produced Generation 010 cohort evidence."""

from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from pathlib import Path


MERGED_FILES = (
    "tier1_role_aware_evidence.csv", "tier1_tactical_baseline_comparisons.csv",
    "tier1_tactical_override_summary.csv", "tier1_hypothesis_survival.csv",
    "tier1_market_state_period_scorecard.csv", "tier1_cost_ladder_scorecard.csv",
    "tier1_baseline_state_leaderboard.csv", "tier1_defensive_baseline_comparisons.csv",
    "tier1_tactical_horizon_comparisons.csv", "tier1_tactical_horizon_summary.csv",
    "tier1_locked_tactical_validation.csv",
    "tier1_defensive_distinctness.csv", "tier1_baseline_era_details.csv",
    "tier1_baseline_era_recurrence.csv",
    "tier1_baseline_confirmation_sensitivity.csv",
    "tier1_state_transition_timing.csv", "tier1_generation_candidate_map.csv",
)
ROW_KEYS = {
    "tier1_role_aware_evidence.csv": ("strategy",),
    "tier1_tactical_baseline_comparisons.csv": ("strategy", "entry_date", "comparison_end_date"),
    "tier1_tactical_override_summary.csv": ("strategy", "core_state"),
    "tier1_hypothesis_survival.csv": ("strategy", "core_state"),
    "tier1_market_state_period_scorecard.csv": ("period", "strategy", "core_state", "cost_bps"),
    "tier1_cost_ladder_scorecard.csv": ("strategy", "cost_bps"),
    "tier1_baseline_state_leaderboard.csv": ("period", "core_state", "strategy"),
    "tier1_defensive_baseline_comparisons.csv": ("period", "core_state", "strategy"),
    "tier1_tactical_horizon_comparisons.csv": ("strategy", "entry_date", "horizon_sessions"),
    "tier1_tactical_horizon_summary.csv": ("strategy", "horizon_sessions", "core_state"),
    "tier1_locked_tactical_validation.csv": ("hypothesis_id", "period"),
    "tier1_defensive_distinctness.csv": ("core_state", "left_strategy", "right_strategy"),
    "tier1_baseline_era_details.csv": ("era", "core_state", "strategy"),
    "tier1_baseline_era_recurrence.csv": ("core_state", "strategy"),
    "tier1_baseline_confirmation_sensitivity.csv": (
        "confirmation_sessions", "core_state", "strategy",
    ),
    "tier1_state_transition_timing.csv": (
        "confirmation_sessions", "transition_id", "strategy", "horizon_sessions",
    ),
    "tier1_generation_candidate_map.csv": (
        "evidence_role", "core_state_or_event", "strategy",
    ),
}


def _read_csv(bundle, name):
    try:
        return list(csv.DictReader(io.TextIOWrapper(bundle.open(name), encoding="utf-8")))
    except KeyError:
        return []


def _write_csv(bundle, name, rows):
    if not rows:
        bundle.writestr(name, "")
        return
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    bundle.writestr(name, stream.getvalue())


def merge_role_cohorts(archives, output):
    sources, rows_by_file = [], {name: [] for name in MERGED_FILES}
    source_sha = state_definition = None
    for archive in map(Path, archives):
        with zipfile.ZipFile(archive) as bundle:
            manifest = json.loads(bundle.read("tier1_manifest.json"))
            definition = json.loads(bundle.read("tier1_market_state_definition.json"))
            if source_sha is None:
                source_sha, state_definition = manifest["source_sha256"], definition
            if manifest["source_sha256"] != source_sha or definition != state_definition:
                raise ValueError("cohort archives do not share the same data and state definition")
            sources.append({"archive": archive.name, "strategies": manifest["strategies"],
                            "source_sha256": manifest["source_sha256"],
                            "hypothesis_id": (manifest.get("experiment") or {}).get("hypothesis_id")})
            for name in MERGED_FILES:
                rows_by_file[name].extend(_read_csv(bundle, name))
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
        for name, rows in rows_by_file.items():
            # Archives must be passed baseline, defensive, tactical. First-wins
            # makes the baseline cohort canonical for shared controls whose FDR
            # values legitimately differ with each cohort's hypothesis family.
            unique = {}
            for row in rows:
                key = tuple(row[field] for field in ROW_KEYS[name])
                unique.setdefault(key, row)
            _write_csv(bundle, name.replace("tier1_", "unified_"), list(unique.values()))
        bundle.writestr("unified_market_state_definition.json", json.dumps(state_definition, indent=2))
        hypothesis_ids = sorted({source["hypothesis_id"] for source in sources
                                 if source["hypothesis_id"]})
        bundle.writestr("unified_manifest.json", json.dumps({
            "generation": hypothesis_ids[0] if len(hypothesis_ids) == 1 else "mixed",
            "hypothesis_ids": hypothesis_ids, "source_sha256": source_sha,
            "cohort_archives": sources, "routing_effect": "none",
        }, indent=2))
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path); parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args(); print(merge_role_cohorts(args.archives, args.output))


if __name__ == "__main__":
    main()
