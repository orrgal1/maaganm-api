"""Synthetic checks for transaction cancellation and its readback."""

import httpx
import pytest

import config
from budget_driver import BudgetDriver
from errors import APIException


def _row(*, line_id="line-1", status="Approved", can_cancel="True"):
    values = [
        line_id, "approval-1", "01/01/2026", "sender", "receiver", "1.00",
        "sender note", "receiver note", "False", can_cancel, status,
    ]
    return "<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>"


def _table(rows=""):
    return (
        '<table id="transTable"><thead><tr>' + "<th>column</th>" * 11
        + '</tr></thead><tbody>' + rows + "</tbody></table>"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "expected_code"),
    [
        ("", "transaction_not_found"),
        (_row() * 2, "transaction_state_ambiguous"),
        (_row(can_cancel="False"), "transaction_not_cancellable"),
    ],
)
async def test_cancel_rejects_missing_ambiguous_or_non_cancellable_row(
    monkeypatch, before, expected_code
):
    driver = BudgetDriver()
    monkeypatch.setattr(config, "MOCK_MODE", False)
    calls = []

    def handler(request):
        calls.append(request.method)
        assert request.method == "GET"
        return httpx.Response(200, text=_table(before))

    async with httpx.AsyncClient(
        base_url="https://budget.example.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        async def get_client(username, password):
            return client

        monkeypatch.setattr(driver, "get_client", get_client)
        with pytest.raises(APIException) as error:
            await driver.cancel_transaction("user", "password", "line-1")

    assert error.value.code == expected_code
    assert calls == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after", "expected_status"),
    [
        (_row(status="Cancelled", can_cancel="False"), "cancelled"),
        (_row(status="Approved"), "cancellation_unverified"),
        ("", "cancellation_unverified"),
        (_row(status="Cancelled", can_cancel="False") * 2, "cancellation_unverified"),
        (None, "cancellation_unverified"),
    ],
)
async def test_cancel_requires_confirmed_single_cancelled_row(
    monkeypatch, after, expected_status
):
    driver = BudgetDriver()
    monkeypatch.setattr(config, "MOCK_MODE", False)
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "DELETE":
            assert request.url.path == "/Budget/MyTransactions"
            assert request.url.params["transactionLineId"] == "line-1"
            return httpx.Response(200, text="<html>login page or generic response</html>")
        assert request.url.path == "/Budget/GetTransactionsTable"
        if len(calls) > 1 and after is None:
            return httpx.Response(200, text="<html>unexpected page</html>")
        return httpx.Response(200, text=_table(_row() if len(calls) == 1 else after))

    async with httpx.AsyncClient(
        base_url="https://budget.example.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        async def get_client(username, password):
            return client

        monkeypatch.setattr(driver, "get_client", get_client)
        result = await driver.cancel_transaction("user", "password", "line-1")

    assert result["status"] == expected_status
    assert result["transaction_line_id"] == "line-1"
    assert calls == ["GET", "DELETE", "GET"]


@pytest.mark.asyncio
async def test_mock_cancel_retains_cancelled_row(monkeypatch):
    monkeypatch.setattr(config, "MOCK_MODE", True)
    driver = BudgetDriver()

    result = await driver.cancel_transaction("user", "password", "90412")
    rows = await driver.get_transactions("user", "password")
    row = next(row for row in rows if row["transaction_id"] == "90412")

    assert result["status"] == "cancelled"
    assert row["status"] == "מבוטל"
    assert row["can_cancel"] is False
