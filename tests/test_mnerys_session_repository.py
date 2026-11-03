"""M1 read-only ownership repository contract; no real database is modified."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.session_repository import SessionOwnership, SqlSessionOwnershipLookup
from src.gateway_admission import GatewayAdmission, Principal, AdmissionDenied


def repository(db_owner, *, ghosts=None):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = (
        None if db_owner is _MISSING else SimpleNamespace(owner=db_owner)
    )
    factory = MagicMock(return_value=db)
    model = SimpleNamespace(owner=MagicMock(name="owner-column"), id=MagicMock(name="id-column"))
    manager = SimpleNamespace(sessions=ghosts or {})
    return SqlSessionOwnershipLookup(factory, model, manager), db, factory


_MISSING = object()


def test_database_owner_wins_over_conflicting_ghost():
    lookup, db, factory = repository(
        "alice", ghosts={"sid": SimpleNamespace(owner="bob")},
    )
    assert lookup("sid") == SessionOwnership("alice")
    factory.assert_called_once_with()
    db.close.assert_called_once()


def test_missing_database_row_falls_back_to_existing_ghost():
    lookup, db, _ = repository(
        _MISSING, ghosts={"sid": SimpleNamespace(owner="alice")},
    )
    assert lookup("sid") == SessionOwnership("alice")
    db.close.assert_called_once()


def test_missing_session_returns_none_and_closes_db():
    lookup, db, _ = repository(_MISSING)
    assert lookup("sid") is None
    db.close.assert_called_once()


def test_session_repository_close_is_guaranteed_on_query_failure():
    lookup, db, _ = repository(_MISSING)
    db.query.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        lookup("sid")
    db.close.assert_called_once()


def test_existing_db_null_owner_cannot_be_claimed_by_browser():
    lookup, db, _ = repository(
        None, ghosts={"sid": SimpleNamespace(owner="alice")},
    )
    with pytest.raises(AdmissionDenied) as denied:
        GatewayAdmission().admit(Principal("alice", "browser"), "sid", lookup)
    assert denied.value.status_code == 404
    db.close.assert_called_once()


def test_ghost_owner_still_checked_by_gateway_admission():
    lookup, db, _ = repository(
        _MISSING, ghosts={"sid": SimpleNamespace(owner="bob")},
    )
    with pytest.raises(AdmissionDenied) as denied:
        GatewayAdmission().admit(Principal("alice", "browser"), "sid", lookup)
    assert denied.value.status_code == 404
    db.close.assert_called_once()


def test_authentication_denial_happens_before_query_even_with_repository():
    lookup, db, factory = repository("alice")
    with pytest.raises(AdmissionDenied) as denied:
        GatewayAdmission().admit(Principal("alice", "bearer", frozenset()), "sid", lookup)
    assert denied.value.status_code == 403
    factory.assert_not_called()
    db.close.assert_not_called()
