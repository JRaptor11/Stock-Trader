# Point-in-time market events

Intraday equity jobs may provide `market_events_csv`. The file combines actual
events with explicit coverage rows so that “no event occurred” is distinguishable
from “event data was unavailable.”

Required columns:

```text
record_type,symbol,effective_date,event_type,start_timestamp,end_timestamp,delisting_return,halt_luld_complete,corporate_actions_complete,delistings_complete,source
```

Coverage rows use `record_type=coverage`, one row per market session, and populate
the three completeness booleans. Event rows use `record_type=event` and one of:
`HALT`, `LULD_PAUSE`, `SPLIT`, `DIVIDEND`, or `DELISTING`.

Halt and LULD timestamps must include a UTC offset. Signals or intended entries
inside those intervals are rejected. Delisting events require a total delisting
return. Because the current engine exits every position intraday, that return is
audited but not applied to its P&L.

Promotion requires complete coverage for all three event families across every
session in the filtered replay dataset. A missing event file therefore fails the
gate safely.
