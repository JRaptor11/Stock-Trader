# Intraday research failure matrix

The engine fails closed for inputs that could make historical results appear more
credible than the underlying data permits. This matrix identifies the behavior
that must remain covered by automated tests.

| Area | Failure | Required behavior |
|---|---|---|
| Bars | Missing OHLCV columns, duplicate symbol/timestamp, naive timestamps | Reject input or fail promotion |
| Security master | Missing columns, invalid dates/booleans, reversed or overlapping intervals | Reject input |
| Security master | Missing effective record for an observed symbol-session | Exclude it, diagnose the gap, fail promotion |
| Survivorship | Current-symbol dataset labeled merely “available” | Ignore legacy flag; require explicit source-universe guarantee |
| Market events | Invalid event type, missing source, malformed halt/LULD interval | Reject input |
| Market events | Missing or duplicate daily coverage declarations | Reject duplicates; fail promotion for gaps |
| Delistings | Missing return or return below -100% | Reject input |
| Fundamentals | Naive `known_at`, nonpositive values, duplicate snapshots | Reject input |
| Fundamentals | Snapshot learned after intended entry | Never use it |
| Fundamentals | Missing causal snapshot when file is supplied | Reject that signal and fail complete-coverage gate |
| Execution | Signal formed on unfinished bar | Prohibited by completed-bar/next-open semantics |
| Execution | Stop and target both touched in one bar | Apply stop first |
| Liquidity | Requested notional exceeds participation capacity | Reject fill |
| Portfolio | Insufficient cash, symbol limit, or position limit | Reject trade with diagnostic reason |
| Statistics | Too little history for a walk-forward fold | Emit no fabricated fold |
| Checkpoint | Corrupt JSON, wrong identity, wrong schema | Ignore and recompute |
| Checkpoint | Duplicate, unknown, or malformed completed variant | Discard restored rows and recompute variants |
| Restart | Compatible completed stability variant | Restore and skip exact rerun |
| Storage | Checkpoint upload fails | Keep running from local state; expose failure in logs |
| Completion | Successful durable result | Delete obsolete checkpoint and local cached datasets |

The full suite should be run before deployment. Optional FastAPI integration tests
are skipped only when their declared dependencies are not installed; deployment
validation should run them in the service dependency environment.
