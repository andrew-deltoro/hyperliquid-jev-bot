# Hyperliquid JEV Adaptive Trading Bot

## Design brief

Design version: 0.1, October 22, 2026. Status: implementation blueprint, not a deployed or trading system.

Build a Python-first trading system for a Linux VPS that discovers Hyperliquid markets, identifies changing market conditions, selects suitable strategy configurations, and uses JEV to evaluate trade candidates. Preserve the supplied workflow: market data, strategy rules, JEV check, risk gate, execution. Add a regime and strategy-routing layer before candidate generation, plus a feedback loop after execution.

The central product requirement is adaptation, not unrestricted AI control. JEV should help choose among explicit strategies and parameter profiles; application code should own arithmetic, account permissions, order placement, and nonnegotiable operating limits.

Backtesting, walk-forward analysis, Sharpe thresholds, statistical significance, and “approved edge statements” are not prerequisites for enabling a strategy. Strategies may be deployed as unproven hypotheses after functional checks and explicit live-trading authorization. This removes the research bottleneck without claiming that an untested strategy is profitable.

## Architecture and responsibilities

```text
Hyperliquid market registry + streaming market/account data
                         |
                  Data integrity checks
                         |
              Numeric features by market and horizon
                         |
Skylark weekly data ---> Regime state and uncertainty
                         |
              JEV strategy/profile selection
                         |
              Deterministic strategy triggers
                         |
                 Candidate trade proposals
                         |
           JEV candidate selection: enter / wait
                         |
              Portfolio allocator + hard risk gate
                         |
               Single execution authority
                         |
           Hyperliquid orders, fills and protection
                         |
           Reconciliation + journal + attribution
                         |
        Bounded online adaptation and monitoring
```

- **Python engine:** Own data ingestion, features, strategy rules, portfolio accounting, risk, execution, and reconciliation.
- **JEV decision service:** Choose strategy profiles, assess supplied context, and select candidates from an enumerated set. Never receive signing keys or raw order execution tools.
- **Skylark adapter:** Supply timestamped macro-liquidity observations. Never interpret stale or missing data as neutral observations.
- **Execution service:** Be the only process permitted to sign and submit trading actions.
- **Monitoring service:** Explain selections, skips, risk rejections, exposure, and operating health without being required for protective exits.

