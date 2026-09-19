# Long-Term Condition Mapping Phase 002

Generation 015 remains paused. This phase does not build a router, retune a
strategy, promote a model, or change live trading.

## Research question

For each independently defined, causally observable long-term market state,
which allocation strategy produces the strongest reliable net result relative
to SPY and every other candidate tested on the same sessions?

SPY is a condition-specific benchmark, not the required global winner or the
only acceptable portfolio. A cash-like, Treasury, inflation-protected, gold,
factor, sector, industry, or diversified allocation may lead a condition.

## Canonical state contract

All strategy families use `ETF_LONG_TERM_STATE_CANONICAL` to calculate trend,
volatility, breadth, dispersion, and correlation. The strategy opportunity set
may differ, but it cannot redefine the state being measured. The canonical
roster deliberately uses SPY and nine long-history sector ETFs so newer fund
inception dates do not change older labels.

Every direct comparison must share:

- the same causal state label for a given session;
- the same date range and benchmark return;
- the same next-session-open execution semantics;
- the 1, 5, 10, and 20 basis-point cost ladder, with 10 basis points primary;
- separate discovery, untouched holdout, chronological-fold, and forward data;
- minimum session and distinct-episode gates.

Leadership requires more than the highest pooled return. Reports must retain
excess wealth versus SPY, episode win rate, median and worst episode, fold
recurrence, drawdown, turnover, cost survival, and sample sufficiency.

## Allocation opportunity roles

The expanded research universe covers broad, international, small-cap, factor,
sector, industry, real-estate, commodity, gold, cash-equivalent, nominal
Treasury, inflation-protected Treasury, dividend-growth, and minimum-volatility
allocations. Stable alternatives are diversified ETFs rather than individual
companies; a historically stable company can still suffer company-specific
failure and creates severe survivorship bias when selected retrospectively.

The explicit stable/defensive benchmarks are BIL, SHY, IEF, TLT, TIP, GLD,
VIG, and SPLV. Their inclusion lets the evidence show whether avoiding a loss
or earning a modest positive return is superior in a hostile state.

## Alpaca Basic boundary

The research universe and live watch roster are separate:

- `ETF_LONG_TERM_RESEARCH_EXPANDED` contains 40 symbols and is intended for
  batched historical or end-of-day research.
- `ETF_LONG_TERM_LIVE_30` contains exactly 30 symbols and fits the current
  Alpaca Basic simultaneous WebSocket subscription ceiling.
- A daily long-term engine does not need every symbol streamed tick by tick.
  It can use completed daily bars through REST and reserve streaming capacity
  for genuinely intraday strategies.

The free feed remains IEX rather than consolidated SIP, so a later live design
must not assume that its quotes or volume represent the full market.
