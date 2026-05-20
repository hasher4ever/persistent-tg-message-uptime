#!/usr/bin/env python3
"""Repoint DB/Redis env vars on a Railway service from literal values to
service references (${{Postgres.PGHOST}} etc.), so connection targets
auto-track the environment instead of going stale on credential rotation.

Dry-run by default. Pass --apply to mutate. Never prints literal values.

Usage:
    # Dry-run (safe — only reads)
    python3 scripts/repoint_to_refs.py files staging

    # Custom Postgres/Redis service names (defaults: Postgres / redis-production)
    python3 scripts/repoint_to_refs.py files staging --postgres postgres-prod

    # Actually patch the values
    python3 scripts/repoint_to_refs.py files staging --apply
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

PROJECT_ID = "23fdc9db-17f9-478c-b98c-3695e0d1ba4d"  # TMS360
REF_RE = re.compile(r"\$\{\{[^}]+\}\}")


def railway_json(cmd: list[str]) -> dict | list:
    """Run a railway CLI command expecting JSON output."""
    r = subprocess.run(
        cmd + ["--json", "--project", PROJECT_ID],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stderr}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def get_service_id(name: str, env: str) -> str | None:
    services = railway_json([
        "railway", "service", "list",
        "--environment", env,
    ])
    for s in services:
        if s["name"] == name:
            return s["id"]
    return None


def build_mappings(postgres: str, redis: str) -> dict[str, str]:
    return {
        "DB_HOST":        f"${{{{{postgres}.PGHOST}}}}",
        "DB_PORT":        f"${{{{{postgres}.PGPORT}}}}",
        "DB_USERNAME":    f"${{{{{postgres}.PGUSER}}}}",
        "DB_PASSWORD":    f"${{{{{postgres}.PGPASSWORD}}}}",
        "DB_DATABASE":    f"${{{{{postgres}.PGDATABASE}}}}",
        "REDIS_HOST":     f"${{{{{redis}.RAILWAY_PRIVATE_DOMAIN}}}}",
        "REDIS_PORT":     f"${{{{{redis}.REDIS_PORT}}}}",
        "REDIS_PASSWORD": f"${{{{{redis}.REDIS_PASSWORD}}}}",
    }


def classify(value: str | None) -> str:
    if not value:
        return "empty"
    return "ref" if REF_RE.search(str(value)) else "literal"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("service", help="Railway service name (e.g. files)")
    ap.add_argument("env", help="Railway env (production | staging | development)")
    ap.add_argument("--postgres", default="Postgres",
                    help="Postgres service name to reference (default: Postgres)")
    ap.add_argument("--redis", default="redis-production",
                    help="Redis service name to reference (default: redis-production)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually run `railway variable set`. Default is dry-run.")
    args = ap.parse_args()

    service_id = get_service_id(args.service, args.env)
    if not service_id:
        sys.exit(f"service {args.service!r} not found in env {args.env!r}")

    current = railway_json([
        "railway", "variable", "list",
        "--service", service_id, "--environment", args.env,
    ])
    if not isinstance(current, dict):
        sys.exit(f"unexpected variable list shape: {type(current).__name__}")

    mappings = build_mappings(args.postgres, args.redis)

    plan = []
    for key, target_ref in mappings.items():
        cur = current.get(key)
        cur_class = classify(cur)
        if cur == target_ref:
            continue  # already correct
        plan.append((key, cur_class, target_ref))

    print(f"\nservice={args.service!r}  env={args.env!r}  service_id={service_id}")
    print(f"postgres-ref={args.postgres!r}  redis-ref={args.redis!r}\n")

    if not plan:
        print("✓ nothing to change — all 8 connection vars already point at the right refs")
        return

    print(f"plan: {len(plan)} variable(s) to repoint")
    print("(current values not printed — secret-safe)\n")
    width = max(len(k) for k, _, _ in plan)
    for key, cur_class, target_ref in plan:
        print(f"  {key:<{width}}  was: {cur_class:7s}  →  {target_ref}")

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to actually patch.")
        return

    print("\napplying...")
    failed = 0
    for key, _, target_ref in plan:
        # --skip-deploys so we patch all 8 vars in one shot before triggering rebuild
        r = subprocess.run([
            "railway", "variable", "--set", f"{key}={target_ref}",
            "--service", service_id,
            "--environment", args.env,
            "--project", PROJECT_ID,
            "--skip-deploys",
        ], capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  ✓ {key}")
        else:
            failed += 1
            print(f"  ✗ {key}: {r.stderr.strip()}")

    if failed:
        print(f"\n{failed} variable(s) failed. Service was NOT redeployed.")
    else:
        print(f"\n✓ all {len(plan)} variables patched.")
        print("Trigger a redeploy of the service in Railway UI to pick up changes.")


if __name__ == "__main__":
    main()
