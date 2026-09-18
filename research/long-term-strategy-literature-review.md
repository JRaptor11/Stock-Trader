# Long-Term Strategy Literature Review and Candidate Map

Date: 2026-09-18

Status: research design only. This document does not promote a strategy, alter the frozen Generation 015 declaration, or authorize paper/live trading.

## Objective

Step back from tactical interrupts and study long-term portfolio engines first. The purpose is to create a broad but organized list of theoretically credible strategies, understand why each might work, identify the environments in which it should be strongest or weakest, and only then design independent historical tests.

“Long term” here includes strategies whose intended holding period is measured in years even when their signals or rebalances are weekly or monthly. Fast tactical trades, one-day breakouts, and a combined strategy router are deliberately outside this phase.

## Main conclusion

The literature does not identify one universally best long-term strategy. It supports a hierarchy:

1. A diversified passive portfolio is the hardest benchmark to beat reliably after costs.
2. Equity exposure offers the highest long-run return potential but carries deep and persistent drawdowns.
3. Value, momentum, profitability/quality, defensive/low-risk, carry, and time-series trend have substantial evidence across samples or asset classes.
4. These premia have distinct failure modes and can remain weak for years.
5. Combining genuinely different return sources is more defensible than selecting one recent winner.
6. Volatility scaling and trend filters are credible risk-management candidates, but their ability to improve terminal return is not guaranteed.
7. Macro labels are useful for attribution. They are not automatically reliable timing signals. Research over a century finds factor premia vary over time, yet profitable factor timing is difficult after information lags and transaction costs.

The next historical phase should therefore compare a deliberately small number of structurally different engines. It should measure their results in causal market states without assuming beforehand that a state-based switching rule will add value.

## Evidence principles for our testing

- Use total returns and point-in-time information.
- Include delisted securities or use investable ETFs to avoid survivorship bias.
- Separate discovery, locked holdout, and untouched forward evidence.
- Treat contiguous market-state sessions as dependent episodes.
- Report absolute return, excess return, volatility, drawdown, recovery time, downside capture, turnover, tax exposure, and capacity.
- Use multiple cost assumptions and avoid conclusions that exist only at zero cost.
- Compare every strategy with SPY, a global equity benchmark, and a simple balanced portfolio.
- Do not infer timing skill merely because a strategy performs differently across retrospectively labeled states.
- Prefer simple signal definitions that survive nearby parameter choices.
- Judge a long-term engine over full cycles, not by one favorable year.

## Long-term strategy families

### 1. Capitalization-weighted equity buy-and-hold

**Construction:** Hold a broad U.S. or global capitalization-weighted equity index; rebalance only when the index changes.

**Why it may work:** It captures aggregate economic growth and the equity risk premium with extremely low turnover, broad diversification, and minimal implementation assumptions.

**Best theoretical environment:** Sustained real growth, benign inflation, expanding earnings, improving liquidity, and long bull markets.

**Weak environment:** Recessions, valuation compression, inflation shocks, liquidity crises, and long secular stagnation. It offers no endogenous downside control.

**Advantages:** Highest simplicity, excellent scalability, low costs, tax efficiency, and low model risk.

**Limitations:** Severe drawdowns, concentration in the largest companies or dominant country, and long recovery periods.

**Research priority:** Mandatory benchmark, not merely a competitor.

### 2. Global equity diversification

**Construction:** Hold U.S., developed ex-U.S., and emerging-market equities at market-cap or fixed strategic weights.

**Why it may work:** Country cash-flow shocks remain imperfectly correlated. Long-horizon research finds global equity diversification can remain valuable even when short-run return correlations rise.

**Best theoretical environment:** Divergent regional growth, valuation mean reversion, U.S.-dollar weakness, and leadership broadening outside the United States.

**Weak environment:** Synchronized global recession, global deleveraging, and periods of persistent U.S. exceptionalism.

**Advantages:** Reduces single-country and valuation concentration; low turnover.

**Limitations:** Currency exposure, emerging-market governance risk, and potentially decade-long relative underperformance.

**Research priority:** High as a passive baseline and diversification control.

### 3. Strategic balanced portfolio

