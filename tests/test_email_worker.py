import base64
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from budget_driver import REPORT_DEFINITIONS
from command_bus import ProtocolError, dispatch_command, parse_command, sign_command
from gmail_adapter import (
    GapiResult,
    GmailAdapter,
    GmailAdapterError,
    GmailMessage,
    SubprocessGapiRunner,
)
from idempotency import RequestStore
from main import EmailWorker
import main as main_module

SECRET = "test-secret"
SENDER = "orgal@mail.instinct.com"
ALIAS = "orrgal+agents+maaganm@gmail.com"


def envelope(request_id="req-1", verb="health", args=None, *, approval=None):
    payload = {
        "id": request_id,
        "verb": verb,
        "args": args or {},
        "issued_at": "2026-01-01T00:00:00Z",
        "approval": approval,
    }
    payload["hmac"] = sign_command(payload, SECRET)
    return payload


class FakeDriver:
    def __init__(self, timeline=None):
        self.calls = []
        self.timeline = timeline

    async def check_login_status(self, username, password):
        self.calls.append(("health", username, password))
        if self.timeline is not None:
            self.timeline.append(("dispatch", username))
        return True, "budget-user"


class FakeAdapter:
    def __init__(self, messages):
        self.messages = {item.id: item for item in messages}
        self.metadata = [
            replace(item, body=None, raw={}) for item in messages
        ]
        self.sent = []
        self.events = []
        self.timeline = []
        self.fetches = []

    async def search_messages(self):
        return list(self.metadata)

    async def fetch(self, message_id):
        self.fetches.append(message_id)
        self.timeline.append(("fetch", message_id))
        return self.messages[message_id]

    async def send_result(self, request_id, result):
        self.events.append(("send", request_id))
        self.timeline.append(("send", request_id))
        self.sent.append((request_id, result))
        return {"status": "sent", "id": "sent"}

    async def mark_processed(self, message_id):
        self.events.append(("mark", message_id))
        self.timeline.append(("mark", message_id))
        return {"id": message_id}


def message(payload, *, subject=None, sender=SENDER, recipient=ALIAS, message_id="m-1"):
    return GmailMessage(
        id=message_id,
        thread_id=f"thread-{message_id}",
        sender=sender,
        recipient=recipient,
        subject=subject or f"[CMD] {payload['verb']} {payload['id']}",
        date="Mon, 01 Jan 2024 00:00:00 +0000",
        body=json.dumps(payload, separators=(",", ":")),
        headers={"From": sender, "To": recipient},
        raw={
            "id": message_id,
            "threadId": f"thread-{message_id}",
            "from": sender,
            "to": recipient,
            "subject": subject or f"[CMD] {payload['verb']} {payload['id']}",
            "date": "Mon, 01 Jan 2024 00:00:00 +0000",
            "body": json.dumps(payload, separators=(",", ":")),
            "headers": {"From": sender, "To": recipient},
        },
    )


@pytest.mark.asyncio
async def test_valid_signed_read_dispatches_and_sends_ok_result(tmp_path):
    driver = FakeDriver()
    adapter = FakeAdapter([message(envelope())])
    worker = EmailWorker(adapter, RequestStore(tmp_path / "requests.sqlite"), driver, SECRET, "login-name", "local-password", SENDER, ALIAS)

    assert await worker.process_once() == 1
    assert len(driver.calls) == 1
    assert adapter.sent[0][0] == "req-1"
    assert adapter.sent[0][1]["status"] == "ok"
    assert adapter.sent[0][1]["payload"] == {"authenticated": True, "user": "budget-user"}
    assert adapter.events == [("send", "req-1"), ("mark", "m-1")]


@pytest.mark.asyncio
async def test_bad_hmac_is_ignored_without_dispatch_reply_or_label(tmp_path):
    payload = envelope()
    payload["hmac"] = "0" * 64
    driver = FakeDriver()
    adapter = FakeAdapter([message(payload)])
    worker = EmailWorker(adapter, RequestStore(tmp_path / "requests.sqlite"), driver, SECRET, "user", "pass", SENDER, ALIAS)

    assert await worker.process_once() == 0
    assert driver.calls == []
    assert adapter.sent == []
    assert adapter.events == []


