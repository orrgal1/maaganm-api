import asyncio
import logging
import re
import csv
import io
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from bs4 import BeautifulSoup
import httpx

import config
from errors import APIException
from idempotency import pending_transfer_store

logger = logging.getLogger("budget_driver")

REPORT_DEFINITIONS = [
    {
        "id": 1,
        "slug": "personal_budget",
        "name": "תקציב אישי",
        "description": "Personal monthly budget statement with allowances, expenses, and closing balance",
        "parameter_type": "monthly",
        "required_parameters": ["year", "from_month", "to_month"],
        "example_args": {"report": "personal_budget", "format": "json", "year": 2026, "from_month": 1, "to_month": 8}
    },
    {
        "id": 2,
        "slug": "travel_sedernet",
        "name": "חיובי נסיעות סדרנט",
        "description": "SederNet vehicle travel charges and mileage",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "travel_sedernet", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 3,
        "slug": "dining_room_by_buyer",
        "name": "קניות חדר אוכל לפי קונה",
        "description": "Dining room meal charges itemized by family member / buyer",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "dining_room_by_buyer", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 4,
        "slug": "dining_room_by_date",
        "name": "קניות חדר אוכל לפי תאריך",
        "description": "Dining room meal charges itemized chronologically by date",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "dining_room_by_date", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 5,
        "slug": "kolbo",
        "name": "קניות כולבו",
        "description": "Kolbo supermarket grocery and household itemized purchases",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "kolbo", "format": "json", "from_date": "01/08/2026", "to_date": "31/08/2026"}
    },
    {
        "id": 6,
        "slug": "makolit",
        "name": "קניות מרכולית",
        "description": "Makolit local convenience store grocery purchases",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "makolit", "format": "json", "from_date": "01/08/2026", "to_date": "31/08/2026"}
    },
    {
        "id": 7,
        "slug": "stores",
        "name": "קניות מחנויות",
        "description": "Local kibbutz branch store charges",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "stores", "format": "json", "from_date": "01/08/2026", "to_date": "31/08/2026"}
    },
    {
        "id": 8,
        "slug": "complementary_medicine",
        "name": "רפואה משלימה",
        "description": "Complementary medicine treatments and wellness services",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "complementary_medicine", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 9,
        "slug": "budget_transfers",
        "name": "העברות בין תקציבים",
        "description": "Inter-budget transfers history between members",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "budget_transfers", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 10,
        "slug": "outside_workers_costing",
        "name": "תמחיר עובדי חוץ",
        "description": "Outside employment costing analysis",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "outside_workers_costing", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 11,
        "slug": "outside_workers",
        "name": "דוח עובדי חוץ",
        "description": "Outside employment statements and income records",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "outside_workers", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 12,
        "slug": "students",
        "name": "דוח סטודנטים",
        "description": "Higher education student expenses and allowances",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "students", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 13,
        "slug": "studies_budget",
        "name": "תקציב לימודים",
        "description": "Educational development budget statement",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "studies_budget", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 14,
        "slug": "pharmacy",
        "name": "חיובי תרופות",
        "description": "Pharmacy & prescription medical charges",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "pharmacy", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 16,
        "slug": "electricity",
        "name": "חיובי חשמל חברים",
        "description": "Residential household electricity meter charges",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "electricity", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 17,
        "slug": "eyeglasses",
        "name": "חיובי משקפיים",
        "description": "Eyeglasses and optical subsidies and charges",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "eyeglasses", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 18,
        "slug": "classes",
        "name": "חוגים",
        "description": "Community sports, arts, and educational classes",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "classes", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 26,
        "slug": "pub_by_buyer",
        "name": "קניות פאב לפי קונה",
        "description": "Kibbutz pub beverage and snack purchases by buyer",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "pub_by_buyer", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 27,
        "slug": "pub_by_date",
        "name": "קניות פאב לפי תאריך",
        "description": "Kibbutz pub purchases chronologically by date",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "pub_by_date", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 69,
        "slug": "residents_ledger",
        "name": "כרטסת תושבים",
        "description": "Resident accounting ledger card",
        "parameter_type": "date_range",
        "required_parameters": ["from_date", "to_date"],
        "example_args": {"report": "residents_ledger", "format": "json", "from_date": "01/01/2026", "to_date": "20/09/2026"}
    },
    {
        "id": 90,
        "slug": "water",
        "name": "פירוט צריכת מים",
        "description": "Residential water meter consumption statement",
        "parameter_type": "none",
        "required_parameters": [],
        "example_args": {"report": "water", "format": "json"}
    }
]

