#!/usr/bin/env python3
"""Populate the 3 Kuma status pages with their env's monitors via API.

Each page (`tms360-prod`, `tms360-stage`, `tms360-dev`) gets the monitors
carrying that env's tag, grouped by role (core / backend / ops / other).

Idempotent — re-running overwrites the publicGroupList each time, so use
this as the source of truth for what shows on each page.

Usage:
    source /Users/abubakr/Documents/TMS_documentation/.venv/bin/activate
    export KUMA_URL='https://uptime-service-production.up.railway.app'
    export KUMA_USER='tms360uptimestatus'
    export KUMA_PASS='...'
    python3 scripts/seed_status_pages.py
"""

from __future__ import annotations

import os
import sys

from uptime_kuma_api import UptimeKumaApi

KUMA_URL  = os.environ.get("KUMA_URL", "https://uptime-service-production.up.railway.app")
KUMA_USER = os.environ.get("KUMA_USER")
KUMA_PASS = os.environ.get("KUMA_PASS")

# slug -> (display title, env tag the monitor must carry)
PAGES = {
    "tms360-prod":  ("TMS 360 PRODUCTION", "prod"),
    "tms360-stage": ("TMS 360 STAGING",    "staging"),
    "tms360-dev":   ("TMS 360 DEV",        "dev"),
}

# Role groups + display order on the status page
ROLE_ORDER = ["core", "backend", "ops"]
OTHER = "other"


def main():
    if not (KUMA_USER and KUMA_PASS):
        sys.exit("set KUMA_USER and KUMA_PASS")

    with UptimeKumaApi(KUMA_URL) as api:
        api.login(KUMA_USER, KUMA_PASS)

        monitors = api.get_monitors()

        for slug, (title, env_tag) in PAGES.items():
            env_monitors = [
                m for m in monitors
                if any(t["name"] == env_tag for t in m.get("tags", []))
            ]

            by_role: dict[str, list] = {r: [] for r in ROLE_ORDER}
            by_role[OTHER] = []
            for m in env_monitors:
                role = next(
                    (t["name"] for t in m.get("tags", []) if t["name"] in ROLE_ORDER),
                    OTHER,
                )
                by_role[role].append(m)

            public_group_list = []
            for weight, role in enumerate(ROLE_ORDER + [OTHER], start=1):
                ms = by_role[role]
                if not ms:
                    continue
                public_group_list.append({
                    "name": role.upper(),
                    "weight": weight,
                    "monitorList": [{"id": m["id"]} for m in sorted(ms, key=lambda x: x["name"])],
                })

            api.save_status_page(
                slug=slug,
                title=title,
                description=f"{env_tag} environment · {len(env_monitors)} monitors",
                showTags=True,
                publicGroupList=public_group_list,
            )
            print(f"  + {slug}: {len(env_monitors)} monitors across {len(public_group_list)} groups "
                  f"({', '.join(g['name'] for g in public_group_list)})")


if __name__ == "__main__":
    main()
