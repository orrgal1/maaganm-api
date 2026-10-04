"""Kehila-Net reads stay allowlisted and out of the durable replay database."""

from __future__ import annotations

import sqlite3
import stat

from fastapi.testclient import TestClient

import http_api
from command_bus import READ_VERBS
from idempotency import RequestStore


class FakeKehilaNet:
    def __init__(self):
        self.calls = 0

    async def search_phonebook(self, username, password, *, query, limit):
        assert (username, password) == ("member-user", "member-password")
        assert (query, limit) == ("Or", 2)
        self.calls += 1
        return [{"name": "Or", "phones": [{"label": "mobile", "number": "0500000000"}], "emails": []}]

    async def list_announcements(self, username, password, *, query, limit, forum_id, page):
        assert (username, password) == ("member-user", "member-password")
        assert (query, limit, forum_id, page) in {
            ("meeting", 1, None, 1),
            ("", 20, "123", 2),
        }
        self.calls += 1
        return [{"id": "1:2", "title": "Meeting", "date": "01/01/2026", "category": "Notice", "teaser": "Meeting", "content": "Notice", "links": []}]

    async def list_categories(self, username, password):
        assert (username, password) == ("member-user", "member-password")
        self.calls += 1
        return [{"id": "123", "name": "Notices"}]

    async def close(self):
        pass


def test_kehilanet_live_reads_are_private_and_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("MAAGANM_API_TOKEN", "test-bearer")
    monkeypatch.setattr(http_api.config, "MAAGANM_EMAIL_HMAC_SECRET", "test-signing-secret")
    monkeypatch.setattr(http_api.config, "BUDGET_USERNAME", "budget-user")
    monkeypatch.setattr(http_api.config, "BUDGET_PASSWORD", "budget-password")
    monkeypatch.setattr(http_api.config, "KEHILANET_USERNAME", "member-user")
    monkeypatch.setattr(http_api.config, "KEHILANET_PASSWORD", "member-password")
    database = tmp_path / "requests.sqlite3"
    monkeypatch.setattr(http_api, "_store", RequestStore(database))
    fake = FakeKehilaNet()
    monkeypatch.setattr(http_api, "kehilanet_driver", fake)
    headers = {"Authorization": "Bearer test-bearer"}

    assert {"kehilanet.phonebook.search", "kehilanet.announcements.list", "kehilanet.announcements.categories"} <= READ_VERBS
    with TestClient(http_api.app) as client:
        request = {"id": "same-id", "verb": "kehilanet.phonebook.search", "args": {"query": "Or", "limit": 2}}
        assert client.post("/commands", json=request).status_code == 401
        first = client.post("/commands", headers=headers, json=request)
        second = client.post("/commands", headers=headers, json=request)
        assert first.status_code == second.status_code == 200
        assert first.json()["payload"]["total"] == 1
        assert fake.calls == 2  # Read again; no persisted member directory replay.

        announcements = client.post("/commands", headers=headers, json={
            "id": "announcements-id", "verb": "kehilanet.announcements.list",
            "args": {"query": "meeting", "limit": 1},
        })
        assert announcements.status_code == 200
        assert announcements.json()["payload"]["items"][0]["title"] == "Meeting"
        categories = client.post("/commands", headers=headers, json={
            "verb": "kehilanet.announcements.categories", "args": {},
        })
        assert categories.status_code == 200
        assert categories.json()["payload"]["items"] == [{"id": "123", "name": "Notices"}]
        category_page = client.post("/commands", headers=headers, json={
            "verb": "kehilanet.announcements.list", "args": {"forum_id": "123", "page": 2},
        })
        assert category_page.status_code == 200
        assert category_page.json()["payload"]["page"] == 2
        assert category_page.json()["payload"]["forum_id"] == "123"
        assert client.post("/commands", headers=headers, json={
            "verb": "kehilanet.announcements.list", "args": {"page": 2},
        }).status_code == 400
        assert client.post("/commands", headers=headers, json={
            "verb": "kehilanet.announcements.list", "args": {"forum_id": "123", "limit": 5},
        }).status_code == 400
        assert client.post("/commands", headers=headers, json={
            "verb": "kehilanet.phonebook.search", "args": {"query": ""},
        }).status_code == 400

    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert b"0500000000" not in database.read_bytes()


def test_kehilanet_missing_local_credentials_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MAAGANM_API_TOKEN", "test-bearer")
    monkeypatch.setattr(http_api.config, "MAAGANM_EMAIL_HMAC_SECRET", "test-signing-secret")
    monkeypatch.setattr(http_api.config, "BUDGET_USERNAME", "budget-user")
    monkeypatch.setattr(http_api.config, "BUDGET_PASSWORD", "budget-password")
    monkeypatch.setattr(http_api.config, "KEHILANET_USERNAME", "")
    monkeypatch.setattr(http_api.config, "KEHILANET_PASSWORD", "")
    monkeypatch.setattr(http_api, "_store", RequestStore(tmp_path / "requests.sqlite3"))
    with TestClient(http_api.app) as client:
        response = client.post("/commands", headers={"Authorization": "Bearer test-bearer"}, json={
            "verb": "kehilanet.announcements.list", "args": {},
        })
    assert response.status_code == 503
    assert "credentials are not configured" in response.json()["detail"]
