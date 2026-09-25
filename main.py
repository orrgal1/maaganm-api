from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import json
import logging
from pathlib import Path
from email.utils import getaddresses
from typing import Any

import config
from budget_driver import budget_driver
from command_bus import (
    IgnoreMessage,
    ProtocolError,
    dispatch_command,
    parse_command,
    result_from_protocol_error,
    result_json_bytes,
)
from help_driver import help_portal_driver
from gmail_adapter import GmailAdapter, GmailAdapterError, GmailMessage
from idempotency import RequestStore

logger = logging.getLogger("maaganm_email_worker")

_MOCK_USERNAME = "mock-user"
_MOCK_PASSWORD = "mock-password"

_WORKER_ALREADY_RUNNING = "Another email worker is already running."


class _WorkerLifetimeLock:
    """Exclusive process lifetime lock for the supported worker CLI."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(f"{database_path}.lock")
        self._file: Any | None = None

    def acquire(self) -> None:
        file = self.path.open("a+")
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            file.close()
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeError(_WORKER_ALREADY_RUNNING) from None
            raise
        self._file = file

    def release(self) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class EmailWorker:
    """Poll authenticated command messages and durably deliver their results."""

    def __init__(
        self,
        adapter: GmailAdapter,
        store: RequestStore,
        driver: Any,
        secret: str | bytes,
        username: str,
        password: str,
        sender: str,
        alias: str,
        *,
        help_driver: Any | None = None,
        help_member_id: str | None = None,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.driver = driver
        self.secret = secret
        self.username = username
        self.password = password
        self.sender = sender
        self.alias = alias
        self.help_driver = help_driver
        self.help_member_id = help_member_id
        self.had_operational_failure = False

    async def process_once(self) -> int:
        """Process one metadata discovery pass and return fully acknowledged count."""
        self.had_operational_failure = False
        metadata_messages = await self.adapter.search_messages()
        processed = 0

        for metadata in metadata_messages:
            if not self._has_expected_headers(metadata):
                continue
            try:
                message = await self.adapter.fetch(metadata.id)
            except GmailAdapterError:
                self.had_operational_failure = True
                logger.warning(
                    "A Gmail message fetch failed; the message remains pending."
                )
                continue
            if not self._has_expected_headers(message):
                continue

            command = None
            protocol_error = None
            try:
                command = parse_command(message.subject, message.body, self.secret)
                request_id = command.id
            except IgnoreMessage:
                continue
            except ProtocolError as error:
                if not _usable_request_id(error.request_id):
                    continue
                protocol_error = error
                request_id = error.request_id

            claim = self.store.claim(request_id)
            if claim.in_progress:
                continue

            if claim.result is not None:
                result_bytes = claim.result
            elif claim.claimed:
                if protocol_error is not None:
                    result = result_from_protocol_error(protocol_error)
                else:
                    result = await dispatch_command(
                        command,
                        self.driver,
                        self.username,
                        self.password,
                        help_driver=self.help_driver,
                        help_member_id=self.help_member_id,
                    )
                result_bytes = self.store.complete(
                    request_id,
                    result_json_bytes(result),
                )
            else:
                raise RuntimeError("invalid request claim state")

            result_payload = json.loads(result_bytes)
            try:
                await self.adapter.send_result(request_id, result_payload)
                await self.adapter.mark_processed(message.id)
            except GmailAdapterError:
                self.had_operational_failure = True
                # The result is already durable, so this message can be replayed
                # without risking another budget operation.
                logger.warning("A Gmail operation failed; the message remains pending.")
                continue

            processed += 1

        return processed

    def _has_expected_headers(self, message: GmailMessage) -> bool:
        return _is_single_mailbox(message.sender, self.sender) and _is_single_mailbox(
            message.recipient,
            self.alias,
        )


def _is_single_mailbox(header_value: object, expected: str) -> bool:
    if not isinstance(header_value, str):
        return False
    parsed = getaddresses([header_value])
    return (
        len(parsed) == 1
        and bool(parsed[0][1])
        and parsed[0][1].casefold() == expected.casefold()
    )


def _usable_request_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not any(character.isspace() for character in value)
    )


def _budget_credentials() -> tuple[str, str]:
    username = config.BUDGET_USERNAME
    password = config.BUDGET_PASSWORD
    if config.MOCK_MODE:
        return username or _MOCK_USERNAME, password or _MOCK_PASSWORD
    if not username or not password:
        raise RuntimeError("Budget credentials are required")
    return username, password

def _help_member_id() -> str | None:
    if config.MOCK_MODE:
        return None
    member_id = config.HELP_MEMBER_ID.strip()
    if member_id:
        return member_id
    raise RuntimeError("HELP_MEMBER_ID is required")


def _build_worker() -> EmailWorker:
    secret = config.require_email_hmac_secret()
    username, password = _budget_credentials()
    help_member_id = _help_member_id()
    adapter = GmailAdapter(
        sender=config.MAAGANM_EMAIL_SENDER,
        alias=config.MAAGANM_EMAIL_ALIAS,
        label_name=config.MAAGANM_EMAIL_LABEL_NAME,
        label_id=config.MAAGANM_EMAIL_LABEL_ID,
        gapi_bin=config.GAPI_BIN,
        max_output_bytes=config.GAPI_MAX_OUTPUT_BYTES,
    )
    store = RequestStore(config.MAAGANM_EMAIL_DB_PATH)
    store.recover_in_progress()
    return EmailWorker(
        adapter,
        store,
        budget_driver,
        secret,
        username,
        password,
        config.MAAGANM_EMAIL_SENDER,
        config.MAAGANM_EMAIL_ALIAS,
        help_driver=None if config.MOCK_MODE else help_portal_driver,
        help_member_id=help_member_id,
    )


async def _run(once: bool) -> int:
    lock = _WorkerLifetimeLock(config.MAAGANM_EMAIL_DB_PATH)
    try:
        lock.acquire()
        worker = _build_worker()
        if once:
            await worker.process_once()
            return 1 if worker.had_operational_failure else 0

        while True:
            try:
                await worker.process_once()
            except GmailAdapterError:
                logger.warning("Gmail polling failed; polling will continue.")
            await asyncio.sleep(config.MAAGANM_EMAIL_POLL_SECONDS)
    finally:
        try:
            await budget_driver.close()
        finally:
            try:
                if not config.MOCK_MODE:
                    await help_portal_driver.close()
            finally:
                lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the MaaganM Gmail command worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="process one polling pass and exit",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args.once))
    except KeyboardInterrupt:
        return 130
    except Exception:
        logger.error("The email worker stopped because of an operational failure.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
