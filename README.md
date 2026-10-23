# Hyperliquid JEV Adaptive Bot

Version 0.1.0 is a runnable, analysis-only Python service for a Linux VPS. It reads public Hyperliquid data, calculates intraday regimes, lets JEV select bounded strategy profiles and candidates, and stores decisions in a local journal.

**This release cannot place orders.** It has no wallet signer, exchange-order adapter, private account reader, live authorization switch, or withdrawal capability. It is not a backtest engine, paper-fill simulator, or proven profitable system.

## What works now

- **Market discovery:** Native perpetuals, HIP-3 DEX perpetual metadata, and spot registry entries. Spot trading/analysis is explicitly unsupported in this version.
- **Live ingestion:** Read-only HTTP polling of completed one-minute candles. No WebSocket ingestion or order-book liquidity measurements yet.
- **Regimes:** Trend up/down, range, volatility expansion, mixed conditions, and shocks. Numeric thresholds are transparent initial heuristics, not calibrated predictions.
- **Strategies:** Trend pullback, volatility breakout, range mean reversion, and liquidity-sweep reversal.
- **Adaptation:** JEV selects a joint strategy/profile choice; routing refresh, minimum dwell, and cached state limit unnecessary switching and calls.
- **JEV integration:** Real TypeSafe HTTP adapter with strict response validation, timeouts, rolling-hour call cap, and no silent substitute on live data.
- **Journal:** SQLite/WAL records inputs, model decisions, versions, reason codes, candidates, and hypothetical sizing. State persists across restarts.
- **Skylark:** Free source-health checks and optional import of a legitimately obtained local reading bundle. No payment code or automatic paid fetch.
- **Operations:** CLI, Docker Compose, single-writer lock, log rotation, healthcheck, deterministic tests, and GitHub Actions configuration.

## Quick start

Use Python 3.12 or later on Linux, macOS, or WSL. Native Windows is not supported because the scanner uses a POSIX process lock.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
pytest -q
jevbot demo
jevbot report
```

The demo makes no network calls. Its synthetic market scenarios use a deterministic `demo_rule_stub`, not JEV, and every record is labeled accordingly. It does not manufacture historical returns or paper fills.

For a separate demo journal:

```bash
jevbot --database data/demo.sqlite3 demo
jevbot --database data/demo.sqlite3 report --output data/demo-report.md
```

## Connect JEV

The official integration uses `https://api.typesafe.ai/v1/systemone` with Bearer authentication and the `model`, `state`, and `questions` fields ([TypeSafe API](https://docs.typesafe.ai/api/system-one)).

Set `TYPESAFE_API_KEY` using your deployment secret mechanism, not in source files or command history. For an interactive Linux shell, this reads it without echoing or including the value in the command:

```bash
read -rs -p "TypeSafe key: " TYPESAFE_API_KEY; echo
export TYPESAFE_API_KEY
export JEV_MODEL=jev-1.13.0
jevbot doctor --check-jev
```

The default pinned model is a documented model ID, but availability must be confirmed for your account. Change `JEV_MODEL` only deliberately and recheck the contract; the response's actual model identifier is journaled ([TypeSafe models](https://docs.typesafe.ai/models)).

`doctor --check-jev` makes one potentially billable model request. Analysis also makes model requests when needed, up to the configured rolling-hour cap; this is a call-count ceiling, not a guaranteed dollar budget.

If a key is missing, public market analysis still runs but records `JEV_UNAVAILABLE`. It does not convert demo decisions into live JEV decisions. `JEV_AUTH_VIA_PROXY=1` is reserved for an authenticated Computer credential-proxy wrapper and should not be set on your VPS.

## Read-only live analysis

```bash
jevbot doctor
jevbot discover
jevbot scan
jevbot run --cycles 3
jevbot run
```

`scan` performs one cycle. `run` repeats until stopped, or until `--cycles` is reached; use Ctrl+C to stop cleanly.

The default active markets are BTC and ETH to keep the first run bounded. To scan the highest-volume eligible perpetuals across discovered DEXes, set `auto_select = true` and adjust `active_limit` in `configs/analysis.toml`; this is an activity ranking, not a claim of trading quality.

Explicit HIP-3 symbols must use the exact namespaced symbol shown by `discover`. The full registry is broader than the active candle-processing set, and metadata-discovery failures are reported rather than silently counted as complete coverage.

Hyperliquid documents market metadata and candle requests through its public Info API; the platform applies rate limits, so this implementation uses a conservative local request-weight budget and a small active set ([Info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint), [perpetual discovery](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals), [rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)).

The local budget cannot account for other applications sharing your IP. Do not run multiple scanners behind one IP without coordinating their budgets.

## Adaptive behavior

The numeric engine classifies the market; JEV chooses among strategies allowed for that regime and a bounded profile. Deterministic rules then find a setup, followed by a separate JEV candidate-or-WAIT decision.

| Regime | Eligible strategy families |
|---|---|
| Trend up/down | Trend pullback, breakout |
| Expansion | Breakout, trend pullback |
| Range | Mean reversion, liquidity sweep |
| Mixed | Liquidity sweep, breakout |
| Shock | No new candidate |

The router chooses joint values such as `volatility_breakout:wide`, so a strategy and profile cannot contradict independent model answers. Route choices are refreshed after five observed bar-minutes or a regime change; eligible non-WAIT route switches have a three-bar minimum dwell by default.