@pytest.mark.asyncio
async def test_duplicate_id_replays_persisted_result_without_reexecution(tmp_path):
    path = tmp_path / "requests.sqlite"
    driver = FakeDriver()
    first_adapter = FakeAdapter([message(envelope("same-id"), message_id="first")])
    first = EmailWorker(first_adapter, RequestStore(path), driver, SECRET, "user", "pass", SENDER, ALIAS)
    assert await first.process_once() == 1

    second_adapter = FakeAdapter([message(envelope("same-id"), message_id="second")])
    second = EmailWorker(second_adapter, RequestStore(path), driver, SECRET, "user", "pass", SENDER, ALIAS)
    assert await second.process_once() == 1
    assert len(driver.calls) == 1
    assert second_adapter.sent[0][1] == first_adapter.sent[0][1]


@pytest.mark.asyncio
async def test_write_approval_missing_expired_and_future_valid():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    base = {"recipient_hid": "recipient", "recipient_name": "Name", "amount_ils": 2.5}
    class Driver:
        def __init__(self): self.calls = 0
        async def transfer(self, *args, **kwargs):
            self.calls += 1
            return {"transfer": "ok"}

    driver = Driver()
    missing = await dispatch_command(parse_command("[CMD] transfer.stage missing", json.dumps(envelope("missing", "transfer.stage", base)), SECRET), driver, "u", "p", now=now)
    assert missing.status == "approval_required"

    expired_payload = envelope("expired", "transfer.stage", base, approval={"ref": "r", "expires_at": "2025-12-31T23:59:59Z"})
    expired = await dispatch_command(parse_command("[CMD] transfer.stage expired", json.dumps(expired_payload), SECRET), driver, "u", "p", now=now)
    assert expired.status == "approval_expired"

    valid_payload = envelope("valid", "transfer.stage", base, approval={"ref": "r", "expires_at": "2026-01-01T00:00:01Z"})
    valid = await dispatch_command(parse_command("[CMD] transfer.stage valid", json.dumps(valid_payload), SECRET), driver, "u", "p", now=now)
    assert valid.status == "ok"
    assert driver.calls == 1


class RecordingRunner:
    def __init__(self, responses):
        self.responses = responses
        self.argvs = []

    async def run(self, argv):
        self.argvs.append(list(argv))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_gmail_adapter_discovers_pending_metadata_oldest_first():
    runner = RecordingRunner([
        GapiResult(0, json.dumps([
            {"id": "new", "threadId": "tn", "from": SENDER, "to": ALIAS, "subject": "s", "date": "Tue, 02 Jan 2024 00:00:00 +0000", "labels": ["UNREAD"]},
            {"id": "old", "threadId": "to", "from": SENDER, "to": ALIAS, "subject": "s", "date": "Mon, 01 Jan 2024 00:00:00 +0000", "labels": []},
        ]), ""),
    ])
    messages = await GmailAdapter(runner=runner).search_messages()
    assert [m.id for m in messages] == ["old", "new"]
    assert all(m.body is None for m in messages)
    assert runner.argvs == [[
        "gapi",
        "gmail",
        "search",
        f"from:{SENDER} to:{ALIAS} -label:Agents",
        "--max",
        "500",
    ]]


@pytest.mark.asyncio
async def test_authenticated_protocol_error_replies_then_labels_and_wrong_identity_is_ignored(tmp_path):
    bad_subject = envelope("bad-subject")
    wrong_identity = envelope("wrong")
    adapter = FakeAdapter([
        message(bad_subject, subject="not-a-command", message_id="protocol"),
        message(wrong_identity, sender="attacker@example.com", message_id="wrong"),
    ])
    worker = EmailWorker(adapter, RequestStore(tmp_path / "requests.sqlite"), FakeDriver(), SECRET, "u", "p", SENDER, ALIAS)

    assert await worker.process_once() == 1
    assert [item[0] for item in adapter.events] == ["send", "mark"]
    assert adapter.events[1][1] == "protocol"
    assert adapter.sent[0][1]["status"] == "error"
    assert adapter.fetches == ["protocol"]


@pytest.mark.asyncio
async def test_worker_fetches_and_processes_one_full_message_at_a_time(tmp_path):
    adapter = FakeAdapter([
        message(envelope("first"), message_id="first"),
        message(envelope("second"), message_id="second"),
    ])
    driver = FakeDriver(adapter.timeline)
    worker = EmailWorker(
        adapter,
        RequestStore(tmp_path / "requests.sqlite"),
        driver,
        SECRET,
        "user",
        "pass",
        SENDER,
        ALIAS,
    )

    assert await worker.process_once() == 2
    assert adapter.timeline == [
        ("fetch", "first"),
        ("dispatch", "user"),
        ("send", "first"),
        ("mark", "first"),
        ("fetch", "second"),
        ("dispatch", "user"),
        ("send", "second"),
        ("mark", "second"),
    ]


