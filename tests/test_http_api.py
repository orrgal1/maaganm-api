from __future__ import annotations

import hmac
import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import http_api
from command_bus import make_result
from idempotency import AccountScopeMismatch, RequestStore


@pytest.fixture(autouse=True)
def local_budget_credentials(monkeypatch):
    monkeypatch.setenv("MAAGANM_API_TOKEN", "test-bearer")
    monkeypatch.setattr(http_api.config, "MAAGANM_EMAIL_HMAC_SECRET", "test-signing-secret")
    monkeypatch.setattr(http_api.config, "BUDGET_USERNAME", "local-user")
    monkeypatch.setattr(http_api.config, "BUDGET_PASSWORD", "local-password")


def _headers():
    return {"Authorization": f"Bearer {http_api._token()}"}


def _scope(username):
    return hmac.new(http_api._token().encode(), username.encode(), hashlib.sha256).hexdigest()


def test_bearer_and_durable_command_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(http_api, "_store", RequestStore(tmp_path / "requests.sqlite3"))
    request_id = f"test-{uuid.uuid4()}"
    with TestClient(http_api.app) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers=_headers()).status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
        assert schema["paths"]["/commands"]["post"]["security"] == [{"BearerAuth": []}]
        command_schema = schema["paths"]["/commands"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert command_schema["required"] == ["verb"]
        assert "budget_credentials" not in command_schema["properties"]
        payload = {"id": request_id, "verb": "health", "args": {}, "approval": None}
        first = client.post("/commands", headers=_headers(), json=payload)
        second = client.post("/commands", headers=_headers(), json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["status"] == "ok"
        assert client.post(
            "/commands", headers=_headers(),
            json={"id": f"test-{uuid.uuid4()}", "verb": "unknown", "args": {}},
        ).status_code == 400
        rejected = client.post(
            "/commands", headers=_headers(),
            json={"id": f"test-{uuid.uuid4()}", "verb": "health", "budget_credentials": {"username": "other", "password": "secret"}},
        )
        assert rejected.status_code == 400
        assert "secret" not in rejected.text


def test_local_account_scoped_replay_and_prior_caller_scoped_rows(tmp_path, monkeypatch):
    database = tmp_path / "requests.sqlite3"
    store = RequestStore(database)
    monkeypatch.setattr(http_api, "_store", store)
    request_id = f"test-{uuid.uuid4()}"
    payload = {"id": request_id, "verb": "health", "args": {}}
    with TestClient(http_api.app) as client:
        first = client.post("/commands", headers=_headers(), json=payload)
        assert first.status_code == 200
        assert client.post("/commands", headers=_headers(), json=payload).json() == first.json()
        monkeypatch.setattr(http_api.config, "BUDGET_USERNAME", "another-local-user")
        wrong_account = client.post("/commands", headers=_headers(), json=payload)
        assert wrong_account.status_code == 409
        assert wrong_account.json()["detail"] == "Request ID is unavailable"
        monkeypatch.setattr(http_api.config, "BUDGET_USERNAME", "local-user")
        assert client.post("/commands", headers=_headers(), json=payload).json() == first.json()

    prior_id = f"prior-{uuid.uuid4()}"
    store.claim(prior_id, account_scope=_scope("local-user"))
    prior_result = make_result(prior_id, "ok", payload={"prior": True})
    store.complete(prior_id, prior_result, account_scope=_scope("local-user"))
    with TestClient(http_api.app) as client:
        replay = client.post("/commands", headers=_headers(), json={"id": prior_id, "verb": "balance"})
    assert replay.status_code == 200
    assert replay.json()["payload"] == {"prior": True}
    database_bytes = database.read_bytes()
    assert b"local-password" not in database_bytes
    assert b"local-user" not in database_bytes


def test_legacy_request_id_cannot_replay_to_scoped_caller(tmp_path, monkeypatch):
    database = tmp_path / "requests.sqlite3"
    store = RequestStore(database)
    monkeypatch.setattr(http_api, "_store", store)
    request_id = f"legacy-{uuid.uuid4()}"
    store.claim(request_id)
    with TestClient(http_api.app) as client:
        response = client.post(
            "/commands", headers=_headers(),
            json={"id": request_id, "verb": "health", "args": {}},
        )
    assert response.status_code == 409


def test_existing_sqlite_schema_migrates_without_replaying_legacy_rows(tmp_path):
    database = tmp_path / "old.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE requests (request_id TEXT PRIMARY KEY, state TEXT, result BLOB, claimed_at TEXT, completed_at TEXT)")
        connection.execute("INSERT INTO requests (request_id, state, claimed_at) VALUES ('old-id', 'in_progress', 'now')")
    store = RequestStore(database)
    with pytest.raises(AccountScopeMismatch):
        store.claim("old-id", account_scope="different")
    assert store.claim("new-id", account_scope="scope").claimed


def test_local_credentials_reach_dispatch_for_read_and_write(tmp_path, monkeypatch):
    monkeypatch.setattr(http_api, "_store", RequestStore(tmp_path / "requests.sqlite3"))
    calls = []

    async def capture(command, driver, username, password, **kwargs):
        calls.append((command.verb, username, password))
        return make_result(command.id, "ok", payload={"seen": command.verb})

    monkeypatch.setattr(http_api, "dispatch_command", capture)
    with TestClient(http_api.app) as client:
        for verb in ("balance", "transfer.stage"):
            payload = {
                "id": str(uuid.uuid4()), "verb": verb,
                "args": {} if verb == "balance" else {
                    "recipient_hid": "123", "recipient_name": "Example", "amount_ils": 1.0},
            }
            response = client.post("/commands", headers=_headers(), json=payload)
            assert response.status_code == 200
            assert response.json()["payload"]["seen"] == verb
    assert calls == [
        ("balance", "local-user", "local-password"),
        ("transfer.stage", "local-user", "local-password"),
    ]


def test_transfer_approve_requires_nonempty_otp_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(http_api, "_store", RequestStore(tmp_path / "requests.sqlite3"))
    dispatched = []

    async def capture(command, driver, username, password, **kwargs):
        dispatched.append(command)
        return make_result(command.id, "ok", payload={"seen": command.verb})

    monkeypatch.setattr(http_api, "dispatch_command", capture)
    approval = {
        "ref": "test-approval",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }

    with TestClient(http_api.app) as client:
        for args in (
            {"transaction_id": "test-transaction"},
            {"transaction_id": "test-transaction", "otp_code": ""},
            {"transaction_id": "test-transaction", "otp_code": "   "},
        ):
            response = client.post(
                "/commands",
                headers=_headers(),
                json={"id": str(uuid.uuid4()), "verb": "transfer.approve", "args": args, "approval": approval},
            )
            assert response.status_code == 400
            assert response.json() == {
                "detail": "transfer.approve requires a nonempty SMS otp_code"
            }
        assert dispatched == []

        response = client.post(
            "/commands",
            headers=_headers(),
            json={
                "id": str(uuid.uuid4()),
                "verb": "transfer.approve",
                "args": {"transaction_id": "test-transaction", "otp_code": "123456"},
                "approval": approval,
            },
        )
    assert response.status_code == 200
    assert response.json()["payload"] == {"seen": "transfer.approve"}
    assert len(dispatched) == 1
    assert dispatched[0].args.transaction_id == "test-transaction"
    assert dispatched[0].args.otp_code == "123456"
    assert dispatched[0].approval.ref == "test-approval"


def test_transfer_stage_rejects_unsupported_transaction_type(tmp_path, monkeypatch):
    monkeypatch.setattr(http_api, "_store", RequestStore(tmp_path / "requests.sqlite3"))
    args = {"recipient_hid": "recipient", "recipient_name": "Recipient", "amount_ils": 1.0}
    with TestClient(http_api.app) as client:
        response = client.post(
            "/commands", headers=_headers(),
            json={"id": str(uuid.uuid4()), "verb": "transfer.stage", "args": {**args, "transaction_type": 2}},
        )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid command or arguments"}


def test_live_startup_requires_local_credentials(monkeypatch):
    monkeypatch.setattr(http_api.config, "MOCK_MODE", False)
    monkeypatch.setattr(http_api.config, "BUDGET_PASSWORD", "")
    with pytest.raises(RuntimeError, match="Local Budget credentials are required"):
        with TestClient(http_api.app):
            pass


def test_rest_api_does_not_require_retired_mail_signing_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(http_api.config, "MAAGANM_EMAIL_HMAC_SECRET", "")
    monkeypatch.setattr(http_api, "_store", RequestStore(tmp_path / "requests.sqlite3"))
    with TestClient(http_api.app) as client:
        assert client.get("/health", headers=_headers()).status_code == 200
