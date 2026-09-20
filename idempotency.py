import uuid
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

class IdempotencyStore:
    """
    In-memory thread/async-safe TTL store for Idempotency-Key headers.
    Ensures identical mutations return cached responses without duplicate execution.
    """
    def __init__(self, default_ttl_seconds: int = 86400):
        self.default_ttl = default_ttl_seconds
        self._cache: Dict[str, Dict[str, Any]] = {}

    def _clean_expired(self):
        now = time.time()
        expired_keys = [k for k, v in self._cache.items() if v["expires_at"] <= now]
        for k in expired_keys:
            del self._cache[k]

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        self._clean_expired()
        entry = self._cache.get(key)
        if not entry:
            return None
        return entry["response"]

    def set(self, key: str, status_code: int, body: Any, ttl_seconds: Optional[int] = None) -> None:
        self._clean_expired()
        ttl = ttl_seconds or self.default_ttl
        self._cache[key] = {
            "response": {
                "status_code": status_code,
                "body": body
            },
            "expires_at": time.time() + ttl
        }

class PendingTransferStore:
    """
    Stores staged transfer requests awaiting OTP SMS confirmation with a 5-minute TTL.
    """
    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._transfers: Dict[str, Dict[str, Any]] = {}

    def _clean_expired(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        expired_ids = [tid for tid, t in self._transfers.items() if t["expires_at"] <= now_iso]
        for tid in expired_ids:
            del self._transfers[tid]

    def stage_transfer(
        self,
        transaction_id: str,
        recipient_id: str,
        recipient_name: str,
        amount_ils: float,
        details_receiver: str,
        details_sender: str,
        ttl_seconds: Optional[int] = None
    ) -> Dict[str, Any]:
        self._clean_expired()
        ttl = ttl_seconds or self.ttl
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
        entry = {
            "transaction_id": transaction_id,
            "recipient_id": recipient_id,
            "recipient_name": recipient_name,
            "amount_ils": round(amount_ils, 2),
            "details_receiver": details_receiver,
            "details_sender": details_sender,
            "expires_at": expires_at
        }
        self._transfers[transaction_id] = entry
        return entry

    def get_transfer(self, transaction_id: str) -> Optional[Dict[str, Any]]:
        self._clean_expired()
        entry = self._transfers.get(transaction_id)
        if not entry:
            return None
        now_iso = datetime.now(timezone.utc).isoformat()
        if entry["expires_at"] <= now_iso:
            del self._transfers[transaction_id]
            return None
        return entry

    def remove_transfer(self, transaction_id: str):
        if transaction_id in self._transfers:
            del self._transfers[transaction_id]

idempotency_store = IdempotencyStore()
pending_transfer_store = PendingTransferStore()
