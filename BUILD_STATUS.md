# Hyperliquid JEV Bot: Build and Verification

Version: 0.1.0. Prepared October 22, 2026, Pacific time, with checks completing October 23 UTC. This is the first analysis-only implementation of the agreed design, not a live execution release.

## Delivered

- **Python project:** Installable package and CLI, pinned dependency manifests, and a reproducible source handoff.
- **Public market ingestion:** Native/HIP-3 perpetual discovery, spot registry discovery, bounded active-market selection, and one-minute candle polling.
- **Regime-aware strategies:** Trend pullback, volatility breakout, mean reversion, and liquidity-sweep rules, with tight/standard/wide parameter profiles.
- **JEV adapter:** Typed strategy/profile and candidate choices, explicit WAIT handling, HTTP and total-call deadlines, enum validation, response provenance, and a persistent call-count cap.
- **Journal and recovery:** SQLite/WAL storage, duplicate-bar suppression, configuration hashes, route caching, dwell control, and one scanner per database.
- **Skylark integration:** Source-health checks and optional local reading-bundle ingestion, with freshness checks and unknown directional interpretation.
- **Deployment files:** Dockerfile, Compose, example environment variables, GitHub Actions, and operating instructions.

SQLite and HTTP polling are intentional first-slice choices. The larger blueprint's PostgreSQL, streaming order books, execution service, dashboard, and outcome-based allocator remain later components.

## Verification results

| Check | Result |
|---|---|
| Python runtime | Exercised on Python 3.12.15 |
| Automated suite | 79 tests passed |
| Ruff lint | Passed |
| Ruff formatting | Passed |
| Python compilation | Passed |
| Source distribution | Built successfully |
| Python wheel | Built successfully |
| Synthetic offline run | Completed all four scenarios |
| Public Hyperliquid discovery | Completed with no discovery warnings in the smoke test |
| Public BTC/ETH candle ingestion | Completed and produced validated feature snapshots |
| Missing JEV key behavior | Correctly journaled JEV_UNAVAILABLE; no fallback decision engine |
| Skylark source-health check | Returned ready during smoke test; no paid readings obtained |
| Authenticated JEV calls | Not verified: API key not provided |
| Docker image build/runtime | Not run here: Docker executable unavailable |
| GitHub remote/Actions | Not connected or run remotely |
| VPS deployment | Not performed |
| Order submission or x402 payment | Not implemented; none performed |

The tests cover schema validation, invalid/stale/gapped data, each strategy direction, risk-preview arithmetic, mock JEV contracts, timeouts, malformed answers, redirect handling, missing credentials, call budgets, cached routes, route changes, dwell behavior, WAIT, expired decisions, journal persistence, and process locking.

The live smoke test verifies connectivity and parsing, not trading performance. Model behavior is tested with controlled HTTP responses until a real key is supplied.

## Demonstration outcomes

The deterministic, synthetic breakout and trend scenarios produced `PROPOSAL_ONLY` records. The range scenario produced `NO_SETUP`, and the shock scenario produced `SHOCK_NO_ENTRY`.

These outcomes validate control flow only. They are not JEV-generated decisions, historical observations, executed trades, or evidence of profitable strategies.

## Explicit boundaries

- **No execution:** There is no `/exchange` adapter, signer, private-account reader, or hidden live switch.
- **No profitability gate:** No backtest or walk-forward metric is required to activate an analysis strategy.
- **No claimed edge:** Initial regime thresholds and trigger rules are hypotheses.
- **No liquidity fiction:** Spread/depth are labeled unmeasured and cannot pass an execution check.
- **No real capital assumptions:** Sizing uses an explicitly hypothetical reference equity and never produces executable order approval.
- **No macro inference:** Skylark sign semantics are not guessed from raw readings.
- **No silent substitution:** Synthetic/demo results and live/JEV records use separate source identities.

## Next actions

Enter a TypeSafe API key through the secure credential form, then run the authenticated contract check and one bounded live analysis cycle. Confirm a private GitHub destination and VPS details before connecting or deploying.

After those checks, the next development milestone is deeper market data and a separately authorized testnet execution module with reconciliation and protective orders. Live capital, leverage ceilings, loss limits, eligibility, and payment policies remain unresolved and are not authorized by this analysis build.