**Construction:** Fixed strategic weights in equities, nominal government bonds, inflation-sensitive assets, and cash; rebalance quarterly or annually.

**Why it may work:** Rebalancing sells relative winners and buys relative losers while combining assets exposed to different economic shocks.

**Best theoretical environment:** Normal business cycles, disinflationary recessions in which bonds hedge equities, and mean-reverting cross-asset leadership.

**Weak environment:** Inflation shocks that hurt stocks and nominal bonds simultaneously, or a sharp rise in cross-asset correlation.

**Advantages:** Stability, transparency, low turnover, and strong behavioral durability.

**Limitations:** Lower upside than an equity-heavy portfolio; stock/bond diversification is regime-dependent.

**Research priority:** High. Test 80/20, 70/30, 60/40, and a broader multi-asset version as distinct risk levels, not optimized weights.

### 4. Equal-weight or diversified-weight equity

**Construction:** Equal-weight stocks, sectors, or broad sleeves and rebalance periodically.

**Why it may work:** Reduces concentration and introduces systematic rebalancing and smaller-company exposure.

**Best theoretical environment:** Broad market participation, small/mid-cap strength, and mean reversion among constituents.

**Weak environment:** Narrow mega-cap leadership and periods when smaller, less liquid companies lag.

**Advantages:** Simple concentration control.

**Limitations:** More turnover, implicit size/value exposures, and greater implementation cost. It is not a pure diversification free lunch.

**Research priority:** Medium.

### 5. Value investing

**Construction:** Overweight assets or companies inexpensive relative to fundamentals, ideally with quality and liquidity controls; rebalance quarterly or annually.

**Why it may work:** Compensation for distress/cyclicality, behavioral overreaction, or both. Value evidence exists across markets and asset classes.

**Best theoretical environment:** Economic recovery, rising risk appetite, valuation-spread compression, and broad cyclical participation.

**Weak environment:** Disruption-driven growth booms, falling-rate duration rallies, deteriorating fundamentals, and “value traps.”

**Advantages:** Strong theoretical and empirical foundation; low-to-moderate rebalance frequency.

**Limitations:** Can underperform for many years; accounting signals arrive slowly; raw value can select distressed businesses.

**Research priority:** High, especially value combined with profitability/quality.

### 6. Profitability and quality

**Construction:** Favor profitable, financially strong companies with durable margins, conservative investment, stable earnings, and manageable leverage.

**Why it may work:** Gross profitability has predictive power comparable to book-to-market and complements value. Quality can avoid financially fragile firms.

**Best theoretical environment:** Late-cycle selectivity, uncertain growth, rising financing costs, and steady compounder-led markets.

**Weak environment:** Speculative rebounds, junk rallies, early-cycle surges in distressed firms, and periods when expensive quality valuations compress.

**Advantages:** Better fundamental durability and a natural defense against value traps.

**Limitations:** Definitions vary; quality can become expensive and overlap heavily with defensive or growth exposures.

**Research priority:** Very high as both a standalone engine and a value-quality blend.

### 7. Cross-sectional momentum

**Construction:** Rank equities, sectors, factors, countries, or asset classes by medium-term performance, commonly excluding the most recent month; hold leaders and rebalance monthly.

**Why it may work:** Information diffuses gradually, investors underreact, and institutional flows persist. Momentum evidence has continued beyond its original discovery sample.

**Best theoretical environment:** Persistent trends, gradual economic change, dispersed leadership, and stable volatility.

**Weak environment:** Abrupt reversals, especially rebounds following crashes; crowded unwinds; very high volatility.

**Advantages:** High return potential and complements value because their returns have historically been negatively correlated.

**Limitations:** Crash risk, turnover, taxes, and sensitivity to formation/skip/holding definitions.

**Research priority:** Very high at asset, sector, industry, and factor levels. Individual-stock implementation requires stricter point-in-time data.

### 8. Time-series momentum / diversified trend following

**Construction:** For each asset, take exposure in the direction of its own medium-term trend; diversify across equity indices, bonds, commodities, and currencies; commonly scale risk by volatility.

**Why it may work:** Persistent adjustment to information and behavioral/institutional herding. Research documents effects across many liquid markets and over a century of history.

