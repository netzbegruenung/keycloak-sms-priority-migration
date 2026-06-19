#!/usr/bin/env python3
"""
Migrate SMS credential priority in Keycloak.

Targets users who have a 'mobile-number' (SMS) credential at position 1
(lowest priority value) AND at least one other 2FA method (otp, webauthn,
APP_CREDENTIAL). Moves the preferred non-SMS 2FA credential to first position
via the Keycloak Admin API so other methods appear before SMS.

Using the API (rather than writing to the DB directly) ensures Keycloak
invalidates its own cache immediately — users see the new order on their
first login after migration.

Two-run gradual rollout:
  Run 1 (first 500):  python migrate.py --realm <realm> --db-... --kc-url <url> --batch-size 500 --execute
  Run 2 (remainder):  python migrate.py --realm <realm> --db-... --kc-url <url> --batch-size 0 --execute

After run 1 the migrated users' preferred 2FA is at position 1 (SMS is no
longer first), so they are naturally excluded in run 2.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# Credential types that count as "another 2FA method"
OTHER_2FA_TYPES = ("otp", "webauthn", "APP_CREDENTIAL")

_SELECT_ELIGIBLE = """
SELECT
    c.id            AS credential_id,
    c.user_id,
    c.priority      AS current_priority,
    u.username,
    (SELECT c2.id
     FROM   credential c2
     WHERE  c2.user_id = c.user_id
       AND  c2.type = ANY(%(other_types)s)
     ORDER  BY c2.priority ASC
     LIMIT  1)      AS preferred_cred_id
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


def _kc_admin_token(kc_url, client_id, client_secret=None, admin_user=None, admin_password=None):
    if client_secret:
        body = (
            f"grant_type=client_credentials"
            f"&client_id={client_id}&client_secret={client_secret}"
        )
    else:
        body = (
            f"grant_type=password&client_id={client_id}"
            f"&username={admin_user}&password={admin_password}"
        )
    req = urllib.request.Request(
        f"{kc_url}/realms/master/protocol/openid-connect/token",
        data=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


def move_credential_to_first(kc_url, token, realm, user_id, credential_id):
    req = urllib.request.Request(
        f"{kc_url}/admin/realms/{realm}/users/{user_id}"
        f"/credentials/{credential_id}/moveToFirst",
        data=b"",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req):
        pass


def fetch_eligible(cur, realm_name, limit=None):
    sql = _SELECT_ELIGIBLE
    if limit:
        sql += f"LIMIT {limit}"
    cur.execute(sql, {"realm_name": realm_name, "other_types": list(OTHER_2FA_TYPES)})
    return cur.fetchall()


def dry_run_counts(conn, realm_name, batch_size):
    """Returns {"total": N, "effective": N}. Never modifies the database."""
    with conn.cursor() as cur:
        all_rows = fetch_eligible(cur, realm_name, limit=None)
    total = len(all_rows)
    effective = total if batch_size == 0 else min(total, batch_size)
    return {"total": total, "effective": effective}


def execute_migration(conn, realm_name, batch_size,
                      kc_url, client_id, client_secret=None,
                      admin_user=None, admin_password=None):
    """
    For each eligible user, moves their preferred non-SMS 2FA credential to first
    position via the Keycloak Admin API. Keycloak handles priority assignment and
    cache invalidation automatically.
    Returns {"updated": N, "rows": [...]}.
    """
    limit = batch_size if batch_size > 0 else None
    with conn.cursor() as cur:
        rows = fetch_eligible(cur, realm_name, limit=limit)
    if not rows:
        return {"updated": 0, "rows": []}
    token = _kc_admin_token(kc_url, client_id, client_secret, admin_user, admin_password)
    updated = 0
    for row in rows:
        move_credential_to_first(
            kc_url, token, realm_name,
            row["user_id"], row["preferred_cred_id"],
        )
        updated += 1
    return {"updated": updated, "rows": list(rows)}


def write_audit_log(rows):
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join("logs", f"migration-{ts}.jsonl")
    with open(path, "w") as fh:
        for row in rows:
            fh.write(
                json.dumps(
                    {
                        "user_id": row["user_id"],
                        "username": row["username"],
                        "sms_credential_id": row["credential_id"],
                        "promoted_credential_id": row["preferred_cred_id"],
                    }
                )
                + "\n"
            )
    return path


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
        "--kc-url",
        default=None,
        help="Keycloak base URL, e.g. http://localhost:8080 (required for --execute)",
    )
    p.add_argument(
        "--kc-client-id",
        default="admin-cli",
        help="Client ID for Keycloak authentication (default: admin-cli)",
    )
    p.add_argument(
        "--kc-client-secret",
        default=os.environ.get("KC_CLIENT_SECRET"),
        help="Client secret — enables client_credentials grant (recommended for production). "
             "Or set KC_CLIENT_SECRET env var.",
    )
    p.add_argument("--kc-admin-user", default="admin",
        help="Admin username for password grant (dev fallback, fails with TOTP)")
    p.add_argument(
        "--kc-admin-password",
        default=os.environ.get("KC_ADMIN_PASSWORD"),
        help="Admin password for password grant (dev fallback). Or set KC_ADMIN_PASSWORD env var.",
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


def main():
    args = parse_args()

    try:
        conn = connect(args)
    except psycopg2.OperationalError as exc:
        print(f"ERROR: Could not connect to database: {exc}", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run and not args.kc_url:
        print("ERROR: --kc-url is required when using --execute", file=sys.stderr)
        sys.exit(1)

    try:
        if args.dry_run:
            counts = dry_run_counts(conn, args.realm, args.batch_size)
            print("DRY RUN — no changes made")
            print(f"  Total eligible:        {counts['total']}")
            if args.batch_size > 0:
                print(f"  This run (limit {args.batch_size}): {counts['effective']}")
            else:
                print(f"  This run (no limit):   {counts['effective']}")
        else:
            result = execute_migration(
                conn, args.realm, args.batch_size,
                args.kc_url, args.kc_client_id, args.kc_client_secret,
                args.kc_admin_user, args.kc_admin_password,
            )
            if result["updated"] == 0:
                print("No eligible users found. Nothing to do.")
            else:
                log_path = write_audit_log(result["rows"])
                print(f"Promoted preferred 2FA to first position for {result['updated']} user(s).")
                print(f"Audit log: {log_path}")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
