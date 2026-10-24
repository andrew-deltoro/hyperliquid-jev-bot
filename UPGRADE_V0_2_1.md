# Hyperliquid JEV v0.2.1: Recursive Evaluation

This update implements recurring evaluation of forward-observed simulated tickets, persistent research memory, and bounded challenger recommendations. It is built for your existing v0.2.0 project; it has not been pushed to GitHub or deployed to your VPS by the assistant.

## What the loop actually does

- **Review:** On the next scanner cycle after each six-hour interval, review live-source shadow tickets. Review snapshots and idea records persist in SQLite across restarts.
- **Retain:** Preserve positive candidates and the history of every review, including losing or deteriorating results. `KEEP_CANDIDATE` means descriptive simulated performance, not a proven edge.
- **Contextualize:** Separate experiment/version, instrument, lane, strategy, profile, direction, technical regime, and any recorded weather/session labels. This release does not ingest weather, so those labels remain `unmeasured`.
- **Inform JEV:** Supply up to five matching research rows in subsequent decision contexts. Only the same experiment identity, instrument, regime and weather/session context qualify. Existing route eligibility and risk settings remain unchanged; cached routes retain their normal refresh schedule.
- **Recommend:** Generate up to three bounded exit-profile challengers per review. JEV can shortlist one or WAIT, with at most one additional API attempt per review interval under the existing shared hourly cap. This first version proposes predefined profile variants, not arbitrary invented algorithms.
- **Repeat:** Re-evaluate as new outcomes arrive. An old JEV shortlist remains historical, and the report separately displays the parent's current assessment.

The evaluator is deterministic; JEV reviews the structured evidence and shortlisted possibilities. It does not fabricate free-text private reasoning or estimate a calibrated probability that a strategy will win.

## Evidence, not a new approval wall

Default labels use 20 closed tickets and three distinct UTC exit dates before calling a positive group `KEEP_CANDIDATE`. These are configurable descriptive thresholds, not statistical validation or prerequisites for placing simulated tickets.

An early observation can already produce an exploratory challenger recommendation. None of the evaluation labels block the existing strategy lanes, and no historical backtest or walk-forward approval is introduced.

The review considers mean net R, net modeled P&L, recent results, closed-sequence drawdown, unresolved paths, and sample size. `R` divides each net result by its initial price-stop risk; costs are included in net, but funding remains the existing allowance rather than measured exchange funding.

Win rate alone never determines retention. A high-win-rate group can still receive `REWORK_CANDIDATE` or `RISK_REVIEW` when losses dominate.

Open or pending tickets are not wins. Unresolved paths trigger a data-review label; malformed records are excluded and counted, while future-dated closes are excluded from an as-of review.

Recent and lifetime samples overlap, trades may be correlated, and profile comparisons change several exit parameters together. The labels do not establish causality, statistical significance, portfolio return, or out-of-sample profitability.

## Boundaries and remaining work

The bot still cannot place real orders or access a wallet. The evaluator cannot raise leverage, change risk settings, buy data, rewrite Python, edit its own scoring rules, or enable a recommended challenger.

The existing JEV policy does receive descriptive outcome memory after deployment, so its decisions can adapt within existing choices. The deterministic baseline lanes do not consume that memory.

New challenger experiments require explicit implementation/enablement and a new experiment identity. When enabled in a future update, their forward results can enter subsequent evaluations; this release does not automatically execute recommended challengers.

The macro-weather/ORB feature remains pending your answers about interpretation, sessions, risk-off behavior, and market-data access. Discovery support for `xyz` already exists, but this update does not broaden the active watchlist or pretend it has VIX, DXY, ETF, anchor-stock or order-book data.

Changing software/configuration changes the experiment hash. Old results remain visible and are evaluated separately, but are not silently imported as performance of the new JEV policy.

## Apply on your computer

Download `hyperliquid-jev-bot-v0.2.1.patch`, then open Git Bash in your local repository. Check for local edits before applying; do not overwrite intentional changes.

```bash
git status --short
git apply --check ~/Downloads/hyperliquid-jev-bot-v0.2.1.patch
git apply ~/Downloads/hyperliquid-jev-bot-v0.2.1.patch
git diff --stat
git status --short
```

Stop if the check fails. The patch targets the previous v0.2.0 handoff; a failure should be resolved by merging your actual files, not resetting or replacing the repository.

Stage only these update paths, inspect the staged list for secrets, then commit and push:

```bash
git add src/jevbot tests/test_improvement.py tests/test_shadow.py \
  configs/analysis.toml pyproject.toml uv.lock README.md BUILD_STATUS.md UPGRADE_V0_2_1.md
git diff --cached --name-only
git diff --cached --stat
git commit -m "Add recurring evidence reviews and bounded JEV research memory"
git push
```

Do not add `.env`, credentials, SQLite databases or journal backups. Check GitHub Actions before proceeding to deployment.

## Back up and deploy on the VPS

Run these commands inside your existing VPS project directory. Use `sudo` if Docker socket access still requires it, and run each step separately, stopping on any error.

```bash
cd ~/hyperliquid-jev-bot
sudo docker compose exec analyzer python -c \
  "import sqlite3; s=sqlite3.connect('/app/data/journal.sqlite3'); d=sqlite3.connect('/app/data/pre-v0.2.1.sqlite3'); s.backup(d); d.close(); s.close()"
sudo docker compose cp analyzer:/app/data/pre-v0.2.1.sqlite3 ./pre-v0.2.1.sqlite3
sudo chmod 600 pre-v0.2.1.sqlite3
sudo docker compose stop analyzer
git pull --ff-only
sudo docker compose build
sudo docker compose up -d
sudo docker compose exec analyzer jevbot --version
sudo docker compose ps
sudo docker compose logs --tail=80 analyzer
```

The version must show `0.2.1`. Keep the existing Compose project name and named volume; do not run `docker compose down -v`.

If the backup fails because the analyzer is not running, stop and inspect the deployment rather than deleting or recreating its journal. This upgrade adds two tables and leaves existing trades and decisions intact.

## Read the results

After deployment:

```bash
sudo docker compose exec analyzer jevbot improvement
sudo docker compose exec analyzer jevbot improvement --json
sudo docker compose exec analyzer jevbot scoreboard --by-regime
sudo docker compose exec analyzer jevbot why-wait --hours 24
```

The improvement command computes current descriptive evidence without making model calls or forcing a scheduled review. Its separate scheduled-review section shows when the recurring loop last ran; the idea library shows historical recommendations alongside current parent status.

Expect `OBSERVE` or thin-sample labels initially, not invented winners. Scheduled reviews continue without a JEV key; shortlisting will report unavailable or budget-limited as appropriate.

## Controls

The new `[improvement]` section in `configs/analysis.toml` contains the defaults. You can set `jev_review = false` to disable the extra research-shortlisting call while retaining deterministic reviews and descriptive memory.

Set `enabled = false` to stop scheduled improvement reviews and stop sending research memory to JEV. This does not erase records or stop the four baseline strategies.

Configuration changes create a new experiment identity. Recreate the analyzer after editing configuration to load it; avoid changing evaluation thresholds repeatedly just to make a label turn positive.