Profiles adjust ATR-based stop distance, target multiples, and proposed maximum holding period. These are proposal parameters only, because no orders or position management exist yet.

No backtest result, Sharpe threshold, statistical significance, or edge statement is required. `NO_SETUP` means the configured trigger did not happen; `JEV_WAIT` means JEV chose not to propose the setup, not that a historical performance committee rejected it.

The proposed follow-up online performance allocator is not implemented. Version 0.1 adapts to observed market conditions, but does not learn profitability from realized fills because it has no fills.

## Hypothetical sizing

The `preview` configuration contains an illustrative reference equity and risk fraction, not your actual account balance or authorized limits. Quantity previews are bounded by intended loss and notional, rounded down to size precision, and include a configurable cost buffer.

Every preview is marked `executable: false`. It explicitly lists unverified checks including actual account equity, correlated exposure, book depth, tiered margin, liquidation distance, minimum notional, and price precision.

Do not turn a preview into an order by copying its fields. A future execution adapter needs account-aware validation, protective orders, reconciliation, authorization, and product-specific semantics first.

## Skylark

Skylark publishes weekly FLP, FLI, and GFC observations; the bot treats them as optional macro context, not an intraday trigger feed. The provider instructs clients to inspect payment requirements and notes that completed paid mainnet delivery has not yet been independently accepted ([Skylark discovery](https://skylark-liquidity-api-portal.vercel.app/discover), [machine-readable guide](https://skylark-liquidity-api-portal.vercel.app/llms.txt)).

This release only calls the public source-health endpoint. It never calls a paid readings endpoint, signs an x402 payment, or requests a wallet.

To use readings you have legitimately obtained, set `skylark_readings_file` to a private JSON file containing the following shape:

```json
{
  "readings": [
    {"official_name": "Financial Liquidity Proxy", "value": 0.0, "as_of": "YYYY-MM-DD"},
    {"official_name": "Financial Liquidity Index", "value": 0.0, "as_of": "YYYY-MM-DD"},
    {"official_name": "Global Funding Conditions", "value": 0.0, "as_of": "YYYY-MM-DD"}
  ]
}
```

These are schema placeholders, not real readings. The importer rejects invalid, future-dated, incomplete, duplicate, or stale bundles.

Directional interpretation remains `unknown` even for a valid bundle until the units and sign semantics are documented. Missing or stale macro data does not disable strategies that do not require it.

## VPS deployment

Provision a Linux VPS and install Docker Engine plus the Compose plugin. Copy this project, configure secrets through your deployment environment or a protected `.env`, then run:

```bash
docker compose build
docker compose up -d
docker compose logs -f --tail=100
docker compose exec analyzer jevbot report
docker compose down
```

Compose publishes no ports. The process runs as a non-root user, has a read-only root filesystem, writes only to the journal volume, and has no trading keys.

Docker restart policy restarts a process that exits; an unhealthy healthcheck alone does not automatically restart it. Monitor container health separately and investigate persistent data/model failures.

Before changing strategy settings or upgrading images, stop the analyzer, preserve a journal backup, then restart. A configuration hash invalidates cached route state; duplicate completed bars remain skipped to avoid accidental repeated calls.

Back up SQLite with its backup API, not by copying only the main database file while WAL writes are in progress:

```bash
docker compose exec analyzer python -c \
  "import sqlite3; s=sqlite3.connect('/app/data/journal.sqlite3'); d=sqlite3.connect('/app/data/backup.sqlite3'); s.backup(d); d.close(); s.close()"
docker compose cp analyzer:/app/data/backup.sqlite3 ./journal-backup.sqlite3
```

Version 0.1 does not automatically prune journal history. Monitor disk growth and archive or rotate the database while the service is stopped; keep the journal private because it contains strategy observations.

## GitHub handoff

The project includes a CI workflow but is not connected or pushed to your GitHub account. Create or choose a private repository and authorize the connection before publishing code.

CI runs mechanical checks and builds an analysis image. It contains no live deployment, secrets, capital authorization, or profitability gate.

## Known limits and next milestone

- **Execution:** No orders, private wallet access, stops, partial-fill management, or live trading.
- **Data:** Polling, not streaming; no depth/spread, account, funding-history, or OI feature ingestion.
- **Universe:** Spot is discovery-only; HIP-3 analysis is generic candle analysis, not an endorsement of its oracle, collateral, or session handling.
- **Regimes:** Initial heuristic thresholds; neither JEV confidence nor regime labels are profit probabilities.
- **Persistence:** SQLite is appropriate for this single-writer first slice; migrate before adding concurrent services or an execution authority.
- **UI:** CLI and Markdown/JSON reports, no web dashboard.
- **Learning:** Regime/profile adaptation exists; outcome-based capital allocation and new-strategy generation do not.
- **Eligibility:** Live deployment requires confirmation of applicable restrictions; an overseas VPS is not permission to trade.

Hyperliquid's terms restrict its hosted interface for U.S. residents and prohibit evading interface restrictions; those terms alone do not resolve eligibility for direct API trading ([Hyperliquid terms](https://app.hyperliquid.xyz/terms)).

The next milestone is authenticated JEV verification, richer market-data quality measurements, and a tested testnet execution module. Live trading remains a separate, explicitly scoped decision.