JEV’s official documentation describes structured Choice, Score, and Noul outputs rather than code generation or conversational reasoning; the application must implement actions around those outputs ([TypeSafe introduction](https://docs.typesafe.ai/introduction), [coding-agent guidance](https://docs.typesafe.ai/introduction/coding-agents)).

## Recommended implementation stack

Use Python 3.12 as the initial project target, subject to dependency compatibility checks. Use TypeScript only if a richer dashboard becomes worthwhile; do not introduce Rust or Go until profiling identifies a genuine bottleneck.

| Component | Proposed implementation | Reason |
|---|---|---|
| Trading engine | Python, asyncio | One language for data, strategy logic, and adapters |
| Exchange adapter | Hyperliquid Python SDK behind a local interface | Keep exchange-specific signing and mapping isolated |
| JEV adapter | TypeSafe Python SDK or async HTTPX | Typed requests, explicit deadlines, auditable responses |
| Validation | Pydantic schemas; Decimal for order quantities/prices | Reject malformed decisions and avoid floating-point order rounding |
| Features | NumPy and Polars | Vectorized calculations outside the model |
| Durable state | PostgreSQL | Orders, fills, positions, decision logs, configuration history |
| Historical feature storage | Partitioned Parquet | Optional analysis without inflating the transaction database |
| Internal control API | FastAPI on a private interface | Pause, inspect, and approve configuration changes |
| Dashboard | Small read-only dashboard initially | Avoid coupling trading reliability to frontend complexity |
| Deployment | Docker Compose on a Linux VPS | Reproducible engine, database, and monitoring services |
| Engineering checks | pytest, Ruff, static typing, secret scanning | Verify mechanics, not historical profitability |

Hyperliquid links its Python SDK from the official API documentation, and TypeSafe provides a Python SDK named `typesafe-sdk` ([Hyperliquid API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api), [TypeSafe quick start](https://docs.typesafe.ai/introduction/quickstart)).

A proposed starting VPS size is 4 vCPU, 8 GB RAM, and 80 GB SSD, with retention caps and a bounded active streaming set. This is a planning allowance, not a measured capacity guarantee. JEV inference would run through its API, so this design does not require a local GPU. Benchmark the actual feed volume before increasing the active universe.

## Market universe: discover broadly, execute selectively

The goal is not a hardcoded BTC/ETH bot. Maintain a registry of every discovered market and a capability matrix stating which operations the bot can safely implement.

Hyperliquid documents native perpetual and DEX-specific discovery using `perpDexs`, `meta`, and `metaAndAssetCtxs`; its spot documentation provides `spotMeta` and `spotMetaAndAssetCtxs` ([perpetual metadata](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals), [spot metadata](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot)).

- **Native perpetuals:** Support directional long/short strategies with configurable leverage.
- **HIP-3 perpetuals:** Discover each DEX, retain namespaced identities, and load its market-specific margin, collateral, oracle, and session characteristics before trading.
- **Spot:** Support inventory-funded buys and sells through a separate capability path. Do not silently apply perpetual leverage logic or assume short borrowing is available.
- **Other or future products:** Discover where supported, but label unsupported order/settlement models explicitly. “Discovered” must not falsely imply “implemented.”

A market’s metadata can specify leverage limits, margin tiers, delisting status, and restricted margin modes; those fields must be checked at runtime rather than inferred from a ticker ([Hyperliquid perpetual metadata](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)).

Store an instrument identity containing venue, market kind, DEX, symbol, exchange asset ID, base/quote or collateral assets, size precision, price rules, lifecycle state, and metadata version. Resolve asset IDs through current metadata and the pinned SDK rather than copying display names or hardcoding an indexing formula.

Use a two-stage scanner:

1. Refresh the broad registry and coarse market summaries.
2. Rank eligible markets for deeper streaming using liquidity, spread, volatility, activity, and strategy relevance.

Always reserve streaming and reconciliation capacity for existing positions, regardless of rank. Use a bounded active set for deeper books and trades, with controlled rotation rather than subscribing to every channel for every instrument.

Hyperliquid imposes REST, WebSocket, subscription, and address-action limits, so whole-universe coverage requires request budgeting and selective subscriptions ([rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)).

## Adaptive regime engine

### Represent conditions as multiple dimensions

Avoid forcing the entire exchange into one daily label. Estimate a global backdrop, then an instrument-specific state for each trading horizon.

| Dimension | Proposed inputs | Output |
|---|---|---|
| Trend | Normalized slope, directional persistence, moving-average alignment, efficiency ratio | Up / down / range / mixed |
| Volatility | Realized volatility, ATR, rolling percentiles, expansion rate | Compressed / normal / expanding / extreme |
| Liquidity | Spread, depth, estimated size-dependent impact, quote freshness | Healthy / thin / dislocated |
| Positioning | Available funding, open-interest changes, price/OI interaction | Balanced / crowded / unstable |
| Market structure | Breakouts, failed breaks, reclaim of prior ranges, volume confirmation | Continuation / rejection / unresolved |
| Macro backdrop | Skylark observation levels and changes once semantics are documented | Supportive / restrictive / mixed / unavailable |
| Session context | Explicit UTC/session calendar and underlying reference-market hours | Session-aware context |

These are proposed features, not claims that they predict returns. The first release should use transparent calculations and rules, with JEV interpreting a compact structured summary rather than receiving thousands of raw candles.

Normalize features per instrument and horizon using trailing observations available at that time. Track both `observed_at` and `received_at`; do not use revised or later-published information as if it was already known.

### How adaptation works

- **Strategy-family selection:** Rank which strategy families fit the current regime. A trending asset and a ranging asset may use different strategies simultaneously.
- **Parameter selection:** Choose an approved profile such as faster/slower breakout confirmation, wider/narrower volatility-scaled stop, or shorter/longer holding window.
- **Capital allocation:** Adjust relative allocation within fixed portfolio limits. Leverage ceilings remain outside JEV control.
- **Exit management:** Tighten, trail, reduce, or close according to explicit rules when the trade’s original premise breaks.
- **Feedback:** Update a regime-by-strategy scorecard from live outcomes, fill quality, and execution cost. Treat early results as uncertain, not as proof of an edge.

Proposed initial cadence: features on completed one-minute bars, strategy routing on completed five-minute bars or a material regime change, and candidate evaluation when an actual trigger occurs. Protective order management should respond to fills and account events independently of those schedules.

### Prevent unstable strategy switching

Use hysteresis and a minimum dwell period: require the proposed replacement profile to remain preferred across multiple completed observations, unless a deterministic shock condition forces immediate risk reduction. Keep an explicit `unknown` regime for conflicting evidence, insufficient history, or bad data.

Pin the strategy version and entry profile to each open trade. A regime change may cancel unfilled entries and affect new trades, but it must not automatically widen an existing stop, reverse exposure, or increase size to rescue a losing position.

## Initial strategy library

The table below is a proposed routing policy, not a ranking of proven profitable strategies. “Preferred” means best fit under the configured rules and available observations, not the strategy that will necessarily earn the most.

| Observed regime | Candidate strategy | Trigger and invalidation | Adaptive controls |
|---|---|---|---|
| Persistent trend, adequate liquidity | Trend pullback continuation | Pullback followed by trend resumption; invalidate beyond a defined swing/volatility boundary | Pullback depth, confirmation, stop width, trailing exit |
| Compression followed by expansion | Volatility breakout | Break of a defined consolidation with activity confirmation; invalidate on failed continuation | Lookback, breakout buffer, confirmation, time stop |
| Stable range without strong directional persistence | Range or VWAP mean reversion | Excursion followed by re-entry/rejection; invalidate beyond the range premise | Deviation threshold, entry confirmation, target, duration |
| Prior high/low sweep with reclaim | Liquidity-sweep reversal | Trade through a prior range boundary and close/reclaim inside; invalidate beyond the sweep | Session definition, reclaim strength, stop buffer |
| Defined session opening with expansion | Session opening-range breakout | Break of the configured opening range; invalidate on failed break or time expiry | Session/time zone, range duration, breakout confirmation |
| Verified catalyst with suitable instrument mapping | Event-driven strategy, later release | Structured event plus market confirmation; invalidate on predefined response failure | Source quality, event TTL, eligible market set |
| Extreme dislocation or unusable data | Defensive policy | No new exposure; reconcile, reduce, or exit according to standing rules | Exposure reduction and cooldown |

For crypto instruments, define session windows deliberately rather than inventing a single universal market opening. For instruments tied to other underlying markets, model their relevant reference sessions, holidays, and stale-oracle conditions.

Use the video’s ORB, news, and liquidity-sweep examples as architectural inspiration rather than transferable performance evidence. The video explicitly describes stock/futures examples, backtests, and paper trading, not verified live Hyperliquid results ([ThinkTrade tutorial](https://www.youtube.com/watch?v=87q_P6iq9mk)).

Start with breakout, trend pullback, mean reversion, and liquidity-sweep modules. Keep news/filings optional until trustworthy event feeds and instrument mappings exist; a stock filing should not be treated as a signal for an unrelated crypto asset.

## JEV integration contract

The primary integration should use TypeSafe’s documented `POST https://api.typesafe.ai/v1/systemone` endpoint with Bearer authentication and the fields `model`, `state`, and `questions` ([TypeSafe API reference](https://docs.typesafe.ai/api/system-one)).

Pin an available model version after an authenticated compatibility check rather than letting a moving alias silently change trading behavior. Store the returned model ID, prompt/rubric version, configuration hash, snapshot ID, and answer with every decision.

### Separate routing from candidate selection

**Router call:** Provide numerical regime features, data quality, macro freshness, available strategy profiles, and portfolio context. Ask JEV to choose one allowed strategy/profile or `WAIT`, and optionally classify supporting conditions.

**Candidate call:** After deterministic strategy rules generate valid setups, provide the exact candidates, their IDs, allowed directions, entry/invalidation levels, time-to-live, and cost estimates. Ask JEV to select a candidate ID or `WAIT`; it cannot invent a new symbol, direction, size, or strategy.

TypeSafe evaluates questions in parallel and in isolation against the supplied state, so a later question in one request must not depend on an earlier answer. Dependent routing and candidate evaluation therefore need separate calls, or an application-built joint candidate set ([TypeSafe introduction](https://docs.typesafe.ai/introduction)).

Illustrative request shape, not a current market observation:

```json
{
  "model": "<pinned-supported-model>",
  "state": {
    "snapshot_id": "example-0001",
    "instrument_id": "example-perp",
    "regime": {
      "trend": "up",
      "volatility": "expanding",
      "liquidity": "healthy"
    },
    "macro": {
      "status": "unavailable",
      "reason": "paid-reading-not-retrieved"
    },
    "candidates": [
      {
        "id": "trend_pullback_long",
        "trigger_valid": true,
        "profile": "standard",
        "risk_precheck": "passed"
      }
    ]
  },
  "questions": {
    "candidate": {
      "type": "choice",
      "instructions": "Select a supplied candidate only when its setup fits the observed context. Otherwise select WAIT. Missing macro data is unknown, not supportive.",
      "criteria": {
        "trend_pullback_long": "The supplied valid pullback setup is consistent with the observed trend and liquidity.",
        "WAIT": "The supplied context does not support entering the candidate now."
      }
    }
  }
}
```

The enclosing application should preserve timestamps and enforce the decision’s time-to-live. Revalidate current price, spread, position state, and risk reservations immediately before order submission.

- **Confidence interpretation:** JEV’s confidence or answer probability is not an empirically established probability that a trade makes money. Never feed it directly into a Kelly formula or turn it into unlimited leverage.
- **Explainability:** Build dashboard explanations from recorded features, selected choices, and fixed reason codes. Do not fabricate free-text reasoning that JEV did not return.
- **Timeouts and invalid output:** Default to no new entries, while the independent position manager continues protecting existing exposure.
- **Privacy:** Send only the necessary feature and portfolio summary. Keep account identifiers where unnecessary, private keys, secrets, and raw confidential records out of model state.
- **Untrusted inputs:** Normalize and label news or third-party text as data. Never permit such content to alter tool permissions, endpoint allowlists, or risk settings.

## Skylark integration and verification

Skylark documents FLP, FLI, and GFC as weekly readings and exposes current readings, a current bundle, and historical products. Use those observations as slower macro context rather than pretending that repeated requests create fresh intraday information ([Skylark discovery](https://skylark-liquidity-api-portal.vercel.app/discover), [OpenAPI specification](https://skylark-liquidity-api-portal.vercel.app/openapi.json)).

Read-only checks during preparation returned `status: ready`, `as_of: 2026-10-18`, and `complete_weeks: 835` from the health endpoint; an unpaid bundle request returned HTTP 402 with Base-mainnet payment requirements. These checks establish reported source readiness and a payment challenge, not successful paid delivery or the actual indicator values ([source health](https://api.skylarkdigitalassets.com/source-health), [bundle endpoint](https://api.skylarkdigitalassets.com/api/v1/bundle/current)).

The provider’s own discovery page says complete Base-mainnet paid settlement and delivery have not yet been independently accepted. Keep the adapter’s commercial-delivery status unverified until an explicitly authorized purchase actually succeeds ([Skylark discovery](https://skylark-liquidity-api-portal.vercel.app/discover)).

### Adapter behavior

1. Check source health and compare the observation date with the cached version.
2. Inspect unsigned HTTP 402 requirements only when a new reading is needed.
3. Validate the exact network, asset contract, recipient, amount, expiry, and resource against a separately approved payment policy.
4. Request authorization before any payment unless the user has approved a specific recurring spending policy.
5. Use a dedicated, narrowly funded data-payment wallet, never the trading signer.
6. Retrieve and validate the paid response, cache it by observation date, and avoid paying repeatedly for the same reading.
7. Store the original payload, observed date, retrieval time, and purchase receipt separately.
8. Surface missing, stale, partial, or unavailable data explicitly.

Skylark specifies Base mainnet USDC/x402 and instructs clients to verify `PAYMENT-REQUIRED` before payment; its public outputs are indicator names, values, and observation dates rather than proprietary formulas or constituent datasets ([Skylark machine-readable guide](https://skylark-liquidity-api-portal.vercel.app/llms.txt)).

Before using FLP/FLI/GFC directionally, obtain documented units, sign interpretation, publication cadence, revision policy, and guidance on comparing readings over time. Do not guess that a higher raw reading is bullish, or infer the proprietary calculation. Historical observations used to normalize a feature are not a backtest approval gate.

Default degradation policy: disable only strategies that explicitly require Skylark when the feed is unavailable; let independent market-data strategies remain eligible. Allow the user to select a stricter macro-required policy later.

No payment was signed or authorized during this design work. No paid indicator value was retrieved.

## Risk, leverage, and portfolio allocation

Support leverage as requested, but treat position risk and selected exchange leverage as different variables. Calculate the intended exposure from loss-at-invalidation first, then verify that margin and liquidation constraints permit it.

For a linear quote-currency perpetual, an illustrative sizing calculation is:

\[
q_{\text{risk}} =
\frac{E \times r}
{|P_{\text{entry}}-P_{\text{stop}}| + C_{\text{unit}}}
\]

Here \(E\) is eligible strategy equity, \(r\) is the approved fraction at risk, and \(C_{\text{unit}}\) estimates adverse execution and fees per unit. Actual quantity is the minimum of this result and liquidity, margin, concentration, and portfolio limits, rounded down to exchange precision. Reject a zero or invalid stop distance; do not size using the formula for an instrument with a different payoff model.

Effective exposure is:

\[
L_{\text{effective}} =
\frac{\text{gross notional exposure}}{\text{eligible account equity}}
\]

Set the per-market leverage no higher than the user-approved ceiling and the applicable exchange margin tier permits. Prefer isolated margin for the initial directional design where supported; treat cross margin and portfolio-wide collateral behavior as explicit additional configuration.

Hyperliquid’s exchange endpoint supports integer leverage updates subject to market constraints, and the metadata includes margin tiers and margin-mode information ([exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint), [perpetual metadata](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)).

Before live activation, the user must choose:

- **Capital scope:** Dedicated account or subaccount, funded amount, and capital allocated to the bot.
- **Risk per position:** Maximum intended loss at the initial stop, recognizing gaps can exceed it.
- **Portfolio limits:** Gross/net exposure, correlated exposure, concurrent positions, and pending-order reservations.
- **Leverage ceiling:** Account-level ceiling plus optional stricter instrument caps.
- **Loss limits:** Defined daily-loss and drawdown behavior, including treatment of unrealized P&L, fees, funding, deposits, withdrawals, and reset time.
- **Execution limits:** Maximum spread, estimated price impact, and acceptable deviation from the candidate’s entry price.
- **Liquidation buffer:** Minimum distance between stop and liquidation conditions, using current margin state rather than a simplistic leverage-only approximation.

No universal “safe leverage” is assumed. Leveraged losses, adverse fills, exchange disruptions, and liquidation can exceed the loss estimated at a stop.

Reserve risk atomically before sending orders so simultaneous candidates cannot each spend the same available budget. Aggregate correlated positions and net conflicting strategies before execution; a collection of individually small trades must not silently become one large directional bet.

Do not automatically increase leverage after losses, average down without an explicit bounded strategy, or let JEV change hard limits.

## Execution and recovery

### Order lifecycle

Persist an intent before submitting it. Use a state machine such as:

```text
PROPOSED -> RESERVED -> SUBMITTING -> ACKNOWLEDGED
                           |              |
                        UNKNOWN       PARTIAL / FILLED
                           |              |
                       RECONCILE      PROTECTED -> EXITED
```

The real implementation must also represent rejected, canceled, expired, and protection-failed states. A timeout is an unknown outcome, not evidence that an order failed.

- **Client IDs:** Allocate stable client order IDs for intents and reconcile them before retrying ambiguous submissions.
- **Nonces:** Use one signing authority with an atomic nonce source and synchronized time.
- **Fill-based protection:** Protect actual filled quantity, including partial fills, with reduce-only protective orders where supported.
- **Slippage controls:** Use capped aggressive limit/IOC execution when appropriate; do not equate “market order” with an assured price.
- **Protection failure:** Halt new entries, attempt a bounded reduce-only flatten if account state and venue access permit, then alert. If the venue is unavailable, report unresolved exposure rather than claiming safety.
- **Restart recovery:** Rebuild state from exchange positions, fills, and open orders before enabling entries. Detect orphan positions, missing stops, and uncertain submissions.
- **Manual intervention:** Reconcile manual trades and external deposits/withdrawals rather than assuming the bot has exclusive control of all account changes.

Hyperliquid documents client order IDs, reduce-only flags, trigger orders, and order grouping, but grouping behavior and partial-fill protection must still be verified in integration tests before relying on them ([exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)).

API wallets sign on behalf of accounts; account queries must use the actual master/subaccount address rather than the agent signer address. Nonces are tracked per signer, and the documentation recommends separate signers for independent trading processes ([nonces and API wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)).

### Do not confuse cancellation with protection

Hyperliquid’s scheduled cancel action cancels all open orders when triggered; it does not flatten positions. Consequently, it can also remove protective orders, so it must not be used as a naive account-wide safety switch while positions rely on resting stops ([scheduled cancel documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)).

Separate three operator commands:

- **Pause entries:** Stop new exposure, cancel pending entry intents, preserve valid exits and stops.
- **Emergency reduce:** Reduce or flatten verified positions according to a preauthorized emergency policy.
- **Full halt:** Stop execution after reconciliation or explicit operator action, with unresolved positions clearly displayed.

An external watchdog should monitor engine heartbeat and independently report outages. It should not become a second uncontrolled signer or cancel every order indiscriminately.

## Online learning without approval paralysis

Record outcomes by strategy version, instrument, regime at entry, and parameter profile. Include realized results net of fees and funding, slippage, maximum adverse/favorable excursion, holding time, and model/API cost attribution where possible.

Use an exponentially weighted performance score or a contextual-bandit allocator only within a bounded allocation envelope. Seed new strategies with small explicit allocations rather than excluding them because they lack historical proof.

- **Cold start:** Choose a reasonable default profile from the routing table and mark it unproven.
- **Exploration:** Permit only a user-approved portion of the already bounded risk budget, never extra leverage on top.
- **Noise control:** Limit how much a strategy’s weight can change at one update; avoid promoting a strategy solely on one or two wins.
- **Nonstationarity:** Decay old observations and distinguish current regime performance from global lifetime performance.
- **Counterfactual honesty:** Label paper or shadow candidates as simulations, not fills. Do not attribute hypothetical fills or profits to rejected trades as if they occurred.
- **Rollback:** Keep versioned parameters and make previous profiles recoverable.

Observed losses can trigger the user’s loss budget, but an absent backtest report or failed historical significance test cannot silently disable all strategy modules. Any skip must have a concrete reason visible to the operator.

## Software checks that remain mandatory

Operational correctness is different from an edge approval process. Keep these checks even when no historical performance testing is requested.

- **Order math:** Precision, minimum size/notional, fees, margin tiers, risk reservations, and reduce-only behavior.
- **JEV contract:** Invalid enums, missing questions, malformed payloads, timeouts, stale answers, model changes, and unavailable service.
- **Exchange recovery:** Lost acknowledgments, duplicate delivery, partial fills, rejected protection, disconnects, rate limiting, and restart reconciliation.
- **Data quality:** Missing candles, out-of-order events, stale books, timestamp skew, and insufficient feature warmup.
- **Security:** Secrets never committed, model cannot sign, payment adapter cannot use trading credentials, and admin routes require authentication.
- **Permission boundaries:** Analysis mode cannot submit trading actions; live mode needs an explicit, bounded authorization.

Mock fixtures and testnet checks can establish mechanical behavior, not profitability. Hyperliquid documents separate mainnet and testnet API URLs, but testnet fills should not be represented as evidence of mainnet liquidity or execution quality ([Hyperliquid API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api)).

Do not require weeks of paper results or a particular return statistic before live use. Once mechanics, eligibility, credentials, and user limits are resolved, a small explicitly authorized live deployment can be an implementation choice rather than a research award.

## Dashboard and operating model

The dashboard should make the bot’s adaptation visible, not merely show a P&L curve. Every strategy selection and rejected setup should be traceable to a snapshot and policy version.

- **Market view:** Discovered/eligible/active market counts, regime by symbol, volatility/liquidity state, and data age.
- **Strategy view:** Selected profile, available alternatives, recent switches, reason codes, and unproven/live/simulated labels.
- **Decision view:** Candidate inputs, JEV choices and confidence, model version, TTL, and final execution disposition.
- **Risk view:** Equity basis, exposure, risk reservations, leverage, margin headroom, liquidation/stop distance, and loss-budget consumption.
- **Operations view:** Feed lag, reconnections, reconciliation status, outstanding unknown orders, database health, and API cost.
- **Controls:** Pause entries, emergency reduction, disable a module, propose a profile change, and inspect pending approvals.

Suggested skip codes include `NO_SETUP`, `REGIME_UNKNOWN`, `JEV_WAIT`, `STALE_DATA`, `DECISION_EXPIRED`, `LIQUIDITY_LIMIT`, `RISK_BUDGET`, `LOSS_LIMIT`, `UNSUPPORTED_MARKET`, and `PROTECTION_UNAVAILABLE`. Avoid a vague “edge not approved” catchall.

Run the database and control API on private networks only. Keep the dashboard behind authenticated private access or an SSH tunnel; never expose trading controls on an unauthenticated public port.

## Repository and deployment plan

Suggested repository name: `hyperliquid-jev-bot`. Start here with the design and implementation, then connect a private GitHub repository once the destination and permissions are confirmed.

```text
hyperliquid-jev-bot/
  pyproject.toml
  uv.lock
  README.md
  .env.example
  .gitignore
  compose.yaml
  Dockerfile
  configs/
    analysis.yaml
    testnet.yaml
    live.example.yaml
    strategies/
  src/jevbot/
    domain/
    market_data/
    features/
    regimes/
    strategies/
    decisions/
    allocation/
    risk/
    execution/
    adapters/
      hyperliquid/
      typesafe/
      skylark/
    persistence/
    monitoring/
    control_api/
  tests/
    unit/
    contract/
    integration/
    recovery/
  docs/
    architecture.md
    operator-runbook.md
    strategy-catalog.md
    live-authorization.md
  .github/workflows/
    ci.yml
```

Keep production secrets out of the repository, images, logs, prompts, and CI artifacts. Use a dedicated trading signer and a separate data-payment wallet; verify the signer’s actual permissions rather than calling it withdrawal-safe without checking.

Use immutable image tags, database migrations, non-root containers, pinned dependencies, encrypted backups, clock synchronization, and a documented restore procedure. A deployment must restart in reconciliation mode, not immediately submit fresh entries.

GitHub CI should run mechanical checks and build an image. Promotion to live should be a deliberate action; merging a strategy change must not automatically authorize additional capital or altered risk limits.

## Build sequence

### Foundation and first vertical slice

Implement schemas, configuration, persistence, market registry, and a read-only stream. Add one deterministic strategy, the actual JEV adapter, a risk preview, and a decision journal so a market event can be followed from input to proposed action.

### Adaptive library and optional macro context

Add the regime feature engine, four initial strategy families, parameter profiles, hysteresis, and allocation. Integrate Skylark health and cache behavior first, with paid retrieval disabled until a purchase policy is approved.

### Execution and failure handling

Implement signing, order IDs, state reconciliation, fill-based protection, and recovery tests. Verify supported market capabilities separately for native perpetuals, HIP-3 markets, and spot rather than assuming one test covers all three.

### VPS operation and bounded live activation

Deploy the container stack, backups, monitoring, and private controls. Enable live only after eligibility, funding scope, signer permissions, leverage/risk settings, and emergency behavior are explicitly settled.

### Adaptation enhancements

Add online allocation updates, event feeds, and additional product adapters based on operational evidence. Keep optional historical analysis available without making it an approval barrier.

## Decisions still needed

- **Trading horizon:** Default proposal is intraday, with minute-based setups and positions lasting minutes to hours. Confirm whether swing trading or faster scalping is intended instead.
- **Capital and risk:** Funded amount, maximum leverage, risk per trade, aggregate exposure, and daily-loss behavior are not specified.
- **JEV account:** Confirm access to TypeSafe or a supported JEV gateway before requesting credentials through a secure channel.
- **Skylark semantics and payments:** Obtain indicator interpretation and publication details; authorize a purchase or bounded payment policy separately.
- **GitHub and VPS:** Choose a private repository destination and confirm VPS provider, operating system, region, and access method.
- **Product scope:** Confirm that all-market discovery with leveraged perps and separately implemented spot capability matches the intended scope; unsupported products must remain visible rather than silently included.
- **Eligibility:** Confirm that live trading is permitted for the user’s circumstances and chosen access route.

Hyperliquid’s published terms expressly restrict its hosted interface for U.S. residents and prohibit using technology to evade interface access restrictions. Those terms describe the interface and do not, by themselves, resolve direct-API eligibility; an overseas VPS is not evidence of permission ([Hyperliquid terms](https://app.hyperliquid.xyz/terms)).

## Completion status

This deliverable defines the proposed architecture, adaptation rules, integration contracts, safeguards, repository layout, and implementation sequence. The supplied images and linked video were reviewed, primary TypeSafe and Hyperliquid integration documentation was checked, and Skylark’s public metadata plus unsigned health/payment challenge were inspected.

No implementation repository was created or connected, no credentials were requested, no server was provisioned, no paid data was purchased, and no orders were placed. The next concrete deliverable is a runnable analysis-mode vertical slice, followed by the explicitly scoped execution components.
