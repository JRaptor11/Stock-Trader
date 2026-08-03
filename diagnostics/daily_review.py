from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
from typing import Any
import zipfile

from zoneinfo import ZoneInfo

from core.state import app_state
from diagnostics.execution_analytics import build_execution_analytics
from layers.layer_csv import LAYER_CSV_FILES, layer_csv_path


EASTERN = ZoneInfo("America/New_York")
_CSV_LOCK = threading.Lock()
REVIEW_PACKAGE_DIR = Path("daily_review_packages")

ACCOUNT_FIELDS = [
    "timestamp", "trade_date", "snapshot_type", "capture_reason",
    "market_is_open", "equity", "last_equity", "cash", "buying_power",
    "portfolio_value", "long_market_value", "short_market_value",
    "gross_exposure", "net_exposure", "position_count",
]
POSITION_FIELDS = [
    "timestamp", "trade_date", "snapshot_type", "capture_reason", "symbol",
    "qty", "avg_entry_price", "current_price", "market_value",
    "cost_basis", "unrealized_pl", "unrealized_plpc", "side",
    "unrealized_intraday_pl", "unrealized_intraday_plpc", "change_today",
]
BENCHMARK_FIELDS = [
    "timestamp", "trade_date", "snapshot_type", "symbol", "bar_timestamp",
    "previous_close", "current_close", "return_pct", "source",
]


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, set, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _append_csv(filename: str, fields: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    path = layer_csv_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CSV_LOCK:
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            for row in rows:
                writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _ensure_csv(filename: str, fields: list[str]) -> None:
    path = layer_csv_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CSV_LOCK:
        if path.exists() and path.stat().st_size > 0:
            return
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=fields).writeheader()


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _serialize_account(account: Any) -> dict:
    return {
        key: _safe_float(getattr(account, key, 0))
        for key in (
            "equity", "last_equity", "cash", "buying_power", "portfolio_value",
            "long_market_value", "short_market_value",
        )
    }


def _serialize_position(position: Any) -> dict:
    return {
        "symbol": str(getattr(position, "symbol", "") or "").upper(),
        "qty": _safe_float(getattr(position, "qty", 0)),
        "avg_entry_price": _safe_float(getattr(position, "avg_entry_price", 0)),
        "current_price": _safe_float(getattr(position, "current_price", 0)),
        "market_value": _safe_float(getattr(position, "market_value", 0)),
        "cost_basis": _safe_float(getattr(position, "cost_basis", 0)),
        "unrealized_pl": _safe_float(getattr(position, "unrealized_pl", 0)),
        "unrealized_plpc": _safe_float(getattr(position, "unrealized_plpc", 0)),
        "unrealized_intraday_pl": _safe_float(
            getattr(position, "unrealized_intraday_pl", 0)
        ),
        "unrealized_intraday_plpc": _safe_float(
            getattr(position, "unrealized_intraday_plpc", 0)
        ),
        "change_today": _safe_float(getattr(position, "change_today", 0)),
        "side": str(getattr(getattr(position, "side", ""), "value", getattr(position, "side", ""))),
    }


def _fetch_benchmarks_sync(data_client: Any, now: datetime) -> list[dict]:
    if data_client is None:
        return []
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    request = StockBarsRequest(
        symbol_or_symbols=["SPY", "QQQ"],
        timeframe=TimeFrame.Day,
        start=now - timedelta(days=10),
        end=now,
        feed=DataFeed.IEX,
    )
    response = data_client.get_stock_bars(request)
    rows = []
    for symbol in ("SPY", "QQQ"):
        bars = list(getattr(response, "data", {}).get(symbol, []) or [])
        if not bars:
            continue
        current = bars[-1]
        previous = bars[-2] if len(bars) > 1 else None
        current_close = _safe_float(getattr(current, "close", 0))
        previous_close = _safe_float(getattr(previous, "close", 0)) if previous else 0
        rows.append({
            "symbol": symbol,
            "bar_timestamp": str(getattr(current, "timestamp", "") or ""),
            "previous_close": previous_close or None,
            "current_close": current_close or None,
            "return_pct": (
                (current_close / previous_close - 1) * 100
                if current_close > 0 and previous_close > 0 else None
            ),
            "source": "Alpaca IEX daily bars",
        })
    return rows


