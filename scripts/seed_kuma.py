#!/usr/bin/env python3
"""Seed Kuma monitors via Socket.IO API (uptime-kuma-api).

Reliable alternative to JSON backup-import — the library knows Kuma's exact
field names per version, surfaces real errors instead of swallowing them,
and is idempotent (skips monitors that already exist by name).

Usage:
    export KUMA_URL='https://uptime-service-production.up.railway.app'
    export KUMA_USER='tms360uptimestatus'
    export KUMA_PASS='your-password'
    python3 scripts/seed_kuma.py

Re-run safely — existing monitors are skipped, new ones added.

Source of truth for what to monitor: `railway service list` for each env.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from uptime_kuma_api import UptimeKumaApi, MonitorType

PROJECT_ID = "23fdc9db-17f9-478c-b98c-3695e0d1ba4d"  # TMS360

KUMA_URL  = os.environ.get("KUMA_URL", "https://uptime-service-production.up.railway.app")
KUMA_USER = os.environ.get("KUMA_USER")
KUMA_PASS = os.environ.get("KUMA_PASS")

ENVS = [
    # env_name (railway),  display_prefix, tag_name, color
    ("production",  "PROD",    "prod",    "#22c55e"),
    ("staging",     "STAGING", "staging", "#f59e0b"),
    ("development", "DEV",     "dev",     "#3b82f6"),
]

# Old single-letter prefixes → new full prefix. Used to migrate existing monitors.
LEGACY_PREFIX_MAP = {"P": "PROD", "S": "STAGING", "D": "DEV"}

SKIP_NAME_PATTERNS = [
    r"^postgres", r"^redis", r"^kafka-production$", r"^ClickHouse",
    r"clickhouse-log-cleaner", r"^persistent-tg-uptime", r"^Uptime service$",
]
SUFFIX_NOISE = re.compile(r"-(development|staging|production|prod|railway)$")

ROLES = {
    "core":    {"apollo", "auth", "files", "frontend", "landing"},
    "backend": {"accounting", "audit", "broker-auth", "fmcsa", "load",
                "mediator", "messaging", "notification", "rc-processor",
                "teams", "tracking"},
    "ops":     {"kafka-ui", "login-dashboard", "mailhog", "sftpgo", "tg-notifier"},
}
ROLE_COLORS = {"core": "#dc2626", "backend": "#7c3aed", "ops": "#64748b"}


def fetch_services(env: str) -> list[dict]:
    r = subprocess.run(
        ["railway", "service", "list", "--json",
         "--project", PROJECT_ID, "--environment", env],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)


def normalize(name: str) -> str:
    return SUFFIX_NOISE.sub("", name)


def role_for(clean: str) -> str | None:
    for role, members in ROLES.items():
        if clean in members:
            return role
    return None


def should_skip(name: str) -> bool:
    return any(re.search(p, name, re.IGNORECASE) for p in SKIP_NAME_PATTERNS)


def get_or_create_tag(api, cache: dict, name: str, color: str) -> int:
    if name in cache:
        return cache[name]
    existing = {t["name"]: t["id"] for t in api.get_tags()}
    if name in existing:
        cache[name] = existing[name]
        return existing[name]
    r = api.add_tag(name=name, color=color)
    cache[name] = r["id"]
    return r["id"]


def main():
    if not (KUMA_USER and KUMA_PASS):
        sys.exit("set KUMA_USER and KUMA_PASS env vars first")

    desired = []  # (monitor_name, url, env_tag, env_color, role_or_none)
    for env_name, env_short, env_tag, env_color in ENVS:
        for svc in fetch_services(env_name):
            name = svc.get("name", "")
            url = svc.get("url")
            if not url or should_skip(name):
                continue
            clean = normalize(name)
            desired.append((f"[{env_short}] {clean}", url, env_tag, env_color, role_for(clean)))

    print(f"target inventory: {len(desired)} monitors", flush=True)

    with UptimeKumaApi(KUMA_URL) as api:
        api.login(KUMA_USER, KUMA_PASS)

        existing_by_name = {m["name"]: m["id"] for m in api.get_monitors()}
        print(f"kuma already has: {len(existing_by_name)} monitors", flush=True)

        # Migration: rename any monitors using legacy single-letter prefixes
        # (e.g. "[P] accounting" → "[PROD] accounting") in place.
        renamed = 0
        for old_name, mid in list(existing_by_name.items()):
            m = re.match(r"^\[([PSD])\] (.+)$", old_name)
            if not m:
                continue
            new_prefix = LEGACY_PREFIX_MAP[m.group(1)]
            new_name = f"[{new_prefix}] {m.group(2)}"
            if new_name == old_name or new_name in existing_by_name:
                continue
            api.edit_monitor(mid, name=new_name)
            existing_by_name[new_name] = mid
            del existing_by_name[old_name]
            renamed += 1
            print(f"  → renamed {old_name} → {new_name}", flush=True)
        if renamed:
            print(f"migrated {renamed} legacy prefixes\n", flush=True)

        tag_cache: dict[str, int] = {}
        created = skipped = 0
        for mon_name, url, env_tag, env_color, role in desired:
            if mon_name in existing_by_name:
                skipped += 1
                continue
            r = api.add_monitor(
                type=MonitorType.HTTP,
                name=mon_name,
                url=url,
                method="GET",
                interval=60,
                retryInterval=60,
                maxretries=2,
                timeout=30,
                maxredirects=10,
                accepted_statuscodes=["200-299", "300-399", "400-499"],
            )
            monitor_id = r["monitorID"]

            env_tag_id = get_or_create_tag(api, tag_cache, env_tag, env_color)
            api.add_monitor_tag(tag_id=env_tag_id, monitor_id=monitor_id)
            if role:
                role_tag_id = get_or_create_tag(api, tag_cache, role, ROLE_COLORS[role])
                api.add_monitor_tag(tag_id=role_tag_id, monitor_id=monitor_id)

            created += 1
            print(f"  +{mon_name}", flush=True)

        print(f"\ndone: created={created} skipped={skipped}", flush=True)


if __name__ == "__main__":
    main()