@pytest.mark.asyncio
async def test_worker_rechecks_identity_after_full_fetch(tmp_path):
    adapter = FakeAdapter([message(envelope(), message_id="changed")])
    adapter.messages["changed"] = message(
        envelope(),
        sender="attacker@example.com",
        message_id="changed",
    )
    driver = FakeDriver()
    worker = EmailWorker(
        adapter,
        RequestStore(tmp_path / "requests.sqlite"),
        driver,
        SECRET,
        "user",
        "pass",
        SENDER,
        ALIAS,
    )

    assert await worker.process_once() == 0
    assert adapter.fetches == ["changed"]
    assert driver.calls == []
    assert adapter.events == []


@pytest.mark.asyncio
async def test_failed_oversized_fetch_does_not_block_later_message(tmp_path):
    class OversizedFirstAdapter(FakeAdapter):
        async def fetch(self, message_id):
            if message_id == "oversized":
                self.fetches.append(message_id)
                raise GmailAdapterError("gapi output limit exceeded")
            return await super().fetch(message_id)

    adapter = OversizedFirstAdapter([
        message(envelope("oversized"), message_id="oversized"),
        message(envelope("valid"), message_id="valid"),
    ])
    driver = FakeDriver()
    worker = EmailWorker(
        adapter,
        RequestStore(tmp_path / "requests.sqlite"),
        driver,
        SECRET,
        "user",
        "pass",
        SENDER,
        ALIAS,
    )

    assert await worker.process_once() == 1
    assert worker.had_operational_failure
    assert len(driver.calls) == 1
    assert adapter.events == [("send", "valid"), ("mark", "valid")]
    assert adapter.fetches == ["oversized", "valid"]

@pytest.mark.asyncio
async def test_search_limit_fails_closed_before_fetch_or_dispatch(tmp_path):
    metadata = [
        {
            "id": f"m-{index}",
            "threadId": f"t-{index}",
            "from": SENDER,
            "to": ALIAS,
            "subject": f"[CMD] health req-{index}",
            "date": "Mon, 01 Jan 2024 00:00:00 +0000",
            "labels": [],
        }
        for index in range(500)
    ]
    runner = RecordingRunner([GapiResult(0, json.dumps(metadata), "")])
    driver = FakeDriver()
    worker = EmailWorker(
        GmailAdapter(runner=runner),
        RequestStore(tmp_path / "requests.sqlite"),
        driver,
        SECRET,
        "user",
        "pass",
        SENDER,
        ALIAS,
    )

    with pytest.raises(
        GmailAdapterError,
        match="^gapi search result limit reached$",
    ):
        await worker.process_once()
    assert driver.calls == []
    assert len(runner.argvs) == 1
    assert runner.argvs[0][2] == "search"


