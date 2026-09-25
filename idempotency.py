from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Optional

from command_bus import CommandResult, make_result, result_json_bytes


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of an atomic request-id claim."""

    claimed: bool
    result: bytes | None = None
    in_progress: bool = False

    @property
    def state(self) -> Literal["claimed", "completed", "in_progress"]:
        if self.claimed:
            return "claimed"
        if self.result is not None:
            return "completed"
        return "in_progress"


class RequestStore:
    """Persistent, atomic request claims and byte-identical result replay."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('in_progress', 'completed')),
                    result BLOB,
                    claimed_at TEXT NOT NULL,
                    completed_at TEXT,
                    CHECK (
                        (state = 'in_progress' AND result IS NULL AND completed_at IS NULL)
                        OR
                        (state = 'completed' AND result IS NOT NULL AND completed_at IS NOT NULL)
                    )
                )
                """
            )
        finally:
            connection.close()

    def claim(self, request_id: str) -> ClaimResult:
        """Atomically claim a new id, or return its immutable prior state/result."""
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a nonempty string")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO requests (request_id, state, claimed_at)
                VALUES (?, 'in_progress', ?)
                ON CONFLICT(request_id) DO NOTHING
                """,
                (request_id, _utc_now()),
            )
            if cursor.rowcount == 1:
                connection.commit()
                return ClaimResult(claimed=True)

            row = connection.execute(
                "SELECT state, result FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("request claim state disappeared")
            state, stored_result = row
            if state == "completed":
                return ClaimResult(claimed=False, result=bytes(stored_result))
            return ClaimResult(claimed=False, in_progress=True)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def complete(
        self,
        request_id: str,
        result: CommandResult | Mapping[str, Any] | bytes,
    ) -> bytes:
        """Permanently complete a claim; a prior completion always wins."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, result FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError("request was not claimed")
            state, stored_result = row
            if state == "completed":
                replay = bytes(stored_result)
                connection.commit()
                return replay
            serialized = _validated_result_bytes(request_id, result)

            connection.execute(
                """
                UPDATE requests
                SET state = 'completed', result = ?, completed_at = ?
                WHERE request_id = ? AND state = 'in_progress'
                """,
                (sqlite3.Binary(serialized), _utc_now(), request_id),
            )
            connection.commit()
            return serialized
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_completed(self, request_id: str) -> bytes | None:
        """Return the exact stored result bytes only after permanent completion."""
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT result FROM requests
                WHERE request_id = ? AND state = 'completed'
                """,
                (request_id,),
            ).fetchone()
        finally:
            connection.close()
        return bytes(row[0]) if row is not None else None

    def recover_in_progress(self) -> int:
        """Fail closed by permanently completing every interrupted request."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT request_id FROM requests WHERE state = 'in_progress'"
                ).fetchall()
            ]
            recovered_at = datetime.now(timezone.utc)
            completed_at = _format_utc(recovered_at)
            for request_id in request_ids:
                result = make_result(
                    request_id,
                    "error",
                    error_code="interrupted_request",
                    error_message="The request was interrupted and will not be re-executed.",
                    as_of=recovered_at,
                )
                connection.execute(
                    """
                    UPDATE requests
                    SET state = 'completed', result = ?, completed_at = ?
                    WHERE request_id = ? AND state = 'in_progress'
                    """,
                    (
                        sqlite3.Binary(result_json_bytes(result)),
                        completed_at,
                        request_id,
                    ),
                )
            connection.commit()
            return len(request_ids)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


def _validated_result_bytes(
    request_id: str,
    result: CommandResult | Mapping[str, Any] | bytes,
) -> bytes:
    try:
        if isinstance(result, bytes):
            validated = CommandResult.model_validate_json(result, strict=True)
            serialized = result
        elif isinstance(result, CommandResult):
            validated = result
            serialized = result_json_bytes(result)
        else:
            validated = CommandResult.model_validate(result, strict=True)
            serialized = result_json_bytes(validated)
    except (TypeError, ValueError):
        raise ValueError("result is not a valid command result") from None
    if validated.id != request_id:
        raise ValueError("result id does not match claimed request id")
    return serialized


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return _format_utc(datetime.now(timezone.utc))


class PendingTransferStore:
    """Stores staged transfer requests awaiting OTP SMS confirmation with a 5-minute TTL."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._transfers: Dict[str, Dict[str, Any]] = {}

    def _clean_expired(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        expired_ids = [tid for tid, transfer in self._transfers.items() if transfer["expires_at"] <= now_iso]
        for transaction_id in expired_ids:
            del self._transfers[transaction_id]

    def stage_transfer(
        self,
        transaction_id: str,
        recipient_id: str,
        recipient_name: str,
        amount_ils: float,
        details_receiver: str,
        details_sender: str,
        ttl_seconds: Optional[int] = None,
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
            "expires_at": expires_at,
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


pending_transfer_store = PendingTransferStore()
