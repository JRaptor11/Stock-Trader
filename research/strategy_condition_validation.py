"""Chronologically validate strategy-condition relationships without routing capital."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
import zipfile
from collections import defaultdict
from pathlib import Path

from research.statistical_safeguards import multiple_testing_report, return_evidence


def _rows(bundle: zipfile.ZipFile, name: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))


def _csv_bytes(rows: list[dict]) -> bytes:
    output = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    return output.getvalue().encode()


def _compound(values: list[float]) -> float:
    return math.prod(1.0 + value for value in values) - 1.0


def _evidence_class(test_dates: list[str], declaration: dict | None) -> str:
    if not declaration:
        return "exploratory_unfocused"
    observed_through = declaration["evidence_observed_through"]
    if test_dates[-1] <= observed_through:
        return "retrospective_reuse"
    if test_dates[0] > observed_through:
        return "untouched_forward"
    return "mixed_boundary"


def _condition_episodes(
    dates: list[str], conditions: dict[str, dict], dimension: str,
) -> list[dict]:
    """Return contiguous, immutable bucket episodes for one condition dimension.

    Missing labels are retained as their own episodes.  They are not eligible
    hypotheses, but retaining them ensures a train/test boundary can never
    silently cut across a labeled episode on either side of missing data.
    """
    episodes: list[dict] = []
    for day in dates:
        bucket = conditions[day].get(dimension) or None
        if not episodes or episodes[-1]["bucket"] != bucket:
            episodes.append({
                "ordinal": len(episodes) + 1,
                "bucket": bucket,
                "dates": [day],
            })
        else:
            episodes[-1]["dates"].append(day)
    for episode in episodes:
        episode["start"] = episode["dates"][0]
        episode["end"] = episode["dates"][-1]
        episode["episode_id"] = (
            f"{dimension}:{episode['ordinal']}:{episode['start']}:{episode['end']}:"
            f"{episode['bucket'] or 'MISSING'}"
        )
    return episodes


def _complete_condition_episodes(
    common_dates: list[str], conditions: dict[str, dict], dimension: str,
) -> tuple[list[dict], list[dict]]:
    """Keep only episodes with a complete aligned return observation set.

    Building episodes directly from the aligned return dates could silently
    splice the two sides of a missing observation together or retain half of
    a causal episode at a dataset boundary.  Episode identity is therefore
    derived from the full condition calendar first; any episode with even one
    unavailable return date is excluded as a whole.
    """
    common = set(common_dates)
    complete = []
    incomplete = []
    for episode in _condition_episodes(sorted(conditions), conditions, dimension):
        if all(day in common for day in episode["dates"]):
            complete.append(episode)
        else:
            incomplete.append(episode)
    return complete, incomplete


def _episode_windows(
    episodes: list[dict], *, train_sessions: int, test_sessions: int,
    step_sessions: int,
) -> list[tuple[list[dict], list[dict]]]:
    """Build expanding walk-forward windows using only whole episodes."""
    if not episodes:
        return []
    test_index = 0
    accumulated = 0
    while test_index < len(episodes) and accumulated < train_sessions:
        accumulated += len(episodes[test_index]["dates"])
        test_index += 1
    windows: list[tuple[list[dict], list[dict]]] = []
    while test_index < len(episodes):
        test_end = test_index
        test_count = 0
        while test_end < len(episodes) and test_count < test_sessions:
            test_count += len(episodes[test_end]["dates"])
            test_end += 1
        if test_count < test_sessions:
            break
        windows.append((episodes[:test_index], episodes[test_index:test_end]))
        # Do not count the same condition episode as outer-test evidence in
        # more than one fold.  ``step_sessions`` may request a wider gap, but
        # a shorter step must never create overlapping test episodes.
        advance = 0
        next_index = test_index
        while next_index < len(episodes) and advance < step_sessions:
            advance += len(episodes[next_index]["dates"])
            next_index += 1
        test_index = max(test_end, next_index)
    return windows


def _evenly_spaced(items: list[int], limit: int) -> list[int]:
    if limit < 1:
        raise ValueError("nested_inner_folds must be positive")
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[-1]]
    chosen = {
        items[round(index * (len(items) - 1) / (limit - 1))]
        for index in range(limit)
    }
    return sorted(chosen)


def _inner_episode_boundaries(
    train_episodes: list[dict], *, minimum_train_sessions: int,
    maximum_folds: int,
) -> list[int]:
    """Choose deterministic expanding inner folds from outer-training episodes."""
    eligible = []
    accumulated = 0
    for index, episode in enumerate(train_episodes[:-1], 1):
        accumulated += len(episode["dates"])
        if accumulated >= minimum_train_sessions:
            eligible.append(index)
    return _evenly_spaced(eligible, maximum_folds)


def _candidate_id(source: str, strategy: str, bucket: str) -> str:
    return f"{source}::{strategy}::{bucket}"


def _resolve_family_count(
    *, visible_count: int, explicit_count: int | None,
    trial_ledger_path: Path | None, trial_family_id: str | None,
) -> dict:
    """Resolve an auditable complete-family assertion for multiplicity tools.

    The aligned return matrix alone cannot prove that discarded trials are
    absent.  Consequently the unconditional DSR/PBO/Reality-Check report is
    skipped unless the caller supplies either an explicit complete-family
    count or a trial-ledger family.  A mismatch is reported, never papered over.
    """
    if explicit_count is not None and explicit_count < 1:
        raise ValueError("multiple_testing_family_trials must be positive")
    if trial_family_id and trial_ledger_path is None:
        raise ValueError("trial_ledger_path is required with trial_family_id")
    if trial_ledger_path is not None:
        if not trial_family_id:
            raise ValueError("trial_family_id is required with trial_ledger_path")
        from research.experiment_audit import trial_family_summary

        summary = trial_family_summary(trial_ledger_path)
        family = summary["families"].get(trial_family_id.upper())
        if family is None:
            return {
                "status": "not_computed",
                "reason": "trial_ledger_family_not_found",
                "trial_family_id": trial_family_id.upper(),
                "visible_aligned_strategies": visible_count,
            }
        ledger_count = int(family["distinct_variant_count"])
        if explicit_count is not None and explicit_count != ledger_count:
            raise ValueError(
                "explicit multiple-testing family count disagrees with trial ledger"
            )
        if family["variant_count_is_lower_bound"]:
            return {
                "status": "not_computed",
                "reason": "trial_ledger_family_count_is_only_a_lower_bound",
                "trial_family_id": trial_family_id.upper(),
                "ledger_family_trials": ledger_count,
                "visible_aligned_strategies": visible_count,
                "unenumerated_job_ids": family["unenumerated_job_ids"],
            }
        explicit_count = ledger_count
        source = "tamper_evident_trial_ledger"
    elif explicit_count is not None:
        source = "caller_asserted_complete_family_count"
    else:
        return {
            "status": "not_computed",
            "reason": "complete_search_family_not_supplied",
            "visible_aligned_strategies": visible_count,
        }
    if explicit_count != visible_count:
        return {
            "status": "not_computed",
            "reason": "complete_family_count_does_not_match_aligned_return_series",
            "family_count_source": source,
            "declared_family_trials": explicit_count,
            "visible_aligned_strategies": visible_count,
        }
    return {
        "status": "ready",
        "family_count_source": source,
        "declared_family_trials": explicit_count,
        "visible_aligned_strategies": visible_count,
        "trial_family_id": trial_family_id.upper() if trial_family_id else None,
    }


def _load_declaration(path: Path | None, cohort: str | None = None) -> dict | None:
    if path is None:
        return None
    declaration = json.loads(path.read_text(encoding="utf-8"))
    hypotheses = declaration.get("hypotheses") or []
    required = {"id", "strategy", "dimension", "bucket"}
    if not hypotheses or any(not required.issubset(item) for item in hypotheses):
        raise ValueError("focused declaration requires non-empty hypotheses with id, strategy, dimension, and bucket")
    ids = [item["id"] for item in hypotheses]
    if len(ids) != len(set(ids)):
        raise ValueError("focused hypothesis ids must be unique")
    if not declaration.get("frozen_at") or not declaration.get("evidence_observed_through"):
        raise ValueError("focused declaration requires frozen_at and evidence_observed_through")
    if cohort:
        hypotheses = [item for item in hypotheses if item.get("cohort") == cohort]
        if not hypotheses:
            raise ValueError(f"focused declaration has no hypotheses for cohort {cohort}")
        declaration = {**declaration, "hypotheses": hypotheses, "selected_cohort": cohort}
    return declaration


def _nested_episode_validation_rows(
    *, common_dates: list[str], conditions: dict[str, dict],
    series: dict[tuple[str, str], dict[str, float]],
    strategy_keys: list[tuple[str, str]], benchmark: dict[str, float],
    benchmark_strategy: str, dimensions: list[str], focused: dict,
    declaration: dict | None, family_trials: int, train_sessions: int,
    test_sessions: int, step_sessions: int, minimum_bucket_sessions: int,
    alpha: float, nested_inner_folds: int,
    nested_max_selected_per_dimension: int,
    nested_min_inner_validation_episodes: int,
) -> tuple[list[dict], int, dict]:
    """Run nested, episode-grouped selection and outer evaluation.

    Each condition dimension is evaluated independently because an episode is
    defined by a contiguous run of *that* dimension's causal label.  Requiring
    all dimensions to change on the same date would make valid boundaries
    vanishingly rare and would not add information to a dimension-specific
    hypothesis.  Both inner and outer boundaries are therefore whole-episode
    boundaries for the dimension being tested.
    """
    if nested_max_selected_per_dimension < 1:
        raise ValueError("nested_max_selected_per_dimension must be positive")
    if nested_min_inner_validation_episodes < 1:
        raise ValueError("nested_min_inner_validation_episodes must be positive")
    rows: list[dict] = []
    fold = 0
    dimension_fold_counts: dict[str, int] = {}
    episode_counts: dict[str, int] = {}
    total_episode_counts: dict[str, int] = {}
    incomplete_episode_ids: dict[str, list[str]] = {}
    for dimension in dimensions:
        episodes, incomplete = _complete_condition_episodes(
            common_dates, conditions, dimension,
        )
        episode_counts[dimension] = len(episodes)
        total_episode_counts[dimension] = len(episodes) + len(incomplete)
        incomplete_episode_ids[dimension] = [
            episode["episode_id"] for episode in incomplete
        ]
        windows = _episode_windows(
            episodes, train_sessions=train_sessions,
            test_sessions=test_sessions, step_sessions=step_sessions,
        )
        dimension_fold_counts[dimension] = len(windows)
        for dimension_fold, (train_episodes, test_episodes) in enumerate(windows, 1):
            fold += 1
            train_dates = [day for episode in train_episodes for day in episode["dates"]]
            test_dates = [day for episode in test_episodes for day in episode["dates"]]
            buckets = sorted({
                episode["bucket"] for episode in train_episodes
                if episode["bucket"] is not None
            })
            candidates: list[dict] = []
            for source, strategy in strategy_keys:
                returns = series[(source, strategy)]
                for bucket in buckets:
                    hypothesis = focused.get((strategy, dimension, bucket))
                    if focused and not hypothesis:
                        continue
                    train_bucket = [
                        day for day in train_dates
                        if conditions[day].get(dimension) == bucket
                    ]
                    train_excess = [returns[day] - benchmark[day] for day in train_bucket]
                    evidence = return_evidence(train_excess, family_trials) if train_excess else {}
                    adjusted_p = evidence.get("bonferroni_adjusted_p")
                    outer_qualified = (
                        len(train_excess) >= minimum_bucket_sessions
                        and statistics.fmean(train_excess) > 0
                        and adjusted_p is not None and adjusted_p <= alpha
                    )
                    candidates.append({
                        "source": source,
                        "strategy": strategy,
                        "bucket": bucket,
                        "hypothesis": hypothesis,
                        "returns": returns,
                        "train_bucket": train_bucket,
                        "train_excess": train_excess,
                        "train_adjusted_p": adjusted_p,
                        "outer_qualified": outer_qualified,
                        "inner_values": [],
                        "inner_episode_ids": [],
                        "inner_selection_opportunities": 0,
                    })

            inner_boundaries = _inner_episode_boundaries(
                train_episodes,
                minimum_train_sessions=minimum_bucket_sessions,
                maximum_folds=nested_inner_folds,
            )
            for boundary in inner_boundaries:
                inner_train_dates = [
                    day for episode in train_episodes[:boundary]
                    for day in episode["dates"]
                ]
                validation_episode = train_episodes[boundary]
                validation_bucket = validation_episode["bucket"]
                if validation_bucket is None:
                    continue
                inner_candidates = []
                for candidate in candidates:
                    if validation_bucket != candidate["bucket"]:
                        continue
                    candidate["inner_selection_opportunities"] += 1
                    returns = candidate["returns"]
                    bucket = candidate["bucket"]
                    inner_train_bucket = [
                        day for day in inner_train_dates
                        if conditions[day].get(dimension) == bucket
                    ]
                    inner_excess = [
                        returns[day] - benchmark[day] for day in inner_train_bucket
                    ]
                    inner_evidence = (
                        return_evidence(inner_excess, family_trials)
                        if inner_excess else {}
                    )
                    inner_adjusted_p = inner_evidence.get("bonferroni_adjusted_p")
                    inner_qualified = (
                        len(inner_excess) >= minimum_bucket_sessions
                        and statistics.fmean(inner_excess) > 0
                        and inner_adjusted_p is not None
                        and inner_adjusted_p <= alpha
                    )
                    if not inner_qualified:
                        continue
                    inner_candidates.append({
                        "candidate": candidate,
                        "mean_daily_excess": statistics.fmean(inner_excess),
                        "adjusted_p": inner_adjusted_p,
                    })
                inner_candidates.sort(key=lambda item: (
                    -float(item["mean_daily_excess"]),
                    float(item["adjusted_p"]),
                    _candidate_id(
                        item["candidate"]["source"],
                        item["candidate"]["strategy"],
                        item["candidate"]["bucket"],
                    ),
                ))
                inner_selected = inner_candidates[:nested_max_selected_per_dimension]
                for item in inner_selected:
                    candidate = item["candidate"]
                    returns = candidate["returns"]
                    candidate["inner_episode_ids"].append(validation_episode["episode_id"])
                    candidate["inner_values"].extend(
                        returns[day] - benchmark[day]
                        for day in validation_episode["dates"]
                    )
            eligible_by_bucket: dict[str, list[dict]] = defaultdict(list)
            for candidate in candidates:
                candidate["inner_validation_episodes"] = len(candidate["inner_episode_ids"])
                candidate["inner_validation_sessions"] = len(candidate["inner_values"])
                candidate["inner_mean_daily_excess"] = (
                    statistics.fmean(candidate["inner_values"])
                    if candidate["inner_values"] else None
                )
                if (
                    candidate["outer_qualified"]
                    and candidate["inner_validation_episodes"]
                    >= nested_min_inner_validation_episodes
                    and candidate["inner_mean_daily_excess"] is not None
                    and candidate["inner_mean_daily_excess"] > 0
                ):
                    eligible_by_bucket[candidate["bucket"]].append(candidate)
            ranks = {}
            selected_ids = set()
            for bucket, eligible in sorted(eligible_by_bucket.items()):
                eligible.sort(key=lambda item: (
                    -float(item["inner_mean_daily_excess"]),
                    _candidate_id(item["source"], item["strategy"], item["bucket"]),
                ))
                for index, item in enumerate(eligible, 1):
                    identity = _candidate_id(
                        item["source"], item["strategy"], item["bucket"],
                    )
                    ranks[identity] = index
                    if index <= nested_max_selected_per_dimension:
                        selected_ids.add(identity)

            for candidate in candidates:
                source = candidate["source"]
                strategy = candidate["strategy"]
                bucket = candidate["bucket"]
                returns = candidate["returns"]
                identity = _candidate_id(source, strategy, bucket)
                selected = identity in selected_ids
                test_bucket = [
                    day for day in test_dates
                    if conditions[day].get(dimension) == bucket
                ] if selected else []
                test_excess = [
                    returns[day] - benchmark[day] for day in test_bucket
                ]
                hypothesis = candidate["hypothesis"]
                rows.append({
                    "fold": fold,
                    "dimension_fold": dimension_fold,
                    "hypothesis_id": hypothesis["id"] if hypothesis else "",
                    "source": source,
                    "strategy": strategy,
                    "benchmark_strategy": benchmark_strategy,
                    "dimension": dimension,
                    "bucket": bucket,
                    "validation_mode": "nested_episode_grouped",
                    "train_start": train_dates[0],
                    "train_end": train_dates[-1],
                    "test_start": test_dates[0],
                    "test_end": test_dates[-1],
                    "outer_train_total_sessions": len(train_dates),
                    "outer_test_total_sessions": len(test_dates),
                    "train_episode_count": len(train_episodes),
                    "test_episode_count": len(test_episodes),
                    "train_episode_ids": json.dumps(
                        [episode["episode_id"] for episode in train_episodes],
                        separators=(",", ":"),
                    ),
                    "train_last_episode_id": train_episodes[-1]["episode_id"],
                    "test_episode_ids": json.dumps(
                        [episode["episode_id"] for episode in test_episodes],
                        separators=(",", ":"),
                    ),
                    "inner_fold_count": len(inner_boundaries),
                    "inner_fold_boundaries": json.dumps(inner_boundaries),
                    "inner_validation_episodes": candidate["inner_validation_episodes"],
                    "inner_selection_opportunities": candidate[
                        "inner_selection_opportunities"
                    ],
                    "inner_validation_sessions": candidate["inner_validation_sessions"],
                    "inner_mean_daily_excess": candidate["inner_mean_daily_excess"],
                    "inner_validation_episode_ids": json.dumps(
                        candidate["inner_episode_ids"], separators=(",", ":"),
                    ),
                    "selected_on_train": selected,
                    "selection_rank": ranks.get(identity),
                    "selection_scope": "condition_bucket_within_dimension",
                    "outer_test_evaluated": bool(selected and test_bucket),
                    "outer_test_descriptive_only": True,
                    "router_eligible": False,
                    "train_sessions": len(candidate["train_excess"]),
                    "test_sessions": len(test_excess),
                    "minimum_bucket_sessions": minimum_bucket_sessions,
                    "train_mean_daily_excess": (
                        statistics.fmean(candidate["train_excess"])
                        if candidate["train_excess"] else None
                    ),
                    "train_compounded_return": (
                        _compound([returns[day] for day in candidate["train_bucket"]])
                        if candidate["train_bucket"] else None
                    ),
                    "train_benchmark_return": (
                        _compound([benchmark[day] for day in candidate["train_bucket"]])
                        if candidate["train_bucket"] else None
                    ),
                    "train_spy_return": (
                        _compound([benchmark[day] for day in candidate["train_bucket"]])
                        if candidate["train_bucket"]
                        and benchmark_strategy == "SPY_BUY_HOLD" else None
                    ),
                    "bonferroni_family_trials": family_trials,
                    "train_adjusted_p": candidate["train_adjusted_p"],
                    "qualified_on_train": candidate["outer_qualified"],
                    "test_compounded_return": (
                        _compound([returns[day] for day in test_bucket])
                        if test_bucket else None
                    ),
                    "test_benchmark_return": (
                        _compound([benchmark[day] for day in test_bucket])
                        if test_bucket else None
                    ),
                    "test_spy_return": (
                        _compound([benchmark[day] for day in test_bucket])
                        if test_bucket and benchmark_strategy == "SPY_BUY_HOLD" else None
                    ),
                    "test_excess_return": (
                        _compound([returns[day] for day in test_bucket])
                        - _compound([benchmark[day] for day in test_bucket])
                        if test_bucket else None
                    ),
                    "test_mean_daily_excess": (
                        statistics.fmean(test_excess) if test_excess else None
                    ),
                    "test_positive_excess": (
                        statistics.fmean(test_excess) > 0 if test_excess else None
                    ),
                    "evidence_class": _evidence_class(test_dates, declaration),
                })
    if not fold:
        raise ValueError(
            "insufficient complete condition episodes for one nested episode-grouped fold"
        )
    return rows, fold, {
        "dimension_fold_counts": dimension_fold_counts,
        "condition_episode_counts": episode_counts,
        "condition_episode_counts_before_alignment": total_episode_counts,
        "incomplete_condition_episode_ids_excluded": incomplete_episode_ids,
        "nested_inner_folds_maximum": nested_inner_folds,
        "nested_max_selected_per_dimension": nested_max_selected_per_dimension,
        "nested_selection_limit_semantics": (
            "maximum selected strategies per condition bucket within each dimension"
        ),
        "nested_min_inner_validation_episodes": nested_min_inner_validation_episodes,
        "episode_boundary_policy": (
            "each dimension is split independently; no contiguous causal bucket "
            "episode for that dimension can straddle an inner or outer boundary; "
            "outer-test episodes are not reused in another outer-test fold"
        ),
        "inner_selection_policy": (
            "for each whole inner-validation episode, candidate-condition pairs "
            "are qualified and ranked using only earlier outer-training episodes; "
            "the selected pair is then scored on that held-out inner episode"
        ),
        "outer_test_policy": (
            "only candidates selected inside the outer-training fold are evaluated; "
            "all outer-test results are descriptive and never router-eligible"
        ),
    }


def validate_bundle(path: Path, train_sessions: int = 252, test_sessions: int = 63,
                    step_sessions: int = 63, minimum_bucket_sessions: int = 30,
                    alpha: float = 0.05, declaration_path: Path | None = None,
                    cohort: str | None = None,
                    dimensions: tuple[str, ...] | None = None,
                    benchmark_strategy: str = "SPY_BUY_HOLD",
                    validation_mode: str = "session_expanding",
                    nested_inner_folds: int = 5,
                    nested_max_selected_per_dimension: int = 1,
                    nested_min_inner_validation_episodes: int = 1,
                    multiple_testing_family_trials: int | None = None,
                    trial_ledger_path: Path | None = None,
                    trial_family_id: str | None = None,
                    multiple_testing_cscv_partitions: int = 10,
                    multiple_testing_bootstrap_samples: int = 2_000,
                    multiple_testing_mean_block_length: float = 10.0,
                    multiple_testing_seed: int = 29,
                    ) -> tuple[list[dict], list[dict], dict]:
    declaration = _load_declaration(declaration_path, cohort)
    if declaration and declaration.get("benchmark_strategy") not in (None, benchmark_strategy):
        raise ValueError(
            "focused declaration benchmark does not match requested benchmark: "
            f"{declaration['benchmark_strategy']} != {benchmark_strategy}"
        )
    with zipfile.ZipFile(path) as bundle:
        names = set(bundle.namelist())
        required = {"normalized_daily_returns.csv", "causal_market_conditions.csv", "strategy_evidence_manifest.json"}
        if not required.issubset(names):
            raise ValueError(f"evidence bundle missing: {sorted(required - names)}")
        daily = _rows(bundle, "normalized_daily_returns.csv")
        conditions = {row["date"]: row for row in _rows(bundle, "causal_market_conditions.csv")}
        source_manifest = json.loads(bundle.read("strategy_evidence_manifest.json"))

    series: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for row in daily:
        series[(row["source"], row["strategy"])][row["date"]] = float(row["daily_return"])
    benchmark_keys = [key for key in series if key[1] == benchmark_strategy]
    if not benchmark_keys:
        raise ValueError(f"benchmark strategy is required: {benchmark_strategy}")
    benchmark = series[benchmark_keys[0]]
    strategy_keys = [key for key in series if key[1] != benchmark_strategy]
    common_dates = sorted(set(conditions).intersection(benchmark, *(series[key] for key in strategy_keys)))
    if len(common_dates) < train_sessions + test_sessions:
        raise ValueError("insufficient common condition-labeled sessions for one fold")
    available_dimensions = sorted({key for day in common_dates for key in conditions[day] if key.endswith("_bucket")})
    if dimensions:
        missing_requested = sorted(set(dimensions).difference(available_dimensions))
        if missing_requested:
            raise ValueError(f"requested condition dimensions are unavailable: {missing_requested}")
        dimensions = sorted(set(dimensions))
    else:
        dimensions = available_dimensions
    focused = {(item["strategy"], item["dimension"], item["bucket"]): item for item in (declaration or {}).get("hypotheses", [])}
    missing_strategies = sorted({key[0] for key in focused}.difference(key[1] for key in strategy_keys))
    missing_dimensions = sorted({key[1] for key in focused}.difference(dimensions))
    if missing_strategies or missing_dimensions:
        raise ValueError(f"focused declaration is incompatible: missing strategies={missing_strategies}, dimensions={missing_dimensions}")
    family_trials = max(1, len(focused) if focused else len(strategy_keys) * len(dimensions) * 5)

    family_resolution = _resolve_family_count(
        visible_count=len(strategy_keys),
        explicit_count=multiple_testing_family_trials,
        trial_ledger_path=trial_ledger_path,
        trial_family_id=trial_family_id,
    )
    unconditional_multiple_testing = dict(family_resolution)
    if family_resolution["status"] == "ready":
        if len(strategy_keys) < 2:
            unconditional_multiple_testing = {
                **family_resolution,
                "status": "not_computed",
                "reason": "multiple_testing_report_requires_at_least_two_strategies",
            }
        else:
            strategy_returns = {
                f"{source}::{strategy}": [series[(source, strategy)][day] for day in common_dates]
                for source, strategy in strategy_keys
            }
            unconditional_multiple_testing = {
                **multiple_testing_report(
                    strategy_returns,
                    [benchmark[day] for day in common_dates],
                    family_trials=family_resolution["declared_family_trials"],
                    cscv_partitions=multiple_testing_cscv_partitions,
                    bootstrap_samples=multiple_testing_bootstrap_samples,
                    mean_block_length=multiple_testing_mean_block_length,
                    seed=multiple_testing_seed,
                ),
                **family_resolution,
                "status": "computed",
                "scope": "full aligned unconditional strategy family",
                "conditioned_on_market_state": False,
                "router_or_promotion_eligible": False,
                "interpretation": (
                    "diagnostic family-wide multiplicity evidence; it does not select "
                    "a condition, strategy, or live routing rule"
                ),
            }

    rows = []
    fold = 0
    nested_metadata = {}
    if validation_mode == "nested_episode_grouped":
        rows, fold, nested_metadata = _nested_episode_validation_rows(
            common_dates=common_dates,
            conditions=conditions,
            series=series,
            strategy_keys=strategy_keys,
            benchmark=benchmark,
            benchmark_strategy=benchmark_strategy,
            dimensions=list(dimensions),
            focused=focused,
            declaration=declaration,
            family_trials=family_trials,
            train_sessions=train_sessions,
            test_sessions=test_sessions,
            step_sessions=step_sessions,
            minimum_bucket_sessions=minimum_bucket_sessions,
            alpha=alpha,
            nested_inner_folds=nested_inner_folds,
            nested_max_selected_per_dimension=nested_max_selected_per_dimension,
            nested_min_inner_validation_episodes=nested_min_inner_validation_episodes,
        )
        test_starts = ()
    elif validation_mode == "session_expanding":
        test_starts = range(
            train_sessions, len(common_dates) - test_sessions + 1, step_sessions,
        )
    else:
        raise ValueError(
            "validation_mode must be session_expanding or nested_episode_grouped"
        )
    for test_start in test_starts:
        fold += 1
        train_dates = common_dates[:test_start]
        test_dates = common_dates[test_start:test_start + test_sessions]
        for source, strategy in strategy_keys:
            returns = series[(source, strategy)]
            for dimension in dimensions:
                buckets = sorted({conditions[day].get(dimension) for day in train_dates if conditions[day].get(dimension)})
                for bucket in buckets:
                    hypothesis = focused.get((strategy, dimension, bucket))
                    if focused and not hypothesis:
                        continue
                    train_bucket = [day for day in train_dates if conditions[day].get(dimension) == bucket]
                    test_bucket = [day for day in test_dates if conditions[day].get(dimension) == bucket]
                    train_excess = [returns[day] - benchmark[day] for day in train_bucket]
                    test_excess = [returns[day] - benchmark[day] for day in test_bucket]
                    evidence = return_evidence(train_excess, family_trials) if train_excess else {}
                    adjusted_p = evidence.get("bonferroni_adjusted_p")
                    qualified = (
                        len(train_excess) >= minimum_bucket_sessions
                        and statistics.fmean(train_excess) > 0
                        and adjusted_p is not None and adjusted_p <= alpha
                    )
                    rows.append({
                        "fold": fold, "hypothesis_id": hypothesis["id"] if hypothesis else "", "source": source, "strategy": strategy,
                        "benchmark_strategy": benchmark_strategy,
                        "dimension": dimension, "bucket": bucket,
                        "validation_mode": "session_expanding",
                        "train_start": train_dates[0], "train_end": train_dates[-1],
                        "test_start": test_dates[0], "test_end": test_dates[-1],
                        "train_sessions": len(train_excess), "test_sessions": len(test_excess),
                        "minimum_bucket_sessions": minimum_bucket_sessions,
                        "train_mean_daily_excess": statistics.fmean(train_excess) if train_excess else None,
                        "train_compounded_return": _compound([returns[day] for day in train_bucket]) if train_bucket else None,
                        "train_benchmark_return": _compound([benchmark[day] for day in train_bucket]) if train_bucket else None,
                        "train_spy_return": _compound([benchmark[day] for day in train_bucket]) if train_bucket and benchmark_strategy == "SPY_BUY_HOLD" else None,
                        "bonferroni_family_trials": family_trials,
                        "train_adjusted_p": adjusted_p, "qualified_on_train": qualified,
                        "test_compounded_return": _compound([returns[day] for day in test_bucket]) if test_bucket else None,
                        "test_benchmark_return": _compound([benchmark[day] for day in test_bucket]) if test_bucket else None,
                        "test_spy_return": _compound([benchmark[day] for day in test_bucket]) if test_bucket and benchmark_strategy == "SPY_BUY_HOLD" else None,
                        "test_excess_return": (_compound([returns[day] for day in test_bucket]) - _compound([benchmark[day] for day in test_bucket])) if test_bucket else None,
                        "test_mean_daily_excess": statistics.fmean(test_excess) if test_excess else None,
                        "test_positive_excess": statistics.fmean(test_excess) > 0 if test_excess else None,
                        "evidence_class": _evidence_class(test_dates, declaration),
                    })

    summary = []
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["source"], row["strategy"], row["dimension"], row["bucket"])].append(row)
    for key, values in sorted(grouped.items()):
        qualified = [row for row in values if row["qualified_on_train"]]
        selected = [
            row for row in values
            if row.get("selected_on_train", row["qualified_on_train"])
        ]
        tested = [row for row in qualified if row["test_mean_daily_excess"] is not None]
        all_tested = [row for row in values if row["test_mean_daily_excess"] is not None]
        summary.append({
            "source": key[0], "strategy": key[1], "dimension": key[2], "bucket": key[3],
            "folds": len(values), "qualified_folds": len(qualified),
            "selected_folds": len(selected), "qualified_test_folds": len(tested),
            "qualified_test_win_rate": sum(bool(row["test_positive_excess"]) for row in tested) / len(tested) if tested else None,
            "mean_qualified_test_daily_excess": statistics.fmean(float(row["test_mean_daily_excess"]) for row in tested) if tested else None,
            "stable_positive_relationship": bool(tested) and len(tested) >= 2 and sum(bool(row["test_positive_excess"]) for row in tested) / len(tested) >= 0.6,
            "exploratory_all_test_folds": len(all_tested),
            "exploratory_test_win_rate": sum(bool(row["test_positive_excess"]) for row in all_tested) / len(all_tested) if all_tested else None,
            "exploratory_mean_test_daily_excess": statistics.fmean(float(row["test_mean_daily_excess"]) for row in all_tested) if all_tested else None,
            "exploratory_only_not_router_eligible": True,
            "outer_test_results_are_descriptive": True,
            "router_eligible": False,
        })
    metadata = {
        "schema_version": 1, "source_bundle": path.name, "source_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        "source_manifest": source_manifest, "folds": fold, "train_sessions": train_sessions,
        "test_sessions": test_sessions, "step_sessions": step_sessions,
        "minimum_bucket_sessions": minimum_bucket_sessions, "alpha": alpha,
        "benchmark_strategy": benchmark_strategy,
        "validation_mode": validation_mode,
        "condition_dimensions": dimensions,
        "family_trials": family_trials, "selection_policy": "training-only; descriptive validation; no router or execution",
        "focused_declaration": declaration,
        "promotion_policy": "only untouched_forward rows may support promotion; retrospective_reuse and mixed_boundary are development evidence",
        "stable_relationships": sum(bool(row["stable_positive_relationship"]) for row in summary),
        "unconditional_multiple_testing_report": unconditional_multiple_testing,
        **nested_metadata,
    }
    return rows, summary, metadata


def write_validation(source: Path, output: Path, **kwargs) -> dict:
    rows, summary, metadata = validate_bundle(source, **kwargs)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("strategy_condition_walk_forward.csv", _csv_bytes(rows))
        bundle.writestr("strategy_condition_stability.csv", _csv_bytes(summary))
        bundle.writestr(
            "unconditional_multiple_testing_report.json",
            json.dumps(
                metadata["unconditional_multiple_testing_report"],
                indent=2, sort_keys=True,
            ),
        )
        bundle.writestr("strategy_condition_validation_manifest.json", json.dumps(metadata, indent=2, sort_keys=True))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--train-sessions", type=int, default=252)
    parser.add_argument("--test-sessions", type=int, default=63)
    parser.add_argument("--step-sessions", type=int, default=63)
    parser.add_argument("--minimum-bucket-sessions", type=int, default=30)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--cohort")
    parser.add_argument("--dimension", action="append", dest="dimensions",
                        help="Limit the multiplicity family to an explicit condition bucket column; repeatable")
    parser.add_argument("--benchmark-strategy", default="SPY_BUY_HOLD",
                        help="Strategy return series used as the direct comparator")
    parser.add_argument(
        "--validation-mode", choices=("session_expanding", "nested_episode_grouped"),
        default="session_expanding",
    )
    parser.add_argument("--nested-inner-folds", type=int, default=5)
    parser.add_argument("--nested-max-selected-per-dimension", type=int, default=1)
    parser.add_argument("--nested-min-inner-validation-episodes", type=int, default=1)
    parser.add_argument(
        "--multiple-testing-family-trials", type=int,
        help=(
            "Assert the exact number of complete aligned tried variants. The "
            "family-wide report is skipped when this or a complete trial ledger is absent."
        ),
    )
    parser.add_argument("--trial-ledger", type=Path)
    parser.add_argument("--trial-family-id")
    parser.add_argument("--multiple-testing-cscv-partitions", type=int, default=10)
    parser.add_argument("--multiple-testing-bootstrap-samples", type=int, default=2_000)
    parser.add_argument("--multiple-testing-mean-block-length", type=float, default=10.0)
    parser.add_argument("--multiple-testing-seed", type=int, default=29)
    args = parser.parse_args()
    metadata = write_validation(args.evidence_bundle, args.output, train_sessions=args.train_sessions,
                                test_sessions=args.test_sessions, step_sessions=args.step_sessions,
                                minimum_bucket_sessions=args.minimum_bucket_sessions, alpha=args.alpha,
                                declaration_path=args.declaration, cohort=args.cohort,
                                dimensions=tuple(args.dimensions) if args.dimensions else None,
                                benchmark_strategy=args.benchmark_strategy,
                                validation_mode=args.validation_mode,
                                nested_inner_folds=args.nested_inner_folds,
                                nested_max_selected_per_dimension=args.nested_max_selected_per_dimension,
                                nested_min_inner_validation_episodes=args.nested_min_inner_validation_episodes,
                                multiple_testing_family_trials=args.multiple_testing_family_trials,
                                trial_ledger_path=args.trial_ledger,
                                trial_family_id=args.trial_family_id,
                                multiple_testing_cscv_partitions=args.multiple_testing_cscv_partitions,
                                multiple_testing_bootstrap_samples=args.multiple_testing_bootstrap_samples,
                                multiple_testing_mean_block_length=args.multiple_testing_mean_block_length,
                                multiple_testing_seed=args.multiple_testing_seed)
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
