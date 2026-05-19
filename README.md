# status-bot

Companion to [Uptime Kuma](https://github.com/louislam/uptime-kuma). Maintains **one pinned Telegram message** that always shows the current up/down state of every monitor on a Kuma status page. Updates in place via `editMessageText` — no chat spam.

Pair it with Kuma's native Telegram notifier:
- **Kuma's notifier** → buzzes on DOWN/UP transitions (rare, loud).
- **status-bot** → silently rewrites the pinned message every minute (always current, never beeps).

## Stack

- Python 3.12, stdlib only — no `pip install`, no `requirements.txt`
- Single file (`index.py`)
- No database, no volume — pinned message ID is read live from Telegram via `getChat`
- ~15–20 MB RAM, well under $1/month on Railway

## Environment variables

See `.env.example`.

| Variable | Required | Notes |
|---|---|---|
| `KUMA_URL` | yes | Base URL of your Uptime Kuma. Use `http://uptime-kuma.railway.internal:3001` on Railway to keep traffic internal (zero egress cost). Comma-list to target multiple Kuma instances (one per target). |
| `STATUS_SLUG` | yes | Slug of the public status page (the part after `/status/` in its URL). Comma-list for multi-target mode (e.g. `tms360-prod,tms360-stage,tms360-dev`). |
| `BOT_TOKEN` | yes | Telegram bot token from `@BotFather`. Use the same one already wired into Kuma. |
| `CHAT_ID` | yes | Same chat ID Kuma uses. For a DM, that's your user ID; for a group, the negative group ID. |
| `MESSAGE_THREAD_ID` | no | Telegram supergroup topic/thread ID. Comma-list aligned with `STATUS_SLUG` for multi-target mode. Find it via right-click on any thread message → *Copy Message Link* → `https://t.me/c/{chat}/{thread_id}/{msg_id}`. |
| `POLL_INTERVAL` | no | Seconds between updates. Defaults to `60`. |
| `TITLE` | no | Header text + alert suffix. Defaults to `Status`. Comma-list aligned with `STATUS_SLUG` for multi-target mode. |
| `PORT` | no | HTTP port for `/healthz`. Railway sets this automatically. |

## Run locally

```bash
cp .env.example .env
# fill in values
set -a; . ./.env; set +a
python3 index.py
```

Then `curl http://localhost:3000/healthz`.

## Deploy to Railway

1. **Push this folder to a GitHub repo.**
2. Railway → **same project as Kuma** → `+ New` → **Deploy from GitHub Repo** → pick this repo.
3. **Variables** tab → paste every key from `.env.example` with real values.
   - For `KUMA_URL`, open your Kuma service → Networking → copy the `*.railway.internal` hostname → use `http://<that>:3001` (or whichever port Kuma listens on).
4. **Settings** → **Networking** → click **Generate Domain** (optional — only needed if you want `/healthz` reachable from outside; Kuma can hit it on the internal hostname either way).
5. Deploy. Logs should show `status-bot listening on :3000, ticking every 60s` and within `POLL_INTERVAL` seconds a new pinned message appears in your Telegram chat.

## Multiple environments from one service (multi-target mode)

`KUMA_URL`, `STATUS_SLUG`, `MESSAGE_THREAD_ID`, and `TITLE` all accept **comma-separated lists**, aligned by index. One status-bot service drives N pinned messages — one per target. Bot token and chat id stay singletons (Telegram side is shared).

If all targets live on a single Kuma, pass `KUMA_URL` as a scalar — it auto-fills for every target. If they live on separate Kumas (one per env), pass all N URLs.

Example for 3 supergroup topics on the same Kuma:
```
KUMA_URL          = http://uptime-service.railway.internal:8080
BOT_TOKEN         = <single token>
CHAT_ID           = -1003802594710
STATUS_SLUG       = tms360-prod,tms360-stage,tms360-dev
MESSAGE_THREAD_ID = 356,354,352
TITLE             = Prod,Staging,Dev
POLL_INTERVAL     = 60
STATE_FILE        = /data/state.json   (optional; single JSON keyed by slug)
```

Each tick iterates every target sequentially:
- pulls that target's status page from Kuma
- edits its pinned message (or creates one on first run)
- fires DOWN/UP transition alerts into that target's thread

Adding a 4th environment is just appending to each list. The General thread stays bot-free for human discussion.

For a single env, just leave the vars as plain scalars (no commas).

## Telegram permissions

- **DM with the bot**: just message the bot once so it can see you.
- **Group chat**: add the bot to the group, then promote it to admin with the **Pin Messages** permission. Without that, `pinChatMessage` returns 400.

## Health endpoint

`GET /healthz` returns `200` if the last tick succeeded recently, `503` otherwise:

```json
{
  "status": "ok",
  "lastTick": {"at": 1715766000000, "ok": true, "action": "edited", "error": null},
  "pollIntervalMs": 60000
}
```

Add a Kuma monitor pointing at `http://status-bot.railway.internal:3000/healthz` so the bot itself shows up on the status page. If the bot dies, Kuma's native Telegram notifier still fires — you never lose visibility.

## Manual trigger

`POST /tick` forces an immediate update cycle and returns the result. Useful for smoke-testing right after deploy:

```bash
curl -X POST https://<your-railway-domain>/tick
```

## Troubleshooting

- **Empty list in the pinned message** — `STATUS_SLUG` doesn't match, or the status page has no monitors attached. Open `https://<kuma>/api/status-page/<slug>` in a browser to verify.
- **`getChat` 400 chat not found** — bot isn't a member of the chat, or `CHAT_ID` is wrong.
- **`pinChatMessage` 400** — bot lacks pin permission in the group. Promote to admin.
- **Stamp is UTC** — by design, no TZ headaches. Change the `time.gmtime()` arg in `render()` to `time.localtime()` if you want local time.
