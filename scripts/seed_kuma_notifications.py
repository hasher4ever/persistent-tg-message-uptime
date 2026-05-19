#!/usr/bin/env python3
"""Create 3 Telegram notifiers in Kuma (one per env) and wire them to monitors
by env tag. Each env's DOWN/UP alerts land in its dedicated supergroup topic.

This replaces status-bot's alert function for forum-supergroup setups —
Kuma's native notifier now supports `Message Thread ID`.

Idempotent: re-runs skip notifiers and monitor-attachments that already exist.

Usage:
    source /Users/abubakr/Documents/TMS_documentation/.venv/bin/activate
    export KUMA_URL='https://uptime-service-production.up.railway.app'
    export KUMA_USER='tms360uptimestatus'
    export KUMA_PASS='...'
    export BOT_TOKEN='...'              # same token already in status-bot
    export CHAT_ID='-1003802594710'     # the supergroup
    python3 scripts/seed_kuma_notifications.py
"""

from __future__ import annotations

import os
import sys

from uptime_kuma_api import UptimeKumaApi, NotificationType

KUMA_URL  = os.environ.get("KUMA_URL", "https://uptime-service-production.up.railway.app")
KUMA_USER = os.environ.get("KUMA_USER")
KUMA_PASS = os.environ.get("KUMA_PASS")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID   = os.environ.get("CHAT_ID", "-1003802594710")

# env tag (matches the monitor tag created by seed_kuma.py) -> (notifier name, telegram thread id)
ENV_NOTIFIERS = {
    "prod":    ("tg-prod",    "356"),
    "staging": ("tg-staging", "354"),
    "dev":     ("tg-dev",     "352"),
}


def main():
    missing = [k for k, v in (("KUMA_USER", KUMA_USER), ("KUMA_PASS", KUMA_PASS), ("BOT_TOKEN", BOT_TOKEN)) if not v]
    if missing:
        sys.exit(f"set env vars: {missing}")

    with UptimeKumaApi(KUMA_URL) as api:
        api.login(KUMA_USER, KUMA_PASS)

        existing = {n["name"]: n for n in api.get_notifications()}
        notifier_id_by_env: dict[str, int] = {}

        # Step 1: create/reuse the 3 notifiers
        for env_tag, (name, thread_id) in ENV_NOTIFIERS.items():
            if name in existing:
                notifier_id_by_env[env_tag] = existing[name]["id"]
                print(f"  = {name} already exists (id={existing[name]['id']})")
                continue
            r = api.add_notification(
                name=name,
                type=NotificationType.TELEGRAM,
                isDefault=False,
                applyExisting=False,
                telegramBotToken=BOT_TOKEN,
                telegramChatID=CHAT_ID,
                telegramMessageThreadID=thread_id,
                telegramSendSilently=False,
                telegramProtectContent=False,
            )
            notifier_id_by_env[env_tag] = r["id"]
            print(f"  + created {name} (id={r['id']}, thread={thread_id})")

        # Step 2: walk monitors, attach the right notifier based on env tag
        monitors = api.get_monitors()
        attached = skipped = no_env = 0
        for m in monitors:
            tags = [t["name"] for t in m.get("tags", [])]
            env_tag = next((t for t in tags if t in ENV_NOTIFIERS), None)
            if not env_tag:
                no_env += 1
                continue
            notifier_id = notifier_id_by_env[env_tag]
            current = m.get("notificationIDList")
            # Kuma returns either a dict {"3": true} or a list [3]; handle both.
            if isinstance(current, dict):
                current_ids = {int(k) for k, v in current.items() if v}
            elif isinstance(current, list):
                current_ids = {int(x) for x in current}
            else:
                current_ids = set()
            if notifier_id in current_ids:
                skipped += 1
                continue
            new_ids = current_ids | {notifier_id}
            new_value = sorted(new_ids) if isinstance(current, list) else {str(i): True for i in new_ids}
            api.edit_monitor(m["id"], notificationIDList=new_value)
            attached += 1
            print(f"  + attached {ENV_NOTIFIERS[env_tag][0]} → {m['name']}")

        print(f"\ndone: attached={attached} already-attached={skipped} no-env-tag={no_env}")


if __name__ == "__main__":
    main()
