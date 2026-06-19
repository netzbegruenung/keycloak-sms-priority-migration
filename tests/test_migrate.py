import pytest
from unittest.mock import patch

from migrate import dry_run_counts, execute_migration
from tests.conftest import seed, sms_priority

REALM = "test-realm"
KC_URL = "http://keycloak:8080"
KC_CLIENT_ID = "migration-tool"


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


def _run(db_conn, realm=REALM, batch_size=0, side_effect=None):
    """Run execute_migration with mocked Keycloak calls. Returns (result, mock_move)."""
    with patch("migrate._kc_admin_token", return_value="fake-token"), \
         patch("migrate.move_credential_to_first", side_effect=side_effect) as mock_move:
        result = execute_migration(
            db_conn, realm, batch_size,
            KC_URL, KC_CLIENT_ID, client_secret="fake-secret",
        )
    return result, mock_move


def _simulate_move_to_first(db_conn):
    """Side-effect for move_credential_to_first that mirrors the Keycloak update in DuckDB."""
    def _effect(kc_url, token, realm, user_id, cred_id):
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE credential SET priority = 0 WHERE id = $id",
                {"id": cred_id},
            )
    return _effect


# ---------------------------------------------------------------------------
# Eligibility rules
# ---------------------------------------------------------------------------

def test_sms_only_skipped(db_conn):
    seed(db_conn, REALM, [{"username": "alice", "credentials": [{"type": "mobile-number", "priority": 10}]}])
    result, mock_move = _run(db_conn)
    assert result["updated"] == 0
    mock_move.assert_not_called()


def test_sms_first_with_otp(db_conn):
    data = seed(db_conn, REALM, [eligible_user("alice", "otp")])
    result, mock_move = _run(db_conn)
    assert result["updated"] == 1
    mock_move.assert_called_once()
    _, _, _, user_id, _ = mock_move.call_args.args
    assert user_id == data["users"]["alice"]


def test_sms_first_with_webauthn(db_conn):
    data = seed(db_conn, REALM, [eligible_user("alice", "webauthn")])
    result, mock_move = _run(db_conn)
    assert result["updated"] == 1
    mock_move.assert_called_once()
    _, _, _, user_id, _ = mock_move.call_args.args
    assert user_id == data["users"]["alice"]


def test_sms_first_with_app_credential(db_conn):
    data = seed(db_conn, REALM, [eligible_user("alice", "APP_CREDENTIAL")])
    result, mock_move = _run(db_conn)
    assert result["updated"] == 1
    mock_move.assert_called_once()
    _, _, _, user_id, _ = mock_move.call_args.args
    assert user_id == data["users"]["alice"]


def test_sms_not_first_skipped(db_conn):
    """OTP at position 1, SMS at position 2 — no change needed."""
    data = seed(db_conn, REALM, [{"username": "alice", "credentials": [
        {"type": "otp", "priority": 10},
        {"type": "mobile-number", "priority": 20},
    ]}])
    result, mock_move = _run(db_conn)
    assert result["updated"] == 0
    mock_move.assert_not_called()
    assert sms_priority(db_conn, data["users"]["alice"]) == 20


def test_wrong_realm_skipped(db_conn):
    seed(db_conn, REALM, [eligible_user("alice")])
    result, mock_move = _run(db_conn, realm="other-realm")
    assert result["updated"] == 0
    mock_move.assert_not_called()


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
    result, mock_move = _run(db_conn, batch_size=3)
    assert result["updated"] == 3
    assert mock_move.call_count == 3


def test_batch_size_0_updates_all(db_conn):
    seed(db_conn, REALM, [eligible_user(f"user{i}") for i in range(5)])
    result, mock_move = _run(db_conn, batch_size=0)
    assert result["updated"] == 5
    assert mock_move.call_count == 5


# ---------------------------------------------------------------------------
# Two-run rollout
# ---------------------------------------------------------------------------

def test_two_run_rollout(db_conn):
    """
    Run 1 migrates the first 3 of 5 eligible users.
    Run 2 (no limit) picks up the remaining 2.
    All 5 end up processed.
    """
    seed(db_conn, REALM, [eligible_user(f"user{i}") for i in range(5)])

    side_effect = _simulate_move_to_first(db_conn)

    with patch("migrate._kc_admin_token", return_value="fake-token"), \
         patch("migrate.move_credential_to_first", side_effect=side_effect) as mock_move:
        run1 = execute_migration(db_conn, REALM, 3, KC_URL, KC_CLIENT_ID, client_secret="fake-secret")
        assert run1["updated"] == 3

        run2 = execute_migration(db_conn, REALM, 0, KC_URL, KC_CLIENT_ID, client_secret="fake-secret")
        assert run2["updated"] == 2

    assert mock_move.call_count == 5