def _fetch_daily_snapshot_sync(client: Any) -> tuple[Any, list[Any], list[Any]]:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    account = client.get_account()
    positions = list(client.get_all_positions() or [])
    try:
        orders = list(client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500)
        ) or [])
    except Exception:
        orders = []
    return account, positions, orders


async def capture_daily_snapshot(
    snapshot_type: str,
    *,
    capture_reason: str,
    market_is_open: bool,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    trade_date = now.astimezone(EASTERN).date().isoformat()
    client = app_state.get("trading_client")
    if client is None:
        raise RuntimeError("missing_trading_client")

    account, positions, orders = await asyncio.to_thread(
        _fetch_daily_snapshot_sync, client
    )
    account_row = _serialize_account(account)
    position_rows = [_serialize_position(position) for position in positions]
    gross_exposure = sum(abs(row["market_value"]) for row in position_rows)
    net_exposure = sum(row["market_value"] for row in position_rows)
    common = {
        "timestamp": now.isoformat(),
        "trade_date": trade_date,
        "snapshot_type": snapshot_type,
        "capture_reason": capture_reason,
        "market_is_open": market_is_open,
    }
    _append_csv("daily_account_snapshots.csv", ACCOUNT_FIELDS, [{
        **common,
        **account_row,
        "gross_exposure": gross_exposure,
        "net_exposure": net_exposure,
        "position_count": len(position_rows),
    }])
    _ensure_csv("daily_position_snapshots.csv", POSITION_FIELDS)
    _append_csv("daily_position_snapshots.csv", POSITION_FIELDS, [
        {**common, **row} for row in position_rows
    ])

    try:
        benchmarks = await asyncio.to_thread(
            _fetch_benchmarks_sync, app_state.get("stock_data_client"), now
        )
    except Exception:
        logging.warning("[DailyReview] Benchmark snapshot failed.", exc_info=True)
        benchmarks = []
    _ensure_csv("daily_benchmark_snapshots.csv", BENCHMARK_FIELDS)
    _append_csv("daily_benchmark_snapshots.csv", BENCHMARK_FIELDS, [
        {**common, **row} for row in benchmarks
    ])

    state = app_state.setdefault("daily_review", {})
    state.setdefault("snapshots", {})[snapshot_type] = {
        **common,
        "account": account_row,
        "positions": position_rows,
        "benchmarks": benchmarks,
        "orders": [_serialize_order(order) for order in orders],
    }
    state["trade_date"] = trade_date
    return state["snapshots"][snapshot_type]


def _serialize_order(order: Any) -> dict:
    return {
        "id": str(getattr(order, "id", "") or ""),
        "client_order_id": str(getattr(order, "client_order_id", "") or ""),
        "symbol": str(getattr(order, "symbol", "") or "").upper(),
        "side": str(getattr(getattr(order, "side", ""), "value", getattr(order, "side", ""))),
        "status": str(getattr(getattr(order, "status", ""), "value", getattr(order, "status", ""))),
        "qty": _safe_float(getattr(order, "qty", 0)),
        "filled_qty": _safe_float(getattr(order, "filled_qty", 0)),
        "filled_avg_price": _safe_float(getattr(order, "filled_avg_price", 0)),
        "submitted_at": str(getattr(order, "submitted_at", "") or ""),
        "filled_at": str(getattr(order, "filled_at", "") or ""),
    }


def _commit_metadata() -> dict:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=5
        ).strip()
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], text=True, timeout=5
        ).strip()
        return {"commit_sha": sha, "branch": branch}
    except Exception:
        return {
            "commit_sha": os.getenv("RENDER_GIT_COMMIT"),
            "branch": os.getenv("RENDER_GIT_BRANCH"),
        }


