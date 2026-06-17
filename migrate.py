#!/usr/bin/env python3
"""
Migrate SMS credential priority in Keycloak's PostgreSQL database.

Targets users who have a 'mobile-number' (SMS) credential at position 1
(lowest priority value) AND at least one other 2FA method (otp, webauthn,
APP_CREDENTIAL). Raises the SMS priority so other methods appear first.

Two-run gradual rollout:
  Run 1 (first 500):  python migrate.py --realm <realm> --db-... --batch-size 500 --execute
  Run 2 (remainder):  python migrate.py --realm <realm> --db-... --batch-size 0 --execute

After run 1 the migrated users' SMS sits at priority 99 (above their other
credentials at 10/20/…), so condition "SMS is at position 1" is no longer
satisfied for them — they are naturally excluded in run 2.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# Credential types that count as "another 2FA method"
OTHER_2FA_TYPES = ("otp", "webauthn", "APP_CREDENTIAL")

# Base query: SMS at position 1, user has at least one other 2FA.
# The final SELECT is used in dry-run (no LIMIT) to get the true total.
# The same query with LIMIT is used for execute mode.
_SELECT_ELIGIBLE = """
SELECT
    c.id            AS credential_id,
    c.user_id,
    c.priority      AS current_priority,
    u.username
FROM   credential c
JOIN   user_entity u ON c.user_id = u.id
JOIN   realm r       ON u.realm_id = r.id
WHERE  c.type = 'mobile-number'
  AND  r.name = %(realm_name)s
  -- SMS is currently first: no credential for this user has a lower priority
  AND  NOT EXISTS (
           SELECT 1 FROM credential c3
           WHERE  c3.user_id = c.user_id
             AND  c3.priority < c.priority
       )
  -- user has at least one other 2FA method
  AND  EXISTS (
           SELECT 1 FROM credential c2
           WHERE  c2.user_id = c.user_id
             AND  c2.type = ANY(%(other_types)s)
       )
ORDER BY c.user_id
"""

_UPDATE_PRIORITY = """
UPDATE credential
SET    priority = %(target_priority)s
WHERE  id = ANY(%(credential_ids)s)
"""


def parse_args():
    p = argparse.ArgumentParser(
        description="Raise SMS credential priority in Keycloak so other 2FA methods appear first."
    )
    p.add_argument("--realm", required=True, help="Keycloak realm name")
    p.add_argument("--db-host", default="localhost")
    p.add_argument("--db-port", type=int, default=5432)
    p.add_argument("--db-name", default="keycloak")
    p.add_argument("--db-user", default="keycloak")
    p.add_argument(
        "--db-password",
        default=os.environ.get("DB_PASSWORD"),
        help="Database password (or set DB_PASSWORD env var)",
    )
    p.add_argument(
        "--priority",
        type=int,
        default=99,
        help="Target priority value for SMS credentials (default: 99)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Max users to migrate in this run; 0 = no limit (default: 500)",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Show counts only, make no changes (default)",
    )
    mode.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help="Actually apply the migration",
    )
    return p.parse_args()


def connect(args):
    return psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def fetch_eligible(cur, realm_name, limit=None):
    sql = _SELECT_ELIGIBLE
    if limit:
        sql += f"LIMIT {limit}"
    cur.execute(sql, {"realm_name": realm_name, "other_types": list(OTHER_2FA_TYPES)})
    return cur.fetchall()


def write_audit_log(rows, target_priority):
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join("logs", f"migration-{ts}.jsonl")
    with open(path, "w") as fh:
        for row in rows:
            fh.write(
                json.dumps(
                    {
                        "credential_id": row["credential_id"],
                        "user_id": row["user_id"],
                        "username": row["username"],
                        "old_priority": row["current_priority"],
                        "new_priority": target_priority,
                    }
                )
                + "\n"
            )
    return path


def main():
    args = parse_args()

    try:
        conn = connect(args)
    except psycopg2.OperationalError as exc:
        print(f"ERROR: Could not connect to database: {exc}", file=sys.stderr)
        sys.exit(1)

    with conn:
        with conn.cursor() as cur:
            if args.dry_run:
                # Always query without LIMIT in dry-run to show true total
                all_rows = fetch_eligible(cur, args.realm)
                total = len(all_rows)
                effective = total if args.batch_size == 0 else min(total, args.batch_size)
                print("DRY RUN — no changes made")
                print(f"  Total eligible:        {total}")
                if args.batch_size > 0:
                    print(f"  This run (limit {args.batch_size}): {effective}")
                else:
                    print(f"  This run (no limit):   {effective}")
            else:
                limit = args.batch_size if args.batch_size > 0 else None
                rows = fetch_eligible(cur, args.realm, limit=limit)

                if not rows:
                    print("No eligible users found. Nothing to do.")
                    return

                credential_ids = [row["credential_id"] for row in rows]
                cur.execute(
                    _UPDATE_PRIORITY,
                    {
                        "target_priority": args.priority,
                        "credential_ids": credential_ids,
                    },
                )
                updated = cur.rowcount
                log_path = write_audit_log(rows, args.priority)
                print(f"Updated {updated} SMS credential(s) to priority {args.priority}.")
                print(f"Audit log: {log_path}")

    conn.close()


if __name__ == "__main__":
    main()