**Best theoretical environment:** Sustained bull or bear trends, prolonged inflation shocks, and major crises after trends have formed.

**Weak environment:** Directionless, high-frequency whipsaw markets and abrupt V-shaped reversals.

**Advantages:** One of the strongest candidates for crisis diversification; works across asset classes rather than relying solely on equities.

**Limitations:** Can lag at turning points, suffer repeated small losses, and may require futures or imperfect ETF proxies.

**Research priority:** Very high. Test long-only/cash ETF implementations separately from long-short futures-style trend.

### 9. Absolute momentum / trend-filtered tactical allocation

**Construction:** Hold an asset only when its trailing return or price trend is positive; otherwise move to cash or defensive bonds. Evaluate monthly.

**Why it may work:** Attempts to retain sustained upside while avoiding prolonged downtrends.

**Best theoretical environment:** Long trends and slowly developing bear markets.

**Weak environment:** Sideways markets, rapid crashes followed by immediate rebounds, and frequent signal crossings.

**Advantages:** Simple, explainable, ETF-compatible, and usually lower turnover than short-horizon trading.

**Limitations:** Timing lag, whipsaw, cash benchmark sensitivity, and possible tax costs.

**Research priority:** High, but distinguish it carefully from cross-sectional momentum.

### 10. Dual momentum

**Construction:** Combine relative momentum (choose the strongest asset) with absolute momentum (require it to have a positive trend before holding it); rebalance monthly.

**Why it may work:** Combines persistent leadership with a defensive hurdle.

**Best theoretical environment:** Clear cross-asset leadership and sustained trends.

**Weak environment:** Rapid rotations, narrow noisy differences among assets, and V-shaped reversals.

**Advantages:** Compact ETF implementation and endogenous defense.

**Limitations:** Concentration, parameter sensitivity, and overlapping exposure to generic trend/momentum.

**Research priority:** High because it is implementable, but compare it with simpler trend and relative-momentum components to identify the true source of value.

### 11. Sector or industry relative momentum

**Construction:** Rank sectors or industries monthly and hold a diversified set of leaders, optionally with an absolute-trend hurdle.

**Why it may work:** Economic and earnings leadership rotates gradually while sector ETFs keep implementation manageable.

**Best theoretical environment:** Broad but differentiated expansions, persistent thematic leadership, and stable trends.

**Weak environment:** Sudden macro reversals, narrow single-stock leadership, and sector-level whipsaw.

**Advantages:** Middle ground between broad assets and individual stocks; feasible with liquid ETFs.

**Limitations:** Concentration, overlapping holdings, and a limited number of independent bets.

**Research priority:** Very high, given existing project evidence, but it must compete fairly with factor and cross-asset momentum.

### 12. Multi-factor equity

**Construction:** Blend value, profitability/quality, momentum, and defensive/low-risk scores using fixed weights; rebalance monthly or quarterly.

**Why it may work:** The factors have different economic and behavioral foundations and imperfect correlations. Value and momentum are historically complementary.

**Best theoretical environment:** Broad markets in which at least some factor premia are rewarded; long horizons spanning multiple style cycles.

**Weak environment:** Factor crowding, poorly designed composites, unintended sector bets, and synchronized factor drawdowns.

**Advantages:** Reduces dependence on correctly timing one factor.

**Limitations:** Greater design freedom creates overfitting risk; factor definitions may duplicate one another.

**Research priority:** Very high, but only after testing each component independently. Start with fixed transparent weights rather than optimized weights.

### 13. Low-volatility / minimum-variance equity

**Construction:** Favor lower-beta or lower-volatility stocks, or optimize an equity portfolio for lower forecast variance with constraints.

**Why it may work:** Leverage-constrained investors may overpay for high-beta securities, leaving safer securities with better risk-adjusted returns.

**Best theoretical environment:** Risk-off markets, slowing growth, moderate disinflation, and steady low-volatility compounding.

**Weak environment:** Sharp speculative rallies, interest-rate shocks affecting defensive sectors, and crowded low-volatility trades.

**Advantages:** Historically improved risk-adjusted equity returns and drawdown behavior.

