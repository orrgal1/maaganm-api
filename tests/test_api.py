import os
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# Force MOCK_MODE for unit testing
os.environ["MOCK_MODE"] = "true"
import config
config.MOCK_MODE = True

from main import app
from security import generate_jwt_token

@pytest.fixture
def auth_token():
    return generate_jwt_token(subject="test-agent", expires_days=1)

@pytest.fixture
def auth_headers(auth_token):
    return {
        "Authorization": f"Bearer {auth_token}",
        "X-Budget-Username": "80241",
        "X-Budget-Password": "secret_password"
    }

@pytest.mark.asyncio
async def test_public_openapi_docs():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert "paths" in schema
        assert "/health" in schema["paths"]
        assert "/balance" in schema["paths"]
        assert "/transfer" in schema["paths"]

        docs_resp = await client.get("/docs")
        assert docs_resp.status_code == 200

@pytest.mark.asyncio
async def test_unauthorized_without_token():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "missing_token"

@pytest.mark.asyncio
async def test_health_endpoint(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "mock"
        assert data["authenticated"] is True
        assert data["user"] == "80241"

@pytest.mark.asyncio
async def test_balance_endpoint(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/balance", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "budget_balance_ils" in data
        assert isinstance(data["budget_balance_ils"], (int, float))

@pytest.mark.asyncio
async def test_recipients_search(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/recipients/search?q=כהן", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) > 0
        assert "כהן" in data["results"][0]["display_name"]

@pytest.mark.asyncio
async def test_transactions_list(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/transactions", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "transactions" in data
        assert data["total"] >= 1
        first = data["transactions"][0]
        assert "transaction_id" in first
        assert "amount_ils" in first

@pytest.mark.asyncio
async def test_transfer_flow_with_otp(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Submit transfer
        transfer_payload = {
            "recipient_hid": "HID_80101",
            "recipient_name": "כהן דוד",
            "amount_ils": 50.0,
            "details_for_receiver": "בדיקת API",
            "details_for_sender": "העברה לדוד",
            "transaction_type": 1
        }
        resp = await client.post("/transfer", json=transfer_payload, headers=auth_headers)
        assert resp.status_code == 200
        t_data = resp.json()
        assert t_data["status"] == "pending_otp"
        assert t_data["requires_otp"] is True
        trx_id = t_data["transaction_id"]
        assert trx_id is not None

        # 2. Confirm OTP
        otp_payload = {
            "transaction_id": trx_id,
            "otp_code": "123456"
        }
        approve_resp = await client.post("/transfer/approve", json=otp_payload, headers=auth_headers)
        assert approve_resp.status_code == 200
        a_data = approve_resp.json()
        assert a_data["status"] == "approved"
        assert a_data["transaction_id"] == trx_id

@pytest.mark.asyncio
async def test_idempotency_on_transfer(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers_with_idempotency = {
            **auth_headers,
            "Idempotency-Key": "idemp-test-999"
        }
        payload = {
            "recipient_hid": "HID_80102",
            "recipient_name": "לוי שרה",
            "amount_ils": 25.50,
            "details_for_receiver": "קפה ועוגה"
        }
        # First request
        resp1 = await client.post("/transfer", json=payload, headers=headers_with_idempotency)
        assert resp1.status_code == 200
        data1 = resp1.json()

        # Second request with same idempotency key
        resp2 = await client.post("/transfer", json=payload, headers=headers_with_idempotency)
        assert resp2.status_code == 200
        data2 = resp2.json()

        # Should return cached identical response
        assert data1 == data2

@pytest.mark.asyncio
async def test_cancel_transaction(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/transactions/90412", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"
        assert data["transaction_line_id"] == "90412"

@pytest.mark.asyncio
async def test_pending_approvals_and_decline(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Get pending
        resp = await client.get("/approvals/pending", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data

        # Decline one
        dec_resp = await client.delete("/approvals/90501", headers=auth_headers)
        assert dec_resp.status_code == 200
        dec_data = dec_resp.json()
        assert dec_data["status"] == "declined"

@pytest.mark.asyncio
async def test_authorized_users(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # List
        resp = await client.get("/account/authorized-users", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data

        # Set
        put_payload = {
            "user_id": "80102",
            "user_name": "לוי שרה",
            "is_authorized": True
        }
        put_resp = await client.put("/account/authorized-users", json=put_payload, headers=auth_headers)
        assert put_resp.status_code == 200
        put_data = put_resp.json()
        assert put_data["status"] == "ok"
        assert put_data["is_authorized"] is True

@pytest.mark.asyncio
async def test_reports_catalog(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/reports/catalog", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "reports" in data
        assert data["total"] >= 20
        first = data["reports"][0]
        assert first["id"] == 1
        assert first["slug"] == "personal_budget"
        assert first["name"] == "תקציב אישי"
        assert "example_query" in first

@pytest.mark.asyncio
async def test_reports_generate_by_slug(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/reports/generate?report=personal_budget&format=json&year=2026&from_month=1&to_month=8", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["report_id"] == 1
        assert data["report_slug"] == "personal_budget"
        assert "summary" in data
        assert "items" in data
        assert len(data["items"]) >= 1

@pytest.mark.asyncio
async def test_reports_invalid_slug(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/reports/generate?report=non_existent_report", headers=auth_headers)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"]["code"] == "invalid_report_id"
        assert "Available options" in data["error"]["message"]
