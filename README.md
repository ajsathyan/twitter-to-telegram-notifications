# Twitter to Telegram Notifications

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![SQLite](https://img.shields.io/badge/state-SQLite-044a64)](https://www.sqlite.org/)
[![Official X API](https://img.shields.io/badge/X%20API-official-111111)](https://developer.x.com/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram-Bot%20API-229ED9)](https://core.telegram.org/bots/api)

Self-hosted X/Twitter to Telegram notifications for a mini PC.

No scraping. No X password. No backlog spam. This daemon polls configured X accounts with the official API, stores durable state in SQLite, and forwards new qualifying posts to a Telegram chat or channel.

## Why this exists

Twitter/X alerts are noisy, fragile, and usually tied to a phone. This project gives you a small always-on notifier that you control:

- Watch specific X/Twitter accounts with per-account settings.
- Send clean Telegram HTML messages with account links, post links, shared links, quotes, reposts, polls, images, and video fallbacks.
- Skip replies globally.
- Send reposts by default, with a Yes/No radio per account in the local web UI.
- Establish a first-run baseline without sending old posts.
- Filter noisy accounts through a local Hermes-style endpoint, xAI/Grok, or any OpenAI-compatible endpoint.
- Run continuously with `systemd` on a low-resource Linux mini PC.

## Quick Start

```bash
git clone https://github.com/ajsathyan/twitter-to-telegram-notifications.git
cd twitter-to-telegram-notifications

python3.11 -m venv .venv || python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .

cp examples/config.toml config.toml
cp examples/.env.example .env
chmod 600 .env
```

Edit `.env`:

```bash
X_BEARER_TOKEN=replace-with-x-api-bearer-token
TELEGRAM_BOT_TOKEN=replace-with-telegram-bot-token
TELEGRAM_CHAT_ID=-1001234567890
```

Validate everything:

```bash
twitter-tg-notifs validate-config --config config.toml --env-file .env
```

Open the local account manager:

```bash
twitter-tg-notifs web --config config.toml --env-file .env
```

The web command prints the localhost URL and opens it in your default browser. Use `--no-open` on a headless machine.

## First Run

The first real poll sets a baseline per account and does not send old posts:

```bash
twitter-tg-notifs run --config config.toml --env-file .env --state-db data/state.sqlite3 --once
```

After that, new qualifying posts are delivered within roughly the configured poll interval:

```bash
twitter-tg-notifs run --config config.toml --env-file .env --state-db data/state.sqlite3
```

Dry-run previews what would be sent:

```bash
twitter-tg-notifs dry-run \
  --config config.toml \
  --env-file .env \
  --state-db data/state.sqlite3 \
  --output-file artifacts/dry-run.txt
```

Dry-run does not send Telegram messages and does not mark tweets as sent. It does advance `last_seen_tweet_id`, so use a throwaway `--state-db` for no-impact experiments.

## Configuration

Non-secret settings live in TOML:

```toml
[polling]
interval_seconds = 60
timezone = "America/New_York"

[x]
exclude_replies = true
default_include_reposts = true

[classifier]
provider = "none"

[[accounts]]
username = "account_a"

[[accounts]]
username = "noisy_account"
include_reposts = true

[accounts.topic_filter]
enabled = true
instructions = "Send only posts about nuclear power, AI data center electricity demand, coal exports, or utility capex."
confidence_threshold = 0.70
on_filter_error = "skip"
```

Secrets stay in `.env` or environment variables. The app masks secrets in CLI output.

## Topic Filtering

Filtering is per account. The classifier only decides relevance; it does not rewrite or summarize the tweet.

Providers:

- `none`: no filtering.
- `http_json`: POST normalized tweet JSON and filter config to a local endpoint such as Hermes.
- `xai`: call hosted xAI/Grok with `XAI_API_KEY`.
- `openai_compatible`: call any OpenAI-compatible `/chat/completions` endpoint with optional `OPENAI_API_KEY`.

Hermes/local endpoint shape:

```http
POST http://127.0.0.1:8787/classify
```

Expected strict JSON:

```json
{
  "send": true,
  "confidence": 0.86,
  "matched_topics": ["AI data center electricity demand"],
  "reason": "The post discusses electricity demand growth from AI infrastructure."
}
```

Failure behavior is set per account with `on_filter_error = "skip" | "send" | "fallback"`.

## Mini PC Install

On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv

sudo mkdir -p /opt/twitter-tg-notifs /etc/twitter-tg-notifs /var/lib/twitter-tg-notifs
sudo chown "$USER":"$USER" /opt/twitter-tg-notifs

git clone https://github.com/ajsathyan/twitter-to-telegram-notifications.git /opt/twitter-tg-notifs
cd /opt/twitter-tg-notifs

python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e .

sudo cp examples/config.toml /etc/twitter-tg-notifs/config.toml
sudo cp examples/.env.example /etc/twitter-tg-notifs/.env
sudo chmod 600 /etc/twitter-tg-notifs/.env
```

Edit `/etc/twitter-tg-notifs/config.toml` and `/etc/twitter-tg-notifs/.env`, then install the service:

```bash
sudo useradd --system --home /opt/twitter-tg-notifs --shell /usr/sbin/nologin twitter-tg-notifs || true
sudo chown -R twitter-tg-notifs:twitter-tg-notifs /var/lib/twitter-tg-notifs

sudo cp deploy/twitter-tg-notifs.service /etc/systemd/system/twitter-tg-notifs.service
sudo systemctl daemon-reload
sudo systemctl enable --now twitter-tg-notifs

sudo systemctl status twitter-tg-notifs
sudo journalctl -u twitter-tg-notifs -f
```

To use the web UI on a headless mini PC:

```bash
ssh -L 4319:127.0.0.1:4319 mini-pc
twitter-tg-notifs web --config /etc/twitter-tg-notifs/config.toml --env-file /etc/twitter-tg-notifs/.env --no-open
```

Then open `http://127.0.0.1:4319` on your laptop.

## Operations

Useful commands:

```bash
twitter-tg-notifs validate-config --config config.toml --env-file .env
twitter-tg-notifs run --config config.toml --env-file .env --once
twitter-tg-notifs dry-run --config config.toml --env-file .env --state-db data/test.sqlite3
twitter-tg-notifs web --config config.toml --env-file .env
```

Local files to keep private:

- `.env`: API tokens and Telegram chat ID.
- `data/*.sqlite3`: watched account state, sent tweet IDs, classifier decisions.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

The test suite covers config validation, secret masking, X response normalization, first-run baseline behavior, dedupe, reply/repost controls, Telegram HTML escaping, message formatting, classifier adapters, web UI config edits, and CSRF protection.