**Limitations:** Can hide sector/rate exposure; minimum-variance optimization is estimation-sensitive; some published BAB results have been challenged as partly driven by construction and other factors.

**Research priority:** High as a stability engine, using simple constrained definitions.

### 14. Volatility-targeted exposure

**Construction:** Scale equity or factor exposure down when realized volatility rises and up when it falls, subject to leverage and exposure caps; update weekly or monthly.

**Why it may work:** Volatility changes are not necessarily matched by proportional changes in expected return. Published research finds improved Sharpe ratios across several factors.

**Best theoretical environment:** Persistent volatility regimes and crises where de-risking occurs before the full drawdown.

**Weak environment:** Sudden gap losses before scaling can react, V-shaped recoveries, and calm periods immediately preceding crashes.

**Advantages:** Direct risk control and comparable risk across strategies.

**Limitations:** Procyclical selling, rebound lag, leverage risk if uncapped, and dependence on the volatility estimator.

**Research priority:** Very high as an overlay tested separately on otherwise fixed engines. It should not be confused with return prediction.

### 15. Risk parity / equal risk contribution

**Construction:** Allocate capital so that stocks, bonds, commodities, and possibly inflation-linked bonds contribute comparable risk; often requires leverage to reach an equity-like return target.

**Why it may work:** Traditional balanced portfolios concentrate most risk in equities. Safer assets may offer better risk-adjusted returns when investors are leverage constrained.

**Best theoretical environment:** Diversified economic shocks, stable leverage/funding costs, and useful stock-bond diversification.

**Weak environment:** Simultaneous stock/bond losses, inflation shocks, rising real yields, deleveraging, and expensive financing.

**Advantages:** Strong diversification logic and smoother risk contribution.

**Limitations:** Leverage, model dependence, bond concentration, and vulnerability when correlations change.

**Research priority:** Medium-to-high. Test unlevered and capped-volatility versions separately.

### 16. Carry across asset classes

**Construction:** Favor assets with higher observable carry—such as yield, roll yield, or forward discount—across bonds, currencies, commodities, equity indices, or credit.

**Why it may work:** Carry predicts returns across multiple asset classes and is not fully explained by value or momentum.

**Best theoretical environment:** Stable growth, abundant liquidity, low volatility, and limited funding stress.

**Weak environment:** Global recessions, liquidity shocks, funding stress, and crash episodes.

**Advantages:** Distinct return source with broad evidence.

**Limitations:** Negative skew/crash risk in some implementations, leverage, derivatives, and complex ETF approximation.

**Research priority:** Medium for the present system; higher if reliable futures data and execution become available.

### 17. Commodity and inflation-sensitive sleeve

**Construction:** Maintain a strategic or trend-managed allocation to diversified commodity futures, gold, inflation-linked bonds, or resource equities.

**Why it may work:** Long-run commodity-futures evidence shows positive average returns, low stock/bond correlation, and strength during inflation cycles and backwardation.

**Best theoretical environment:** Rising or unexpectedly high inflation, supply shocks, dollar weakness, and commodity uptrends.

**Weak environment:** Disinflation, contango, oversupply, and strong real-dollar environments.

**Advantages:** Inflation diversification missing from stock/bond portfolios.

**Limitations:** Roll yield, tax structure, high volatility, and imperfect proxy behavior among commodity ETFs and resource stocks.

**Research priority:** High as a portfolio diversifier, not necessarily as the primary return engine.

### 18. Systematic rebalancing / contrarian allocation

**Construction:** Maintain strategic weights and rebalance on a calendar schedule or when allocations breach fixed bands.

**Why it may work:** Harvests mean reversion and controls drift without forecasting returns.

**Best theoretical environment:** Oscillating or mean-reverting relative asset performance.

**Weak environment:** Long one-directional trends where repeatedly selling the winner reduces return.

**Advantages:** Simple, disciplined, low model risk, and suitable for tax-aware implementation.

**Limitations:** “Rebalancing bonus” is not guaranteed and depends on return, volatility, correlation, and costs.

**Research priority:** High as an implementation policy shared by strategic portfolios.

### 19. Lifecycle / glide-path allocation

