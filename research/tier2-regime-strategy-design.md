# Regime-aware strategy research design

## Objective

Test whether a fixed combination of independently motivated strategies can improve net return and drawdown across observable market states. This is research-only. It does not alter live or paper-trading decisions.

The objective is not to discover the best strategy after labeling each historical period. A valid router must classify the next session using only information available at the preceding close, preserve every failed trial, and be evaluated chronologically.

## Evidence summary

- Medium-horizon momentum and trend have evidence across markets and asset classes, but momentum can crash during high-volatility rebounds following market declines.
- Scaling exposure down when realized volatility is high can improve risk-adjusted performance, although it may lag an uninterrupted equity advance.
- Value and momentum have historically diversified one another, but a proper value implementation requires valuation inputs that were available at the decision date.
- Quality has historically earned attractive risk-adjusted returns and can complement momentum, but stock-level testing requires point-in-time fundamentals and survivorship-safe membership.
- Factor premia vary through time, but broad attempts to time them with macroeconomic or market-state variables have generally produced weak results after lags and trading costs. A regime router is therefore a challenger, not an assumed improvement.

## Causal market states

The first router uses two lagged daily features:

1. Trend: SPY close above or below its trailing 200-session average.
2. Volatility: trailing 60-session annualized SPY volatility above or below 18%.

The state calculated after session *t* controls the target filled at the next available session open. The four labels are `BULL_LOW_VOL`, `BULL_HIGH_VOL`, `BEAR_LOW_VOL`, and `BEAR_HIGH_VOL`.

The 18% regime boundary is separate from the 12% portfolio volatility target. It is frozen for this generation and cannot be moved after results are viewed.

## First fixed router

- Bull, low volatility: hold the three strongest positively trending sectors equally.
- Bull, high volatility: hold those sectors with exposure scaled toward the 12% volatility target.
- Bear, low volatility: hold the qualifying sector sleeve at 50% and the defensive proxy at 50%.
- Bear, high volatility: hold the defensive proxy.

This design tests whether protection during persistent stress compensates for reduced participation and whipsaw. It does not select the historically best sleeve separately in each state.

## Candidate families and data gates

| Family | Expected role | Required data | Current status |
| --- | --- | --- | --- |
| Sector momentum | Primary return-seeking sleeve | Adjusted sector ETF daily bars | Active |
| Regime-routed sector momentum | Conditional risk control | Same bars; causal trend and volatility | Active shadow research |
| Factor ETF momentum | Diversify sector-specific momentum | Adjusted factor ETF bars and inception-aware cohorts | Next implementation |
| Industry ETF momentum | Finer economic leadership signals | Adjusted industry ETF bars and inception-aware cohorts | Next implementation |
| Value plus momentum | Diversifying return mechanisms | Point-in-time valuations | Blocked on data |
| Stock momentum plus quality | Higher-alpha cross-sectional candidate | Point-in-time fundamentals, constituents, and delistings | Blocked on data |
| Event underreaction | Independent event-driven sleeve | Timestamped earnings/news and executable prices | Blocked on data |
| Machine-learning allocator | Nonlinear sleeve combination | Validated sleeve returns and purged walk-forward folds | Deferred |

## Evaluation

Historical results through 2026-09-03 are development evidence. Newly designed candidates begin untouched forward confirmation on 2026-09-04.

Every historical run must report:

- SPY-relative return at 1, 5, 10, and 20 basis points;
- rolling three-year and expanding walk-forward results;
- calendar-year attribution;
- performance and sample size in each causal regime;
- turnover, trade count, volatility, maximum drawdown, and recovery time;
- paired block-bootstrap uncertainty;
- whether one year or one regime supplies most of the claimed advantage.

A regime router does not advance merely because it lowers drawdown. It must meet the predeclared account objective: higher net return for the return-seeking track, or a separately declared improvement in return per unit of drawdown for a defensive track.

## Sources

- Daniel and Moskowitz, Momentum Crashes: https://www.nber.org/papers/w20439
- Moreira and Muir, Volatility Managed Portfolios: https://www.nber.org/papers/w22208
- Asness, Moskowitz, and Pedersen, Value and Momentum Everywhere: https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere
- Asness, Frazzini, and Pedersen, Quality Minus Junk: https://conference.nber.org/confer/2013/APf13/Frazzini_Pedersen_Asness.pdf
- Ilmanen et al., How Do Factor Premia Vary Over Time?: https://www.aqr.com/Insights/Research/Journal-Article/How-Do-Factor-Premia-Vary-Over-Time-A-Century-of-Evidence
- Moskowitz, Ooi, and Pedersen, Time Series Momentum: https://fairmodel.econ.yale.edu/ec439/mosk.pdf
- Hurst, Ooi, and Pedersen, A Century of Evidence on Trend-Following Investing: https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing

