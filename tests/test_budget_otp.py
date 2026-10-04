"""Synthetic contract checks for the budget site's OTP transaction flow."""

import json
from datetime import datetime, timezone

import httpx
import pytest

import config
from budget_driver import BudgetDriver
from command_bus import dispatch_command, parse_command, sign_command
from errors import APIException


def _table_row(*, status="Unapproved", can_approve="True"):
    values = [
        "line-1", "approval-1", "01/01/2026", "sender", "receiver", "1.00",
        "sender note", "receiver note", can_approve, "False", status,
    ]
    return "<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>"


def _client(monkeypatch, driver, handler):
    client = httpx.AsyncClient(
        base_url="https://budget.example.invalid",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )

    async def get_client(username, password):
        return client

    monkeypatch.setattr(driver, "get_client", get_client)
    monkeypatch.setattr(config, "MOCK_MODE", False)
    return client


@pytest.mark.asyncio
async def test_transactions_expose_site_approval_id_separately(monkeypatch):
    driver = BudgetDriver()

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/Budget/GetTransactionsTable"
        return httpx.Response(200, text=_table_row())

    async with _client(monkeypatch, driver, handler):
        rows = await driver.get_transactions("user", "password")

    assert len(rows) == 1
    assert rows[0]["transaction_id"] == "line-1"
    assert rows[0]["approval_transaction_id"] == "approval-1"
    assert rows[0]["can_approve"] is True


@pytest.mark.asyncio
async def test_request_otp_posts_only_the_observed_resend_field(monkeypatch):
    driver = BudgetDriver()
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.content))
        if request.method == "GET":
            return httpx.Response(200, text=_table_row())
        return httpx.Response(200, text="accepted")

    async with _client(monkeypatch, driver, handler):
        result = await driver.request_otp("user", "password", "approval-1")

    assert result["status"] == "otp_request_submitted"
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/Budget/GetTransactionsTable"),
        ("POST", "/Budget/GetNewPassword"),
    ]
    assert calls[1][2] == b"TransactionId=approval-1"


@pytest.mark.asyncio
async def test_unlinked_send_money_response_is_unverified(monkeypatch):
    driver = BudgetDriver()

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/Budget/SendMoney"
        return httpx.Response(200, text="generic page")

    async with _client(monkeypatch, driver, handler):
        result = await driver.transfer("user", "password", "recipient", "Recipient", 1.0)

    assert result["status"] == "submission_unverified"
    assert result["requires_otp"] is None
    assert result["transaction_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table_html", "expected_status"),
    [
        (_table_row(), "approval_unverified"),
        (_table_row(status="Approved", can_approve="False"), "approved"),
        (_table_row(status="Executed", can_approve="False"), "approved"),
        (_table_row(status="Approved", can_approve="False") * 2, "approval_unverified"),
    ],
)
async def test_approve_otp_requires_unique_confirmed_table_row(
    monkeypatch, table_html, expected_status
):
    driver = BudgetDriver()
    calls = []
    removed = []
    monkeypatch.setattr("budget_driver.pending_transfer_store.remove_transfer", removed.append)

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/Budget/ApproveTransaction":
            assert request.content == b"TransactionId=approval-1&Password=123456"
            return httpx.Response(200, text="generic page")
        assert request.url.path == "/Budget/GetTransactionsTable"
        return httpx.Response(200, text=_table_row() if len(calls) == 1 else table_html)

    async with _client(monkeypatch, driver, handler):
        result = await driver.approve_otp("user", "password", "approval-1", "123456")

    assert result["status"] == expected_status
    assert removed == (["approval-1"] if expected_status == "approved" else [])
    assert calls == [
        ("GET", "/Budget/GetTransactionsTable"),
        ("POST", "/Budget/ApproveTransaction"),
        ("GET", "/Budget/GetTransactionsTable"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table_html", "expected_code"),
    [
        ("", "transfer_not_found"),
        (_table_row(status="Approved", can_approve="False"), "transfer_not_approvable"),
        (_table_row() * 2, "approval_state_ambiguous"),
    ],
)
async def test_otp_writes_reject_nonpending_or_ambiguous_row_before_post(
    monkeypatch, table_html, expected_code
):
    driver = BudgetDriver()
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET"
        return httpx.Response(200, text=table_html)

    async with _client(monkeypatch, driver, handler):
        for write in (
            driver.request_otp("user", "password", "approval-1"),
            driver.approve_otp("user", "password", "approval-1", "123456"),
        ):
            with pytest.raises(APIException) as error:
                await write
            assert error.value.code == expected_code

    assert calls == [("GET", "/Budget/GetTransactionsTable")] * 2


@pytest.mark.asyncio
async def test_otp_request_command_requires_its_own_approval():
    class Driver:
        def __init__(self):
            self.calls = []

        async def request_otp(self, username, password, approval_transaction_id):
            self.calls.append((username, password, approval_transaction_id))
            return {"status": "otp_request_submitted"}

    driver = Driver()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def command(request_id, approval):
        body = {
            "id": request_id,
            "verb": "transfer.otp.request",
            "args": {"approval_transaction_id": "approval-1"},
            "issued_at": "2026-01-01T00:00:00Z",
            "approval": approval,
        }
        body["hmac"] = sign_command(body, "secret")
        return parse_command(
            f"[CMD] transfer.otp.request {request_id}", json.dumps(body), "secret"
        )

    missing = await dispatch_command(command("missing", None), driver, "user", "password", now=now)
    expired = await dispatch_command(
        command("expired", {"ref": "ref", "expires_at": "2025-12-31T23:59:59Z"}),
        driver, "user", "password", now=now,
    )
    approved = await dispatch_command(
        command("approved", {"ref": "ref", "expires_at": "2026-01-01T00:00:01Z"}),
        driver, "user", "password", now=now,
    )

    assert [missing.status, expired.status, approved.status] == [
        "approval_required", "approval_expired", "ok",
    ]
    assert driver.calls == [("user", "password", "approval-1")]