@pytest.mark.asyncio
async def test_successful_new_thread_send_is_acknowledged_without_retry(tmp_path):
    payload = envelope("new-thread")
    metadata = {
        "id": "source",
        "threadId": "source-thread",
        "from": SENDER,
        "to": ALIAS,
        "subject": "[CMD] health new-thread",
        "date": "Mon, 01 Jan 2024 00:00:00 +0000",
        "labels": [],
    }
    full_message = {
        **metadata,
        "body": json.dumps(payload, separators=(",", ":")),
        "headers": {"From": SENDER, "To": ALIAS},
    }
    runner = RecordingRunner([
        GapiResult(0, json.dumps([metadata]), ""),
        GapiResult(0, json.dumps(full_message), ""),
        GapiResult(
            0,
            json.dumps({
                "status": "sent",
                "id": "result-message",
                "threadId": "different-thread",
            }),
            "",
        ),
        GapiResult(0, json.dumps({"id": "source"}), ""),
        GapiResult(0, "No messages found.\n", ""),
    ])
    driver = FakeDriver()
    worker = EmailWorker(
        GmailAdapter(runner=runner),
        RequestStore(tmp_path / "requests.sqlite"),
        driver,
        SECRET,
        "user",
        "pass",
        SENDER,
        ALIAS,
    )

    assert await worker.process_once() == 1
    assert await worker.process_once() == 0
    assert len(driver.calls) == 1
    send_calls = [argv for argv in runner.argvs if argv[2] == "send"]
    assert len(send_calls) == 1
    assert "--thread-id" not in send_calls[0]
    subject_index = send_calls[0].index("--subject")
    assert send_calls[0][subject_index + 1] == "[RESULT] new-thread"
    assert [argv[2] for argv in runner.argvs] == [
        "search",
        "get",
        "send",
        "modify",
        "search",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
async def test_subprocess_runner_rejects_oversized_output_safely(stream_name):
    script = (
        "import sys;"
        f"sys.{stream_name}.write('sensitive-content-' * 1_000_000);"
        f"sys.{stream_name}.flush()"
    )
    runner = SubprocessGapiRunner(max_output_bytes=64)

    with pytest.raises(
        GmailAdapterError,
        match="^gapi output limit exceeded$",
    ) as raised:
        await runner.run([sys.executable, "-c", script])
    assert "sensitive-content" not in str(raised.value)


def test_report_catalog_examples_are_transport_neutral_command_args():
    serialized = json.dumps(REPORT_DEFINITIONS)
    assert "example_query" not in serialized
    assert "/reports/generate?" not in serialized

    for definition in REPORT_DEFINITIONS:
        args = definition["example_args"]
        request_id = f"report-{definition['id']}"
        payload = envelope(request_id, "reports.generate", args)
        command = parse_command(
            f"[CMD] reports.generate {request_id}",
            json.dumps(payload),
            SECRET,
        )
        assert command.args.report == definition["slug"]


class FakeHelpDriver:
    def __init__(self):
        self.calls = []

    async def get_catalog(self, member_id):
        self.calls.append(("catalog", member_id))
        return {
            "categories": [{"id": "category", "name": "Category"}],
            "contacts": [{"id": "contact", "name": "Contact"}],
        }

    async def list_calls(self, member_id):
        self.calls.append(("list", member_id))
        return [{"id": "call", "is_open": True}]

    async def create_call(self, member_id, **kwargs):
        self.calls.append(("create", member_id, kwargs))
        return {"id": "call", "is_open": True}

    async def act_on_call(self, member_id, **kwargs):
        self.calls.append(("action", member_id, kwargs))
        return {"id": kwargs["call_id"], "is_open": kwargs["action"] == "reopen"}

    async def get_schedule_options(self, call_id):
        self.calls.append(("schedule_options", call_id))
        return {
            "call_id": call_id,
            "slots": [
                {
                    "id": "slot-public",
                    "start": "2026-01-02T10:00:00Z",
                    "end": "2026-01-02T10:30:00Z",
                }
            ],
        }

    async def get_schedule_replacements(self, member_id, call_id):
        self.calls.append(("schedule_replacements", member_id, call_id))
        return {
            "call_id": call_id,
            "scheduling": {"mode": "calendar"},
            "appointment": {
                "start": "2026-01-01T10:00:00Z",
                "end": "2026-01-01T10:30:00Z",
                "status": "scheduled",
            },
            "replacement_slots": [
                {
                    "id": "slot-public",
                    "start": "2026-01-02T10:00:00Z",
                    "end": "2026-01-02T10:30:00Z",
                }
            ],
        }

    async def book_schedule(self, call_id, slot_id):
        self.calls.append(("schedule_book", call_id, slot_id))
        return {
            "call_id": call_id,
            "appointment": {
                "id": slot_id,
                "start": "2026-01-02T10:00:00Z",
                "end": "2026-01-02T10:30:00Z",
            },
        }

    async def move_schedule(self, member_id, call_id, slot_id):
        self.calls.append(("schedule_move", member_id, call_id, slot_id))
        return {
            "call_id": call_id,
            "appointment": {
                "id": slot_id,
                "start": "2026-01-02T10:00:00Z",
                "end": "2026-01-02T10:30:00Z",
                "status": "scheduled",
            },
            "status": "rescheduled",
        }


def help_create_args(attachments=None):
    return {
        "category_id": "category",
        "description": "Description",
        "details": "Details",
        "contact_id": "contact",
        "contact_name": "Contact",
        "contact_phone": "contact-channel",
        "contact_email": "contact-address",
        "attachments": attachments or [],
    }


def parse_help_command(request_id, verb, args, *, approval=None):
    payload = envelope(request_id, verb, args, approval=approval)
    return parse_command(
        f"[CMD] {verb} {request_id}",
        json.dumps(payload),
        SECRET,
    )


@pytest.mark.asyncio
async def test_help_catalog_preserves_portal_data_and_exposes_scheduling_args():
    driver = FakeHelpDriver()
    command = parse_help_command("read-help-catalog", "help.catalog", {})

    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )

    assert result.status == "ok"
    assert result.payload["categories"] == [{"id": "category", "name": "Category"}]
    assert result.payload["contacts"] == [{"id": "contact", "name": "Contact"}]
    assert set(result.payload) == {"categories", "contacts", "command_schemas"}
    schemas = result.payload["command_schemas"]
    assert set(schemas) == {
        "help.call.schedule.options",
        "help.call.schedule.replacements",
        "help.call.schedule.book",
        "help.call.schedule.move",
    }
    call_id = {
        "oneOf": [
            {"type": "string", "pattern": "^[0-9]{1,32}$"},
            {
                "type": "integer",
                "minimum": 0,
                "maximum": (10**32) - 1,
            },
        ]
    }
    assert schemas["help.call.schedule.replacements"] == {
        "kind": "read",
        "approval_required": False,
        "args": {
            "type": "object",
            "properties": {"call_id": call_id},
            "required": ["call_id"],
            "additionalProperties": False,
        },
    }
    assert schemas["help.call.schedule.move"] == {
        "kind": "write",
        "approval_required": True,
        "args": {
            "type": "object",
            "properties": {
                "call_id": call_id,
                "slot_id": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"\S",
                },
            },
            "required": ["call_id", "slot_id"],
            "additionalProperties": False,
        },
    }
    serialized_schemas = json.dumps(schemas, sort_keys=True).lower()
    assert "hmac" not in serialized_schemas
    assert "csrf" not in serialized_schemas
    assert "event_id" not in serialized_schemas
    assert driver.calls == [("catalog", "test-member")]


