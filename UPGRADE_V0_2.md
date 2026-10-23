# Hyperliquid JEV v0.2: Upgrade and Docker Access

This upgrade adds a forward-only simulated strategy scoreboard and an observed “why WAIT?” breakdown. It does not add wallet access, real orders, paid data purchases, a web dashboard, or a profitability approval gate.

## Fix Docker permissions first

Run these commands on the Ubuntu VPS, not on Windows unless Git Bash is already connected to the VPS through SSH. Start in the directory containing the bot's `compose.yaml`.

For immediate access:

```bash
sudo docker compose ps
sudo docker compose logs --tail=50 analyzer
sudo docker compose exec analyzer jevbot report
```

If these work, the daemon is reachable and your normal SSH user's socket access needs attention. Docker documents that its Unix socket is owned by root and that members of the `docker` group can access it without `sudo` ([Docker Linux post-installation guide](https://docs.docker.com/engine/install/linux-postinstall/)).

To grant persistent access, run the following while logged in as your intended normal SSH user, not root:

```bash
whoami
getent group docker || sudo groupadd docker
sudo usermod -aG docker "$(id -un)"
```

Completely log out of SSH, then log back in as that same user. Check membership and access:

```bash
id -nG
docker ps
cd ~/hyperliquid-jev-bot
docker compose ps
```

If `whoami` originally printed `root`, add your actual non-root deployment user instead, for example `sudo usermod -aG docker jevbot`, and then reconnect as `jevbot`. Adjust the project path if you cloned somewhere else.

Docker documents logout/login or `newgrp docker` as ways to activate the new group membership; the `docker` group grants root-level privileges, so add only a trusted administrator account ([Docker Linux post-installation guide](https://docs.docker.com/engine/install/linux-postinstall/)).

Do not use `chmod 666 /var/run/docker.sock`, make the socket world-writable, or expose the Docker API publicly. Continuing to use `sudo docker ...` is also an option if you do not want permanent group membership.

If even `sudo docker ps` fails, collect these outputs instead of changing socket permissions:

```bash
whoami
id
sudo systemctl status docker --no-pager
sudo ls -l /var/run/docker.sock
docker context show
```

Never send API keys or `.env` contents with diagnostic output. The socket check is separate from bot code and cannot be repaired by changing JEV settings.

## Apply the code update to your GitHub project

The assistant has built and tested the update locally, not deployed it to your VPS or pushed it to GitHub. Download `hyperliquid-jev-bot-v0.2.0.patch`, then open Git Bash inside your existing local project folder on Windows.

Confirm where you are and check for local edits:

```bash
pwd
git status --short
git diff --stat
```

Preserve any intentional local edits before continuing. Do not overwrite `.env`, reset your repository, or force-push.

Apply the patch from Downloads, adjusting its path if necessary:

```bash
git apply --check ~/Downloads/hyperliquid-jev-bot-v0.2.0.patch
git apply ~/Downloads/hyperliquid-jev-bot-v0.2.0.patch
git diff --stat
git status --short
```

The check must succeed before applying. If it fails, stop and share the exact error; it likely means your files differ from the v0.1 baseline, and should be merged rather than replaced blindly.

The patch changes code, tests, configuration examples, locked package version metadata, and documentation. It contains no keys, databases, or `.env` file.

Commit and push only the update paths:

```bash
git add src tests scripts configs/analysis.toml pyproject.toml uv.lock README.md BUILD_STATUS.md UPGRADE_V0_2.md
git diff --cached --stat
git diff --cached --name-only
git commit -m "Add simulated strategy scoreboard and WAIT diagnostics"
git push
```

Confirm that no secret file is staged before committing. Check the GitHub Actions result after the push and resolve any failure before deploying.

## Back up and deploy on the VPS

Use `sudo` below until the Docker group change works. The backup command uses SQLite's backup API so it includes committed WAL data.

```bash
cd ~/hyperliquid-jev-bot
sudo docker compose exec analyzer python -c \
  "import sqlite3; s=sqlite3.connect('/app/data/journal.sqlite3'); d=sqlite3.connect('/app/data/pre-v0.2.sqlite3'); s.backup(d); d.close(); s.close()"
sudo docker compose cp analyzer:/app/data/pre-v0.2.sqlite3 ./pre-v0.2.sqlite3
sudo chmod 600 pre-v0.2.sqlite3
sudo docker compose stop analyzer
git pull --ff-only
sudo docker compose build
sudo docker compose up -d
sudo docker compose exec analyzer jevbot --version
sudo docker compose ps
sudo docker compose logs --tail=50 analyzer
```

The version should be `0.2.0`. Run each command in order and stop if any step fails; do not continue to restart an old image after a failed build.

This uses the existing Compose project and named journal volume. Do not run `docker compose down -v`, rename the deployment folder, or change the Compose project name during the upgrade.

The database gains shadow-trade and experiment tables on first startup. Original decisions, events, cached state, and call reservations are retained; historical proposals are not backfilled as profitable simulated trades.

If the application is not currently running, the initial backup command will fail. Stop there and inspect the container/volume state rather than deleting or replacing the journal.

## Use the new commands

After updating the image:

```bash
sudo docker compose exec analyzer jevbot why-wait --hours 24
sudo docker compose exec analyzer jevbot scoreboard
sudo docker compose exec analyzer jevbot scoreboard --by-regime
```

Detailed machine-readable results:

```bash
sudo docker compose exec analyzer jevbot why-wait --hours 24 --json
sudo docker compose exec analyzer jevbot scoreboard --json
```

Reports default to real public-feed observations and exclude synthetic demo data. To inspect a demo journal, explicitly select `--source synthetic`; use `--source all` only when you want separately labeled groups together.

The scoreboard starts empty or with zero-ticket lanes. It needs new setups and subsequently observed future candles to produce simulated entries and exits; this is not a fault.

The WAIT breakdown can inspect older decisions, but will label their stage unknown when the old record lacks enough evidence. It will not invent explanations retroactively.

## What the results mean

- **Rules lanes:** Each deterministic strategy generates candidates from the same validated snapshot, with a fixed configurable profile. These do not require another JEV call.
- **JEV policy lane:** Only candidates actually selected by JEV enter this hypothetical lane.
- **Closed net:** Simulated gross P&L less modeled fees and a funding-cost allowance, grouped by instrument and configuration. It is not a real account return.
- **Open estimate:** Hypothetical exit value at the latest observed close, including assumed costs. Stale marks are flagged separately.
- **Unresolved:** An essential price path was missed; no convenient or profitable exit is invented.
- **WAIT stage:** Strategy routing and candidate rejection are different outcomes. Call-budget exhaustion and unavailable services are operational issues, not bearish or bullish opinions.

The original 30-calls/hour cap is unchanged. This upgrade neither buys data nor raises the JEV call limit; spending decisions remain separate.
