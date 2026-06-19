#!/usr/bin/env python3
"""
Dev seed script — manages test data for manual migration testing.

On first run it downloads the plugin JARs into dev/providers/ so Keycloak
loads them at startup, creates a test realm with users and credentials, and
configures the browser flow to require 2FA for users who have it set up.

Modes:
  (default)    seed realm + users + credentials
  --reset      restore credential priorities to pre-migration state
  --clean      delete the test realm and all its data via the Keycloak API
  --jars-only  download plugin JARs and exit

Usage (from repo root):
  docker compose up -d
  uv run python dev/seed.py --db-password keycloak
"""

import argparse
import json
import os
import sys
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Plugin JARs
# ---------------------------------------------------------------------------

_PLUGIN_VERSION = "v26.6.5"
_RELEASE_BASE = (
    f"https://github.com/netzbegruenung/keycloak-mfa-plugins"
    f"/releases/download/{_PLUGIN_VERSION}"
)
_JARS = [
    f"netzbegruenung.sms-authenticator-{_PLUGIN_VERSION}.jar",
    f"netzbegruenung.app-authenticator-{_PLUGIN_VERSION}.jar",
    f"netzbegruenung.enforce-mfa-{_PLUGIN_VERSION}.jar",
]
_PROVIDERS_DIR = os.path.join(os.path.dirname(__file__), "providers")

# ---------------------------------------------------------------------------
# Test-user credential setup
# ---------------------------------------------------------------------------

_TOTP_SECRET = "JBSWY3DPEHPK3PXP"

_OTP_SECRET_DATA = json.dumps({"value": _TOTP_SECRET})
_OTP_CREDENTIAL_DATA = json.dumps(
    {"subType": "totp", "digits": 6, "period": 30, "algorithm": "HmacSHA1"}
)

_SMS_SECRET_DATA = json.dumps({})
_SMS_CREDENTIAL_DATA = json.dumps({"mobilePhoneNumber": "+49 30 1234 5678"})

# (username, [(type, priority, secret_data, credential_data, user_label)])
_TEST_USERS = [
    (
        "user_eligible_otp",
        [
            ("mobile-number", 10, _SMS_SECRET_DATA, _SMS_CREDENTIAL_DATA, "+49 30 *** 678"),
            ("otp",           20, _OTP_SECRET_DATA, _OTP_CREDENTIAL_DATA, "TOTP App"),
        ],
    ),
    (
        "user_eligible_webauthn",
        [
            ("mobile-number", 10, _SMS_SECRET_DATA, _SMS_CREDENTIAL_DATA, "+49 30 *** 678"),
            ("webauthn",      20, "{}", "{}", "Security Key"),
        ],
    ),
    (
        "user_sms_only",
        [
            ("mobile-number", 10, _SMS_SECRET_DATA, _SMS_CREDENTIAL_DATA, "+49 30 *** 678"),
        ],
    ),
    (
        "user_otp_first",
        [
            ("otp",           10, _OTP_SECRET_DATA, _OTP_CREDENTIAL_DATA, "TOTP App"),
            ("mobile-number", 20, _SMS_SECRET_DATA, _SMS_CREDENTIAL_DATA, "+49 30 *** 678"),
        ],
    ),
]

_DEFAULT_PASSWORD = "pass"