@pytest.mark.asyncio
async def test_help_calls_list_maps_to_help_driver():
    driver = FakeHelpDriver()
    command = parse_help_command("read-help-calls", "help.calls.list", {})

    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )

    assert result.status == "ok"
    assert result.payload == {
        "items": [{"id": "call", "is_open": True}],
        "total": 1,
    }
    assert driver.calls == [("list", "test-member")]


@pytest.mark.asyncio
@pytest.mark.parametrize("call_id", ["6457734", 6457734])
async def test_help_schedule_replacements_normalizes_call_id_before_dispatch(call_id):
    driver = FakeHelpDriver()
    command = parse_help_command(
        f"schedule-replacements-{type(call_id).__name__}",
        "help.call.schedule.replacements",
        {"call_id": call_id},
    )
    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )
    assert result.status == "ok"
    assert result.payload["call_id"] == "6457734"
    assert result.payload["replacement_slots"][0]["id"] == "slot-public"
    assert driver.calls == [("schedule_replacements", "test-member", "6457734")]


@pytest.mark.asyncio
async def test_help_schedule_move_maps_with_valid_approval():
    driver = FakeHelpDriver()
    command = parse_help_command(
        "schedule-move",
        "help.call.schedule.move",
        {"call_id": "6457734", "slot_id": "slot-public"},
        approval={"ref": "approved", "expires_at": "2099-01-01T00:00:00Z"},
    )
    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )
    assert result.status == "ok"
    assert result.payload["status"] == "rescheduled"
    assert driver.calls == [
        ("schedule_move", "test-member", "6457734", "slot-public")
    ]


@pytest.mark.asyncio
async def test_help_schedule_options_read_maps_without_approval():
    driver = FakeHelpDriver()
    command = parse_help_command(
        "schedule-options",
        "help.call.schedule.options",
        {"call_id": "6457734"},
    )

    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )

    assert result.status == "ok"
    assert result.payload == {
        "call_id": "6457734",
        "slots": [
            {
                "id": "slot-public",
                "start": "2026-01-02T10:00:00Z",
                "end": "2026-01-02T10:30:00Z",
            }
        ],
    }
    assert driver.calls == [("schedule_options", "6457734")]


