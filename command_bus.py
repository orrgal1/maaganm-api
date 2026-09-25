from __future__ import annotations

import base64
import hashlib
import hmac as hmac_module
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping, Protocol, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from errors import APIException


SUBJECT_RE = re.compile(r"^\[CMD\] ([^\s]+) ([^\s]+)$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_CONTENT_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$"
)
_MAX_HELP_ATTACHMENTS = 5
_MAX_HELP_ATTACHMENT_BYTES = 4 * 1024 * 1024
_MAX_SCHEDULE_CALL_ID_DIGITS = 32
_MAX_SCHEDULE_CALL_ID = (10**_MAX_SCHEDULE_CALL_ID_DIGITS) - 1
_SCHEDULE_CALL_ID_RE = re.compile(
    rf"^[0-9]{{1,{_MAX_SCHEDULE_CALL_ID_DIGITS}}}$"
)


class IgnoreMessage(Exception):
    """The message cannot be authenticated and must not receive a reply."""


class ProtocolError(Exception):
    """An authenticated message has a safe, replyable protocol error."""

    def __init__(
        self,
        code: str,
        message: str,
        request_id: str | None = None,
        status: Literal["error"] = "error",
    ) -> None:
        self.code = code
        self.message = message
        self.request_id = request_id
        self.status = status
        super().__init__(message)


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


NonEmptyStr = Annotated[str, Field(min_length=1), AfterValidator(_non_empty)]

def _normalize_schedule_call_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("must be a decimal string or integer")
    candidate = str(value)
    if not _SCHEDULE_CALL_ID_RE.fullmatch(candidate):
        raise ValueError("must contain 1 to 32 decimal digits")
    return str(int(candidate))


