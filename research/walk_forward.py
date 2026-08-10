from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_dates: tuple[str, ...]
    test_dates: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


def build_walk_forward_folds(
    session_dates,
    *,
    min_train_sessions: int = 60,
    test_sessions: int = 20,
    step_sessions: int | None = None,
    expanding: bool = True,
    rolling_train_sessions: int | None = None,
) -> list[WalkForwardFold]:
    """Create chronological folds without randomized or overlapping leakage."""
    dates = sorted({str(value)[:10] for value in session_dates if value})
    min_train_sessions = max(1, int(min_train_sessions))
    test_sessions = max(1, int(test_sessions))
    step_sessions = max(1, int(step_sessions or test_sessions))
    if len(dates) < min_train_sessions + test_sessions:
        return []

    folds = []
    train_end = min_train_sessions
    fold_number = 1
    while train_end + test_sessions <= len(dates):
        train_start = 0
        if not expanding:
            window = max(min_train_sessions, int(rolling_train_sessions or min_train_sessions))
            train_start = max(0, train_end - window)
        train = tuple(dates[train_start:train_end])
        test = tuple(dates[train_end:train_end + test_sessions])
        folds.append(WalkForwardFold(
            fold=fold_number,
            train_start=train[0], train_end=train[-1],
            test_start=test[0], test_end=test[-1],
            train_dates=train, test_dates=test,
        ))
        fold_number += 1
        train_end += step_sessions
    return folds