class BudgetDriver:
    """
    Async HTTP client driver for https://budget.mmm.org.il/.
    Maintains authenticated sessions per user, parses ASP.NET MVC responses,
    and supports both live scraping and mock mode for testing/offline environments.
    """

    def __init__(self):
        self._clients: Dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()
        self._recipients_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

        # In-memory mock database for mock mode
        self._mock_users = {
            "mock_user": {
                "user_id": "80241",
                "name": "ישראל ישראלי",
                "phone": "050-1234567",
                "budget_balance": 1850.40,
                "savings_balance": 24000.00
            }
        }
        self._mock_directory = [
            {"user_id": "80101", "user_hid": "HID_80101", "display_name": "כהן דוד", "department": "מדגה"},
            {"user_id": "80102", "user_hid": "HID_80102", "display_name": "לוי שרה", "department": "חינוך"},
            {"user_id": "80103", "user_hid": "HID_80103", "display_name": "ישראלי ישראל", "department": "פלסאון"},
            {"user_id": "80104", "user_hid": "HID_80104", "display_name": "אברהם יעל", "department": "הנהלת חשבונות"},
            {"user_id": "80105", "user_hid": "HID_80105", "display_name": "ברקאי ענבר", "department": "מערכות מידע"}
        ]
        self._mock_transactions = [
            {
                "transaction_id": "90412",
                "display_id": "1420/90412",
                "date": "18/09/2026",
                "type": "העברה רגילה",
                "counterparty": "כהן דוד",
                "details": "החזר הוצאות דלק",
                "amount_ils": -120.00,
                "balance_ils": 1850.40,
                "status": "אושר",
                "can_cancel": True,
                "can_approve": False
            },
            {
                "transaction_id": "90380",
                "display_id": "1418/90380",
                "date": "14/09/2026",
                "type": "זיכוי מתקציב",
                "counterparty": "לוי שרה",
                "details": "עוגה ליום הולדת",
                "amount_ils": 85.00,
                "balance_ils": 1970.40,
                "status": "אושר",
                "can_cancel": False,
                "can_approve": False
            }
        ]
        self._mock_authorized_users = [
            {"user_id": "80101", "user_name": "כהן דוד", "is_authorized": True},
            {"user_id": "80102", "user_name": "לוי שרה", "is_authorized": False}
        ]
        self._mock_pending_approvals = [
            {
                "transaction_id": "90501",
                "display_id": "1425/90501",
                "date": "20/09/2026",
                "initiator": "כהן דוד",
                "amount_ils": 50.00,
                "details": "ארוחת צהריים משותפת"
            }
        ]

    async def get_client(self, username: str, password: str) -> httpx.AsyncClient:
        """Retrieves or creates an authenticated HTTP client for the given credentials."""
        session_key = f"{username}:{password}"
        async with self._lock:
            client = self._clients.get(session_key)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(
                    base_url=config.BUDGET_BASE_URL,
                    timeout=35.0,
                    follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7"
                    }
                )
                self._clients[session_key] = client
                if not config.MOCK_MODE:
                    await self._login_client(client, username, password)
            return client

    async def _login_client(self, client: httpx.AsyncClient, username: str, password: str):
        """Performs form login against ASP.NET MVC /Home/Login."""
        logger.info("Authenticating budget session.")
        resp = await client.post(
            "/Home/Login?",
            data={
                "UserName": username,
                "Password": password
            }
        )
        
        # Check validation errors in returned HTML
        if "שם משתמש או סיסמא שגויים" in resp.text:
            logger.warning("Budget authentication failed.")
            raise APIException(
                status_code=401,
                code="invalid_credentials",
                message="Authentication failed at budget.mmm.org.il: Incorrect username or password."
            )

        if resp.status_code >= 400:
            raise APIException(
                status_code=502,
                code="upstream_error",
                message=f"Budget site returned HTTP {resp.status_code} during authentication."
            )

        logger.info("Budget authentication succeeded.")

    async def check_login_status(self, username: str, password: str) -> Tuple[bool, Optional[str]]:
        """Verifies if the credentials are valid and active."""
        if config.MOCK_MODE:
            return True, username

        try:
            client = await self.get_client(username, password)
            resp = await client.get("/Budget/SendMoney")
            # If redirected back to login page, credentials failed or session expired
            if "UserName" in resp.text and "Password" in resp.text and "התחבר" in resp.text:
                return False, None
            return True, username
        except Exception:
            logger.warning("Budget login status check failed.")
            return False, None

    async def get_balance(self, username: str, password: str) -> Dict[str, Any]:
        """Fetches current budget balance, savings balance, and registered OTP phone."""
        if config.MOCK_MODE:
            user_data = self._mock_users.get(username, {
                "user_id": username,
                "name": "ישראל ישראלי",
                "phone": "050-1234567",
                "budget_balance": 1850.40,
                "savings_balance": 24000.00
            })
            return {
                "budget_balance_ils": user_data["budget_balance"],
                "savings_balance_ils": user_data.get("savings_balance"),
                "phone_for_otp": user_data.get("phone"),
                "user_name": user_data.get("name"),
                "user_id": user_data.get("user_id")
            }

        client = await self.get_client(username, password)
        resp = await client.get("/Budget/SendMoney")
        if resp.status_code != 200:
            raise APIException(
                status_code=502,
                code="upstream_error",
                message=f"Failed to fetch balance page (HTTP {resp.status_code})"
            )

        soup = BeautifulSoup(resp.text, "html.parser")
        budget_balance = 0.0
        savings_balance = 0.0
        phone_for_otp = None
        user_name = None

        top_msg = soup.find(class_="top-message")
        if top_msg:
            top_text = top_msg.get_text()
            phone_match = re.search(r'05\d-?\d{7}', top_text)
            if phone_match:
                phone_for_otp = phone_match.group(0)

            budget_match = re.search(r'יתרת תקציב היא:\s*₪?\s*([-\d,.]+)', top_text)
            if budget_match:
                try:
                    budget_balance = float(budget_match.group(1).replace(",", ""))
                except ValueError:
                    pass

            savings_match = re.search(r'יתרת חסכונך היא:\s*₪?\s*([-\d,.]+)', top_text)
            if savings_match:
                try:
                    savings_balance = float(savings_match.group(1).replace(",", ""))
                except ValueError:
                    pass

        header_div = soup.find(class_="page-header")
        if header_div:
            # Contains e.g. "3850 orrgal@gmail.com"
            for h in header_div.find_all(["h1", "h4", "div", "span"]):
                txt = h.get_text(strip=True)
                if "@" in txt or username in txt:
                    user_name = txt
                    break

        return {
            "budget_balance_ils": budget_balance,
            "savings_balance_ils": savings_balance,
            "phone_for_otp": phone_for_otp,
            "user_name": user_name,
            "user_id": username
        }

    async def search_recipients(
        self,
        username: str,
        password: str,
        query: str = "",
        transaction_type: int = 1
    ) -> List[Dict[str, Any]]:
        """Searches kibbutz members directory for transfer recipients."""
        if config.MOCK_MODE:
            results = []
            q = query.strip().lower()
            for u in self._mock_directory:
                if not q or q in u["display_name"].lower() or q in u["user_id"]:
                    results.append(u)
            return results

        # Check in-memory cache (TTL: 1 hour)
        cache_key = f"recipients_{transaction_type}"
        now_ts = time.time()
        cached = self._recipients_cache.get(cache_key)
        all_recipients = None

        if cached and (now_ts - cached[0] < 3600):
            all_recipients = cached[1]
        else:
            client = await self.get_client(username, password)
            resp = await client.post(
                "/Budget/GetUsers",
                data={"transactionType": transaction_type}
            )
            if resp.status_code != 200:
                raise APIException(
                    status_code=502,
                    code="upstream_error",
                    message=f"Failed to retrieve member list (HTTP {resp.status_code})"
                )

            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table")
            all_recipients = []
            if table:
                rows = table.find_all("tr")
                for r in rows:
                    tds = r.find_all("td")
                    if len(tds) >= 3:
                        user_hid = tds[0].get_text(strip=True)
                        sort_order = tds[1].get_text(strip=True)
                        display_name = tds[2].get_text(strip=True)
                        dept = tds[3].get_text(strip=True) if len(tds) > 3 else None
                        all_recipients.append({
                            "user_id": user_hid,
                            "user_hid": user_hid,
                            "display_name": display_name,
                            "department": dept
                        })
            self._recipients_cache[cache_key] = (now_ts, all_recipients)

        q = query.strip().lower()
        if not q:
            return all_recipients[:50]

        filtered = [
            r for r in all_recipients
            if q in r["display_name"].lower() or q in r["user_hid"]
        ]
        return filtered

    async def get_transactions(
        self,
        username: str,
        password: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        types: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetches transactions table."""
        if config.MOCK_MODE:
            return self._mock_transactions

        # Default: 3 months ago to today, DD/MM/YYYY
        now = datetime.now()
        f_date = from_date or (now - timedelta(days=90)).strftime("%d/%m/%Y")
        t_date = to_date or now.strftime("%d/%m/%Y")
        t_types = types or "Regular,FromSavings,ToSavings,Charge,BulkCharge"

        client = await self.get_client(username, password)
        data_param = f"{f_date}¥{t_date}¥{t_types}"
        resp = await client.get("/Budget/GetTransactionsTable", params={"data": data_param})

        if resp.status_code != 200:
            raise APIException(
                status_code=502,
                code="upstream_error",
                message=f"Failed to fetch transactions table (HTTP {resp.status_code})"
            )

        status_map = {
            "Executed": "בוצע",
            "Cancelled": "מבוטל",
            "Approved": "מאושר",
            "Unapproved": "ממתין לאישור",
            "Declined": "נדחה",
            "Delayed": "מעוכב"
        }

        soup = BeautifulSoup(resp.text, "html.parser")
        transactions = []
        rows = soup.find_all("tr")
        for r in rows:
            tds = r.find_all("td")
            if len(tds) >= 11:
                trx_id = tds[0].get_text(strip=True)
                disp_id = tds[1].get_text(strip=True)
                date_str = tds[2].get_text(strip=True)
                sender = tds[3].get_text(strip=True)
                receiver = tds[4].get_text(strip=True)
                try:
                    amount = float(tds[5].get_text(strip=True).replace(",", "").replace("₪", ""))
                except ValueError:
                    amount = 0.0
                sender_note = tds[6].get_text(strip=True)
                receiver_note = tds[7].get_text(strip=True)
                can_approve = tds[8].get_text(strip=True).lower() == "true"
                can_cancel = tds[9].get_text(strip=True).lower() == "true"
                raw_status = tds[10].get_text(strip=True)
                status_str = status_map.get(raw_status, raw_status)

                transactions.append({
                    "transaction_id": trx_id,
                    "display_id": f"{disp_id}/{trx_id}" if disp_id else trx_id,
                    "date": date_str,
                    "type": "העברה",
                    "counterparty": receiver if sender == username or "גל אור" in sender else sender,
                    "details": sender_note or receiver_note,
                    "amount_ils": amount,
                    "balance_ils": None,
                    "status": status_str,
                    "can_cancel": can_cancel,
                    "can_approve": can_approve
                })

        return transactions

    async def transfer(
        self,
        username: str,
        password: str,
        recipient_hid: str,
        recipient_name: str,
        amount_ils: float,
        details_receiver: str = "",
        details_sender: str = "",
        transaction_type: int = 1
    ) -> Dict[str, Any]:
        """Submits a money transfer to another member."""
        if config.MOCK_MODE:
            trx_id = f"TRX_{int(datetime.now().timestamp())}"
            pending_transfer_store.stage_transfer(
                transaction_id=trx_id,
                recipient_id=recipient_hid,
                recipient_name=recipient_name,
                amount_ils=amount_ils,
                details_receiver=details_receiver,
                details_sender=details_sender
            )
            return {
                "status": "pending_otp",
                "transaction_id": trx_id,
                "requires_otp": True,
                "message": f"Transfer of {amount_ils:.2f} ILS to {recipient_name} staged. SMS OTP sent to registered phone."
            }

        client = await self.get_client(username, password)
        form_payload = {
            "Transactions[0].Type": "Regular",
            "Transactions[0].ReceiverDisplayName": recipient_name,
            "Transactions[0].ReceiverHID": recipient_hid,
            "Transactions[0].CurCode": "0",
            "Transactions[0].TrnsTypeId": "0",
            "Transactions[0].Amount": f"{amount_ils:.2f}",
            "Transactions[0].DetailsForReceiver": details_receiver[:20] if details_receiver else "",
            "Transactions[0].DetailsForSender": details_sender[:20] if details_sender else ""
        }

        resp = await client.post("/Budget/SendMoney", data=form_payload)
        
        # Check if redirected to ApproveTransaction with TransactionId
        trx_match = re.search(r'TransactionId=(\d+)', str(resp.url)) or re.search(r'TransactionId=(\d+)', resp.text)
        if trx_match:
            trx_id = trx_match.group(1)
            pending_transfer_store.stage_transfer(
                transaction_id=trx_id,
                recipient_id=recipient_hid,
                recipient_name=recipient_name,
                amount_ils=amount_ils,
                details_receiver=details_receiver,
                details_sender=details_sender
            )
            return {
                "status": "pending_otp",
                "transaction_id": trx_id,
                "requires_otp": True,
                "message": f"Transfer submitted. Confirmation OTP sent via SMS for transaction {trx_id}."
            }

        if resp.status_code >= 400:
            raise APIException(
                status_code=502,
                code="transfer_failed",
                message=f"Failed to submit transfer: HTTP {resp.status_code}"
            )

        return {
            "status": "submitted",
            "transaction_id": None,
            "requires_otp": False,
            "message": "Transfer submitted successfully."
        }

    async def approve_otp(
        self,
        username: str,
        password: str,
        transaction_id: str,
        otp_code: str
    ) -> Dict[str, Any]:
        """Confirms SMS OTP for staged transfer."""
        if config.MOCK_MODE:
            pending = pending_transfer_store.get_transfer(transaction_id)
            if not pending:
                raise APIException(
                    status_code=404,
                    code="transfer_not_found",
                    message=f"Pending transfer {transaction_id} not found or expired"
                )
            pending_transfer_store.remove_transfer(transaction_id)
            return {
                "status": "approved",
                "transaction_id": transaction_id,
                "message": f"Transfer {transaction_id} successfully verified with OTP and approved."
            }

        client = await self.get_client(username, password)
        resp = await client.post(
            "/Budget/ApproveTransaction",
            data={
                "TransactionId": transaction_id,
                "Password": otp_code
            }
        )

        if "אושרה" in resp.text or resp.status_code == 200:
            pending_transfer_store.remove_transfer(transaction_id)
            return {
                "status": "approved",
                "transaction_id": transaction_id,
                "message": f"Transfer {transaction_id} approved successfully."
            }

        raise APIException(
            status_code=400,
            code="otp_verification_failed",
            message="OTP verification failed. Check code and try again."
        )

    async def cancel_transaction(
        self,
        username: str,
        password: str,
        transaction_line_id: str
    ) -> Dict[str, Any]:
        """Cancels a pending or completed line transaction."""
        if config.MOCK_MODE:
            self._mock_transactions = [
                t for t in self._mock_transactions if t["transaction_id"] != transaction_line_id
            ]
            return {
                "status": "cancelled",
                "transaction_line_id": transaction_line_id,
                "message": f"Transaction line {transaction_line_id} successfully cancelled."
            }

        client = await self.get_client(username, password)
        resp = await client.request(
            "DELETE",
            f"/Budget/MyTransactions?transactionLineId={transaction_line_id}"
        )
        if resp.status_code != 200:
            raise APIException(
                status_code=502,
                code="upstream_error",
                message=f"Cancel request failed with HTTP {resp.status_code}"
            )

        return {
            "status": "cancelled",
            "transaction_line_id": transaction_line_id,
            "message": f"Transaction {transaction_line_id} cancelled."
        }

    def get_report_types(self) -> List[Dict[str, Any]]:
        """Returns catalogue of supported report types and their required parameters."""
        return REPORT_DEFINITIONS

    def resolve_report(self, report_identifier: Any) -> Dict[str, Any]:
        """
        Resolves report by numeric ID (e.g. 1, 5) or slug (e.g. 'personal_budget', 'kolbo').
        """
        rep = None
        s_id = str(report_identifier).strip()
        if s_id.isdigit():
            num_id = int(s_id)
            rep = next((r for r in REPORT_DEFINITIONS if r["id"] == num_id), None)
        else:
            slug_norm = s_id.lower()
            rep = next((r for r in REPORT_DEFINITIONS if r["slug"] == slug_norm), None)

        if not rep:
            valid_options = ", ".join([f"'{r['slug']}' ({r['id']})" for r in REPORT_DEFINITIONS])
            raise APIException(
                status_code=400,
                code="invalid_report_id",
                message=f"Unknown report identifier '{report_identifier}'. Available options: {valid_options}"
            )
        return rep

    async def generate_report(
        self,
        username: str,
        password: str,
        report: Any,
        format: str = "json",
        year: Optional[int] = None,
        from_month: Optional[int] = None,
        to_month: Optional[int] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None
    ) -> Tuple[Any, str]:
        """
        Generates report from /Reports/Report.
        Accepts either numeric report ID (e.g. 1) or string slug (e.g. 'personal_budget').
        Returns (result_data, content_type).
        """
        rep_def = self.resolve_report(report)
        report_id = rep_def["id"]
        report_slug = rep_def["slug"]
        rep_name = rep_def["name"]
        param_type = rep_def["parameter_type"]

        now = datetime.now()
        cur_year = year or now.year
        f_month = from_month or 1
        t_month = to_month or (now.month if cur_year == now.year else 12)
        f_date = from_date or (now - timedelta(days=90)).strftime("%d/%m/%Y")
        t_date = to_date or now.strftime("%d/%m/%Y")

        if config.MOCK_MODE:
            if format == "json":
                mock_items = [
                    {
                        "date": f"01/{f_month:02d}/{cur_year}",
                        "document_no": "213000101",
                        "details": "הקצבה חודשית",
                        "credit_ils": 9245.78,
                        "debit_ils": 0.0,
                        "quantity": 1.0,
                        "month": f"{f_month}/{cur_year}"
                    },
                    {
                        "date": f"15/{f_month:02d}/{cur_year}",
                        "document_no": "213000102",
                        "details": "קניות כולבו",
                        "credit_ils": 0.0,
                        "debit_ils": 420.50,
                        "quantity": 1.0,
                        "month": f"{f_month}/{cur_year}"
                    }
                ]
                return {
                    "report_id": report_id,
                    "report_slug": report_slug,
                    "report_name": rep_name,
                    "period": f"{f_month}/{cur_year} - {t_month}/{cur_year}" if param_type == "monthly" else f"{f_date} - {t_date}",
                    "summary": {
                        "opening_balance_ils": 1500.00,
                        "total_credits_ils": 9245.78,
                        "total_debits_ils": 420.50,
                        "interest_ils": 0.0,
                        "closing_balance_ils": 10325.28
                    },
                    "items": mock_items,
                    "total_items": len(mock_items)
                }, "application/json"
            elif format == "csv":
                csv_bytes = b"Date,Document,Details,Credit,Debit\n01/01/2026,101,Mock,9000,0\n"
                return csv_bytes, "text/csv"
            elif format == "pdf":
                return b"%PDF-1.7 mock", "application/pdf"
            else:
                return b"mock xls", "application/vnd.ms-excel"

        # Build form payload for live report
        form_data: Dict[str, str] = {
            "selectedReportList[0].UserType": "1",
            "selectedReportList[0].SelectedReport": str(report_id)
        }

        if param_type == "monthly":
            form_data.update({
                "Params[0].ParameterName": "Year",
                "Params[0].ParameterType": "int",
                "Params[0].Value": str(cur_year),
                "Params[1].ParameterName": "FromMonth",
                "Params[1].ParameterType": "int",
                "Params[1].Value": str(f_month),
                "Params[2].ParameterName": "ToMonth",
                "Params[2].ParameterType": "int",
                "Params[2].Value": str(t_month)
            })
        elif param_type == "date_range":
            form_data.update({
                "Params[0].ParameterName": "FromDate",
                "Params[0].ParameterType": "DateTime",
                "Params[0].Value": f_date,
                "Params[1].ParameterName": "ToDate",
                "Params[1].ParameterType": "DateTime",
                "Params[1].Value": t_date
            })

        # Set export format trigger button
        if format == "pdf":
            form_data["pdf"] = ""
        elif format == "xls":
            form_data["xls"] = ""
        else:
            form_data["csv"] = "CSV"

        client = await self.get_client(username, password)
        resp = await client.post("/Reports/Report", data=form_data, timeout=60.0)

        if resp.status_code != 200:
            raise APIException(
                status_code=502,
                code="report_generation_failed",
                message=f"Failed to generate report {report_id} (HTTP {resp.status_code})"
            )

        if format == "pdf":
            return resp.content, "application/pdf"
        elif format == "xls":
            return resp.content, "application/vnd.ms-excel"
        elif format == "csv":
            return resp.content, "text/csv; charset=utf-8"

        # Format is JSON: parse CSV content
        content_str = resp.content.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(content_str))

        line_items = []
        opening_bal = None
        closing_bal = None
        total_credits = 0.0
        total_debits = 0.0

        for row in reader:
            if not row:
                continue

            # Look for summary fields if present
            for c_idx, val in enumerate(row):
                if val == "יתרת פתיחה" and c_idx > 0:
                    try:
                        opening_bal = float(row[c_idx - 1].replace(",", ""))
                    except ValueError:
                        pass
                elif val == "יתרת סגירה" and c_idx > 0:
                    try:
                        closing_bal = float(row[c_idx - 1].replace(",", ""))
                    except ValueError:
                        pass

            # Detect date-anchored transaction line
            for c_idx, val in enumerate(row):
                if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", val.strip()):
                    date_val = val.strip()
                    doc_no = row[c_idx + 1].strip() if c_idx + 1 < len(row) else None
                    details = row[c_idx - 1].strip() if c_idx - 1 >= 0 else ""
                    qty_str = row[c_idx - 2].strip().replace(",", "") if c_idx - 2 >= 0 else "0"
                    debit_str = row[c_idx - 3].strip().replace(",", "") if c_idx - 3 >= 0 else "0"
                    credit_str = row[c_idx - 4].strip().replace(",", "") if c_idx - 4 >= 0 else "0"

                    try:
                        credit = float(credit_str)
                    except ValueError:
                        credit = 0.0
                    try:
                        debit = float(debit_str)
                    except ValueError:
                        debit = 0.0
                    try:
                        qty = float(qty_str)
                    except ValueError:
                        qty = 0.0

                    total_credits += credit
                    total_debits += debit

                    line_items.append({
                        "date": date_val,
                        "document_no": doc_no,
                        "details": details,
                        "credit_ils": credit,
                        "debit_ils": debit,
                        "quantity": qty,
                        "month": f"{f_month}/{cur_year}"
                    })
                    break

        summary = {
            "opening_balance_ils": opening_bal,
            "total_credits_ils": round(total_credits, 2),
            "total_debits_ils": round(total_debits, 2),
            "interest_ils": None,
            "closing_balance_ils": closing_bal
        }

        period_desc = f"{f_month}/{cur_year} - {t_month}/{cur_year}" if param_type == "monthly" else f"{f_date} - {t_date}"
        return {
            "report_id": report_id,
            "report_slug": report_slug,
            "report_name": rep_name,
            "period": period_desc,
            "summary": summary,
            "items": line_items,
            "total_items": len(line_items)
        }, "application/json"

    async def get_pending_approvals(self, username: str, password: str) -> List[Dict[str, Any]]:
        """Fetches pending incoming charges/requests requiring approval."""
        if config.MOCK_MODE:
            return self._mock_pending_approvals

        client = await self.get_client(username, password)
        resp = await client.get("/Budget/PendingApproval")
        if resp.status_code != 200:
            raise APIException(
                status_code=502,
                code="upstream_error",
                message=f"Failed to fetch pending approvals (HTTP {resp.status_code})"
            )

        soup = BeautifulSoup(resp.text, "html.parser")
        items = []
        rows = soup.find_all("tr")
        for r in rows:
            tds = r.find_all("td")
            if len(tds) >= 5:
                trx_id = tds[0].get_text(strip=True)
                disp_id = tds[1].get_text(strip=True)
                initiator = tds[2].get_text(strip=True)
                details = tds[3].get_text(strip=True)
                try:
                    amount = float(tds[4].get_text(strip=True).replace(",", "").replace("₪", ""))
                except ValueError:
                    amount = 0.0
                items.append({
                    "transaction_id": trx_id,
                    "display_id": disp_id,
                    "date": datetime.now().strftime("%d/%m/%Y"),
                    "initiator": initiator,
                    "amount_ils": amount,
                    "details": details
                })
        return items

    async def decline_pending_approval(
        self,
        username: str,
        password: str,
        transaction_line_id: str
    ) -> Dict[str, Any]:
        """Declines an incoming charge request."""
        if config.MOCK_MODE:
            self._mock_pending_approvals = [
                i for i in self._mock_pending_approvals if i["transaction_id"] != transaction_line_id
            ]
            return {
                "status": "declined",
                "transaction_id": transaction_line_id,
                "message": f"Charge {transaction_line_id} declined."
            }

        client = await self.get_client(username, password)
        resp = await client.request(
            "DELETE",
            "/Budget/DeclineTransaction",
            data={"transactionLineId": transaction_line_id}
        )
        if resp.status_code != 200:
            raise APIException(
                status_code=502,
                code="upstream_error",
                message=f"Decline request failed with HTTP {resp.status_code}"
            )
        return {
            "status": "declined",
            "transaction_id": transaction_line_id,
            "message": f"Charge {transaction_line_id} successfully declined."
        }

    async def get_authorized_users(self, username: str, password: str) -> List[Dict[str, Any]]:
        """Retrieves list of users with authorization to charge caller's account."""
        if config.MOCK_MODE:
            return self._mock_authorized_users

        client = await self.get_client(username, password)
        resp = await client.get("/Account/Index")
        if resp.status_code != 200:
            raise APIException(
                status_code=502,
                code="upstream_error",
                message=f"Failed to fetch authorized users (HTTP {resp.status_code})"
            )

        soup = BeautifulSoup(resp.text, "html.parser")
        users = []
        list_items = soup.select(".selected-users-list li")
        for li in list_items:
            name_span = li.find(class_="user-name")
            name = name_span.get_text(strip=True) if name_span else li.get_text(strip=True)
            users.append({
                "user_id": name,
                "user_name": name,
                "is_authorized": True
            })
        return users

    async def set_authorized_user(
        self,
        username: str,
        password: str,
        user_id: str,
        user_name: str,
        is_authorized: bool
    ) -> Dict[str, Any]:
        """Adds or revokes authorization for a member to charge caller's budget."""
        if config.MOCK_MODE:
            found = False
            for u in self._mock_authorized_users:
                if u["user_id"] == user_id:
                    u["is_authorized"] = is_authorized
                    found = True
                    break
            if not found:
                self._mock_authorized_users.append({
                    "user_id": user_id,
                    "user_name": user_name,
                    "is_authorized": is_authorized
                })
            return {
                "status": "ok",
                "user_id": user_id,
                "is_authorized": is_authorized
            }

        client = await self.get_client(username, password)
        resp = await client.request(
            "PUT",
            "/Account/SetUserSelection",
            data={
                "userId": user_id,
                "isSelected": str(is_authorized).lower()
            }
        )
        if resp.status_code != 200:
            raise APIException(
                status_code=502,
                code="upstream_error",
                message=f"Failed to update user authorization (HTTP {resp.status_code})"
            )

        return {
            "status": "ok",
            "user_id": user_id,
            "is_authorized": is_authorized
        }

    async def close(self):
        """Closes all open HTTP clients."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

budget_driver = BudgetDriver()