def _utc_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("must be a UTC ISO-8601 string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("must be a UTC ISO-8601 string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("must include a UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError("must use UTC")
    return parsed.astimezone(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Approval(StrictModel):
    ref: NonEmptyStr
    expires_at: datetime

    @field_validator("expires_at", mode="before")
    @classmethod
    def validate_expires_at(cls, value: Any) -> datetime:
        return _utc_datetime(value)


class EmptyArgs(StrictModel):
    pass


class RecipientSearchArgs(StrictModel):
    query: str = ""
    transaction_type: int = Field(default=1, ge=1)


class TransactionsListArgs(StrictModel):
    from_date: NonEmptyStr | None = None
    to_date: NonEmptyStr | None = None
    types: NonEmptyStr | None = None


class ReportsGenerateArgs(StrictModel):
    report: NonEmptyStr | int
    format: Literal["json", "csv", "pdf", "xls"] = "json"
    year: int | None = Field(default=None, ge=1)
    from_month: int | None = Field(default=None, ge=1, le=12)
    to_month: int | None = Field(default=None, ge=1, le=12)
    from_date: NonEmptyStr | None = None
    to_date: NonEmptyStr | None = None


class TransferStageArgs(StrictModel):
    recipient_hid: NonEmptyStr
    recipient_name: NonEmptyStr
    amount_ils: float = Field(gt=0)
    details_receiver: str = ""
    details_sender: str = ""
    transaction_type: int = Field(default=1, ge=1)


class TransferApproveArgs(StrictModel):
    transaction_id: NonEmptyStr
    otp_code: NonEmptyStr


class TransactionDeleteArgs(StrictModel):
    transaction_line_id: NonEmptyStr


class ApprovalDeclineArgs(StrictModel):
    transaction_line_id: NonEmptyStr


class AuthorizedUserSetArgs(StrictModel):
    user_id: NonEmptyStr
    user_name: NonEmptyStr
    is_authorized: bool

class HelpAttachment(StrictModel):
    filename: NonEmptyStr
    content_type: NonEmptyStr
    content_base64: str
    _content: bytes = PrivateAttr(default=b"")

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("must be a safe filename")
        return value

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, value: str) -> str:
        if not _SAFE_CONTENT_TYPE_RE.fullmatch(value):
            raise ValueError("must be a safe media type")
        return value

    @model_validator(mode="after")
    def decode_content(self) -> "HelpAttachment":
        try:
            self._content = base64.b64decode(
                self.content_base64.encode("ascii"),
                validate=True,
            )
        except (UnicodeEncodeError, ValueError):
            raise ValueError("must contain strict base64") from None
        return self

    @property
    def content(self) -> bytes:
        return self._content


class HelpCallCreateArgs(StrictModel):
    category_id: NonEmptyStr
    description: NonEmptyStr
    details: str = ""
    contact_id: NonEmptyStr
    contact_name: str = ""
    contact_phone: str = ""
    contact_email: str = ""
    attachments: list[HelpAttachment] = Field(
        default_factory=list,
        max_length=_MAX_HELP_ATTACHMENTS,
    )

    @model_validator(mode="after")
    def validate_attachment_size(self) -> "HelpCallCreateArgs":
        if sum(len(attachment.content) for attachment in self.attachments) > (
            _MAX_HELP_ATTACHMENT_BYTES
        ):
            raise ValueError("attachments exceed the aggregate size limit")
        return self


class HelpCallTextArgs(StrictModel):
    call_id: NonEmptyStr
    text: NonEmptyStr


class HelpCallIdArgs(StrictModel):
    call_id: NonEmptyStr


class HelpCallScheduleArgs(StrictModel):
    call_id: str

    @field_validator("call_id", mode="before")
    @classmethod
    def normalize_call_id(cls, value: Any) -> str:
        return _normalize_schedule_call_id(value)


class HelpCallScheduleBookArgs(HelpCallScheduleArgs):
    slot_id: NonEmptyStr


class _CommandBase(StrictModel):
    id: NonEmptyStr
    issued_at: datetime
    approval: Approval | None
    hmac: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("issued_at", mode="before")
    @classmethod
    def validate_issued_at(cls, value: Any) -> datetime:
        return _utc_datetime(value)


class HealthCommand(_CommandBase):
    verb: Literal["health"]
    args: EmptyArgs


class BalanceCommand(_CommandBase):
    verb: Literal["balance"]
    args: EmptyArgs


class RecipientsSearchCommand(_CommandBase):
    verb: Literal["recipients.search"]
    args: RecipientSearchArgs


class TransactionsListCommand(_CommandBase):
    verb: Literal["transactions.list"]
    args: TransactionsListArgs


class ApprovalsPendingCommand(_CommandBase):
    verb: Literal["approvals.pending"]
    args: EmptyArgs


class AuthorizedUsersListCommand(_CommandBase):
    verb: Literal["authorized_users.list"]
    args: EmptyArgs


class ReportsCatalogCommand(_CommandBase):
    verb: Literal["reports.catalog"]
    args: EmptyArgs


class ReportsGenerateCommand(_CommandBase):
    verb: Literal["reports.generate"]
    args: ReportsGenerateArgs


class TransferStageCommand(_CommandBase):
    verb: Literal["transfer.stage"]
    args: TransferStageArgs


class TransferApproveCommand(_CommandBase):
    verb: Literal["transfer.approve"]
    args: TransferApproveArgs


class TransactionDeleteCommand(_CommandBase):
    verb: Literal["transaction.delete"]
    args: TransactionDeleteArgs


class ApprovalDeclineCommand(_CommandBase):
    verb: Literal["approval.decline"]
    args: ApprovalDeclineArgs


class AuthorizedUserSetCommand(_CommandBase):
    verb: Literal["authorized_user.set"]
    args: AuthorizedUserSetArgs

class HelpCatalogCommand(_CommandBase):
    verb: Literal["help.catalog"]
    args: EmptyArgs


class HelpCallsListCommand(_CommandBase):
    verb: Literal["help.calls.list"]
    args: EmptyArgs


class HelpCallCreateCommand(_CommandBase):
    verb: Literal["help.call.create"]
    args: HelpCallCreateArgs


class HelpCallFeedbackCommand(_CommandBase):
    verb: Literal["help.call.feedback"]
    args: HelpCallTextArgs


class HelpCallExpediteCommand(_CommandBase):
    verb: Literal["help.call.expedite"]
    args: HelpCallTextArgs


class HelpCallCloseCommand(_CommandBase):
    verb: Literal["help.call.close"]
    args: HelpCallIdArgs


class HelpCallReopenCommand(_CommandBase):
    verb: Literal["help.call.reopen"]
    args: HelpCallIdArgs


class HelpCallScheduleOptionsCommand(_CommandBase):
    verb: Literal["help.call.schedule.options"]
    args: HelpCallScheduleArgs


class HelpCallScheduleReplacementsCommand(_CommandBase):
    verb: Literal["help.call.schedule.replacements"]
    args: HelpCallScheduleArgs


class HelpCallScheduleBookCommand(_CommandBase):
    verb: Literal["help.call.schedule.book"]
    args: HelpCallScheduleBookArgs


class HelpCallScheduleMoveCommand(_CommandBase):
    verb: Literal["help.call.schedule.move"]
    args: HelpCallScheduleBookArgs


Command = Annotated[
    Union[
        HealthCommand,
        BalanceCommand,
        RecipientsSearchCommand,
        TransactionsListCommand,
        ApprovalsPendingCommand,
        AuthorizedUsersListCommand,
        ReportsCatalogCommand,
        ReportsGenerateCommand,
        TransferStageCommand,
        TransferApproveCommand,
        TransactionDeleteCommand,
        ApprovalDeclineCommand,
        AuthorizedUserSetCommand,
        HelpCatalogCommand,
        HelpCallsListCommand,
        HelpCallCreateCommand,
        HelpCallFeedbackCommand,
        HelpCallExpediteCommand,
        HelpCallCloseCommand,
        HelpCallReopenCommand,
        HelpCallScheduleOptionsCommand,
        HelpCallScheduleReplacementsCommand,
        HelpCallScheduleBookCommand,
        HelpCallScheduleMoveCommand,
    ],
    Field(discriminator="verb"),
]
_COMMAND_ADAPTER = TypeAdapter(Command)


@dataclass(frozen=True)
class VerbSpec:
    kind: Literal["read", "write"]
    args_model: type[StrictModel]


VERBS: Mapping[str, VerbSpec] = MappingProxyType(
    {
        "health": VerbSpec("read", EmptyArgs),
        "balance": VerbSpec("read", EmptyArgs),
        "recipients.search": VerbSpec("read", RecipientSearchArgs),
        "transactions.list": VerbSpec("read", TransactionsListArgs),
        "approvals.pending": VerbSpec("read", EmptyArgs),
        "authorized_users.list": VerbSpec("read", EmptyArgs),
        "reports.catalog": VerbSpec("read", EmptyArgs),
        "reports.generate": VerbSpec("read", ReportsGenerateArgs),
        "transfer.stage": VerbSpec("write", TransferStageArgs),
        "transfer.approve": VerbSpec("write", TransferApproveArgs),
        "transaction.delete": VerbSpec("write", TransactionDeleteArgs),
        "approval.decline": VerbSpec("write", ApprovalDeclineArgs),
        "authorized_user.set": VerbSpec("write", AuthorizedUserSetArgs),
        "help.catalog": VerbSpec("read", EmptyArgs),
        "help.calls.list": VerbSpec("read", EmptyArgs),
        "help.call.create": VerbSpec("write", HelpCallCreateArgs),
        "help.call.feedback": VerbSpec("write", HelpCallTextArgs),
        "help.call.expedite": VerbSpec("write", HelpCallTextArgs),
        "help.call.close": VerbSpec("write", HelpCallIdArgs),
        "help.call.reopen": VerbSpec("write", HelpCallIdArgs),
        "help.call.schedule.options": VerbSpec("read", HelpCallScheduleArgs),
        "help.call.schedule.replacements": VerbSpec("read", HelpCallScheduleArgs),
        "help.call.schedule.book": VerbSpec("write", HelpCallScheduleBookArgs),
        "help.call.schedule.move": VerbSpec("write", HelpCallScheduleBookArgs),
    }
)
READ_VERBS = frozenset(verb for verb, spec in VERBS.items() if spec.kind == "read")
WRITE_VERBS = frozenset(verb for verb, spec in VERBS.items() if spec.kind == "write")


class ErrorInfo(StrictModel):
    code: NonEmptyStr
    message: NonEmptyStr


ResultStatus = Literal["ok", "error", "approval_required", "approval_expired"]


class CommandResult(StrictModel):
    id: NonEmptyStr
    status: ResultStatus
    payload: Any | None
    error: ErrorInfo | None
    as_of: datetime

    @field_validator("as_of", mode="before")
    @classmethod
    def validate_as_of(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("must be timezone-aware")
            return value.astimezone(timezone.utc)
        return _utc_datetime(value)

    @model_validator(mode="after")
    def validate_status_fields(self) -> "CommandResult":
        if self.status == "ok" and self.error is not None:
            raise ValueError("successful results cannot contain an error")
        if self.status != "ok" and self.error is None:
            raise ValueError("non-success results require an error")
        return self


def canonical_signing_json(payload: Mapping[str, Any]) -> str:
    """Return the canonical JSON covered by the command HMAC."""
    unsigned = {key: value for key, value in payload.items() if key != "hmac"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sign_command(payload: Mapping[str, Any], secret: str | bytes) -> str:
    """Calculate the lowercase HMAC-SHA256 signature for a command object."""
    secret_bytes = _secret_bytes(secret)
    canonical = canonical_signing_json(payload).encode("utf-8")
    return hmac_module.new(secret_bytes, canonical, hashlib.sha256).hexdigest()


def verify_command_hmac(payload: Mapping[str, Any], secret: str | bytes) -> bool:
    """Verify a command signature without raising on attacker-controlled fields."""
    supplied = payload.get("hmac")
    if not isinstance(supplied, str):
        return False
    try:
        expected = sign_command(payload, secret)
        return hmac_module.compare_digest(supplied, expected)
    except (RecursionError, TypeError, ValueError):
        return False


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, bytes) or not secret:
        raise RuntimeError("A nonempty command HMAC secret is required")
    return secret


def parse_command(subject: str, body: str | bytes, secret: str | bytes) -> Command:
    """Authenticate and validate a command email.

    Unparseable or unauthenticated bodies raise ``IgnoreMessage`` and must not be
    acknowledged. Once the HMAC is valid, protocol defects raise
    ``ProtocolError`` with safe reply fields.
    """
    _secret_bytes(secret)
    raw = _parse_json_object(body)
    if not verify_command_hmac(raw, secret):
        raise IgnoreMessage("message signature is missing or invalid")

    request_id = raw.get("id") if isinstance(raw.get("id"), str) else None
    subject_match = SUBJECT_RE.fullmatch(subject) if isinstance(subject, str) else None
    if subject_match is None:
        raise ProtocolError(
            "invalid_subject",
            "Subject must use '[CMD] <verb> <request-id>'.",
            request_id,
        )

    subject_verb, subject_id = subject_match.groups()
    signed_id = raw.get("id") if isinstance(raw.get("id"), str) else None
    request_id = signed_id if signed_id and not any(char.isspace() for char in signed_id) else subject_id
    body_verb = raw.get("verb")
    if not isinstance(body_verb, str) or body_verb not in VERBS:
        raise ProtocolError("unknown_verb", "Command verb is not supported.", request_id)
    if not isinstance(signed_id, str) or not signed_id.strip():
        raise ProtocolError("invalid_command", "Command id must be a string.", request_id)
    if subject_id != signed_id or subject_verb != body_verb:
        raise ProtocolError(
            "subject_body_mismatch",
            "Subject verb and id must match the signed command body.",
            request_id,
        )
    if not isinstance(raw.get("args"), dict):
        raise ProtocolError("invalid_args", "Command args must be an object.", request_id)

    try:
        return _COMMAND_ADAPTER.validate_python(raw, strict=True)
    except ValidationError as exc:
        locations = [error.get("loc", ()) for error in exc.errors(include_url=False)]
        code = "invalid_args" if any("args" in location for location in locations) else "invalid_command"
        message = (
            "Command arguments are invalid."
            if code == "invalid_args"
            else "Signed command fields are invalid."
        )
        raise ProtocolError(code, message, request_id) from None


def _parse_json_object(body: str | bytes) -> dict[str, Any]:
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        if not isinstance(body, str):
            raise TypeError
        parsed = json.loads(
            body,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (RecursionError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise IgnoreMessage("message body is not a JSON object") from None
    if not isinstance(parsed, dict):
        raise IgnoreMessage("message body is not a JSON object")
    return parsed


def make_result(
    request_id: str,
    status: ResultStatus,
    *,
    payload: Any | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    as_of: datetime | None = None,
) -> CommandResult:
    """Create a validated command result with a UTC timestamp."""
    error = None
    if status != "ok":
        if not error_code or not error_message:
            raise ValueError("non-success results require an error code and message")
        error = ErrorInfo(code=error_code, message=error_message)
    return CommandResult(
        id=request_id,
        status=status,
        payload=payload,
        error=error,
        as_of=as_of or datetime.now(timezone.utc),
    )


def result_from_protocol_error(error: ProtocolError) -> CommandResult:
    if error.request_id is None:
        raise ValueError("A protocol error without a request id cannot be replied to")
    return make_result(
        error.request_id,
        error.status,
        error_code=error.code,
        error_message=error.message,
    )


def result_json_bytes(result: CommandResult | Mapping[str, Any]) -> bytes:
    """Serialize a result deterministically for durable, byte-identical replay."""
    validated = (
        result
        if isinstance(result, CommandResult)
        else CommandResult.model_validate(result, strict=True)
    )
    return json.dumps(
        validated.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class BudgetDriverProtocol(Protocol):
    async def check_login_status(self, username: str, password: str) -> tuple[bool, str | None]: ...
    async def get_balance(self, username: str, password: str) -> dict[str, Any]: ...
    async def search_recipients(self, username: str, password: str, query: str = "", transaction_type: int = 1) -> list[dict[str, Any]]: ...
    async def get_transactions(self, username: str, password: str, from_date: str | None = None, to_date: str | None = None, types: str | None = None) -> list[dict[str, Any]]: ...
    async def get_pending_approvals(self, username: str, password: str) -> list[dict[str, Any]]: ...
    async def get_authorized_users(self, username: str, password: str) -> list[dict[str, Any]]: ...
    def get_report_types(self) -> list[dict[str, Any]]: ...
    async def generate_report(self, username: str, password: str, report: Any, format: str = "json", year: int | None = None, from_month: int | None = None, to_month: int | None = None, from_date: str | None = None, to_date: str | None = None) -> tuple[Any, str]: ...
    async def transfer(self, username: str, password: str, recipient_hid: str, recipient_name: str, amount_ils: float, details_receiver: str = "", details_sender: str = "", transaction_type: int = 1) -> dict[str, Any]: ...
    async def approve_otp(self, username: str, password: str, transaction_id: str, otp_code: str) -> dict[str, Any]: ...
    async def cancel_transaction(self, username: str, password: str, transaction_line_id: str) -> dict[str, Any]: ...
    async def decline_pending_approval(self, username: str, password: str, transaction_line_id: str) -> dict[str, Any]: ...
    async def set_authorized_user(self, username: str, password: str, user_id: str, user_name: str, is_authorized: bool) -> dict[str, Any]: ...

class HelpPortalDriverProtocol(Protocol):
    async def get_catalog(self, member_id: str) -> dict[str, Any]: ...
    async def list_calls(self, member_id: str) -> list[dict[str, Any]]: ...
    async def get_schedule_options(self, call_id: str) -> dict[str, Any]: ...
    async def get_schedule_replacements(self, member_id: str, call_id: str) -> dict[str, Any]: ...
    async def book_schedule(self, call_id: str, slot_id: str) -> dict[str, Any]: ...
    async def move_schedule(self, member_id: str, call_id: str, slot_id: str) -> dict[str, Any]: ...
    async def create_call(
        self,
        member_id: str,
        *,
        category_id: str,
        description: str,
        details: str,
        contact_id: str,
        contact_name: str,
        contact_phone: str,
        contact_email: str,
        attachments: list[dict[str, Any]],
    ) -> dict[str, Any]: ...
    async def act_on_call(
        self,
        member_id: str,
        *,
        call_id: str,
        action: str,
        text: str,
    ) -> dict[str, Any]: ...


async def dispatch_command(
    command: Command,
    driver: BudgetDriverProtocol,
    username: str,
    password: str,
    *,
    help_driver: HelpPortalDriverProtocol | None = None,
    help_member_id: str | None = None,
    now: datetime | None = None,
) -> CommandResult:
    """Safely dispatch a validated command to its explicitly mapped driver method."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if command.verb in WRITE_VERBS:
        if command.approval is None:
            return make_result(
                command.id,
                "approval_required",
                error_code="approval_required",
                error_message="A valid approval is required for write commands.",
                as_of=current_time,
            )
        if command.approval.expires_at <= current_time:
            return make_result(
                command.id,
                "approval_expired",
                error_code="approval_expired",
                error_message="The command approval has expired.",
                as_of=current_time,
            )

    is_help_command = command.verb.startswith("help.")
    try:
        payload = await _invoke_driver(
            command,
            driver,
            username,
            password,
            help_driver=help_driver,
            help_member_id=help_member_id,
        )
        payload = _redact_values(payload, (username, password))
        return make_result(command.id, "ok", payload=payload, as_of=current_time)
    except APIException as exc:
        code = exc.code if isinstance(exc.code, str) and _SAFE_CODE_RE.fullmatch(exc.code) else "driver_error"
        return make_result(
            command.id,
            "error",
            error_code=code,
            error_message=_safe_driver_message(code, is_help=is_help_command),
            as_of=current_time,
        )
    except (ValidationError, TypeError, ValueError):
        service = "help" if is_help_command else "budget"
        return make_result(
            command.id,
            "error",
            error_code="driver_response_invalid",
            error_message=f"The {service} service returned an invalid response.",
            as_of=current_time,
        )
    except Exception:
        service = "help" if is_help_command else "budget"
        return make_result(
            command.id,
            "error",
            error_code="driver_error",
            error_message=f"The {service} operation failed.",
            as_of=current_time,
        )


def _help_command_schemas() -> dict[str, dict[str, Any]]:
    def args_schema(*, slot: bool = False) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "call_id": {
                "oneOf": [
                    {
                        "type": "string",
                        "pattern": rf"^[0-9]{{1,{_MAX_SCHEDULE_CALL_ID_DIGITS}}}$",
                    },
                    {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": _MAX_SCHEDULE_CALL_ID,
                    },
                ]
            }
        }
        required = ["call_id"]
        if slot:
            properties["slot_id"] = {
                "type": "string",
                "minLength": 1,
                "pattern": r"\S",
            }
            required.append("slot_id")
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    return {
        "help.call.schedule.options": {
            "kind": "read",
            "approval_required": False,
            "args": args_schema(),
        },
        "help.call.schedule.replacements": {
            "kind": "read",
            "approval_required": False,
            "args": args_schema(),
        },
        "help.call.schedule.book": {
            "kind": "write",
            "approval_required": True,
            "args": args_schema(slot=True),
        },
        "help.call.schedule.move": {
            "kind": "write",
            "approval_required": True,
            "args": args_schema(slot=True),
        },
    }


async def _invoke_driver(
    command: Command,
    driver: BudgetDriverProtocol,
    username: str,
    password: str,
    *,
    help_driver: HelpPortalDriverProtocol | None,
    help_member_id: str | None,
) -> Any:
    args = command.args
    if isinstance(command, HealthCommand):
        authenticated, user = await driver.check_login_status(username, password)
        return {"authenticated": authenticated, "user": user}
    if isinstance(command, BalanceCommand):
        return await driver.get_balance(username, password)
    if isinstance(command, RecipientsSearchCommand):
        items = await driver.search_recipients(
            username,
            password,
            query=args.query,
            transaction_type=args.transaction_type,
        )
        return _list_payload(items)
    if isinstance(command, TransactionsListCommand):
        items = await driver.get_transactions(
            username,
            password,
            from_date=args.from_date,
            to_date=args.to_date,
            types=args.types,
        )
        return _list_payload(items)
    if isinstance(command, ApprovalsPendingCommand):
        return _list_payload(await driver.get_pending_approvals(username, password))
    if isinstance(command, AuthorizedUsersListCommand):
        return _list_payload(await driver.get_authorized_users(username, password))
    if isinstance(command, ReportsCatalogCommand):
        return _list_payload(driver.get_report_types())
    if isinstance(command, ReportsGenerateCommand):
        data, media_type = await driver.generate_report(
            username,
            password,
            report=args.report,
            format=args.format,
            year=args.year,
            from_month=args.from_month,
            to_month=args.to_month,
            from_date=args.from_date,
            to_date=args.to_date,
        )
        if args.format == "json":
            if isinstance(data, (bytes, bytearray, memoryview)):
                raise ValueError("JSON report returned bytes")
            return data
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError("binary report did not return bytes")
        return {
            "content_base64": base64.b64encode(bytes(data)).decode("ascii"),
            "media_type": media_type,
            "filename": _report_filename(args.report, args.format),
        }
    if isinstance(command, TransferStageCommand):
        return await driver.transfer(
            username,
            password,
            recipient_hid=args.recipient_hid,
            recipient_name=args.recipient_name,
            amount_ils=args.amount_ils,
            details_receiver=args.details_receiver,
            details_sender=args.details_sender,
            transaction_type=args.transaction_type,
        )
    if isinstance(command, TransferApproveCommand):
        return await driver.approve_otp(
            username,
            password,
            transaction_id=args.transaction_id,
            otp_code=args.otp_code,
        )
    if isinstance(command, TransactionDeleteCommand):
        return await driver.cancel_transaction(
            username,
            password,
            transaction_line_id=args.transaction_line_id,
        )
    if isinstance(command, ApprovalDeclineCommand):
        return await driver.decline_pending_approval(
            username,
            password,
            transaction_line_id=args.transaction_line_id,
        )
    if isinstance(command, AuthorizedUserSetCommand):
        return await driver.set_authorized_user(
            username,
            password,
            user_id=args.user_id,
            user_name=args.user_name,
            is_authorized=args.is_authorized,
        )
    if isinstance(command, (HelpCatalogCommand, HelpCallsListCommand)):
        portal, member_id = _require_help_dependencies(
            help_driver,
            help_member_id,
        )
        if isinstance(command, HelpCatalogCommand):
            catalog = await portal.get_catalog(member_id)
            return {**catalog, "command_schemas": _help_command_schemas()}
        return _list_payload(await portal.list_calls(member_id))
    if isinstance(command, HelpCallScheduleOptionsCommand):
        portal = _require_help_driver(help_driver)
        return await portal.get_schedule_options(call_id=args.call_id)
    if isinstance(command, HelpCallScheduleReplacementsCommand):
        portal, member_id = _require_help_dependencies(
            help_driver,
            help_member_id,
        )
        return await portal.get_schedule_replacements(
            member_id,
            call_id=args.call_id,
        )
    if isinstance(command, HelpCallScheduleBookCommand):
        portal = _require_help_driver(help_driver)
        return await portal.book_schedule(
            call_id=args.call_id,
            slot_id=args.slot_id,
        )
    if isinstance(command, HelpCallScheduleMoveCommand):
        portal, member_id = _require_help_dependencies(
            help_driver,
            help_member_id,
        )
        return await portal.move_schedule(
            member_id,
            call_id=args.call_id,
            slot_id=args.slot_id,
        )
    if isinstance(command, HelpCallCreateCommand):
        portal, member_id = _require_help_dependencies(
            help_driver,
            help_member_id,
        )
        return await portal.create_call(
            member_id,
            category_id=args.category_id,
            description=args.description,
            details=args.details,
            contact_id=args.contact_id,
            contact_name=args.contact_name,
            contact_phone=args.contact_phone,
            contact_email=args.contact_email,
            attachments=[
                {
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "content": attachment.content,
                }
                for attachment in args.attachments
            ],
        )
    if isinstance(
        command,
        (
            HelpCallFeedbackCommand,
            HelpCallExpediteCommand,
            HelpCallCloseCommand,
            HelpCallReopenCommand,
        ),
    ):
        portal, member_id = _require_help_dependencies(
            help_driver,
            help_member_id,
        )
        action = {
            "help.call.feedback": "complain",
            "help.call.expedite": "hurryup",
            "help.call.close": "close",
            "help.call.reopen": "reopen",
        }[command.verb]
        text = args.text if isinstance(args, HelpCallTextArgs) else ""
        return await portal.act_on_call(
            member_id,
            call_id=args.call_id,
            action=action,
            text=text,
        )
    raise TypeError("Unsupported validated command")


def _require_help_driver(
    help_driver: HelpPortalDriverProtocol | None,
) -> HelpPortalDriverProtocol:
    if help_driver is None:
        raise RuntimeError("help portal dependencies are not configured")
    return help_driver


def _require_help_dependencies(
    help_driver: HelpPortalDriverProtocol | None,
    help_member_id: str | None,
) -> tuple[HelpPortalDriverProtocol, str]:
    portal = _require_help_driver(help_driver)
    if not help_member_id:
        raise RuntimeError("help portal dependencies are not configured")
    return portal, help_member_id


def _list_payload(items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        raise ValueError("list operation returned a non-list")
    return {"items": items, "total": len(items)}


def _report_filename(report: str | int, report_format: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(report)).strip("-").lower()
    if not stem:
        stem = "report"
    return f"{stem}.{report_format}"


def _redact_values(value: Any, secrets: tuple[str, ...]) -> Any:
    protected = tuple(secret for secret in secrets if secret)
    if isinstance(value, str):
        for secret in protected:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact_values(item, protected) for item in value]
    if isinstance(value, tuple):
        return [_redact_values(item, protected) for item in value]
    if isinstance(value, dict):
        return {key: _redact_values(item, protected) for key, item in value.items()}
    return value


def _safe_driver_message(code: str, *, is_help: bool = False) -> str:
    messages = {
        "invalid_credentials": "Budget credentials were rejected.",
        "upstream_error": (
            "The help service could not complete the operation."
            if is_help
            else "The budget service could not complete the operation."
        ),
        "transfer_failed": "The transfer could not be submitted.",
        "transfer_not_found": "The pending transfer was not found or has expired.",
        "otp_verification_failed": "The transfer approval code was rejected.",
        "invalid_report_id": "The requested report is not supported.",
        "report_generation_failed": "The report could not be generated.",
    }
    return messages.get(
        code,
        "The help operation failed." if is_help else "The budget operation failed.",
    )