**Construction:** Change strategic risk as the investor’s horizon, labor income, and withdrawal needs evolve; rebalance annually.

**Why it may work:** Portfolio choice should reflect total household wealth and the ability to survive drawdowns, not return alone.

**Best theoretical environment:** Not state-specific; it is investor-specific risk management.

**Weak environment:** A rigid age-only glide path can ignore valuation, funded status, and unusual income risk.

**Advantages:** Aligns risk with real financial objectives.

**Limitations:** Not a return anomaly and not directly comparable with an alpha strategy.

**Research priority:** Relevant for eventual Roth/taxable portfolio design, but separate from strategy discovery.

## Theoretical market-condition map

This table states hypotheses to test, not conclusions to encode in a router.

| Market condition | Theoretically favored candidates | Candidates likely challenged | Reasoning |
|---|---|---|---|
| Sustained growth, low/normal inflation, broad bull trend | Buy-and-hold equity, momentum, sector/industry momentum, quality, multi-factor | Cash-heavy defense, over-hedged portfolios | Earnings growth and persistent leadership reward equity exposure |
| Accelerating speculative bull market | Momentum, broad equity, selected high-beta exposure | Low volatility, value, defensive allocation | Risk appetite and duration expansion can dominate fundamentals |
| Mature/decelerating bull market | Quality, value-quality, relative momentum with defensive hurdle, low volatility | Pure high-beta momentum | Selectivity and balance-sheet strength become more valuable |
| Gradual disinflationary slowdown | Quality, low volatility, government bonds, balanced allocation, volatility targeting | Cyclical value, commodities | Falling inflation can support duration while growth risk favors defense |
| Deflationary recession | High-quality government bonds, cash, diversified trend following, absolute-momentum defense | Equities, credit, commodities, carry | Growth collapse and deleveraging hurt risky assets; established downside trends help trend |
| Early-cycle recovery | Value, smaller companies, cyclical sectors, momentum after confirmation | Static defense, long-duration safety | Distressed/cyclical assets rebound as growth expectations recover |
| Rising growth and rising inflation | Equities with pricing power, value/cyclicals, commodities, trend | Long-duration bonds, expensive growth | Nominal growth supports earnings while inflation hurts duration |
| Stagflation / inflation shock | Commodities, inflation-linked bonds, trend following, cash/short duration | Conventional stock/bond balance, long-duration assets | Stocks and nominal bonds may fall together; real assets diversify |
| Stable low-volatility range | Carry, quality, strategic rebalancing, low volatility | Trend following | Carry accrues and mean reversion dominates; trend signals whipsaw |
| Persistent high-volatility trend | Trend following, volatility-managed exposure, defensive quality | Carry, unscaled momentum, static high equity | Trends can persist but risk must be scaled |
| Abrupt V-shaped reversal | Buy-and-hold, strategic rebalancing | Trend filters, volatility targeting, slow momentum | Defensive systems exit late and re-enter late |
| Narrow mega-cap leadership | Cap-weighted index, quality/growth if leaders qualify | Equal weight, small/value, broad sector rotation | Concentration mechanically benefits cap weighting |
| Broadening participation | Equal weight, value, sector/industry momentum, smaller companies | Narrow cap-weighted concentration | More securities contribute to returns |
| Liquidity/funding stress | Cash, Treasuries when inflation is contained, defensive quality, established trend | Carry, leverage, crowded factors, risk parity | Funding constraints and correlation spikes damage leveraged premia |

## Strategy shortlist for the next historical phase

The vast list above should be preserved, but the first long-term comparison should use a smaller orthogonal set:

1. U.S. equity buy-and-hold.
2. Global equity buy-and-hold.
3. Fixed strategic multi-asset portfolio.
4. Cross-asset relative momentum with a defensive hurdle.
5. Cross-asset dual momentum.
6. Sector/industry relative momentum.
7. Time-series trend following across liquid ETF sleeves.
8. Value-quality equity.
9. Fixed-weight value/momentum/quality multi-factor equity.
10. Simple constrained low-volatility equity.
11. Volatility-targeted broad equity.
12. Unlevered equal-risk or capped-risk multi-asset allocation.
13. Strategic inflation-sensitive sleeve.

