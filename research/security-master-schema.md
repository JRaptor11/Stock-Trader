# Point-in-time security master

Intraday equity jobs may provide `security_master_csv` beside `bars_csv`. Upload both
through the research dataset endpoint before submitting the job.

Required columns:

```text
symbol,effective_from,effective_to,listed,tradable,exchange,security_type
```

- Dates use ISO `YYYY-MM-DD` form and both endpoints are inclusive.
- Leave `effective_to` empty only when the record remains effective.
- Intervals for one symbol may not overlap.
- `listed` and `tradable` accept only `true`, `false`, `1`, or `0`.
- Every observed symbol-session must have an effective record for complete coverage.

Example job fields:

```json
{
  "bars_csv": "intraday-bars.csv",
  "security_master_csv": "security-master.csv",
  "intraday_config": {
    "survivorship_safe_universe": false
  }
}
```

Complete classification coverage does not by itself establish a survivorship-safe
universe. Set `survivorship_safe_universe` only when the upstream data construction
explicitly included securities that later delisted or otherwise left the eligible
universe. The result manifest records this declaration, and the promotion gate
requires both complete observed coverage and that upstream guarantee.
