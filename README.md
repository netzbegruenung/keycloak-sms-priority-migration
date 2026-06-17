# keycloak-sms-priority-migration

Raises the `PRIORITY` of SMS (`mobile-number`) credentials in Keycloak's
PostgreSQL database so that other 2FA methods (OTP, WebAuthn, app) appear
before SMS when a user has multiple methods configured.

Only users where **SMS is currently at position 1** and who have **at least one
other 2FA method** are affected. Users with SMS only, or whose SMS is already
not first, are left untouched.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
```

## Usage

```
python migrate.py \
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
python migrate.py --realm myrealm --db-host db.example.com --db-name keycloak \
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
python migrate.py --realm myrealm --db-host db.example.com --db-name keycloak \
  --db-user keycloak --db-password secret --batch-size 500 --execute
```

Monitor for issues. Re-run in dry-run mode to confirm the remaining eligible
count dropped to ~743 (total − 500).

### 3. Run 2 — all remaining users

```bash
python migrate.py --realm myrealm --db-host db.example.com --db-name keycloak \
  --db-user keycloak --db-password secret --batch-size 0 --execute
```

A final dry-run should report `Total eligible: 0`.

## Audit logs

Each `--execute` run writes a JSON-lines file to `logs/migration-<timestamp>.jsonl`:

```json
{"credential_id": "...", "user_id": "...", "username": "alice", "old_priority": 10, "new_priority": 99}
```

Log files are gitignored.

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
