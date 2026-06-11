# LuckyBet2 — Discord Casino Bot

A Discord bot offering virtual-currency casino games including Crash, Blackjack, Mines, Coin Flip, Slots, and Roulette. Uses a "Provably Fair" system, rank-based roles, and dynamic image generation for game results.

## Setup

1. Add your `DISCORD_TOKEN` secret in the Secrets tab (Discord Developer Portal → Bot → Token).
2. Run the **Start application** workflow to launch the bot.

## GitHub Actions CI/CD

A workflow at `.github/workflows/ci.yml` runs automatically on every push to `main`:

1. **CI job** — installs dependencies and checks syntax of `bot.py` and `images.py`.
2. **Deploy job** — triggers a Replit redeploy via a deploy hook (only on pushes to `main`, after CI passes).

### One-time GitHub secret setup

To enable the deploy job, add `REPLIT_DEPLOY_HOOK` as a GitHub repository secret:

1. In Replit, go to **Deployments → Settings → Deploy Hooks** and copy the hook URL.
2. In GitHub, go to **Settings → Secrets and variables → Actions → New repository secret**.
3. Name it `REPLIT_DEPLOY_HOOK` and paste the URL.

After this, every push to `main` will automatically redeploy the bot on Replit once CI passes.

## Project Layout

- `bot.py` — Main entry point; all game commands and bot logic.
- `images.py` — Pillow-based image card generation for game results.
- `bot/user_data.json` — Local JSON database for user balances, stats, and configuration.
- `requirements.txt` — Python dependencies (discord.py, python-dotenv, Pillow).

## Running

```
python bot.py
```

Requires `DISCORD_TOKEN` environment secret to be set.

## User preferences

