# keycloak-sms-priority-migration

Reorders 2FA credentials in Keycloak so that non-SMS methods (OTP, WebAuthn,
app) appear before SMS when a user has multiple methods configured.

For each eligible user the migration calls the Keycloak Admin API to move the
user's preferred non-SMS credential to first position. Keycloak updates
credential priorities and invalidates its own cache, so the new order is
visible to users on their very next login.

Only users where **SMS is currently at position 1** and who have **at least one
other 2FA method** are affected. Users with SMS only, or whose SMS is already
not first, are left untouched.

## Setup

```bash
uv sync
```

## Authentication

The migration calls the Keycloak Admin REST API and needs a token from the
`master` realm. Two options:

### Recommended: service account

A service account is a clientcredentials-based identity — no human login, no
interactive prompts. The client authenticates with a secret instead of a
username and password.

#### Step 1 — Create a dedicated client

1. Open the Keycloak Admin Console and switch to the **master** realm using
   the realm dropdown in the top-left corner.
2. Go to **Clients** → **Create client**.
3. Set **Client type** to `OpenID Connect` and choose a **Client ID**
   (e.g. `migration-tool`). Click **Next**.
4. On the *Capability config* screen:
   - Turn **Client authentication** ON.
   - Turn **Authorization** OFF.
   - Under *Authentication flow* uncheck everything and check only
     **Service accounts roles**.
   - Click **Next**, then **Save**.

#### Step 2 — Copy the client secret

Open the **Credentials** tab of the newly created client and copy the
**Client secret**. Store it in the `KC_CLIENT_SECRET` environment variable
when running the migration.

#### Step 3 — Assign the manage-users role

The service account needs permission to manage user credentials in the
target realm. In Keycloak each realm is represented by a dedicated client
inside the master realm (named `<realm-name>-realm`), and that client exposes
fine-grained management roles.

1. On the client's detail page open the **Service accounts roles** tab.
2. Click **Assign role**.
3. In the dialog change the filter from *"Filter by realm roles"* to
   **"Filter by clients"**.
4. Search for `<your-target-realm>-realm` (e.g. `myrealm-realm`).
5. Select **manage-users** and click **Assign**.

#### Running the migration

```bash
KC_CLIENT_SECRET=<secret> uv run python migrate.py \
  --realm <realm-name> \
  --kc-url https://keycloak.example.com \
  --kc-client-id migration-tool \
  ...
```

### Alternative: admin user + password (dev / testing)

Pass `--kc-admin-user` and `--kc-admin-password` (or `KC_ADMIN_PASSWORD`).
Uses `admin-cli` with a direct grant.

## Usage

```
uv run python migrate.py \
  --realm <realm-name> \
  --db-host <host> \
  --db-port 5432 \
  --db-name keycloak \
  --db-user keycloak \
  [--db-password <pw>]          # or export DB_PASSWORD=<pw>
  [--kc-url <url>]              # default: http://localhost:8080
  [--kc-client-id <id>]         # service account client ID (default: migration-tool)
  [--kc-client-secret <secret>] # or export KC_CLIENT_SECRET=<secret>
  [--kc-admin-user <user>]      # dev fallback (default: admin)
  [--kc-admin-password <pw>]    # dev fallback, or export KC_ADMIN_PASSWORD=<pw>
  [--batch-size 500]            # max users per run; 0 = no limit (default: 500)
  [--username <name>]           # migrate only this user (mutually exclusive with --batch-size)
  [--dry-run | --execute]       # dry-run is the default
```

## Gradual rollout (two runs)

### 1. Dry-run first — check the numbers

```bash
uv run python migrate.py --realm myrealm \
  --db-host db.example.com --db-name keycloak --db-user keycloak \
  --batch-size 500
```

Output example:
```
DRY RUN — no changes made
  Total eligible:        1243
  This run (limit 500):   500
```

### 2. Run 1 — first 500 users

```bash
KC_CLIENT_SECRET=<secret> uv run python migrate.py --realm myrealm \
  --db-host db.example.com --db-name keycloak --db-user keycloak \
  --kc-url https://keycloak.example.com --kc-client-id migration-tool \
  --batch-size 500 --execute
```

Monitor for errors. Re-run in dry-run mode to confirm the remaining eligible
count dropped to ~743 (total − 500).

### 3. Run 2 — all remaining users

```bash
KC_CLIENT_SECRET=<secret> uv run python migrate.py --realm myrealm \
  --db-host db.example.com --db-name keycloak --db-user keycloak \
  --kc-url https://keycloak.example.com --kc-client-id migration-tool \
  --batch-size 0 --execute
```

A final dry-run should report `Total eligible: 0`.

## Audit logs

Each `--execute` run writes a JSON-lines file to `logs/migration-<timestamp>.jsonl`:

```json
{"user_id": "...", "username": "alice", "sms_credential_id": "...", "promoted_credential_id": "..."}
```

Log files are gitignored.

## Production deployment

