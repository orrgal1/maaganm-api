# Maagan Michael Budget Local API Wrapper

A persistent local FastAPI service wrapping the Kibbutz Maagan Michael budget transfer portal (`https://budget.mmm.org.il/`) behind an authenticated JSON HTTP API, designed for remote AI agents and automated workflows with **JWT authentication** and an unauthenticated **OpenAPI** inspection endpoint.

---

## 🌐 Public Deployment & Access

- **Local Base URL:** `http://127.0.0.1:8001`
- **OpenAPI Schema (Inspection):** `http://127.0.0.1:8001/openapi.json`
- **Interactive Swagger UI:** `http://127.0.0.1:8001/docs`
- **Authentication:** `Authorization: Bearer <JWT_TOKEN>`
- **Caller Credentials:** Dynamic headers `X-Budget-Username` and `X-Budget-Password` (or fallback to local `.env.local`).

---

## 🔐 Credentials Setup (Local Testing)

Credentials are provided dynamically by the caller via request headers, but can also be seeded in a local untracked `.env.local` file for local testing and background tasks.

### Copy & Paste Credentials Script

Run this command in the project directory to write your credentials into `.env.local` (which is git-ignored and never committed):

```bash
cat << 'EOF' > .env.local
# Kibbutz Maagan Michael Budget Portal Credentials
BUDGET_USERNAME="YOUR_EMPLOYEE_ID_HERE"
BUDGET_PASSWORD="YOUR_PORTAL_PASSWORD_HERE"

# Local Server Settings
HOST="127.0.0.1"
PORT=8001
MOCK_MODE=false
EOF
chmod 600 .env.local
echo "Saved credentials to .env.local"
```

> **Note:** The username is your employee number including the trailing 0 or 1, as used on `budget.mmm.org.il`.

---

## 🔑 Generating JWT Tokens for Remote Agents

A JWT token generator utility is included:

```bash
./venv/bin/python generate_token.py --subject "budget-agent" --days 365
```

The script automatically generates a secure token and saves it to `.current_jwt` (which is untracked by git).

---

## 🚀 Service Architecture & Guarantees

- **Caller-Provided Credentials:** Remote callers can pass `X-Budget-Username` and `X-Budget-Password` headers on every request to authenticate as different kibbutz members, or omit them to use the local `.env.local` defaults.
- **Session Pooling:** Maintains high-performance authenticated HTTP client sessions per user with automated cookie handling (`.AspNet.ApplicationCookie`).
- **Direct ASP.NET MVC Integration:** Direct, sub-100ms response times by calling backend controller actions directly.
- **JWT Authentication:** Strict signature and expiration verification on every private endpoint using `HS256`.
- **OpenAPI Inspection:** `/openapi.json` and `/docs` are open for external agent discovery without requiring authentication headers.
- **Idempotency Protection:** Accepts `Idempotency-Key` headers on mutations (`/transfer`, `/transfer/approve`, `/transactions/{id}`) to prevent double-transfers or duplicate requests.
- **SMS OTP Flow:** Transparently manages the 2-step transfer flow required by the portal (stage transfer -> SMS OTP code -> verify & complete).
- **Safe Logging:** Automatically masks passwords, JWT signatures, and sensitive data from all service logs.

---

## 📚 API Reference & Curl Examples

### 1. Inspect OpenAPI Specification (Unauthenticated)
```bash
curl -s http://127.0.0.1:8001/openapi.json | jq .
```

### 2. Check Health & Authentication Status
```bash
curl -X GET http://127.0.0.1:8001/health \
  -H "Authorization: Bearer $JWT" \
  -H "X-Budget-Username: 801230" \
  -H "X-Budget-Password: mysecretpassword"
```

### 3. Check Current Budget & Savings Balance
```bash
curl -X GET http://127.0.0.1:8001/balance \
  -H "Authorization: Bearer $JWT"
```