_DEV_CLIENT_ID = "migration-tool"
_DEV_CLIENT_SECRET = "dev-migration-secret"

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _request(method, url, token=None, data=None, expected=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        if e.code in expected:
            return e.code, None
        raise


def _get(url, token):
    _, body = _request("GET", url, token=token)
    return body


def _post(url, token, data):
    status, body = _request("POST", url, token=token, data=data, expected=(200, 201, 204, 409))
    return status, body


def _put(url, token, data):
    status, _ = _request("PUT", url, token=token, data=data, expected=(200, 201, 204))
    return status


def _delete(url, token):
    _request("DELETE", url, token=token, expected=(200, 204, 404))


def _get_admin_token(kc_url, admin_user, admin_password):
    data = (
        f"grant_type=password&client_id=admin-cli"
        f"&username={admin_user}&password={admin_password}"
    ).encode()
    req = urllib.request.Request(
        f"{kc_url}/realms/master/protocol/openid-connect/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


# ---------------------------------------------------------------------------
# JAR download
# ---------------------------------------------------------------------------


def download_jars():
    os.makedirs(_PROVIDERS_DIR, exist_ok=True)
    downloaded = False
    for jar in _JARS:
        dest = os.path.join(_PROVIDERS_DIR, jar)
        if os.path.exists(dest):
            print(f"  already present: {jar}")
            continue
        url = f"{_RELEASE_BASE}/{jar}"
        print(f"  downloading {jar} …", end=" ", flush=True)
        urllib.request.urlretrieve(url, dest)
        print("done")
        downloaded = True
    if downloaded:
        print("  NOTE: restart Keycloak if it is already running so it loads the new JARs.")


# ---------------------------------------------------------------------------
# Wait for Keycloak
# ---------------------------------------------------------------------------


def wait_for_keycloak(kc_url, timeout=180):
    print(f"Waiting for Keycloak at {kc_url} …", end="", flush=True)
    deadline = time.time() + timeout
    health_url = kc_url.replace(":8080", ":9000") + "/health/ready"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=3) as r:
                if r.status == 200:
                    print(" ready.")
                    return
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(3)
    print()
    print("ERROR: Keycloak did not become ready in time.", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def create_realm(kc_url, token, realm):
    status, _ = _post(
        f"{kc_url}/admin/realms",
        token,
        {"realm": realm, "enabled": True, "displayName": "Dev migration test"},
    )
    if status == 409:
        print(f"  realm '{realm}' already exists — skipping creation")
    else:
        print(f"  created realm '{realm}'")


def get_user_id(kc_url, token, realm, username):
    users = _get(f"{kc_url}/admin/realms/{realm}/users?username={username}&exact=true", token)
    if not users:
        return None
    return users[0]["id"]


def create_user(kc_url, token, realm, username):
    existing = get_user_id(kc_url, token, realm, username)
    if existing:
        print(f"  user '{username}' already exists")
        user_id = existing
    else:
        status, _ = _post(
            f"{kc_url}/admin/realms/{realm}/users",
            token,
            {"username": username, "enabled": True},
        )
        if status not in (201, 409):
            print(f"  WARNING: unexpected status {status} creating user '{username}'")

        user_id = get_user_id(kc_url, token, realm, username)
        if user_id is None:
            print(f"  WARNING: could not resolve user_id for '{username}', skipping password reset")
            return None

    try:
        status = _put(
            f"{kc_url}/admin/realms/{realm}/users/{user_id}/reset-password",
            token,
            {"type": "password", "value": _DEFAULT_PASSWORD, "temporary": False},
        )
        if status != 204:
            print(f"  WARNING: unexpected status {status} setting password for '{username}'")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  ERROR: failed to set password for '{username}': HTTP {e.code} – {body}")

    print(f"  {'updated' if existing else 'created'} user '{username}'")
    return user_id


def insert_credentials(db_conn, user_id, credentials):
    created_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with db_conn.cursor() as cur:
        # Remove existing credentials for this user before inserting
        cur.execute("DELETE FROM credential WHERE user_id = %s AND type <> 'password'", (user_id,))
        for cred_type, priority, secret_data, cred_data, label in credentials:
            cur.execute(
                """
                INSERT INTO credential
                    (id, user_id, type, user_label, priority, created_date,
                     secret_data, credential_data, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
                """,
                (
                    str(uuid.uuid4()),
                    user_id,
                    cred_type,
                    label,
                    priority,
                    created_ms,
                    secret_data,
                    cred_data,
                ),
            )
    db_conn.commit()


def setup_service_account(kc_url, token, realm):
    """Provision migration-tool service account client in master realm for local dev."""
    # Create client (409 = already exists, proceed anyway)
    status, _ = _post(
        f"{kc_url}/admin/realms/master/clients",
        token,
        {
            "clientId": _DEV_CLIENT_ID,
            "protocol": "openid-connect",
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "directAccessGrantsEnabled": False,
            "authorizationServicesEnabled": False,
        },
    )
    if status == 409:
        print(f"  client '{_DEV_CLIENT_ID}' already exists in master realm")
    else:
        print(f"  created client '{_DEV_CLIENT_ID}' in master realm")

    # Get client UUID
    clients = _get(f"{kc_url}/admin/realms/master/clients?clientId={_DEV_CLIENT_ID}", token)
    client_uuid = clients[0]["id"]

    # Fix the secret to the known dev value (idempotent)
    client_rep = _get(f"{kc_url}/admin/realms/master/clients/{client_uuid}", token)
    client_rep["secret"] = _DEV_CLIENT_SECRET
    _put(f"{kc_url}/admin/realms/master/clients/{client_uuid}", token, client_rep)

    # Get {realm}-realm client UUID in master
    realm_clients = _get(
        f"{kc_url}/admin/realms/master/clients?clientId={realm}-realm", token
    )
    realm_client_uuid = realm_clients[0]["id"]

    # Get manage-users role from {realm}-realm client
    role = _get(
        f"{kc_url}/admin/realms/master/clients/{realm_client_uuid}/roles/manage-users",
        token,
    )

    # Get service account user for migration-tool
    sa_user = _get(
        f"{kc_url}/admin/realms/master/clients/{client_uuid}/service-account-user",
        token,
    )

    # Assign manage-users role (idempotent)
    _post(
        f"{kc_url}/admin/realms/master/users/{sa_user['id']}/role-mappings/clients/{realm_client_uuid}",
        token,
        [role],
    )
    print(f"  assigned manage-users ({realm}-realm) to service account")


def seed(args):
    print("\n1. Downloading plugin JARs …")
    download_jars()

    print("\n2. Waiting for Keycloak …")
    wait_for_keycloak(args.kc_url)

    print("\n3. Authenticating …")
    token = _get_admin_token(args.kc_url, "admin", "pass")

    print(f"\n4. Creating realm '{args.realm}' …")
    create_realm(args.kc_url, token, args.realm)

    print(f"\n5. Creating test users …")
    db_conn = psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )

    try:
        for username, credentials in _TEST_USERS:
            user_id = create_user(args.kc_url, token, args.realm, username)
            insert_credentials(db_conn, user_id, credentials)
            cred_summary = ", ".join(f"{t}@{p}" for t, p, *_ in credentials)
            print(f"  credentials for '{username}': {cred_summary}")
    finally:
        db_conn.close()

    print(f"\n6. Provisioning service account …")
    setup_service_account(args.kc_url, token, args.realm)

    print(f"""
Done! Test users created in realm '{args.realm}' with password '{_DEFAULT_PASSWORD}'.

Eligible for migration (SMS at position 1, other 2FA present):
  user_eligible_otp      — mobile-number(10), otp(20)
  user_eligible_webauthn — mobile-number(10), webauthn(20)

Not eligible:
  user_sms_only  — mobile-number(10) only
  user_otp_first — otp(10), mobile-number(20)

TOTP test secret (add to any authenticator app):
  Secret : {_TOTP_SECRET}
  Type   : Time-based (TOTP), SHA-1, 6 digits, 30 s

SMS OTP codes are printed to Keycloak logs (no gateway configured):
  docker compose logs -f keycloak | grep -i sms

Account Console (see credential order):
  http://localhost:8080/realms/{args.realm}/account/#/security/signingin

Run the migration:
  KC_CLIENT_SECRET={_DEV_CLIENT_SECRET} uv run python migrate.py --realm {args.realm} --db-password keycloak
""")


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def reset(args):
    print("Resetting credential priorities to pre-migration state …")

    print("  authenticating …")
    wait_for_keycloak(args.kc_url)
    token = _get_admin_token(args.kc_url, "admin", "pass")

    db_conn = psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        for username, credentials in _TEST_USERS:
            user_id = get_user_id(args.kc_url, token, args.realm, username)
            if not user_id:
                print(f"  WARNING: user '{username}' not found — skipping")
                continue
            insert_credentials(db_conn, user_id, credentials)
            print(f"  reset '{username}'")
    finally:
        db_conn.close()

    print("Reset complete. You can run the migration again.")


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------


def clean(args):
    print(f"Deleting realm '{args.realm}' …")
    wait_for_keycloak(args.kc_url)
    token = _get_admin_token(args.kc_url, "admin", "pass")
    _delete(f"{args.kc_url}/admin/realms/{args.realm}", token)
    print(f"Realm '{args.realm}' deleted (users and credentials removed).")
    print("Plugin JARs in dev/providers/ are kept.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(
        description="Manage dev test data for the SMS priority migration script."
    )
    p.add_argument("--realm", default="dev")
    p.add_argument("--kc-url", default="http://localhost:8080")
    p.add_argument("--db-host", default="localhost")
    p.add_argument("--db-port", type=int, default=5432)
    p.add_argument("--db-name", default="keycloak")
    p.add_argument("--db-user", default="keycloak")
    p.add_argument("--db-password", default="keycloak")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--reset", action="store_true", help="Restore original credential priorities")
    mode.add_argument("--clean", action="store_true", help="Delete the test realm and all its data")
    mode.add_argument("--jars-only", action="store_true", help="Download plugin JARs and exit")
    return p.parse_args()


def main():
    args = parse_args()
    if args.jars_only:
        print("Downloading plugin JARs …")
        download_jars()
        return
    if args.reset:
        reset(args)
        return
    if args.clean:
        clean(args)
        return
    seed(args)


if __name__ == "__main__":
    main()
