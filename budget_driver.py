import asyncio
import logging
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from bs4 import BeautifulSoup
import httpx

import config
from security import APIException, sanitize_log_message
from idempotency import pending_transfer_store

logger = logging.getLogger("budget_driver")

class BudgetDriver:
    """
    Async HTTP client driver for https://budget.mmm.org.il/.
    Maintains authenticated sessions per user, parses ASP.NET MVC responses,
    and supports both live scraping and mock mode for testing/offline environments.
    """

    def __init__(self):
        self._clients: Dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

        # In-memory mock database for mock mode
        self._mock_users = {
            "mock_user": {
                "user_id": "80241",
                "name": "ישראל ישראלי",
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
                    timeout=30.0,
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
        logger.info(f"Authenticating session for user '{username}' on {config.BUDGET_BASE_URL}...")
        resp = await client.post(
            "/Home/Login?",
            data={
                "UserName": username,
                "Password": password
            }
        )
        
        # Check validation errors in returned HTML
        if "שם משתמש או סיסמא שגויים" in resp.text:
            logger.warning(f"Login failed for user '{username}': incorrect credentials")
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

        logger.info(f"User '{username}' authenticated successfully.")

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
        except Exception as e:
            logger.warning(f"check_login_status failed: {e}")
            return False, None

    async def get_balance(self, username: str, password: str) -> Dict[str, Any]:
        """Fetches current budget balance and savings balance."""
        if config.MOCK_MODE:
            user_data = self._mock_users.get(username, {
                "user_id": username,
                "name": "חבר קיבוץ",
                "budget_balance": 1500.00,
                "savings_balance": 10000.00
            })
            return {
                "budget_balance_ils": user_data["budget_balance"],
                "savings_balance_ils": user_data.get("savings_balance"),
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
        
        # Parse balances from page
        budget_balance = 0.0
        savings_balance = None
        user_name = None

        # Look for balance amounts in text/spans
        # Usually displayed in warnings-container or balance labels
        text = soup.get_text()
        balance_match = re.search(r'יתרת(?:ך)?\s*בתקציב\s*[:=]?\s*([-\d,.]+)', text)
        if balance_match:
            try:
                budget_balance = float(balance_match.group(1).replace(",", ""))
            except ValueError:
                pass

        savings_match = re.search(r'חיסכון\s*פרטי\s*[:=]?\s*([-\d,.]+)', text)
        if savings_match:
            try:
                savings_balance = float(savings_match.group(1).replace(",", ""))
            except ValueError:
                pass

        # Parse user name from header if available
        header_h4 = soup.find("h4", class_="col-sm-2")
        if header_h4 and header_h4.get_text(strip=True):
            user_name = header_h4.get_text(strip=True)

        return {
            "budget_balance_ils": budget_balance,
            "savings_balance_ils": savings_balance,
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
        recipients = []
        q = query.strip().lower()

        # Parse rows of member table
        rows = soup.find_all("tr")
        for row in rows:
            tds = row.find_all("td")
            if len(tds) >= 3:
                user_id = tds[0].get_text(strip=True)
                user_hid = tds[1].get_text(strip=True)
                display_name = tds[2].get_text(strip=True)
                dept = tds[3].get_text(strip=True) if len(tds) > 3 else None

                if not q or (q in display_name.lower()) or (q in user_id):
                    recipients.append({
                        "user_id": user_id,
                        "user_hid": user_hid or user_id,
                        "display_name": display_name,
                        "department": dept
                    })

        return recipients

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
        t_types = types or "1,2,3,4"

        client = await self.get_client(username, password)
        data_param = f"{f_date}¥{t_date}¥{t_types}"
        resp = await client.get(f"/Budget/GetTransactionsTable?data={urllib.parse.quote(data_param)}")

        if resp.status_code != 200:
            raise APIException(
                status_code=502,
                code="upstream_error",
                message=f"Failed to fetch transactions table (HTTP {resp.status_code})"
            )

        soup = BeautifulSoup(resp.text, "html.parser")
        transactions = []
        rows = soup.find_all("tr")
        for r in rows:
            tds = r.find_all("td")
            if len(tds) >= 8:
                trx_id = tds[0].get_text(strip=True)
                disp_id = tds[1].get_text(strip=True)
                date_str = tds[2].get_text(strip=True)
                trx_type = tds[3].get_text(strip=True)
                counterparty = tds[4].get_text(strip=True)
                details = tds[5].get_text(strip=True)
                try:
                    amount = float(tds[6].get_text(strip=True).replace(",", "").replace("₪", ""))
                except ValueError:
                    amount = 0.0
                status_str = tds[7].get_text(strip=True)
                
                can_cancel = "True" in str(tds) or "ביטול" in str(tds)
                can_approve = "thumbs-up" in str(tds)

                transactions.append({
                    "transaction_id": trx_id,
                    "display_id": disp_id,
                    "date": date_str,
                    "type": trx_type,
                    "counterparty": counterparty,
                    "details": details,
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
            # Stage transfer in pending store awaiting OTP SMS
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
            "Transactions[0].Type": str(transaction_type),
            "Transactions[0].ReceiverHID": recipient_hid,
            "Transactions[0].ReceiverDisplayName": recipient_name,
            "Transactions[0].Amount": f"{amount_ils:.2f}",
            "Transactions[0].DetailsForReceiver": details_receiver,
            "Transactions[0].DetailsForSender": details_sender
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
            # Add to mock transactions
            self._mock_transactions.insert(0, {
                "transaction_id": transaction_id,
                "display_id": f"REF/{transaction_id}",
                "date": datetime.now().strftime("%d/%m/%Y"),
                "type": "העברה רגילה",
                "counterparty": pending["recipient_name"],
                "details": pending["details_receiver"],
                "amount_ils": -pending["amount_ils"],
                "balance_ils": 1850.40 - pending["amount_ils"],
                "status": "אושר",
                "can_cancel": True,
                "can_approve": False
            })
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
            f"/Budget/CancelTransaction?transactionLineId={transaction_line_id}"
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
        # Selected list: .selected-users-list li
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
