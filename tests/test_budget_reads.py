"""Synthetic checks for Budget read sessions and HTML response contracts."""

import json
from datetime import datetime, timezone

import httpx
import pytest

import config
from budget_driver import BudgetDriver
from command_bus import dispatch_command, parse_command, sign_command
from errors import APIException


LOGIN_HTML = (
    '<html><form action="/Home/Login">'
    '<input name="UserName"><input name="Password"></form></html>'
)


def _table(kind, rows=""):
    table_id, columns = (
        ("transTable", 11) if kind == "transactions" else ("transactions", 7)
    )
    return (
        f'<table id="{table_id}"><thead><tr>'
        + "<th>column</th>" * columns
        + "</tr></thead><tbody>"
        + rows
        + "</tbody></table>"
    )


def _row(kind):
    values = (
        ["line-1", "approval-1", "01/01/2026", "sender", "receiver", "1.00",
         "sender note", "receiver note", "True", "False", "Unapproved"]
        if kind == "transactions"
        else ["line-1", "approval-1", "sender", "detail", "1.00", "", ""]
    )
    return "<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>"


def _path(kind):
    return (
        "/Budget/GetTransactionsTable" if kind == "transactions"
        else "/Budget/PendingApproval"
    )


async def _read(driver, kind):
    if kind == "transactions":
        return await driver.get_transactions("user", "password")
    return await driver.get_pending_approvals("user", "password")


def _mock_client(handler):
    return httpx.AsyncClient(
        base_url="https://budget.example.invalid",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["transactions", "approvals"])
@pytest.mark.parametrize("row_count", [0, 1])
async def test_budget_reads_accept_known_empty_and_nonempty_tables(
    monkeypatch, kind, row_count
):
    driver = BudgetDriver()
    monkeypatch.setattr(config, "MOCK_MODE", False)

    def handler(request):
        assert request.url.path == _path(kind)
        return httpx.Response(200, text=_table(kind, _row(kind) * row_count))

    async with _mock_client(handler) as client:
        async def get_client(username, password):
            return client

        monkeypatch.setattr(driver, "get_client", get_client)
        rows = await _read(driver, kind)

    assert len(rows) == row_count


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["transactions", "approvals"])
@pytest.mark.parametrize(
    "body", ["<html><p>Unexpected page</p></html>", "<table><tr><td>changed</td></tr></table>"],
)
async def test_budget_reads_reject_unrecognized_http_200(monkeypatch, kind, body):
    driver = BudgetDriver()
    monkeypatch.setattr(config, "MOCK_MODE", False)
    async with _mock_client(lambda request: httpx.Response(200, text=body)) as client:
        async def get_client(username, password):
            return client

        monkeypatch.setattr(driver, "get_client", get_client)
        with pytest.raises(APIException) as error:
            await _read(driver, kind)
    assert error.value.code == "budget_response_invalid"
    assert body not in error.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["transactions", "approvals"])
@pytest.mark.parametrize("stale_style", ["redirect", "login_form"])
async def test_budget_reads_refresh_expired_session_once(monkeypatch, kind, stale_style):
    driver = BudgetDriver()
    monkeypatch.setattr(config, "MOCK_MODE", False)
    calls = []

    def stale_handler(request):
        calls.append(("stale", request.url.path))
        if stale_style == "redirect" and request.url.path != "/":
            return httpx.Response(302, headers={"Location": "/"})
        return httpx.Response(200, text=LOGIN_HTML)

    def fresh_handler(request):
        calls.append(("fresh", request.url.path))
        return httpx.Response(200, text=_table(kind, _row(kind)))

    async with _mock_client(stale_handler) as stale, _mock_client(fresh_handler) as fresh:
        async def get_client(username, password):
            return stale

        async def refresh_client(username, password, old_client):
            assert old_client is stale
            return fresh

        monkeypatch.setattr(driver, "get_client", get_client)
        monkeypatch.setattr(driver, "_refresh_client", refresh_client)
        rows = await _read(driver, kind)

    assert len(rows) == 1
    assert calls.count(("stale", _path(kind))) == 1
    assert calls.count(("fresh", _path(kind))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["transactions", "approvals"])
async def test_budget_reads_fail_after_second_login_page(monkeypatch, kind):
    driver = BudgetDriver()
    monkeypatch.setattr(config, "MOCK_MODE", False)
    async with _mock_client(lambda request: httpx.Response(200, text=LOGIN_HTML)) as client:
        calls = []

        async def get_client(username, password):
            return client

        async def refresh_client(username, password, old_client):
            calls.append(old_client)
            return client

        monkeypatch.setattr(driver, "get_client", get_client)
        monkeypatch.setattr(driver, "_refresh_client", refresh_client)
        with pytest.raises(APIException) as error:
            await _read(driver, kind)

    assert error.value.code == "budget_session_expired"
    assert calls == [client]


@pytest.mark.asyncio
async def test_failed_budget_login_does_not_cache_client(monkeypatch):
    driver = BudgetDriver()
    monkeypatch.setattr(config, "MOCK_MODE", False)

    async def login(client, username, password):
        raise APIException(502, "budget_authentication_unverified", "Login was not verified")

    monkeypatch.setattr(driver, "_login_client", login)
    with pytest.raises(APIException):
        await driver.get_client("user", "password")
    assert driver._clients == {}


@pytest.mark.asyncio
async def test_refresh_replaces_cached_budget_session_once(monkeypatch):
    driver = BudgetDriver()
    monkeypatch.setattr(config, "MOCK_MODE", False)
    stale = _mock_client(lambda request: httpx.Response(200, text=LOGIN_HTML))
    session_key = driver._session_key("user", "password")
    driver._clients[session_key] = stale
    logins = []

    async def login(client, username, password):
        logins.append(client)

    monkeypatch.setattr(driver, "_login_client", login)
    fresh = await driver._refresh_client("user", "password", stale)
    assert fresh is driver._clients[session_key]
    assert fresh is not stale
    assert logins == [fresh]
    assert stale in driver._retired_clients
    assert not stale.is_closed

    await driver.close()
    assert stale.is_closed and fresh.is_closed


@pytest.mark.asyncio
async def test_login_rejects_http_200_login_form():
    driver = BudgetDriver()
    async with _mock_client(lambda request: httpx.Response(200, text=LOGIN_HTML)) as client:
        with pytest.raises(APIException) as error:
            await driver._login_client(client, "user", "password")
    assert error.value.code == "budget_authentication_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,verb", [
    ("transactions", "transactions.list"),
    ("approvals", "approvals.pending"),
])
async def test_unrecognized_table_is_command_error_not_empty_success(
    monkeypatch, kind, verb
):
    driver = BudgetDriver()
    monkeypatch.setattr(config, "MOCK_MODE", False)
    body = {
        "id": "read-1",
        "verb": verb,
        "args": {},
        "approval": None,
        "issued_at": "2026-01-01T00:00:00Z",
    }
    body["hmac"] = sign_command(body, "secret")
    command = parse_command(f"[CMD] {verb} read-1", json.dumps(body), "secret")

    async with _mock_client(lambda request: httpx.Response(200, text="<html>unexpected</html>")) as client:
        async def get_client(username, password):
            return client

        monkeypatch.setattr(driver, "get_client", get_client)
        result = await dispatch_command(
            command, driver, "user", "password", now=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )

    assert result.status == "error"
    assert result.error.code == "budget_response_invalid"
    assert result.payload is None
