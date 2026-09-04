# Point-in-time market-cap and float data

Intraday equity jobs may provide `fundamentals_csv`. Upload it through the same
dataset endpoint used for bars, then reference its filename in the job.

Required columns:

```text
symbol,effective_date,known_at,market_cap,float_shares,source
```

- `effective_date` is the economic date represented by the observation.
- `known_at` is the timezone-aware timestamp when the value became available to
  the strategy. Replay selection requires both fields to be causal.
- Market cap and float shares must be positive raw values, not millions.
- Duplicate symbol/effective-date/known-at observations are rejected.

Optional `intraday_config` filters are `minimum_market_cap`,
`maximum_market_cap`, `minimum_float_shares`, and `maximum_float_shares`.
When a fundamentals file is supplied, a missing causal snapshot makes that
symbol ineligible at that entry. When no file is supplied, exploratory runs may
continue, but the promotion gate fails closed.

The archive includes `point_in_time_fundamentals_diagnostics.csv`, overall
coverage, and the source-file SHA-256 digest.
