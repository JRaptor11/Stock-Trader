# Research Pause Checkpoint — 2026-09-18

This checkpoint preserves the exact place at which tactical/state-routing research was paused so the work can be resumed later without reconstructing decisions from memory.

## Repository state

- Branch: `research-engine`
- Committed HEAD: `8a05483` (`Add frozen Generation 015 forward validation`)
- The following deployment-health improvements remain intentionally uncommitted:
  - `README.md`
  - `research/app.py`
  - `tests/test_research_app.py`
- No historical strategy, Generation 014 output, or Generation 015 declaration was removed or rewritten.

## Generation 014

All three transition/episode-validation jobs completed successfully on 2026-09-17:

- `transition-episode-validation-generation-014-baseline-history-2026-09-17`
- `transition-episode-validation-generation-014-defensive-history-2026-09-17`
- `transition-episode-validation-generation-014-tactical-history-2026-09-17`

Durable archives were downloaded to:

`C:\Users\joshu\OneDrive\Documents\Stock Trader Codex\research-results\generation-014`

The detailed analysis is:

`C:\Users\joshu\OneDrive\Documents\Stock Trader Codex\research-results\generation-014\ANALYSIS.md`

Archive SHA-256 values:

- Baseline: `3D3EB9E3396CD8903A1F898520D8363E48189E59EFC29FBE5B0D94B930D44C4A`
- Defensive: `C09CDA16F52B4F096CA76D28BEA08A4E8C1C86A574E0A34EFFBDC308920AD979`
- Tactical: `BE847E1170BD0951406515FDFDEAB5D213A64A1330D1AE35EF25CC9293FE6745`

## Generation 015

- Declaration: `research/frozen-forward-validation-generation-015.json`
- Forward start: 2026-09-18
- Status: frozen research declaration; no strategy promotion and no router.
- The declaration and its hypotheses remain unchanged.
- Forward evidence collection is paused while long-term strategy research is reassessed.

## Resume conditions

When this work is resumed:

1. Do not retune Generation 015 from information observed after its declaration.
2. Preserve the three-session causal state definition.
3. Keep historical discovery, locked holdout, and forward evidence separate.
4. Do not count multiple signals inside one state episode as independent confirmation.
5. Do not construct a combined router until isolated long-term engines and their conditional behavior have been adequately validated.

## Current focus

The active research focus is now **Long-Term Engine Study 001**, a separate
research line covering canonical long-term strategy families. Tactical
breakouts, defensive interrupts, and the combined state-aware router are
deferred—not discarded. Generation 015 remains frozen and paused; this study
does not replace or mutate it.