### 4. Search Members Directory (Recipients)
```bash
curl -X GET "http://127.0.0.1:8001/recipients/search?q=כהן" \
  -H "Authorization: Bearer $JWT"
```

### 5. Transfer Money (Step 1: Stage Transfer)
```bash
curl -X POST http://127.0.0.1:8001/transfer \
  -H "Authorization: Bearer $JWT" \
  -H "Idempotency-Key: trx-req-001" \
  -H "Content-Type: application/json" \
  -d '{
    "recipient_hid": "HID_80101",
    "recipient_name": "כהן דוד",
    "amount_ils": 50.00,
    "details_for_receiver": "החזר הוצאות",
    "details_for_sender": "העברה לדוד",
    "transaction_type": 1
  }'
```
Response:
```json
{
  "status": "pending_otp",
  "transaction_id": "90821",
  "requires_otp": true,
  "message": "Transfer submitted. Confirmation OTP sent via SMS for transaction 90821."
}
```

### 6. Confirm Transfer with SMS OTP (Step 2: Approve Transfer)
```bash
curl -X POST http://127.0.0.1:8001/transfer/approve \
  -H "Authorization: Bearer $JWT" \
  -H "Idempotency-Key: trx-approve-001" \
  -H "Content-Type: application/json" \
  -d '{
    "transaction_id": "90821",
    "otp_code": "482910"
  }'
```

### 7. View Transactions History
```bash
curl -X GET "http://127.0.0.1:8001/transactions?from_date=01/01/2026&to_date=20/09/2026" \
  -H "Authorization: Bearer $JWT"
```

### 8. Cancel a Transaction Line
```bash
curl -X DELETE http://127.0.0.1:8001/transactions/90821 \
  -H "Authorization: Bearer $JWT"
```

### 9. View Pending Incoming Charge Requests
```bash
curl -X GET http://127.0.0.1:8001/approvals/pending \
  -H "Authorization: Bearer $JWT"
```

### 10. Pre-Authorized Members (Direct Charges)
```bash
# List authorized members
curl -X GET http://127.0.0.1:8001/account/authorized-users \
  -H "Authorization: Bearer $JWT"

# Grant or revoke authorization
curl -X PUT http://127.0.0.1:8001/account/authorized-users \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "80101",
    "user_name": "כהן דוד",
    "is_authorized": true
  }'
```

### 11. List Available Report Types
```bash
curl -X GET http://127.0.0.1:8001/reports/types \
  -H "Authorization: Bearer $JWT"
```
Returns all 20+ available Kibbutz reports (Personal budget, Kolbo supermarket, Dining room meals, Electricity, Water consumption, etc.) and their required parameter types (`monthly`, `date_range`, or `none`).

### 12. Generate and View Reports (JSON or PDF/CSV Export)
```bash
# View personal monthly budget report as structured JSON with line items and balance totals
curl -X GET "http://127.0.0.1:8001/reports/generate?report_id=1&format=json&year=2026&from_month=8&to_month=8" \
  -H "Authorization: Bearer $JWT"

# Download official PDF statement
curl -X GET "http://127.0.0.1:8001/reports/generate?report_id=1&format=pdf&year=2026&from_month=8&to_month=8" \
  -H "Authorization: Bearer $JWT" \
  -o budget_report_08_2026.pdf

# Download Kolbo supermarket itemized charges as CSV
curl -X GET "http://127.0.0.1:8001/reports/generate?report_id=5&format=csv&from_date=01/08/2026&to_date=31/08/2026" \
  -H "Authorization: Bearer $JWT" \
  -o kolbo_charges.csv
```

---

## 🧪 Running Tests

The test suite runs with pytest and includes coverage for authentication, caller-provided credentials, mock data, and idempotency guarantees:

```bash
./venv/bin/pytest tests/test_api.py -v
```

---

## 🏃 Running the Local Server

```bash
./venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001 --reload
```