This selection spans passive beta, diversification, relative trend, absolute trend, fundamental factors, defensive equity, and explicit risk scaling. Carry should remain on the roadmap but may require instruments and data beyond the cleanest current ETF implementation.

## Weekly, monthly, and annual perspectives

### Weekly decisions

Best suited to volatility estimation, exposure scaling, and risk monitoring. Weekly strategy changes can react faster, but create more turnover and false signals. They should be tested principally as risk overlays, not assumed to improve return forecasts.

### Monthly decisions

The natural starting frequency for cross-sectional momentum, time-series trend, sector rotation, factor allocation, and absolute momentum. It balances responsiveness with implementability and is common in the supporting literature.

### Quarterly or annual decisions

Best suited to strategic asset allocation, global diversification, value, quality/profitability, rebalancing, and lifecycle decisions. Slower updates reduce costs, noise, taxes, and the temptation to overfit short-term fluctuations.

## What not to do yet

- Do not build the combined state router.
- Do not choose state thresholds by maximizing historical return.
- Do not discard a long-term engine because it loses in the aggregate before examining risk and state behavior.
- Do not promote a strategy merely because it wins one historical crisis.
- Do not mix one-day breakout evidence into this phase.
- Do not optimize dozens of lookbacks and portfolio weights simultaneously.
- Do not treat overlapping defensive variants as independent strategies.

## Proposed research sequence

1. Freeze simple canonical definitions for the 13 shortlisted engines.
2. Audit data availability, ETF inception bias, point-in-time membership, dividends, and delistings.
3. Run each strategy independently over the same timeline and cost ladder.
4. Report full-period, rolling five-year, fixed-era, and walk-forward results.
5. Attribute performance to causal growth/trend, inflation, volatility, breadth, liquidity, and correlation states.
6. Require recurrence across independent episodes and eras.
7. Test nearby parameter values only for robustness, not selection.
8. Create a shortlist of baseline engines based on return, stability, drawdown, and evidence quality.
9. Only after that phase, reconsider whether a state-aware combination has enough causal predictive evidence to test.

## Primary research sources

- Hurst, Ooi, and Pedersen, “A Century of Evidence on Trend-Following Investing”: https://www.aqr.com/insights/research/journal-article/a-century-of-evidence-on-trend-following-investing
- Moskowitz, Ooi, and Pedersen, “Time Series Momentum”: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2089463
- Jegadeesh and Titman, “Profitability of Momentum Strategies”: https://www.nber.org/papers/w7159
- Asness, Moskowitz, and Pedersen, “Value and Momentum Everywhere”: https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere
- Novy-Marx, “The Other Side of Value: The Gross Profitability Premium”: https://www.nber.org/papers/w15940
- Frazzini and Pedersen, “Betting Against Beta”: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2049939
- Novy-Marx and Velikov, “Betting Against Betting Against Beta”: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3300965
- Moreira and Muir, “Volatility Managed Portfolios”: https://www.nber.org/papers/w22208
- Asness, Frazzini, and Pedersen, “Leverage Aversion and Risk Parity”: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1990493
- Koijen, Moskowitz, Pedersen, and Vrugt, “Carry”: https://www.nber.org/papers/w19325
- Levine, Ooi, and Richardson, “Commodities for the Long Run”: https://www.nber.org/papers/w22793
- Viceira and Wang, “Global Portfolio Diversification for Long-Horizon Investors”: https://www.nber.org/papers/w24646
- Ilmanen, Israel, Lee, Moskowitz, and Thapar, “How Do Factor Premia Vary Over Time?”: https://www.aqr.com/insights/research/journal-article/how-do-factor-premia-vary-over-time-a-century-of-evidence
- Faber, “A Quantitative Approach to Tactical Asset Allocation”: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461
- Novy-Marx, “Pseudo-Predictability in Conditional Asset Pricing Tests”: https://www.nber.org/papers/w18063

## Interpretation warning

Much of the literature contains simulated or backtested results, and publication itself can reduce future performance. A century-scale study of factor premia estimates that out-of-sample premia are approximately 30% lower, with overfitting a likely contributor. These sources justify candidates for testing; they do not establish what this program will earn.