def _redacted_config() -> dict:
    sensitive_fragments = (
        "KEY", "SECRET", "PASSWORD", "TOKEN", "EMAIL", "PHONE",
        "USER", "CHAT", "RECIPIENT",
    )
    allowed = {}
    for key, value in sorted(os.environ.items()):
        if any(fragment in key.upper() for fragment in sensitive_fragments):
            continue
        if key.startswith(("LAYER", "BAR_", "MAX_", "MIN_", "OLD_", "SYMBOL", "ENV", "LOG_")):
            allowed[key] = value
    return {
        "environment": allowed,
        "config_defaults": {
            key: value for key, value in app_state.get("config_defaults", {}).items()
            if not any(fragment in key.upper() for fragment in sensitive_fragments)
        },
        "config_overrides": {
            key: value for key, value in app_state.get("config_overrides", {}).items()
            if not any(fragment in key.upper() for fragment in sensitive_fragments)
        },
    }


def _daily_summary(
    snapshots: dict,
    *,
    trade_date: str | None = None,
    execution_rows: list[dict] | None = None,
    plan_rows: list[dict] | None = None,
) -> dict:
    opening = snapshots.get("open", {})
    closing = snapshots.get("close", {})
    open_account = opening.get("account", {})
    close_account = closing.get("account", {})
    open_equity = _safe_float(open_account.get("equity"))
    close_equity = _safe_float(close_account.get("equity"))
    open_positions = {
        row.get("symbol"): row for row in opening.get("positions", [])
        if row.get("symbol")
    }
    close_positions = {
        row.get("symbol"): row for row in closing.get("positions", [])
        if row.get("symbol")
    }
    symbols = sorted(set(open_positions) | set(close_positions))
    trade_date = (
        trade_date
        or closing.get("trade_date")
        or opening.get("trade_date")
        or datetime.now(EASTERN).date().isoformat()
    )
    execution_analytics = build_execution_analytics(
        snapshots,
        trade_date=trade_date,
        execution_rows=execution_rows,
        plan_rows=plan_rows,
    )
    return {
        "open_equity": open_equity or None,
        "close_equity": close_equity or None,
        "equity_change": (
            close_equity - open_equity if open_equity and close_equity else None
        ),
        "return_pct": (
            (close_equity / open_equity - 1) * 100
            if open_equity and close_equity else None
        ),
        "open_gross_exposure": sum(
            abs(_safe_float(row.get("market_value")))
            for row in open_positions.values()
        ),
        "close_gross_exposure": sum(
            abs(_safe_float(row.get("market_value")))
            for row in close_positions.values()
        ),
        "symbol_position_changes": [
            {
                "symbol": symbol,
                "open_qty": _safe_float(open_positions.get(symbol, {}).get("qty")),
                "close_qty": _safe_float(close_positions.get(symbol, {}).get("qty")),
                "open_market_value": _safe_float(
                    open_positions.get(symbol, {}).get("market_value")
                ),
                "close_market_value": _safe_float(
                    close_positions.get(symbol, {}).get("market_value")
                ),
                "close_unrealized_pl": _safe_float(
                    close_positions.get(symbol, {}).get("unrealized_pl")
                ),
                "close_unrealized_intraday_pl": _safe_float(
                    close_positions.get(symbol, {}).get(
                        "unrealized_intraday_pl"
                    )
                ),
            }
            for symbol in symbols
        ],
        "benchmarks": closing.get("benchmarks", []),
        "broker_order_count_at_close": len(closing.get("orders", [])),
        "execution_analytics": execution_analytics,
    }


def _read_diagnostic_rows(filename: str, trade_date: str) -> list[dict]:
    path = layer_csv_path(filename)
    if not path.exists():
        return []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        logging.warning(
            "[DailyReview] Failed reading %s for analytics.",
            filename,
            exc_info=True,
        )
        return []
    return [
        row for row in rows
        if str(
            row.get("timestamp")
            or row.get("broker_submitted_at")
            or row.get("plan_created_at")
            or ""
        )[:10] == trade_date
    ]


