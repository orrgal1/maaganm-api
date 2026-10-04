"""Bearer protected HTTP transport for the existing MaaganM command dispatcher."""

from __future__ import annotations

import json
import os
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import config
from budget_driver import budget_driver
from command_bus import (
    _COMMAND_ADAPTER,
    dispatch_command,
    result_json_bytes,
    sign_command,
)
from help_driver import help_portal_driver
from kehilanet_driver import kehilanet_driver
from idempotency import AccountScopeMismatch, RequestStore
from local_api_core.auth import bearer_dependency


class CommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    verb: str
    args: dict = Field(default_factory=dict)
    approval: dict | None = None


def _token() -> str:
    value = os.getenv("MAAGANM_API_TOKEN", "").strip()
    if not value:
        try:
            value = Path(".api_token").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
    if not value:
        raise RuntimeError("MAAGANM_API_TOKEN is required")
    return value


_INTERNAL_SIGNING_KEY = secrets.token_hex(32)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _token()
    _budget_credentials()
    _store.recover_in_progress()
    try:
        yield
    finally:
        await budget_driver.close()
        if not config.MOCK_MODE:
            await help_portal_driver.close()
            await kehilanet_driver.close()


app = FastAPI(title="MaaganM API", lifespan=lifespan)
_auth = Depends(bearer_dependency(_token))
_store = RequestStore(os.getenv("MAAGANM_HTTP_DB_PATH", "maaganm_http.sqlite3"))


def _budget_credentials() -> tuple[str, str]:
    username, password = config.BUDGET_USERNAME, config.BUDGET_PASSWORD
    if config.MOCK_MODE:
        return username or "mock-user", password or "mock-password"
    if not username.strip() or not password.strip():
        raise RuntimeError("Local Budget credentials are required")
    return username, password


@app.get("/health", dependencies=[_auth])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/commands",
    dependencies=[_auth],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["verb"],
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "description": "Stable request ID for retries; generated when omitted."},
                            "verb": {"type": "string", "description": "Allowlisted command verb."},
                            "args": {"type": "object", "default": {}},
                            "approval": {
                                "anyOf": [
                                    {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["ref", "expires_at"],
                                        "properties": {
                                            "ref": {"type": "string", "minLength": 1},
                                            "expires_at": {"type": "string", "format": "date-time", "description": "Future UTC timestamp."},
                                        },
                                    },
                                    {"type": "null"},
                                ]
                            },
                        },
                    }
                }
            },
        }
    },
)
async def command(request: Request):
    body = await request.body()
    if len(body) > 1_048_576:
        raise HTTPException(status_code=413, detail="Request is too large")
    try:
        supplied = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    if not isinstance(supplied, dict):
        raise HTTPException(status_code=400, detail="Invalid command or arguments")
    try:
        payload = CommandInput.model_validate(supplied)
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid command or arguments") from None
    if payload.verb == "transfer.approve":
        otp_code = payload.args.get("otp_code")
        if "otp_code" not in payload.args or (isinstance(otp_code, str) and not otp_code.strip()):
            raise HTTPException(
                status_code=400,
                detail="transfer.approve requires a nonempty SMS otp_code",
            )
    data = payload.model_dump()
    data["issued_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # The retired mail worker's HMAC secret is not an HTTP API dependency.
    data["hmac"] = sign_command(data, _INTERNAL_SIGNING_KEY)
    try:
        parsed = _COMMAND_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="Invalid command or arguments") from exc

    username, password = _budget_credentials()
    member_id = None if config.MOCK_MODE else config.HELP_MEMBER_ID.strip() or None
    if parsed.verb.startswith("kehilanet."):
        # Member contact and announcement data are live reads. Do not persist
        # their results in the general request replay database.
        if not config.KEHILANET_USERNAME or not config.KEHILANET_PASSWORD:
            raise HTTPException(status_code=503, detail="Local Kehila-Net credentials are not configured")
        result = await dispatch_command(
            parsed,
            budget_driver,
            username,
            password,
            help_driver=None if config.MOCK_MODE else help_portal_driver,
            help_member_id=member_id,
            kehilanet_driver=None if config.MOCK_MODE else kehilanet_driver,
            kehilanet_username=config.KEHILANET_USERNAME,
            kehilanet_password=config.KEHILANET_PASSWORD,
        )
        return json.loads(result_json_bytes(result))

    # A keyed identifier keeps account names out of the SQLite replay ledger.
    account_scope = hmac.new(
        _token().encode("utf-8"), username.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    try:
        claim = _store.claim(parsed.id, account_scope=account_scope)
    except AccountScopeMismatch:
        raise HTTPException(status_code=409, detail="Request ID is unavailable") from None
    if claim.result is not None:
        return json.loads(claim.result)
    if claim.in_progress:
        raise HTTPException(status_code=409, detail="Request is already in progress")

    result = await dispatch_command(
        parsed,
        budget_driver,
        username,
        password,
        help_driver=None if config.MOCK_MODE else help_portal_driver,
        help_member_id=member_id,
    )
    return json.loads(_store.complete(parsed.id, result_json_bytes(result), account_scope=account_scope))