@pytest.mark.asyncio
async def test_help_schedule_book_maps_with_valid_approval():
    driver = FakeHelpDriver()
    command = parse_help_command(
        "schedule-book",
        "help.call.schedule.book",
        {"call_id": "6457734", "slot_id": "slot-public"},
        approval={"ref": "approved", "expires_at": "2099-01-01T00:00:00Z"},
    )

    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )

    assert result.status == "ok"
    assert result.payload == {
        "call_id": "6457734",
        "appointment": {
            "id": "slot-public",
            "start": "2026-01-02T10:00:00Z",
            "end": "2026-01-02T10:30:00Z",
        },
    }
    assert driver.calls == [("schedule_book", "6457734", "slot-public")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval", "expected_status"),
    [
        (None, "approval_required"),
        ({"ref": "expired", "expires_at": "2025-12-31T23:59:59Z"}, "approval_expired"),
    ],
)
async def test_help_schedule_book_rejects_missing_or_expired_approval(
    approval, expected_status
):
    driver = FakeHelpDriver()
    command = parse_help_command(
        f"schedule-book-{expected_status}",
        "help.call.schedule.book",
        {"call_id": "6457734", "slot_id": "slot-public"},
        approval=approval,
    )
    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )
    assert result.status == expected_status
    assert driver.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval", "expected_status"),
    [
        (None, "approval_required"),
        ({"ref": "expired", "expires_at": "2025-12-31T23:59:59Z"}, "approval_expired"),
    ],
)
async def test_help_schedule_move_rejects_missing_or_expired_approval(
    approval, expected_status
):
    driver = FakeHelpDriver()
    command = parse_help_command(
        f"schedule-move-{expected_status}",
        "help.call.schedule.move",
        {"call_id": "6457734", "slot_id": "slot-public"},
        approval=approval,
    )
    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )
    assert result.status == expected_status
    assert driver.calls == []


@pytest.mark.parametrize(
    ("verb", "args"),
    [
        ("help.call.schedule.options", {}),
        ("help.call.schedule.options", {"call_id": True}),
        ("help.call.schedule.options", {"call_id": ""}),
        ("help.call.schedule.options", {"call_id": " 6457734"}),
        ("help.call.schedule.options", {"call_id": "6457734", "extra": "value"}),
        ("help.call.schedule.book", {}),
        ("help.call.schedule.book", {"call_id": -42, "slot_id": "slot-public"}),
        ("help.call.schedule.book", {"call_id": "", "slot_id": "slot-public"}),
        ("help.call.schedule.book", {"call_id": "6457734"}),
        ("help.call.schedule.book", {"call_id": "6457734", "slot_id": 42}),
        ("help.call.schedule.book", {"call_id": "6457734", "slot_id": ""}),
        (
            "help.call.schedule.book",
            {"call_id": "6457734", "slot_id": "slot-public", "extra": "value"},
        ),
        ("help.call.schedule.replacements", {}),
        ("help.call.schedule.replacements", {"call_id": False}),
        ("help.call.schedule.replacements", {"call_id": ""}),
        ("help.call.schedule.replacements", {"call_id": "6457734.0"}),
        ("help.call.schedule.replacements", {"call_id": 10**32}),
        (
            "help.call.schedule.replacements",
            {"call_id": "6457734", "extra": "value"},
        ),
        ("help.call.schedule.move", {}),
        ("help.call.schedule.move", {"call_id": "6457734"}),
        ("help.call.schedule.move", {"call_id": "6457734", "slot_id": 42}),
        ("help.call.schedule.move", {"call_id": True, "slot_id": "slot-public"}),
        ("help.call.schedule.move", {"call_id": "", "slot_id": "slot-public"}),
        ("help.call.schedule.move", {"call_id": "6457734", "slot_id": ""}),
        ("help.call.schedule.move", {"call_id": "6457734", "slot_id": " "}),
        (
            "help.call.schedule.move",
            {"call_id": "6457734", "slot_id": "slot-public", "extra": "value"},
        ),
    ],
)
def test_help_schedule_verbs_reject_strict_invalid_args(verb, args):
    with pytest.raises(ProtocolError, match=r"^Command arguments are invalid\.$"):
        parse_help_command(f"invalid-{verb}", verb, args)


@pytest.mark.asyncio
async def test_help_create_maps_decoded_attachments_to_help_driver():
    driver = FakeHelpDriver()
    command = parse_help_command(
        "create",
        "help.call.create",
        help_create_args(
            [
                {
                    "filename": "document.txt",
                    "content_type": "text/plain",
                    "content_base64": base64.b64encode(b"decoded").decode("ascii"),
                }
            ]
        ),
        approval={"ref": "approved", "expires_at": "2099-01-01T00:00:00Z"},
    )

    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )

    assert result.status == "ok"
    assert driver.calls == [
        (
            "create",
            "test-member",
            {
                "category_id": "category",
                "description": "Description",
                "details": "Details",
                "contact_id": "contact",
                "contact_name": "Contact",
                "contact_phone": "contact-channel",
                "contact_email": "contact-address",
                "attachments": [
                    {
                        "filename": "document.txt",
                        "content_type": "text/plain",
                        "content": b"decoded",
                    }
                ],
            },
        )
    ]


