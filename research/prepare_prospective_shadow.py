"""Freeze and retrieve the readiness manifest for a prospective session."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from research.submit_prospective_shadow import _token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-session", required=True)
    parser.add_argument("--token-env", required=True, type=Path)
    parser.add_argument("--url", default="https://stock-trader-9du8.onrender.com")
    args = parser.parse_args()
    request = urllib.request.Request(
        args.url.rstrip("/") + "/api/shadow/readiness",
        data=json.dumps({"expected_session": args.expected_session}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + _token(args.token_env),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    safe = {
        key: payload.get(key) for key in (
            "status", "expected_session", "manifest_sha256", "durable_uri",
            "checks", "missing_strategies", "wrong_execution_session",
            "missing_prior_closes",
        )
    }
    print(json.dumps(safe, indent=2))


if __name__ == "__main__":
    main()
