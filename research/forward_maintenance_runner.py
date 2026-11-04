"""Deterministic, restart-safe maintenance for every frozen forward track.

This command is intended for an operating-system scheduler.  It replaces the
interactive heartbeat as the executor while keeping the heartbeat useful as a
read-only monitor.  Every mutation is append-only/idempotent, a process lock
prevents overlap, and the existing maintenance guard runs before and after.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from research.download_alpaca_bars import download as download_alpaca_bars
from research.forward_maintenance_guard import build_report, fetch_service_status
from research.qqq_bull_leadership_forward_ledger import append_session as append_qqq
from research.static_balanced_forward_ledger import append_session as append_static
from research.submit_prospective_shadow import submit_completed_session
from research.tier1_forward_ledger import append_missing_observations
from research.turning_point_forward_ledger import append_observations
from research.universes import resolve_universe


UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")
DEFAULT_SERVICE_URL = "https://stock-trader-9du8.onrender.com"
DEFAULT_MINIMUM_REVISION = "01c1105"
FRESHNESS_PREFIXES = ("latest session is ", "remote shadow is at ")


@dataclass(frozen=True)
class CalendarPlan:
    sessions: tuple[str, ...]
    latest_completed: str
    next_session: str
    weekly_required: str
    latest_is_week_final: bool

    def sessions_between(self, first: str, last: str) -> list[str]:
        if first not in self.sessions:
            raise ValueError(f"session cursor {first} is not in the market calendar")
        if last not in self.sessions:
            raise ValueError(f"target session {last} is not in the market calendar")
        start = self.sessions.index(first)
        end = self.sessions.index(last)
        if start > end:
            return []
        return list(self.sessions[start:end + 1])

    def following(self, session: str) -> str:
        index = self.sessions.index(session)
        if index + 1 >= len(self.sessions):
            raise ValueError(f"market calendar lacks a next session after {session}")
        return self.sessions[index + 1]


class RunLock:
    """Small cross-platform lock with conservative stale-owner recovery."""

    def __init__(self, path: Path, stale_seconds: int = 6 * 60 * 60) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    payload = {}
                created = str(payload.get("created_at") or "")
                try:
                    age = (datetime.now(UTC) - datetime.fromisoformat(created)).total_seconds()
                except ValueError:
                    age = self.stale_seconds + 1
                pid = int(payload.get("pid") or 0)
                if age <= self.stale_seconds or self._pid_alive(pid):
                    raise RuntimeError(
                        f"forward maintenance is already running under pid {pid or 'unknown'}"
                    )
                self.path.unlink(missing_ok=True)
                continue
            document = {
                "pid": os.getpid(),
                "created_at": datetime.now(UTC).isoformat(),
            }
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True)
            self.acquired = True
            return self

    def __exit__(self, *_: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.document: dict[str, Any] = {
            "status": "starting",
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
            "stages": [],
            "broker_orders_enabled": False,
        }
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.document, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def stage(self, name: str, **details: Any) -> None:
        self.document["status"] = "running"
        self.document["stage"] = name
        self.document["stages"].append({
            "name": name,
            "completed_at": datetime.now(UTC).isoformat(),
            **details,
        })
        self._write()

    def complete(self, summary: dict) -> None:
        self.document.update({
            "status": "complete",
            "stage": "complete",
            "completed_at": datetime.now(UTC).isoformat(),
            "summary": summary,
        })
        self._write()

    def fail(self, exc: BaseException) -> None:
        self.document.update({
            "status": "failed",
            "failed_at": datetime.now(UTC).isoformat(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        self._write()


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _research_token(path: Path) -> str:
    token = _env_values(path).get("RESEARCH_API_TOKEN")
    if not token:
        raise ValueError("RESEARCH_API_TOKEN is missing")
    return token


def _json_request(url: str, *, headers: dict[str, str] | None = None,
                  timeout: float = 30.0) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _parse_calendar_time(day: str, value: str) -> datetime:
    parsed = time.fromisoformat(value)
    return datetime.combine(date.fromisoformat(day), parsed, EASTERN)


def build_calendar_plan(rows: list[dict], now: datetime) -> CalendarPlan:
    if now.tzinfo is None:
        raise ValueError("calendar clock must be timezone-aware")
    ordered = sorted(rows, key=lambda row: str(row["date"]))
    sessions = tuple(str(row["date"]) for row in ordered)
    completed = [
        str(row["date"])
        for row in ordered
        if _parse_calendar_time(str(row["date"]), str(row["close"])) <= now.astimezone(EASTERN)
    ]
    if not completed:
        raise ValueError("market calendar contains no completed session")
    latest = completed[-1]
    return calendar_plan_for_latest(sessions, latest)


def calendar_plan_for_latest(sessions: tuple[str, ...], latest: str) -> CalendarPlan:
    latest_index = sessions.index(latest)
    completed = sessions[:latest_index + 1]
    if latest_index + 1 >= len(sessions):
        raise ValueError("market calendar does not include the next trading session")
    next_session = sessions[latest_index + 1]
    latest_week = date.fromisoformat(latest).isocalendar()[:2]
    next_week = date.fromisoformat(next_session).isocalendar()[:2]
    latest_is_week_final = latest_week != next_week
    if latest_is_week_final:
        weekly = latest
    else:
        prior = [
            session for session in completed
            if date.fromisoformat(session).isocalendar()[:2] != latest_week
        ]
        if not prior:
            raise ValueError("market calendar lacks a prior completed week")
        weekly = prior[-1]
    return CalendarPlan(
        sessions=sessions,
        latest_completed=latest,
        next_session=next_session,
        weekly_required=weekly,
        latest_is_week_final=latest_is_week_final,
    )


def _calendar_summary(plan: CalendarPlan) -> dict:
    return {
        "latest_completed": plan.latest_completed,
        "next_session": plan.next_session,
        "weekly_required": plan.weekly_required,
        "latest_is_week_final": plan.latest_is_week_final,
        "calendar_first": plan.sessions[0],
        "calendar_last": plan.sessions[-1],
        "calendar_sessions": len(plan.sessions),
    }


def fetch_calendar_plan(alpaca_env: Path) -> CalendarPlan:
    credentials = _env_values(alpaca_env)
    api_key = credentials.get("API_KEY") or credentials.get("APCA_API_KEY_ID")
    secret = credentials.get("SECRET_KEY") or credentials.get("APCA_API_SECRET_KEY")
    if not api_key or not secret:
        raise ValueError("Alpaca read-only credentials are missing")
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
    clock = _json_request(
        "https://paper-api.alpaca.markets/v2/clock",
        headers=headers,
    )
    now = datetime.fromisoformat(str(clock["timestamp"]))
    start = (now.date() - timedelta(days=370)).isoformat()
    end = (now.date() + timedelta(days=14)).isoformat()
    query = urllib.parse.urlencode({"start": start, "end": end})
    rows = _json_request(
        "https://paper-api.alpaca.markets/v2/calendar?" + query,
        headers=headers,
    )
    return build_calendar_plan(rows, now)


def _service_health(service_url: str) -> dict:
    health = _json_request(service_url.rstrip("/") + "/healthz")
    if health.get("status") != "ok" or health.get("coordinator_ready") is not True:
        raise RuntimeError(f"research service is not coordinator-ready: {health}")
    if health.get("service_mode") != "historical_research":
        raise RuntimeError("research service has the wrong service mode")
    return health


def _require_descendant(repo_root: Path, minimum_revision: str, deployed: str) -> None:
    if not deployed:
        raise RuntimeError("research service does not report its deployed revision")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", minimum_revision, deployed],
        cwd=repo_root,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"deployed research revision {deployed} is not a descendant of {minimum_revision}"
        )


def _guard_report(repo_root: Path, workspace_root: Path, service_url: str,
                  token: str, plan: CalendarPlan) -> dict:
    policy = json.loads(
        (repo_root / "research" / "forward-maintenance-policy-001.json").read_text(
            encoding="utf-8"
        )
    )
    status = fetch_service_status(service_url, token)
    return build_report(
        policy,
        workspace_root,
        plan.latest_completed,
        plan.weekly_required,
        status,
    )


def validate_preflight(report: dict) -> None:
    errors: list[str] = []
    if report.get("broker_orders_enabled") is not False:
        errors.append("maintenance guard does not explicitly disable broker orders")
    for error in report.get("service", {}).get("errors", []):
        if not str(error).startswith(FRESHNESS_PREFIXES):
            errors.append("service: " + str(error))
    for ledger in report.get("ledgers", []):
        for error in ledger.get("errors", []):
            if not str(error).startswith(FRESHNESS_PREFIXES):
                errors.append(f"{ledger.get('id')}: {error}")
    if errors:
        raise RuntimeError("preflight integrity failure: " + "; ".join(errors))


def _csv_latest_session(path: Path) -> str:
    latest = ""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            latest = max(latest, str(row["timestamp"])[:10])
    if not latest:
        raise ValueError(f"daily-bar source is empty: {path}")
    return latest


def _merge_nonoverlapping_csv(base: Path, incremental: Path, output: Path) -> None:
    base_latest = _csv_latest_session(base)
    incremental_rows: list[dict] = []
    with incremental.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        incremental_rows = list(reader)
    if not fields or not incremental_rows:
        raise ValueError("incremental daily-bar source is empty")
    incremental_first = min(row["timestamp"][:10] for row in incremental_rows)
    if incremental_first <= base_latest:
        raise ValueError(
            f"daily-bar ranges overlap: base ends {base_latest}, increment starts {incremental_first}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base, output)
    with output.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writerows(incremental_rows)


def _download(
    *,
    symbols: tuple[str, ...],
    start: str,
    end: str,
    alpaca_env: Path,
    output: Path,
) -> dict:
    values = _env_values(alpaca_env)
    api_key = values.get("API_KEY") or values.get("APCA_API_KEY_ID")
    secret = values.get("SECRET_KEY") or values.get("APCA_API_SECRET_KEY")
    if not api_key or not secret:
        raise ValueError("Alpaca read-only credentials are missing")
    result = download_alpaca_bars(
        symbols=list(symbols),
        start=start,
        end=end,
        feed="iex",
        timeframe="1Day",
        chunk_days=366,
        api_key=api_key,
        secret_key=secret,
        output=output,
    )
    missing = [
        symbol for symbol in symbols
        if (result.get("per_symbol", {}).get(symbol) or {}).get("last") is None
    ]
    if missing:
        raise ValueError(f"adjusted daily download is missing symbols: {missing}")
    return result


def _require_download_through(result: dict, symbols: tuple[str, ...], session: str) -> None:
    stale = [
        symbol for symbol in symbols
        if str((result.get("per_symbol", {}).get(symbol) or {}).get("last") or "")[:10]
        != session
    ]
    if stale:
        raise ValueError(f"adjusted daily download lacks {session} for symbols: {stale}")


def _read_state_cursor(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"rolling forward state is missing: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("broker_orders_enabled") is not False:
        raise ValueError(f"rolling state does not explicitly disable broker orders: {path}")
    cursor = str(state.get("next_session") or "")
    if not cursor:
        raise ValueError(f"rolling state has no next-session cursor: {path}")
    return cursor


def _run_worker_job(job: Path, data_root: Path, results_root: Path) -> Path:
    environment = dict(os.environ)
    environment.update({
        "SERVICE_MODE": "historical_research",
        "BROKER_EXECUTION_ENABLED": "false",
    })
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "research.worker",
            "--job",
            str(job),
            "--data-root",
            str(data_root),
            "--results-root",
            str(results_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=4 * 60 * 60,
        env=environment,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "worker failed")[-4000:]
        raise RuntimeError(detail)
    definition = json.loads(job.read_text(encoding="utf-8"))
    archive = results_root / f"{definition['job_id']}-results.zip"
    if not archive.is_file():
        raise RuntimeError(f"research worker did not produce {archive.name}")
    return archive


def _make_job(source: Path, destination: Path, *, job_id: str,
              bars_csv: str, data_source: str) -> None:
    document = json.loads(source.read_text(encoding="utf-8"))
    document["job_id"] = job_id
    document["bars_csv"] = bars_csv
    document["data_source"] = data_source
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _ledger_latest(path: Path, field: str) -> str:
    if not path.is_file():
        return ""
    latest = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            latest = max(latest, str(json.loads(line).get(field) or ""))
    return latest


def _daily_dataset_needed(report: dict) -> bool:
    if not report.get("service", {}).get("ok"):
        return True
    return any(
        not row.get("ok")
        for row in report.get("ledgers", [])
        if row.get("id") in {"static-60-30-10", "qqq-bull-leadership"}
    )


def _run_daily_tracks(
    *,
    report: dict,
    plan: CalendarPlan,
    repo_root: Path,
    workspace_root: Path,
    alpaca_env: Path,
    token_env: Path,
    service_url: str,
    deployed_revision: str,
    temporary: Path,
) -> dict:
    results: dict[str, Any] = {"download": None, "service": [], "static": [], "qqq": []}
    if not _daily_dataset_needed(report):
        return results
    start = (date.fromisoformat(plan.latest_completed) - timedelta(days=900)).isoformat()
    end = (date.fromisoformat(plan.latest_completed) + timedelta(days=1)).isoformat()
    daily_csv = temporary / "expanded-daily.csv"
    results["download"] = _download(
        symbols=tuple(resolve_universe("ETF_LONG_TERM_RESEARCH_EXPANDED")),
        start=start,
        end=end,
        alpaca_env=alpaca_env,
        output=daily_csv,
    )
    _require_download_through(
        results["download"],
        tuple(resolve_universe("ETF_LONG_TERM_RESEARCH_EXPANDED")),
        plan.latest_completed,
    )

    service_status = fetch_service_status(service_url, _research_token(token_env))
    service_cursor = str(service_status.get("expected_next_session") or "")
    if service_status.get("last_session") != plan.latest_completed:
        for session in plan.sessions_between(service_cursor, plan.latest_completed):
            results["service"].append(submit_completed_session(
                csv_path=daily_csv,
                session=session,
                next_session=plan.following(session),
                code_revision=deployed_revision,
                token_env=token_env,
                url=service_url,
                alpaca_env=alpaca_env,
            ))

    static_root = workspace_root / "static-60-30-10-forward-shadow"
    static_cursor = _read_state_cursor(static_root / "state.json")
    for session in plan.sessions_between(static_cursor, plan.latest_completed):
        results["static"].append(append_static(
            daily_csv,
            static_root / "ledger.jsonl",
            static_root / "state.json",
            session,
            plan.following(session),
        ))

    qqq_root = workspace_root / "qqq-bull-leadership-forward-validation-001"
    qqq_cursor = _read_state_cursor(qqq_root / "state.json")
    for session in plan.sessions_between(qqq_cursor, plan.latest_completed):
        results["qqq"].append(append_qqq(
            daily_csv,
            qqq_root / "ledger.jsonl",
            qqq_root / "state.json",
            session,
            plan.following(session),
        ))
    return results


def _run_turning_point(
    *,
    plan: CalendarPlan,
    repo_root: Path,
    workspace_root: Path,
    alpaca_env: Path,
    temporary: Path,
) -> dict:
    ledger_root = workspace_root / "long-term-turning-point-forward-validation-001"
    ledger = ledger_root / "ledger.jsonl"
    if _ledger_latest(ledger, "session") == plan.latest_completed:
        return {"status": "unchanged", "session": plan.latest_completed}
    base = repo_root / "research_data" / "long_term_research_expanded_adjusted_daily_2011_current.csv"
    base_latest = _csv_latest_session(base)
    incremental = temporary / "turning-incremental.csv"
    merged = temporary / "turning-data" / base.name
    symbols = tuple(resolve_universe("ETF_LONG_TERM_STATE_SPECIALISTS_EXACT"))
    downloaded = _download(
        symbols=symbols,
        start=(date.fromisoformat(base_latest) + timedelta(days=1)).isoformat(),
        end=(date.fromisoformat(plan.latest_completed) + timedelta(days=1)).isoformat(),
        alpaca_env=alpaca_env,
        output=incremental,
    )
    _require_download_through(downloaded, symbols, plan.latest_completed)
    _merge_nonoverlapping_csv(base, incremental, merged)
    job = temporary / "turning-job.json"
    job_id = f"long-term-turning-point-forward-validation-001-{plan.latest_completed}"
    _make_job(
        repo_root / "research" / "long-term-turning-point-forward-validation-001-job.json",
        job,
        job_id=job_id,
        bars_csv=merged.name,
        data_source="frozen historical source plus adjusted Alpaca IEX daily incremental",
    )
    archive = _run_worker_job(job, merged.parent, temporary / "turning-results")
    destination = ledger_root / f"long-term-turning-point-forward-validation-001-{plan.latest_completed}-results.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, destination)
    result = append_observations(
        destination,
        repo_root / "research" / "long-term-turning-point-forward-validation-001.json",
        ledger,
    )
    result["archive"] = str(destination)
    result["archive_bytes"] = destination.stat().st_size
    return result


WEEKLY_COHORTS = {
    "cross-asset": "long-term-validation-phase-001-cross-asset-job.json",
    "factor": "long-term-validation-phase-001-factor-job.json",
    "industry": "long-term-validation-phase-001-industry-job.json",
    "sector": "long-term-validation-phase-001-sector-job.json",
}

WEEKLY_LEDGERS = (
    ("cross-asset", "CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", "cross_asset_relative_momentum_defensive.jsonl"),
    ("cross-asset", "STATIC_INFLATION_AWARE", "static_inflation_aware.jsonl"),
    ("factor", "VALUE_QUALITY_STATIC", "value_quality_static.jsonl"),
    ("industry", "INDUSTRY_ETF_MOMENTUM", "industry_etf_momentum.jsonl"),
    ("sector", "SECTOR_ETF_ROTATION", "sector_etf_rotation.jsonl"),
)


def _run_weekly_tracks(
    *,
    report: dict,
    plan: CalendarPlan,
    repo_root: Path,
    workspace_root: Path,
    alpaca_env: Path,
    temporary: Path,
) -> dict:
    weekly_rows = [row for row in report["ledgers"] if row["id"].startswith("phase001-")]
    if all(row.get("latest_session") == plan.weekly_required for row in weekly_rows):
        return {"status": "unchanged", "session": plan.weekly_required}
    weekly_root = workspace_root / "long-term-forward-shadow"
    base = weekly_root / "long_term_phase001_2013_2026-10-23.csv"
    base_latest = _csv_latest_session(base)
    incremental = temporary / "weekly-incremental.csv"
    weekly_names = (
        "ETF_LONG_TERM_CROSS_ASSET_HISTORY",
        "ETF_LONG_TERM_FACTOR_HISTORY",
        "ETF_LONG_TERM_INDUSTRY_HISTORY",
        "ETF_LONG_TERM_SECTOR_HISTORY",
    )
    symbols = tuple(sorted(set().union(*(
        set(resolve_universe(name)) for name in weekly_names
    ))))
    downloaded = _download(
        symbols=symbols,
        start=(date.fromisoformat(base_latest) + timedelta(days=1)).isoformat(),
        end=(date.fromisoformat(plan.weekly_required) + timedelta(days=1)).isoformat(),
        alpaca_env=alpaca_env,
        output=incremental,
    )
    _require_download_through(downloaded, symbols, plan.weekly_required)
    merged = temporary / "weekly-data" / f"long-term-phase001-through-{plan.weekly_required}.csv"
    _merge_nonoverlapping_csv(base, incremental, merged)
    archives: dict[str, Path] = {}
    archive_metadata = []
    for cohort, filename in WEEKLY_COHORTS.items():
        job = temporary / f"weekly-{cohort}-job.json"
        job_id = f"long-term-phase001-forward-{plan.weekly_required}-{cohort}"
        _make_job(
            repo_root / "research" / filename,
            job,
            job_id=job_id,
            bars_csv=merged.name,
            data_source="frozen Yahoo adjusted history plus adjusted Alpaca IEX daily incremental",
        )
        archive = _run_worker_job(job, merged.parent, temporary / f"weekly-results-{cohort}")
        destination = weekly_root / f"{job_id}-results.zip"
        shutil.copy2(archive, destination)
        archives[cohort] = destination
        archive_metadata.append({
            "cohort": cohort,
            "path": str(destination),
            "bytes": destination.stat().st_size,
        })
    appends = []
    for cohort, strategy, filename in WEEKLY_LEDGERS:
        appends.append(append_missing_observations(
            archives[cohort],
            weekly_root / filename,
            "2026-09-25",
            strategy,
            "phase001-frozen",
        ))
    return {
        "status": "appended",
        "session": plan.weekly_required,
        "archives": archive_metadata,
        "ledgers": appends,
    }


def run(args: argparse.Namespace) -> dict:
    repo_root = args.repo_root.resolve()
    workspace_root = args.workspace_root.resolve()
    runtime_root = args.runtime_root.resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    journal = Journal(runtime_root / "last-run.json")
    try:
        plan = fetch_calendar_plan(args.alpaca_env)
        if args.completed_session:
            if args.completed_session not in plan.sessions:
                raise ValueError("completed-session override is not in the market calendar")
            plan = calendar_plan_for_latest(plan.sessions, args.completed_session)
        health = _service_health(args.service_url)
        deployed = str(health.get("git_commit") or "")
        _require_descendant(repo_root, args.minimum_revision, deployed)
        token = _research_token(args.token_env)
        preflight = _guard_report(
            repo_root, workspace_root, args.service_url, token, plan
        )
        validate_preflight(preflight)
        journal.stage(
            "preflight",
            calendar=_calendar_summary(plan),
            deployed_revision=deployed,
            guard_ok=preflight["ok"],
        )
        if args.plan_only:
            summary = {
                "status": "planned",
                "calendar": _calendar_summary(plan),
                "preflight": preflight,
                "broker_orders_enabled": False,
            }
            journal.complete(summary)
            return summary
        if preflight["ok"]:
            summary = {
                "status": "unchanged",
                "calendar": _calendar_summary(plan),
                "post_guard": preflight,
                "broker_orders_enabled": False,
            }
            journal.complete(summary)
            return summary

        temporary_parent = runtime_root / "tmp"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"forward-{plan.latest_completed}-",
            dir=temporary_parent,
        ) as folder:
            temporary = Path(folder)
            daily = _run_daily_tracks(
                report=preflight,
                plan=plan,
                repo_root=repo_root,
                workspace_root=workspace_root,
                alpaca_env=args.alpaca_env,
                token_env=args.token_env,
                service_url=args.service_url,
                deployed_revision=deployed,
                temporary=temporary,
            )
            journal.stage(
                "daily_tracks",
                service_sessions=[row.get("session") for row in daily["service"]],
                static_sessions=[row.get("session") for row in daily["static"]],
                qqq_sessions=[row.get("session") for row in daily["qqq"]],
            )
            turning = _run_turning_point(
                plan=plan,
                repo_root=repo_root,
                workspace_root=workspace_root,
                alpaca_env=args.alpaca_env,
                temporary=temporary,
            )
            journal.stage("turning_point", result=turning)
            weekly = _run_weekly_tracks(
                report=preflight,
                plan=plan,
                repo_root=repo_root,
                workspace_root=workspace_root,
                alpaca_env=args.alpaca_env,
                temporary=temporary,
            )
            journal.stage("weekly_tracks", result=weekly)
        post = _guard_report(repo_root, workspace_root, args.service_url, token, plan)
        if not post["ok"] or post.get("broker_orders_enabled") is not False:
            raise RuntimeError("post-maintenance guard did not pass: " + json.dumps(post))
        summary = {
            "status": "complete",
            "calendar": _calendar_summary(plan),
            "daily": daily,
            "turning_point": turning,
            "weekly": weekly,
            "post_guard": post,
            "broker_orders_enabled": False,
        }
        journal.complete(summary)
        return summary
    except BaseException as exc:
        journal.fail(exc)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_repo = Path(__file__).resolve().parents[1]
    parser.add_argument("--repo-root", type=Path, default=default_repo)
    parser.add_argument("--workspace-root", type=Path, default=default_repo.parent)
    parser.add_argument("--alpaca-env", type=Path, required=True)
    parser.add_argument("--token-env", type=Path, required=True)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=default_repo.parent / "forward-maintenance-runtime",
    )
    parser.add_argument("--service-url", default=DEFAULT_SERVICE_URL)
    parser.add_argument("--minimum-revision", default=DEFAULT_MINIMUM_REVISION)
    parser.add_argument("--completed-session")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args(argv)
    lock = args.runtime_root.resolve() / "maintenance.lock"
    try:
        with RunLock(lock):
            summary = run(args)
    except Exception as exc:
        print(json.dumps({
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "broker_orders_enabled": False,
        }, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
