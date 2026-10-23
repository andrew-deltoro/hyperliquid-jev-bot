# Hyperliquid JEV Bot: Build and Verification

Version: 0.2.0. Prepared October 23, 2026. This adds a forward-only hypothetical strategy scoreboard and observed decision-path diagnostics; it is not a live execution release.

## Delivered

- **Python project:** Installable package and CLI, pinned dependency manifests, and a reproducible source handoff.
- **Public market ingestion:** Native/HIP-3 perpetual discovery, spot registry discovery, bounded active-market selection, and one-minute candle polling.
- **Regime-aware strategies:** Trend pullback, volatility breakout, mean reversion, and liquidity-sweep rules, with tight/standard/wide parameter profiles.
- **JEV adapter:** Typed strategy/profile and candidate choices, explicit WAIT handling, HTTP and total-call deadlines, enum validation, response provenance, and a persistent call-count cap.
- **Journal and recovery:** SQLite/WAL storage, duplicate-bar suppression, configuration hashes, route caching, dwell control, and one scanner per database.
- **Skylark integration:** Source-health checks and optional local reading-bundle ingestion, with freshness checks and unknown directional interpretation.
- **Deployment files:** Dockerfile, Compose, example environment variables, GitHub Actions, and operating instructions.
- **Scoreboard:** Four fixed-profile rule lanes and one JEV policy lane; future-only hypothetical entries, conservative ambiguous exits, explicit modeled costs, restart persistence, and visible unresolved paths.
- **WAIT breakdown:** Router/candidate stage separation, measured trigger conditions, fresh versus cached routing, and operational-limit/error categories without invented model explanations.

SQLite and HTTP polling remain intentional first-slice choices. The larger blueprint's PostgreSQL, streaming order books, execution service, web dashboard, and outcome-based allocator remain later components.

## Verification results

| Check | Result |
|---|---|
| Python runtime | Exercised on Python 3.12.15 |
| Automated suite | 118 tests passed |
| Ruff lint | Passed |
| Ruff formatting | Passed |
| Python compilation | Passed |
| Source distribution | Built successfully |
| Python wheel | Built successfully |
| Synthetic offline run | Completed all four scenarios |
| Shadow lifecycle demo | Four controlled synthetic tickets closed; fees, allowance, and ambiguous-exit labels validated |
| New CLI JSON reports | Scoreboard and WAIT outputs parsed and validated |
| Original v0.1 journal | Read successfully; legacy WAIT stages inferred only where recorded evidence supports them |
| Public Hyperliquid discovery | Completed with no discovery warnings in the v0.1 smoke test |
| Public BTC/ETH candle ingestion | Completed and produced validated feature snapshots in v0.1 |
| Missing JEV key behavior | Correctly journaled JEV_UNAVAILABLE; no fallback decision engine |
| Skylark source-health check | Returned ready during smoke test; no paid readings obtained |
| Authenticated JEV calls | Verified in v0.1 using the securely saved credential and model jev-1.13.0; no new paid API verification was required for v0.2 |
| Docker image build/runtime | Not run here: Docker executable unavailable |
| GitHub remote/Actions | User completed the v0.1 push; v0.2 has not been pushed by the assistant |
| VPS deployment | User reports v0.1 installed; v0.2 is not yet deployed or remotely inspected |
| Order submission or x402 payment | Not implemented; none performed |

The tests cover schema validation, invalid/stale/gapped data, each strategy direction, risk-preview arithmetic, mock JEV contracts, timeouts, malformed answers, redirect handling, missing credentials, call budgets, cached routes, route changes, dwell behavior, WAIT, expired decisions, journal persistence, and process locking.

New tests cover future-only shadow entries, same-bar rejection, long/short adverse slippage, fees and funding-allowance arithmetic, stop-first collisions, known gap ordering, time exits, cancelled invalid entries, missing paths, replay idempotency, open mark separation, experiment/source/instrument isolation, version-one database migration, rollback atomicity, watchlist retention, and independent baseline collection when JEV waits.

The v0.1 live smoke test verified connectivity and parsing, not trading performance. After the key was saved securely, the real JEV API passed the contract check and evaluated fresh BTC and ETH snapshots; both routing decisions returned `WAIT`.

A separately labeled synthetic breakout fixture also received a real JEV `WAIT` decision. Those runs verified authentication, model availability, request/response compatibility, and routing/journaling, but did not reach candidate-level selection because the router selected no strategy.

## Demonstration outcomes

The deterministic, synthetic breakout and trend scenarios produced `PROPOSAL_ONLY` records. The range scenario produced `NO_SETUP`, and the shock scenario produced `SHOCK_NO_ENTRY`. The new `scripts/shadow_demo.py` helper adds explicitly contrived future price paths for lifecycle verification, not market forecasts.

These offline outcomes validate control flow only. They are not JEV-generated decisions, historical observations, executed trades, or evidence of profitable strategies.

## Explicit boundaries

- **No execution:** There is no `/exchange` adapter, signer, private-account reader, or hidden live switch.
- **No profitability gate:** No backtest or walk-forward metric is required to activate an analysis strategy.
- **No claimed edge:** Initial regime thresholds and trigger rules are hypotheses.
- **No liquidity fiction:** Spread/depth are labeled unmeasured and cannot pass an execution check.
- **No real capital assumptions:** Sizing uses an explicitly hypothetical reference equity and never produces executable order approval.
- **No macro inference:** Skylark sign semantics are not guessed from raw readings.
- **No silent substitution:** Synthetic/demo results and live/JEV records use separate source identities.

## Next actions

Follow `UPGRADE_V0_2.md` to resolve Docker socket permissions, apply the patch to the user's existing repository, back up the journal, and rebuild the existing Compose deployment. Credentials, GitHub publication, and VPS access have not been modified by the assistant in this upgrade.

After those checks, the next development milestone is deeper market data and a separately authorized testnet execution module with reconciliation and protective orders. Live capital, leverage ceilings, loss limits, eligibility, and payment policies remain unresolved and are not authorized by this analysis build.
