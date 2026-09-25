"""Small, injection-safe adapter for the installed ``gapi`` Gmail command.

The adapter deliberately keeps the subprocess boundary narrow: callers can inject
an async runner in tests, while the default runner executes an argv list without a
shell.  Errors raised by this module never include command arguments or process
output, since those values can contain message contents and credentials.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Protocol, Sequence


SEARCH_MAX_MESSAGES = 500
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_OUTPUT_LIMIT_ERROR = "gapi output limit exceeded"
_SEARCH_LIMIT_ERROR = "gapi search result limit reached"


class GmailAdapterError(RuntimeError):
    """Safe adapter failure; intentionally contains no subprocess details."""


@dataclass(frozen=True)
class GapiResult:
    returncode: int
    stdout: str
    stderr: str


class GapiRunner(Protocol):
    async def run(self, argv: Sequence[str]) -> GapiResult:
        """Run argv and return captured process data."""


class _GapiOutputOverflow(Exception):
    pass


class SubprocessGapiRunner:
    """Run ``gapi`` without a shell and with bounded process output."""

    def __init__(self, max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> None:
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer")
        self.max_output_bytes = max_output_bytes

    async def run(self, argv: Sequence[str]) -> GapiResult:
        process: asyncio.subprocess.Process | None = None
        readers: list[asyncio.Task[bytes]] = []
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if process.stdout is None or process.stderr is None:
                raise OSError("gapi output pipes unavailable")
            readers = [
                asyncio.create_task(self._read_limited(process.stdout)),
                asyncio.create_task(self._read_limited(process.stderr)),
            ]
            stdout, stderr = await asyncio.gather(*readers)
            returncode = await process.wait()
            return GapiResult(
                returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
        except _GapiOutputOverflow:
            await self._kill_and_reap(process, readers)
            raise GmailAdapterError(_OUTPUT_LIMIT_ERROR) from None
        except asyncio.CancelledError:
            await self._kill_and_reap(process, readers)
            raise
        except (OSError, ValueError):
            await self._kill_and_reap(process, readers)
            raise GmailAdapterError("gapi invocation failed") from None

    async def _read_limited(self, stream: asyncio.StreamReader) -> bytes:
        output = bytearray()
        while True:
            remaining = self.max_output_bytes + 1 - len(output)
            chunk = await stream.read(min(64 * 1024, remaining))
            if not chunk:
                return bytes(output)
            output.extend(chunk)
            if len(output) > self.max_output_bytes:
                raise _GapiOutputOverflow

    @staticmethod
    async def _kill_and_reap(
        process: asyncio.subprocess.Process | None,
        readers: Sequence[asyncio.Task[bytes]],
    ) -> None:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

        for reader in readers:
            if not reader.done():
                reader.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)

        if process is not None:
            drains = [
                SubprocessGapiRunner._discard(stream)
                for stream in (process.stdout, process.stderr)
                if stream is not None
            ]
            await asyncio.gather(process.wait(), *drains)

    @staticmethod
    async def _discard(stream: asyncio.StreamReader) -> None:
        while await stream.read(64 * 1024):
            pass


@dataclass(frozen=True)
class GmailMessage:
    """Normalized Gmail message while retaining all fetched header information."""

    id: str
    thread_id: str
    sender: str
    recipient: str
    subject: str
    date: str
    labels: tuple[str, ...] = ()
    body: Any = None
    headers: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


class GmailAdapter:
    """Search, fetch, send, and label Gmail messages through ``gapi``."""

    def __init__(
        self,
        *,
        sender: str = "orgal@mail.instinct.com",
        alias: str = "orrgal+agents+maaganm@gmail.com",
        label_name: str = "Agents",
        label_id: str = "Label_35",
        gapi_bin: str = "gapi",
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        runner: GapiRunner | None = None,
    ) -> None:
        self.sender = sender
        self.alias = alias
        self.label_name = label_name
        self.label_id = label_id
        self.gapi_bin = gapi_bin
        self.runner = runner or SubprocessGapiRunner(max_output_bytes)

    async def search_messages(self) -> list[GmailMessage]:
        """Discover pending message metadata, globally oldest first when complete."""
        query = (
            f"from:{self.sender} to:{self.alias} "
            f"-label:{self.label_name}"
        )
        result = await self._invoke(
            "search",
            query,
            "--max",
            str(SEARCH_MAX_MESSAGES),
        )
        if result.stdout.strip() == "No messages found.":
            return []
        parsed = self._json(result)
        if not isinstance(parsed, list) or any(
            not isinstance(item, dict) for item in parsed
        ):
            raise GmailAdapterError("invalid gapi search response")
        if len(parsed) >= SEARCH_MAX_MESSAGES:
            raise GmailAdapterError(_SEARCH_LIMIT_ERROR)

        messages = [self._normalize(item) for item in parsed]
        messages.sort(key=self._sort_key)
        return messages

    async def fetch(self, message_id: str) -> GmailMessage:
        result = await self._invoke("get", message_id)
        payload = self._json(result)
        if not isinstance(payload, dict):
            raise GmailAdapterError("invalid gapi message response")
        return self._normalize(payload)

    async def send_result(
        self, request_id: str, result_payload: Any
    ) -> Mapping[str, Any]:
        body = json.dumps(result_payload, ensure_ascii=False, separators=(",", ":"))
        result = await self._invoke(
            "send",
            "--to",
            self.sender,
            "--subject",
            f"[RESULT] {request_id}",
            "--body",
            body,
        )
        payload = self._json(result)
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "sent"
            or not isinstance(payload.get("id"), str)
            or not payload["id"]
        ):
            raise GmailAdapterError("invalid gapi send response")
        return payload

    async def mark_processed(self, message_id: str) -> Mapping[str, Any]:
        result = await self._invoke(
            "modify",
            message_id,
            "--add-labels",
            self.label_id,
            "--remove-labels",
            "UNREAD",
        )
        payload = self._json(result)
        if not isinstance(payload, dict):
            raise GmailAdapterError("invalid gapi modify response")
        return payload


    async def _invoke(self, command: str, *args: str) -> GapiResult:
        argv = [self.gapi_bin, "gmail", command, *args]
        try:
            result = await self.runner.run(argv)
        except GmailAdapterError:
            raise
        except Exception:
            raise GmailAdapterError("gapi invocation failed") from None
        if not isinstance(result, GapiResult):
            try:
                result = GapiResult(
                    int(result.returncode), str(result.stdout), str(result.stderr)
                )
            except (AttributeError, TypeError, ValueError):
                raise GmailAdapterError("invalid gapi runner result") from None
        if result.returncode != 0:
            raise GmailAdapterError("gapi command failed")
        return result

    @staticmethod
    def _json(result: GapiResult) -> Any:
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise GmailAdapterError("invalid gapi JSON response") from None


    @staticmethod
    def _normalize(payload: Mapping[str, Any]) -> GmailMessage:
        required = ("id", "threadId", "from", "to", "subject", "date")
        if any(not isinstance(payload.get(key), str) for key in required):
            raise GmailAdapterError("invalid gapi message response")
        labels = payload.get("labels", ())
        if not isinstance(labels, (list, tuple)) or any(not isinstance(x, str) for x in labels):
            raise GmailAdapterError("invalid gapi message response")
        headers = payload.get("headers", {})
        if not isinstance(headers, Mapping):
            raise GmailAdapterError("invalid gapi message response")
        return GmailMessage(
            id=payload["id"],
            thread_id=payload["threadId"],
            sender=payload["from"],
            recipient=payload["to"],
            subject=payload["subject"],
            date=payload["date"],
            labels=tuple(labels),
            body=payload.get("body"),
            headers=dict(headers),
            raw=dict(payload),
        )

    @staticmethod
    def _sort_key(message: GmailMessage) -> tuple[int, float, str, str]:
        try:
            parsed = parsedate_to_datetime(message.date)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (0, parsed.timestamp(), message.date, message.id)
        except (TypeError, ValueError, OverflowError, IndexError):
            return (1, 0.0, message.date, message.id)
