import pytest

from migrate import dry_run_counts, execute_migration
from tests.conftest import seed, sms_priority

REALM = "test-realm"
TARGET = 99


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def eligible_user(username, other_type="otp"):
    """User with SMS at position 1 and one other 2FA method."""
    return {
        "username": username,
        "credentials": [
            {"type": "mobile-number", "priority": 10},
            {"type": other_type, "priority": 20},
        ],
    }


# ---------------------------------------------------------------------------
# Eligibility rules
# ---------------------------------------------------------------------------

def test_sms_only_skipped(db_conn):
    seed(db_conn, REALM, [{"username": "alice", "credentials": [{"type": "mobile-number", "priority": 10}]}])
    result = execute_migration(db_conn, REALM, TARGET, batch_size=0)
    assert result["updated"] == 0


def test_sms_first_with_otp(db_conn):
    data = seed(db_conn, REALM, [eligible_user("alice", "otp")])
    result = execute_migration(db_conn, REALM, TARGET, batch_size=0)
    assert result["updated"] == 1
    assert sms_priority(db_conn, data["users"]["alice"]) == TARGET


def test_sms_first_with_webauthn(db_conn):
    data = seed(db_conn, REALM, [eligible_user("alice", "webauthn")])
    result = execute_migration(db_conn, REALM, TARGET, batch_size=0)
    assert result["updated"] == 1
    assert sms_priority(db_conn, data["users"]["alice"]) == TARGET


def test_sms_first_with_app_credential(db_conn):
    data = seed(db_conn, REALM, [eligible_user("alice", "APP_CREDENTIAL")])
    result = execute_migration(db_conn, REALM, TARGET, batch_size=0)
    assert result["updated"] == 1
    assert sms_priority(db_conn, data["users"]["alice"]) == TARGET


def test_sms_not_first_skipped(db_conn):
    """OTP at position 1, SMS at position 2 — no change needed."""
    data = seed(db_conn, REALM, [{"username": "alice", "credentials": [
        {"type": "otp", "priority": 10},
        {"type": "mobile-number", "priority": 20},
    ]}])
    result = execute_migration(db_conn, REALM, TARGET, batch_size=0)
    assert result["updated"] == 0
    assert sms_priority(db_conn, data["users"]["alice"]) == 20


def test_wrong_realm_skipped(db_conn):
    seed(db_conn, REALM, [eligible_user("alice")])
    result = execute_migration(db_conn, "other-realm", TARGET, batch_size=0)
    assert result["updated"] == 0


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

def test_dry_run_returns_correct_counts(db_conn):
    seed(db_conn, REALM, [eligible_user("alice"), eligible_user("bob")])
    counts = dry_run_counts(db_conn, REALM, batch_size=0)
    assert counts["total"] == 2
    assert counts["effective"] == 2


def test_dry_run_makes_no_changes(db_conn):
    data = seed(db_conn, REALM, [eligible_user("alice")])
    dry_run_counts(db_conn, REALM, batch_size=0)
    assert sms_priority(db_conn, data["users"]["alice"]) == 10


def test_dry_run_effective_capped_by_batch_size(db_conn):
    seed(db_conn, REALM, [eligible_user(f"user{i}") for i in range(5)])
    counts = dry_run_counts(db_conn, REALM, batch_size=3)
    assert counts["total"] == 5
    assert counts["effective"] == 3


# ---------------------------------------------------------------------------
# Batch-size
# ---------------------------------------------------------------------------

def test_batch_size_limits_update(db_conn):
    seed(db_conn, REALM, [eligible_user(f"user{i}") for i in range(5)])
    result = execute_migration(db_conn, REALM, TARGET, batch_size=3)
    assert result["updated"] == 3


def test_batch_size_0_updates_all(db_conn):
    seed(db_conn, REALM, [eligible_user(f"user{i}") for i in range(5)])
    result = execute_migration(db_conn, REALM, TARGET, batch_size=0)
    assert result["updated"] == 5


# ---------------------------------------------------------------------------
# Two-run rollout
# ---------------------------------------------------------------------------

def test_two_run_rollout(db_conn):
    """
    Run 1 migrates the first 3 of 5 eligible users.
    Run 2 (no limit) picks up the remaining 2.
    All 5 end up at the target priority.
    """
    data = seed(db_conn, REALM, [eligible_user(f"user{i}") for i in range(5)])

    run1 = execute_migration(db_conn, REALM, TARGET, batch_size=3)
    assert run1["updated"] == 3

    run2 = execute_migration(db_conn, REALM, TARGET, batch_size=0)
    assert run2["updated"] == 2

    for user_id in data["users"].values():
        assert sms_priority(db_conn, user_id) == TARGET
