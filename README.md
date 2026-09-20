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

### 11. Discover Reports Catalog
```bash
curl -X GET http://127.0.0.1:8001/reports/catalog \
  -H "Authorization: Bearer $JWT"
```
Returns the full catalog of all 21 available Kibbutz reports with their machine-readable **slugs**, descriptions, expected parameters, and copy-paste sample queries:

| Slug | ID | Name | Parameters |
|---|---|---|---|
| `personal_budget` | 1 | תקציב אישי | `year`, `from_month`, `to_month` |
| `travel_sedernet` | 2 | חיובי נסיעות סדרנט | `from_date`, `to_date` |
| `dining_room_by_buyer` | 3 | קניות חדר אוכל לפי קונה | `from_date`, `to_date` |
| `dining_room_by_date` | 4 | קניות חדר אוכל לפי תאריך | `from_date`, `to_date` |
| `kolbo` | 5 | קניות כולבו | `from_date`, `to_date` |
| `makolit` | 6 | קניות מרכולית | `from_date`, `to_date` |
| `stores` | 7 | קניות מחנויות | `from_date`, `to_date` |
| `complementary_medicine` | 8 | רפואה משלימה | `from_date`, `to_date` |
| `budget_transfers` | 9 | העברות בין תקציבים | `from_date`, `to_date` |
| `outside_workers_costing` | 10 | תמחיר עובדי חוץ | `from_date`, `to_date` |
| `outside_workers` | 11 | דוח עובדי חוץ | `from_date`, `to_date` |
| `students` | 12 | דוח סטודנטים | `from_date`, `to_date` |
| `studies_budget` | 13 | תקציב לימודים | `from_date`, `to_date` |
| `pharmacy` | 14 | חיובי תרופות | `from_date`, `to_date` |
| `electricity` | 16 | חיובי חשמל חברים | `from_date`, `to_date` |
| `eyeglasses` | 17 | חיובי משקפיים | `from_date`, `to_date` |
| `classes` | 18 | חוגים | `from_date`, `to_date` |
| `pub_by_buyer` | 26 | קניות פאב לפי קונה | `from_date`, `to_date` |
| `pub_by_date` | 27 | קניות פאב לפי תאריך | `from_date`, `to_date` |
| `residents_ledger` | 69 | כרטסת תושבים | `from_date`, `to_date` |
| `water` | 90 | פירוט צריכת מים | *(none)* |

### 12. Generate and View Reports (JSON or PDF/CSV Export)
Accepts either human/machine-readable **slugs** (recommended) or numeric IDs:

```bash
# 1. View personal budget as structured JSON (using slug 'personal_budget')
curl -X GET "http://127.0.0.1:8001/reports/generate?report=personal_budget&format=json&year=2026&from_month=8&to_month=8" \
  -H "Authorization: Bearer $JWT"

# 2. Download official PDF statement
curl -X GET "http://127.0.0.1:8001/reports/generate?report=personal_budget&format=pdf&year=2026&from_month=8&to_month=8" \
  -H "Authorization: Bearer $JWT" \
  -o budget_report_08_2026.pdf

# 3. View Kolbo supermarket itemized charges (using slug 'kolbo')
curl -X GET "http://127.0.0.1:8001/reports/generate?report=kolbo&format=json&from_date=01/08/2026&to_date=31/08/2026" \
  -H "Authorization: Bearer $JWT"

# 4. View Water consumption report (using slug 'water')
curl -X GET "http://127.0.0.1:8001/reports/generate?report=water&format=json" \
  -H "Authorization: Bearer $JWT"
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