def build_daily_review_package(trade_date: str | None = None) -> Path:
    state = app_state.setdefault("daily_review", {})
    trade_date = trade_date or state.get("trade_date") or datetime.now(EASTERN).date().isoformat()
    REVIEW_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_PACKAGE_DIR / f"daily_review_{trade_date}.zip"
    snapshots = state.get("snapshots", {})
    execution_rows = _read_diagnostic_rows(
        LAYER_CSV_FILES["orders"], trade_date
    )
    plan_rows = _read_diagnostic_rows(
        LAYER_CSV_FILES["plans"], trade_date
    )
    daily_summary = _daily_summary(
        snapshots,
        trade_date=trade_date,
        execution_rows=execution_rows,
        plan_rows=plan_rows,
    )
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trade_date": trade_date,
        **_commit_metadata(),
        "snapshots_present": sorted(snapshots),
        "files": [],
    }

    candidates = {
        filename: layer_csv_path(filename)
        for filename in set(LAYER_CSV_FILES.values()) | {
            "fail_safe_lifecycle.csv",
            "daily_account_snapshots.csv",
            "daily_position_snapshots.csv",
            "daily_benchmark_snapshots.csv",
            "trade_history.csv",
            "trade_summary.csv",
        }
    }
    log_dir = Path("logs")
    if log_dir.exists():
        for log_path in log_dir.glob("trading_bot.log*"):
            candidates[f"logs/{log_path.name}"] = log_path

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, source in sorted(candidates.items()):
            if source.exists() and source.is_file():
                archive.write(source, arcname=arcname)
                metadata["files"].append({
                    "filename": arcname,
                    "size_bytes": source.stat().st_size,
                })
        archive.writestr("snapshots.json", json.dumps(snapshots, indent=2, default=str))
        archive.writestr(
            "daily_summary.json",
            json.dumps(daily_summary, indent=2, default=str),
        )
        archive.writestr(
            "execution_analytics.json",
            json.dumps(
                daily_summary.get("execution_analytics", {}),
                indent=2,
                default=str,
            ),
        )
        archive.writestr("config_redacted.json", json.dumps(_redacted_config(), indent=2, default=str))
        archive.writestr("manifest.json", json.dumps(metadata, indent=2, default=str))

    state["latest_package"] = str(path.resolve())
    state["latest_package_at"] = datetime.now(timezone.utc).isoformat()
    logging.warning("[DailyReview] Review package created: %s", path)
    return path


async def run_daily_review_monitor(poll_seconds: float = 30.0) -> None:
    shutdown = app_state["stream"]["shutdown_event"]
    state = app_state.setdefault("daily_review", {})
    previous_open: bool | None = None

    while not shutdown.is_set():
        client = app_state.get("trading_client")
        if client is None:
            await asyncio.sleep(min(poll_seconds, 5.0))
            continue
        try:
            clock = await asyncio.to_thread(client.get_clock)
            is_open = bool(getattr(clock, "is_open", False))
            now = datetime.now(timezone.utc)
            trade_date = now.astimezone(EASTERN).date().isoformat()
            if state.get("trade_date") != trade_date:
                state["trade_date"] = trade_date
                state["snapshots"] = {}
                state["package_created_for"] = None

            snapshots = state.setdefault("snapshots", {})
            if is_open and "open" not in snapshots:
                await capture_daily_snapshot(
                    "open",
                    capture_reason=(
                        "market_transition" if previous_open is False
                        else "startup_during_market_hours"
                    ),
                    market_is_open=True,
                    now=now,
                )
            local_now = now.astimezone(EASTERN)
            close_transition = previous_open is True and not is_open
            post_close_startup = (
                previous_open is None
                and not is_open
                and local_now.weekday() < 5
                and local_now.hour >= 16
            )
            if (
                (close_transition or post_close_startup)
                and "close" not in snapshots
            ):
                await capture_daily_snapshot(
                    "close",
                    capture_reason=(
                        "market_close_transition"
                        if close_transition else "startup_after_market_close"
                    ),
                    market_is_open=False,
                    now=now,
                )
                await asyncio.to_thread(build_daily_review_package, trade_date)
                state["package_created_for"] = trade_date
            previous_open = is_open
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.warning("[DailyReview] Monitor pass failed.", exc_info=True)
        await asyncio.sleep(poll_seconds)
