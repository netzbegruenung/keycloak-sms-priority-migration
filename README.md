# keycloak-sms-priority-migration

Raises the `PRIORITY` of SMS (`mobile-number`) credentials in Keycloak's
PostgreSQL database so that other 2FA methods (OTP, WebAuthn, app) appear
before SMS when a user has multiple methods configured.

Only users where **SMS is currently at position 1** and who have **at least one
other 2FA method** are affected. Users with SMS only, or whose SMS is already
not first, are left untouched.

## Setup

```bash
uv sync
```

## Usage

```
uv run python migrate.py \
  --realm <realm-name> \
  --db-host <host> \
  --db-port 5432 \
  --db-name keycloak \
  --db-user keycloak \
  [--db-password <pw>]    # or export DB_PASSWORD=<pw>
  [--priority 99]         # target SMS priority (default: 99)
  [--batch-size 500]      # max users per run; 0 = no limit (default: 500)
  [--dry-run | --execute] # dry-run is the default
```

## Gradual rollout (two runs)

### 1. Dry-run first — check the numbers

```bash
uv run python migrate.py --realm myrealm --db-host db.example.com --db-name keycloak \
  --db-user keycloak --db-password secret --batch-size 500
```

Output example:
```
DRY RUN — no changes made
  Total eligible:        1243
  This run (limit 500):   500
```

### 2. Run 1 — first 500 users

```bash
uv run python migrate.py --realm myrealm --db-host db.example.com --db-name keycloak \
  --db-user keycloak --db-password secret --batch-size 500 --execute
```

Monitor for issues. Re-run in dry-run mode to confirm the remaining eligible
count dropped to ~743 (total − 500).

### 3. Run 2 — all remaining users

```bash
uv run python migrate.py --realm myrealm --db-host db.example.com --db-name keycloak \
  --db-user keycloak --db-password secret --batch-size 0 --execute
```

A final dry-run should report `Total eligible: 0`.

## Audit logs

Each `--execute` run writes a JSON-lines file to `logs/migration-<timestamp>.jsonl`:

```json
{"credential_id": "...", "user_id": "...", "username": "alice", "old_priority": 10, "new_priority": 99}
```

Log files are gitignored.

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
uv run python dev/seed.py --db-password keycloak
```

The seed script downloads three plugin JARs into `dev/providers/` on first run
(SMS authenticator, app authenticator, enforce-mfa) so Keycloak recognises all
credential types. JARs are cached locally and gitignored.

### 2. Test users

| Username | Credentials | Expected after migration |
|----------|-------------|--------------------------|
| `user_eligible_otp` | mobile-number(10) + otp(20) | ✅ SMS moves to 99 |
| `user_eligible_webauthn` | mobile-number(10) + webauthn(20) | ✅ SMS moves to 99 |
| `user_sms_only` | mobile-number(10) only | ⛔ skipped |
| `user_otp_first` | otp(10) + mobile-number(20) | ⛔ skipped |

Password for all users: **`Test1234!`**

### 3. Verify credential order before migration

Open the Keycloak Account Console and check the credential order:

```
http://localhost:8080/realms/dev/account/#/security/signingin
```

Log in as `user_eligible_otp` / `Test1234!`. Under **Two-factor
authentication**, mobile-number (priority 10) is listed before OTP (priority 20).

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

```bash
# Dry-run — check the numbers (expect Total eligible: 2)
uv run python migrate.py --realm dev --db-password keycloak

# Batch 1 — migrate one user
uv run python migrate.py --realm dev --db-password keycloak --batch-size 1 --execute

# Dry-run again — expect Total eligible: 1
uv run python migrate.py --realm dev --db-password keycloak

# Batch 2 — migrate the rest
uv run python migrate.py --realm dev --db-password keycloak --batch-size 0 --execute

# Final dry-run — expect Total eligible: 0
uv run python migrate.py --realm dev --db-password keycloak
```

### 5. Verify credential order after migration

Reload the Account Console. For `user_eligible_otp`, OTP (priority 20) now
appears before mobile-number (priority 99).

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

SMS (`mobile-number`) should now appear last (priority 99), with OTP/WebAuthn/app
credentials above it.