Releases are published to
[GitHub Releases](https://github.com/netzbegruenung/keycloak-sms-priority-migration/releases)
as a Python wheel. Copy the `.whl` asset URL from the latest release, then:

### 1. One-time setup (run as root)

```bash
python3 -m venv /root/migrate-env
/root/migrate-env/bin/pip install <wheel-url>
```

### 2. Set secrets (not stored on disk or in history)

```bash
export KC_CLIENT_SECRET='<client-secret>'
export DB_PASSWORD='<db-password>'
```

### 3. Dry-run — verify eligible count

```bash
/root/migrate-env/bin/migrate \
  --realm <realm> \
  --db-host <host> --db-name keycloak --db-user keycloak \
  --kc-url https://keycloak.example.com
```

### 4. Pilot run — your own account first

```bash
/root/migrate-env/bin/migrate \
  --realm <realm> \
  --db-host <host> --db-name keycloak --db-user keycloak \
  --kc-url https://keycloak.example.com \
  --username <your-username> --execute
```

Log in to the Account Console and confirm the credential order changed before proceeding.

### 5. Batch 1 — first 500 users

```bash
/root/migrate-env/bin/migrate \
  --realm <realm> \
  --db-host <host> --db-name keycloak --db-user keycloak \
  --kc-url https://keycloak.example.com \
  --batch-size 500 --execute
```

Dry-run again to confirm the eligible count dropped by ~500, then continue.

### 6. Batch 2 — all remaining users

```bash
/root/migrate-env/bin/migrate \
  --realm <realm> \
  --db-host <host> --db-name keycloak --db-user keycloak \
  --kc-url https://keycloak.example.com \
  --batch-size 0 --execute
```

A final dry-run should report `Total eligible: 0`. Audit logs are written to
`logs/migration-<timestamp>.jsonl` in the working directory.

### Publishing a new release

```bash
git tag v1.0.0
git push origin v1.0.0
```

The GitHub Actions workflow runs the test suite, builds the wheel, and attaches it to the release automatically.

## Tests

The test suite uses an in-memory DuckDB database — no Docker or PostgreSQL needed.

```bash
uv sync --extra test
uv run pytest
```

## Local dev environment

Run a full Keycloak 26.6.3 + PostgreSQL stack locally with pre-seeded test
users so you can verify the migration end-to-end.

**Prerequisites:** Docker, Docker Compose, `uv`

### 1. Start the stack and seed test data

```bash
# Install dependencies (if not done yet)
uv sync

# Start Keycloak + PostgreSQL (Keycloak takes ~30–60 s to initialise)
docker compose up -d

# Download plugin JARs and seed test data (waits for Keycloak automatically)
uv run python dev/seed.py
```

The seed script downloads three plugin JARs into `dev/providers/` on first run
(SMS authenticator, app authenticator, enforce-mfa) so Keycloak recognises all
credential types. JARs are cached locally and gitignored.

### 2. Test users

| Username | Credentials | Expected after migration |
|----------|-------------|--------------------------|
| `user_eligible_otp` | mobile-number(10) + otp(20) | ✅ otp moves to first |
| `user_eligible_webauthn` | mobile-number(10) + webauthn(20) | ✅ webauthn moves to first |
| `user_sms_only` | mobile-number(10) only | ⛔ skipped |
| `user_otp_first` | otp(10) + mobile-number(20) | ⛔ skipped |

Password for all users: **`pass`**

### 3. Verify credential order before migration

Open the Keycloak Account Console and check the credential order:

```
http://localhost:8080/realms/dev/account/#/security/signingin
```

Log in as `user_eligible_otp` / `pass`. Under **Two-factor authentication**,
mobile-number is listed before OTP.

To test login with TOTP, add the following secret to any authenticator app
(Google Authenticator, Aegis, etc.):

```
Secret : JBSWY3DPEHPK3PXP
Type   : Time-based (TOTP), SHA-1, 6 digits, 30 s
```

SMS OTP codes are logged by Keycloak when no SMS gateway is configured:

```bash
docker compose logs -f keycloak | grep -i "mobile\|sms\|otp"
```

### 4. Run the migration

The seed script provisions a `migration-tool` service account with secret
`dev-migration-secret`, so the recommended auth path works out of the box:

```bash
# Dry-run — check the numbers (expect Total eligible: 2)
KC_CLIENT_SECRET=dev-migration-secret uv run python migrate.py \
  --realm dev --db-password keycloak

# Batch 1 — migrate one user
KC_CLIENT_SECRET=dev-migration-secret uv run python migrate.py \
  --realm dev --db-password keycloak --batch-size 1 --execute

# Dry-run again — expect Total eligible: 1
KC_CLIENT_SECRET=dev-migration-secret uv run python migrate.py \
  --realm dev --db-password keycloak

# Batch 2 — migrate the rest
KC_CLIENT_SECRET=dev-migration-secret uv run python migrate.py \
  --realm dev --db-password keycloak --batch-size 0 --execute

# Final dry-run — expect Total eligible: 0
KC_CLIENT_SECRET=dev-migration-secret uv run python migrate.py \
  --realm dev --db-password keycloak
```

### 5. Verify credential order after migration

Reload the Account Console. For `user_eligible_otp`, OTP now appears before
mobile-number (SMS).

### 6. Reset and repeat

```bash
# Restore original credential priorities without recreating users
uv run python dev/seed.py --reset --db-password keycloak
```

### 7. Clean up

```bash
uv run python dev/seed.py --clean --db-password keycloak
docker compose down -v
```

---

## Verification

Spot-check a migrated user directly in the database:

```sql
SELECT c.type, c.priority, u.username
FROM   credential c
JOIN   user_entity u ON c.user_id = u.id
WHERE  u.username = 'alice'
ORDER BY c.priority;
```

SMS (`mobile-number`) should appear after OTP/WebAuthn/app credentials.