@pytest.mark.asyncio
async def test_help_create_existing_contact_defaults_custom_fields_before_driver_call():
    driver = FakeHelpDriver()
    command = parse_help_command(
        "create-existing-contact",
        "help.call.create",
        {
            "category_id": "category",
            "description": "Description",
            "contact_id": "contact",
        },
        approval={"ref": "approved", "expires_at": "2099-01-01T00:00:00Z"},
    )

    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )

    assert result.status == "ok"
    assert driver.calls == [
        (
            "create",
            "test-member",
            {
                "category_id": "category",
                "description": "Description",
                "details": "",
                "contact_id": "contact",
                "contact_name": "",
                "contact_phone": "",
                "contact_email": "",
                "attachments": [],
            },
        )
    ]



@pytest.mark.parametrize(
    "extra_args",
    [
        {"unexpected": "value"},
        {"details": 123},
        {"contact_name": 123},
    ],
)
def test_help_create_rejects_unknown_fields_and_non_string_custom_fields(extra_args):
    args = {
        "category_id": "category",
        "description": "Description",
        "contact_id": "contact",
        **extra_args,
    }

    with pytest.raises(ProtocolError, match="^Command arguments are invalid\\.$"):
        parse_help_command(
            "invalid-create-args",
            "help.call.create",
            args,
            approval={"ref": "approved", "expires_at": "2099-01-01T00:00:00Z"},
        )

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verb", "args", "action", "text"),
    [
        ("help.call.feedback", {"call_id": "call", "text": "Feedback"}, "complain", "Feedback"),
        ("help.call.expedite", {"call_id": "call", "text": "Please expedite"}, "hurryup", "Please expedite"),
        ("help.call.close", {"call_id": "call"}, "close", ""),
        ("help.call.reopen", {"call_id": "call"}, "reopen", ""),
    ],
)
async def test_help_action_verbs_map_to_explicit_driver_actions(
    verb,
    args,
    action,
    text,
):
    driver = FakeHelpDriver()
    command = parse_help_command(
        f"action-{action}",
        verb,
        args,
        approval={"ref": "approved", "expires_at": "2099-01-01T00:00:00Z"},
    )

    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )

    assert result.status == "ok"
    assert driver.calls == [
        (
            "action",
            "test-member",
            {"call_id": "call", "action": action, "text": text},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verb", "args"),
    [
        ("help.call.create", help_create_args()),
        ("help.call.feedback", {"call_id": "call", "text": "Feedback"}),
        ("help.call.expedite", {"call_id": "call", "text": "Expedite"}),
        ("help.call.close", {"call_id": "call"}),
        ("help.call.reopen", {"call_id": "call"}),
    ],
)
async def test_help_writes_require_approval_before_help_driver_call(verb, args):
    driver = FakeHelpDriver()
    command = parse_help_command(f"unapproved-{verb}", verb, args)

    result = await dispatch_command(
        command,
        FakeDriver(),
        "user",
        "password",
        help_driver=driver,
        help_member_id="test-member",
    )

    assert result.status == "approval_required"
    assert driver.calls == []


@pytest.mark.parametrize(
    "attachments",
    [
        [
            {
                "filename": f"file-{index}.txt",
                "content_type": "text/plain",
                "content_base64": "",
            }
            for index in range(6)
        ],
        [
            {
                "filename": "document.txt",
                "content_type": "text/plain",
                "content_base64": "sensitive-invalid-base64!",
            }
        ],
        [
            {
                "filename": "../document.txt",
                "content_type": "text/plain",
                "content_base64": "",
            }
        ],
        [
            {
                "filename": "document.txt",
                "content_type": "text/plain\r\nunsafe",
                "content_base64": "",
            }
        ],
        [
            {
                "filename": "first.bin",
                "content_type": "application/octet-stream",
                "content_base64": base64.b64encode(
                    b"x" * (2 * 1024 * 1024 + 1)
                ).decode("ascii"),
            },
            {
                "filename": "second.bin",
                "content_type": "application/octet-stream",
                "content_base64": base64.b64encode(
                    b"y" * (2 * 1024 * 1024)
                ).decode("ascii"),
            },
        ],
    ],
)
def test_help_attachment_limits_and_strict_base64_fail_safely(attachments):
    with pytest.raises(ProtocolError, match="^Command arguments are invalid\\.$") as raised:
        parse_help_command(
            "invalid-attachment",
            "help.call.create",
            help_create_args(attachments),
            approval={"ref": "approved", "expires_at": "2099-01-01T00:00:00Z"},
        )

    assert "sensitive-invalid-base64" not in str(raised.value)


@pytest.mark.asyncio
async def test_help_read_is_idempotent_through_email_worker(tmp_path):
    path = tmp_path / "requests.sqlite"
    payload = envelope("same-help-id", "help.catalog")
    driver = FakeHelpDriver()
    first_adapter = FakeAdapter([message(payload, message_id="first-help")])
    first = EmailWorker(
        first_adapter,
        RequestStore(path),
        FakeDriver(),
        SECRET,
        "user",
        "password",
        SENDER,
        ALIAS,
        help_driver=driver,
        help_member_id="test-member",
    )
    assert await first.process_once() == 1

    second_adapter = FakeAdapter([message(payload, message_id="second-help")])
    second = EmailWorker(
        second_adapter,
        RequestStore(path),
        FakeDriver(),
        SECRET,
        "user",
        "password",
        SENDER,
        ALIAS,
        help_driver=driver,
        help_member_id="test-member",
    )
    assert await second.process_once() == 1
    assert driver.calls == [("catalog", "test-member")]
    assert second_adapter.sent[0][1] == first_adapter.sent[0][1]


@pytest.mark.asyncio
async def test_help_schedule_book_is_idempotent_through_email_worker(tmp_path):
    path = tmp_path / "requests.sqlite"
    payload = envelope(
        "same-schedule-book-id",
        "help.call.schedule.book",
        {"call_id": "6457734", "slot_id": "slot-public"},
        approval={"ref": "approved", "expires_at": "2099-01-01T00:00:00Z"},
    )
    driver = FakeHelpDriver()
    first_adapter = FakeAdapter([message(payload, message_id="first-book")])
    first = EmailWorker(
        first_adapter,
        RequestStore(path),
        FakeDriver(),
        SECRET,
        "user",
        "password",
        SENDER,
        ALIAS,
        help_driver=driver,
        help_member_id="test-member",
    )
    assert await first.process_once() == 1

    second_adapter = FakeAdapter([message(payload, message_id="second-book")])
    second = EmailWorker(
        second_adapter,
        RequestStore(path),
        FakeDriver(),
        SECRET,
        "user",
        "password",
        SENDER,
        ALIAS,
        help_driver=driver,
        help_member_id="test-member",
    )
    assert await second.process_once() == 1
    assert driver.calls == [("schedule_book", "6457734", "slot-public")]
    assert second_adapter.sent[0][1] == first_adapter.sent[0][1]


def test_help_member_id_is_required_outside_mock_mode(monkeypatch):
    monkeypatch.setattr(main_module.config, "HELP_MEMBER_ID", "")
    monkeypatch.setattr(main_module.config, "MOCK_MODE", False)

    with pytest.raises(RuntimeError, match="^HELP_MEMBER_ID is required$"):
        main_module._help_member_id()


@pytest.mark.asyncio
async def test_mock_worker_disables_help_dependencies_and_fails_closed(
    monkeypatch, tmp_path
):
    adapter = FakeAdapter([message(envelope("mock-help", "help.catalog"))])
    real_help_driver = object()

    monkeypatch.setattr(main_module.config, "MOCK_MODE", True)
    monkeypatch.setattr(main_module.config, "MAAGANM_EMAIL_HMAC_SECRET", SECRET)
    monkeypatch.setattr(main_module.config, "HELP_MEMBER_ID", "configured-member")
    monkeypatch.setattr(
        main_module.config,
        "MAAGANM_EMAIL_DB_PATH",
        str(tmp_path / "requests.sqlite"),
    )
    monkeypatch.setattr(main_module, "GmailAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(main_module, "help_portal_driver", real_help_driver)

    worker = main_module._build_worker()

    assert worker.help_driver is None
    assert worker.help_member_id is None
    assert await worker.process_once() == 1
    result = adapter.sent[0][1]
    assert result["status"] == "error"
    assert result["error"]["code"] == "driver_error"
    assert result["error"]["message"] == "The help operation failed."
