import json
import re
from urllib.parse import parse_qs

import httpx
import pytest

from errors import APIException
from help_driver import HelpPortalDriver


MEMBER = "member-test"


def call_row(
    call_id: str,
    *,
    is_open: bool = True,
    actions: tuple[str, ...] = ("complain", "hurryup", "close"),
    note: str = "Initial note",
    description: str = "Fixture request",
    handler: str = "Service desk",
) -> str:
    buttons = "".join(
        f'<button name="callsb" value="{action}">Action</button>' for action in actions
    )
    return f"""
      <tr>
        <td>{call_id}</td>
        <td>2026-01-02 03:04</td>
        <td>{handler}</td>
        <td>{description}</td>
        <td>{note}</td>
        <td>
          <form name="{call_id}" method="post">
            <textarea name="callactiontext"></textarea>
            <input type="hidden" name="callid" value="{call_id}">
            <input type="hidden" name="callisopen" value="{1 if is_open else 0}">
            <input type="hidden" name="member" value="fixture-member">
            {buttons}
          </form>
        </td>
      </tr>
    """


def portal_page(
    *rows: str,
    category_name: str = "General service",
    categories: tuple[tuple[str, str], ...] | None = None,
) -> str:
    category_options = categories or (("category-a", category_name),)
    rendered_categories = "".join(
        f'<option value="{category_id}">{name}</option>'
        for category_id, name in category_options
    )
    return f"""<!doctype html>
    <html><head><meta charset="windows-1255"></head><body>
      <form method="post">
        <input name="member" value="fixture-member">
        <input name="dira" value="residence-fixture">
        <select name="strm">
          <option value="">Choose</option>
          {rendered_categories}
          <option value="category-disabled" disabled>Disabled</option>
        </select>
        <input type="radio" name="cntct" value="contact-a">Contact fixture<br>
        <input type="radio" name="cntct" value="0">Custom contact<br>
        <input name="cntct_name" value="">
        <input name="cntct_cell" value="">
        <input name="cntct_email" value="">
      </form>
      <table>{''.join(rows)}</table>
    </body></html>"""


def scheduler_page(
    call_id: str,
    slots: list[dict],
    *,
    token: str = "synthetic-csrf",
    include_token: bool = True,
) -> str:
    fields = {
        "ScheduleViewModel.CallId": call_id,
        "ScheduleViewModel.StrmCode": "616",
        "ScheduleViewModel.CustomerName": "Synthetic Resident",
        "ScheduleViewModel.TechnicianEmail": "technician@example.invalid",
        "ScheduleViewModel.AvailabilityCalendarName": "synthetic-calendar",
        "ScheduleViewModel.CustomerEmail": "resident@example.invalid",
        "ScheduleViewModel.AppointmentSubject": "Synthetic appointment",
        "ScheduleViewModel.AppointmentDescription": "Synthetic details",
        "ScheduleViewModel.SelectedStartTime": "",
        "ScheduleViewModel.SelectedEndTime": "",
        "ScheduleViewModel.SelectedEventId": "",
        "AdminMode": "false",
        "AdminToken": "",
        "AdminRangeSteps": "4",
    }
    if include_token:
        fields["__RequestVerificationToken"] = token
    controls = "".join(
        f'<input type="hidden" name="{name}" value="{value}">'
        for name, value in fields.items()
    )
    available = [
        slot
        for slot in slots
        if slot.get("isCurrent") is not True and slot.get("isAvailable") is True
    ]
    current = next(
        (slot for slot in slots if slot.get("isCurrent") is True),
        None,
    )
    current_event = (
        {
            "id": "__current_slot__",
            "title": "Synthetic current appointment",
            "start": current["start"],
            "end": current["end"],
            "isCurrent": True,
            "isAvailable": False,
            "editable": False,
        }
        if current is not None
        else None
    )
    return f"""<!doctype html><html><body>
      <form method="post">{controls}</form>
      <script>
        const availableSlots = {json.dumps(available)};
        const currentSlotEvent = {json.dumps(current_event)};
      </script>
    </body></html>"""

def schedule_slot(
    source_event_id: str,
    start: str,
    end: str,
    *,
    available: bool = True,
    current: bool = False,
) -> dict:
    return {
        "id": f"synthetic-{source_event_id}",
        "sourceEventId": source_event_id,
        "title": "Synthetic available appointment",
        "start": start,
        "end": end,
        "isAvailable": available,
        "isCurrent": current,
        "editable": False,
        "overlap": False,
    }


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        status_code = 200
        if isinstance(response, dict):
            status_code = response.get("status", 200)
            content = response.get("content", "")
            if isinstance(content, str):
                content = content.encode("utf-8")
            headers = response.get("headers", {})
        elif isinstance(response, tuple):
            content, headers = response
        else:
            content, headers = response.encode("utf-8"), {"content-type": "text/html; charset=utf-8"}
        return httpx.Response(
            status_code,
            content=content,
            headers=headers,
            request=request,
        )


def make_driver(*responses):
    transport = ScriptedTransport(responses)
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return (
        HelpPortalDriver(
            "https://service.invalid",
            client,
            "https://scheduler.invalid",
        ),
        client,
        transport,
    )


@pytest.mark.asyncio
async def test_close_closes_injected_client():
    driver, client, _ = make_driver()

    await driver.close()

    assert client.is_closed


@pytest.mark.asyncio
async def test_catalog_and_calls_parse_field_names_and_windows_1255_meta():
    page = portal_page(call_row("call-a"), category_name="שירות כללי")
    encoded = page.encode("windows-1255")
    response = (encoded, {"content-type": "text/html"})
    driver, client, _ = make_driver(response, response)

    try:
        catalog = await driver.get_catalog(MEMBER)
        calls = await driver.list_calls(MEMBER)
    finally:
        await client.aclose()

    assert catalog == {
        "categories": [
            {
                "id": "category-a",
                "name": "שירות כללי",
                "scheduling": {"mode": "unknown"},
            }
        ],
        "contacts": [
            {"id": "contact-a", "name": "Contact fixture"},
            {"id": "0", "name": "Custom contact"},
        ],
        "residence_id": "residence-fixture",
    }
    assert calls == [
        {
            "id": "call-a",
            "opened_at": "2026-01-02 03:04",
            "handler": "Service desk",
            "description": "Fixture request",
            "note": "Initial note",
            "is_open": True,
            "available_actions": ["complain", "hurryup", "close"],
        }
    ]

@pytest.mark.asyncio
async def test_catalog_scheduling_modes_are_evidence_backed():
    page = portal_page(
        categories=(
            ("616", "Synthetic electricity"),
            ("624", "Synthetic contact service"),
            ("999", "Synthetic other service"),
        )
    )
    driver, client, _ = make_driver(page)

    try:
        catalog = await driver.get_catalog(MEMBER)
    finally:
        await client.aclose()

    assert catalog["categories"] == [
        {
            "id": "616",
            "name": "Synthetic electricity",
            "scheduling": {"mode": "calendar"},
        },
        {
            "id": "624",
            "name": "Synthetic contact service",
            "scheduling": {
                "mode": "contact",
                "phone": "077-7076023",
                "extension": "2",
            },
        },
        {
            "id": "999",
            "name": "Synthetic other service",
            "scheduling": {"mode": "unknown"},
        },
    ]


@pytest.mark.asyncio
async def test_malformed_page_raises_fixed_safe_error():
    driver, client, _ = make_driver("<html><body>unexpected</body></html>")

    try:
        with pytest.raises(APIException) as raised:
            await driver.get_catalog(MEMBER)
    finally:
        await client.aclose()

    assert raised.value.status_code == 502
    assert raised.value.code == "help_portal_invalid_response"
    assert raised.value.message == "Service center returned an invalid response."
    assert MEMBER not in raised.value.message


@pytest.mark.asyncio
async def test_create_uses_multipart_safe_attachment_and_confirms_new_call():
    before = portal_page(call_row("call-old"))
    after = portal_page(
        call_row("call-old"), call_row("call-new", note="Fixture details", description="Fixture request")
    )
    driver, client, transport = make_driver(before, after)

    try:
        created = await driver.create_call(
            MEMBER,
            category_id="category-a",
            description="Fixture request",
            details="Fixture details",
            contact_id="contact-a",
            contact_name="Contact fixture",
            contact_phone="",
            contact_email="",
            attachments=[
                {
                    "filename": "../folder\\evidence.txt",
                    "content_type": "text/plain",
                    "content": b"sanitized attachment",
                }
            ],
        )
    finally:
        await client.aclose()

    assert created["id"] == "call-new"
    assert created["scheduling"] == {
        "mode": "assigned",
        "handler": "Service desk",
    }
    assert len(transport.requests) == 2
    write = transport.requests[1]
    assert write.url.path == "/hhopencall.pl"
    assert write.headers["content-type"].startswith("multipart/form-data;")
    body = write.content
    for field in (
        b"member",
        b"dira",
        b"strm",
        b"dscr",
        b"rmrk",
        b"cntct",
        b"cntct_name",
        b"cntct_cell",
        b"cntct_email",
        b"imagefile",
    ):
        assert b'name="' + field + b'"' in body
    assert b'filename="evidence.txt"' in body
    assert b"Content-Type: text/plain" in body
    assert b"sanitized attachment" in body
    assert b"folder" not in body


@pytest.mark.asyncio
async def test_create_rejects_unconfirmed_http_success():
    unchanged = portal_page(call_row("call-old"))
    driver, client, _ = make_driver(unchanged, unchanged)

    try:
        with pytest.raises(APIException) as raised:
            await driver.create_call(
                MEMBER,
                category_id="category-a",
                description="Fixture request",
                details="Fixture details",
                contact_id="contact-a",
                contact_name="Contact fixture",
                contact_phone="",
                contact_email="",
                attachments=[],
            )
    finally:
        await client.aclose()

    assert raised.value.code == "help_write_unconfirmed"


@pytest.mark.parametrize(
    ("action", "before_open", "before_actions", "after_open", "after_actions", "after_note"),
    [
        ("complain", True, ("complain", "close"), True, ("close",), "Please review"),
        ("hurryup", True, ("hurryup", "close"), True, ("close",), "Please review"),
        ("close", True, ("close",), False, ("reopen",), "Initial note"),
        ("reopen", False, ("reopen",), True, ("close",), "Initial note"),
    ],
)
@pytest.mark.asyncio
async def test_each_action_submits_exact_payload_and_confirms_result(
    action, before_open, before_actions, after_open, after_actions, after_note
):
    before = portal_page(
        call_row("call-a", is_open=before_open, actions=before_actions)
    )
    after = portal_page(
        call_row(
            "call-a",
            is_open=after_open,
            actions=after_actions,
            note=after_note,
        )
    )
    driver, client, transport = make_driver(before, after)

    try:
        result = await driver.act_on_call(
            MEMBER, call_id="call-a", action=action, text="Please review"
        )
    finally:
        await client.aclose()

    assert result["is_open"] is after_open
    assert result["available_actions"] == list(after_actions)
    assert len(transport.requests) == 2
    payload = parse_qs(transport.requests[1].content.decode("ascii"), keep_blank_values=True)
    assert payload == {
        "callactiontext": ["Please review"],
        "callid": ["call-a"],
        "callisopen": ["1" if before_open else "0"],
        "callsb": [action],
        "member": [MEMBER],
    }


@pytest.mark.asyncio
async def test_unavailable_action_is_rejected_before_write_post():
    before = portal_page(call_row("call-a", actions=("close",)))
    driver, client, transport = make_driver(before)

    try:
        with pytest.raises(APIException) as raised:
            await driver.act_on_call(
                MEMBER, call_id="call-a", action="hurryup", text="Please review"
            )
    finally:
        await client.aclose()

    assert raised.value.code == "help_action_unavailable"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_attachment_limits_fail_before_mutation_post():
    before = portal_page()
    driver, client, transport = make_driver(before)
    attachments = [
        {"filename": f"fixture-{index}.bin", "content_type": "application/octet-stream", "content": b"x"}
        for index in range(6)
    ]

    try:
        with pytest.raises(APIException) as raised:
            await driver.create_call(
                MEMBER,
                category_id="category-a",
                description="Fixture request",
                details="Fixture details",
                contact_id="contact-a",
                contact_name="Contact fixture",
                contact_phone="",
                contact_email="",
                attachments=attachments,
            )
    finally:
        await client.aclose()

    assert raised.value.code == "help_attachment_invalid"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_action_url_encoding_uses_windows_1255_bytes():
    before = portal_page(call_row("call-a"))
    after = portal_page(call_row("call-a", note="שלום"))
    driver, client, transport = make_driver(before, after)
    try:
        await driver.act_on_call(MEMBER, call_id="call-a", action="complain", text="שלום")
    finally:
        await client.aclose()
    request = transport.requests[1]
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert b"%F9%EC%E5%ED" in request.content
    assert b"%D7%A9%D7%9C%D7%95%D7%9D" not in request.content


@pytest.mark.asyncio
async def test_unencodable_mutation_input_fails_after_initial_fetch():
    before = portal_page(call_row("call-a"))
    driver, client, transport = make_driver(before)
    try:
        with pytest.raises(APIException) as raised:
            await driver.act_on_call(MEMBER, call_id="call-a", action="complain", text="😀")
    finally:
        await client.aclose()
    assert raised.value.code == "help_invalid_input"
    assert raised.value.message == "The request contains invalid input."
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_action_text_already_present_does_not_confirm_unrelated_change():
    before = portal_page(call_row("call-a", note="Please review"))
    after = portal_page(call_row("call-a", note="Please review", actions=("close",)))
    driver, client, _ = make_driver(before, after)
    try:
        with pytest.raises(APIException) as raised:
            await driver.act_on_call(MEMBER, call_id="call-a", action="complain", text="Please review")
    finally:
        await client.aclose()
    assert raised.value.code == "help_write_unconfirmed"


@pytest.mark.asyncio
async def test_create_multipart_scalars_use_windows_1255_bytes():
    before = portal_page(call_row("call-old"))
    after = portal_page(call_row("call-old"), call_row("call-new", description="שלום", note="פרטים"))
    driver, client, transport = make_driver(before, after)
    try:
        await driver.create_call(
            MEMBER,
            category_id="category-a",
            description="שלום",
            details="פרטים",
            contact_id="contact-a",
            contact_name="Contact fixture",
            contact_phone="",
            contact_email="",
            attachments=[],
        )
    finally:
        await client.aclose()
    body = transport.requests[1].content
    assert b"\xf9\xec\xe5\xed" in body
    assert "שלום".encode("utf-8") not in body


@pytest.mark.asyncio
async def test_create_rejects_matching_call_when_an_unrelated_call_is_also_new():
    before = portal_page(call_row("call-old"))
    after = portal_page(
        call_row("call-old"),
        call_row(
            "call-matching",
            description="Fixture request",
            note="Fixture details",
        ),
        call_row("call-unrelated", description="Another request"),
    )
    driver, client, _ = make_driver(before, after)

    try:
        with pytest.raises(APIException) as raised:
            await driver.create_call(
                MEMBER,
                category_id="category-a",
                description="Fixture request",
                details="Fixture details",
                contact_id="contact-a",
                contact_name="Contact fixture",
                contact_phone="",
                contact_email="",
                attachments=[],
            )
    finally:
        await client.aclose()

    assert raised.value.code == "help_write_unconfirmed"


@pytest.mark.asyncio
async def test_action_rejects_unrelated_field_change_without_submitted_text():
    before = portal_page(call_row("call-a"))
    after = portal_page(call_row("call-a", note="Unrelated update"))
    driver, client, _ = make_driver(before, after)

    try:
        with pytest.raises(APIException) as raised:
            await driver.act_on_call(
                MEMBER,
                call_id="call-a",
                action="complain",
                text="Please review",
            )
    finally:
        await client.aclose()

    assert raised.value.code == "help_write_unconfirmed"


@pytest.mark.asyncio
async def test_unencodable_create_text_fails_before_mutation_post():
    driver, client, transport = make_driver(portal_page())

    try:
        with pytest.raises(APIException) as raised:
            await driver.create_call(
                MEMBER,
                category_id="category-a",
                description="😀",
                details="",
                contact_id="contact-a",
                contact_name="Contact fixture",
                contact_phone="",
                contact_email="",
                attachments=[],
            )
    finally:
        await client.aclose()

    assert raised.value.code == "help_invalid_input"
    assert raised.value.message == "The request contains invalid input."
    assert "😀" not in str(raised.value)
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_create_calendar_redirect_refetches_and_binds_created_call():
    before = portal_page(
        call_row("10000"),
        categories=(("616", "Synthetic electricity"),),
    )
    after = portal_page(
        call_row("10000"),
        call_row(
            "12345",
            description="Synthetic fixture request",
            note="Synthetic fixture details",
        ),
        categories=(("616", "Synthetic electricity"),),
    )
    driver, client, transport = make_driver(
        before,
        {
            "status": 302,
            "headers": {
                "location": "https://scheduler.invalid/ScheduleAppointment/12345"
            },
        },
        "<html><body>Scheduler landing</body></html>",
        after,
    )

    try:
        created = await driver.create_call(
            MEMBER,
            category_id="616",
            description="Synthetic fixture request",
            details="Synthetic fixture details",
            contact_id="contact-a",
            contact_name="Synthetic contact",
            contact_phone="",
            contact_email="",
            attachments=[],
        )
    finally:
        await client.aclose()

    assert created["id"] == "12345"
    assert created["description"] == "Synthetic fixture request"
    assert created["scheduling"] == {"mode": "calendar", "call_id": "12345"}
    assert [request.url.path for request in transport.requests] == [
        "/hhopencall.pl",
        "/hhopencall.pl",
        "/ScheduleAppointment/12345",
        "/hhopencall.pl",
    ]


@pytest.mark.asyncio
async def test_normal_create_without_handler_reports_unknown_scheduling():
    empty_handler_row = """
      <tr><td>
        <form>
          <input name="callid" value="12345">
          <input name="callisopen" value="1">
          <input name="callopened" value="2026-01-02 03:04">
          <input name="calldescription" value="Synthetic fixture request">
          <input name="callnote" value="Synthetic fixture details">
          <button name="callsb" value="close">Action</button>
        </form>
      </td></tr>
    """
    before = portal_page()
    after = portal_page(empty_handler_row)
    driver, client, _ = make_driver(before, after)

    try:
        created = await driver.create_call(
            MEMBER,
            category_id="category-a",
            description="Synthetic fixture request",
            details="Synthetic fixture details",
            contact_id="contact-a",
            contact_name="Synthetic contact",
            contact_phone="",
            contact_email="",
            attachments=[],
        )
    finally:
        await client.aclose()

    assert created["handler"] == ""
    assert created["scheduling"] == {"mode": "unknown"}


@pytest.mark.asyncio
async def test_schedule_options_parse_structural_json_and_redact_internals():
    slots = [
        schedule_slot(
            "synthetic-event-]};",
            "2026-10-06T12:00:00",
            "2026-10-06T13:00:00",
        ),
        schedule_slot(
            "synthetic-unavailable",
            "2026-10-07T12:00:00",
            "2026-10-07T13:00:00",
            available=False,
        ),
        schedule_slot(
            "synthetic-current",
            "2026-10-05T09:00:00",
            "2026-10-05T10:00:00",
            available=False,
            current=True,
        ),
    ]
    driver, client, _ = make_driver(scheduler_page("12345", slots))

    try:
        result = await driver.get_schedule_options("12345")
    finally:
        await client.aclose()

    assert result["call_id"] == "12345"
    assert result["slots"] == [
        {
            "id": result["slots"][0]["id"],
            "start": "2026-10-06T12:00:00",
            "end": "2026-10-06T13:00:00",
        }
    ]
    assert result["current"]["start"] == "2026-10-05T09:00:00"
    assert re.fullmatch(r"[0-9a-f]{64}", result["slots"][0]["id"])
    serialized = json.dumps(result)
    for private_value in (
        "synthetic-event",
        "synthetic-csrf",
        "Synthetic Resident",
        "resident@example.invalid",
    ):
        assert private_value not in serialized


@pytest.mark.asyncio
async def test_booking_freshly_resolves_slot_posts_complete_utf8_form_and_confirms():
    available = schedule_slot(
        "synthetic-booking-event",
        "2026-10-06T12:00:00",
        "2026-10-06T13:00:00",
    )
    confirmed = schedule_slot(
        "synthetic-booking-event",
        "2026-10-06T12:00:00",
        "2026-10-06T13:00:00",
        available=False,
        current=True,
    )
    driver, client, transport = make_driver(
        scheduler_page("12345", [available], token="options-token"),
        scheduler_page("12345", [available], token="fresh-token"),
        "<html><body>Accepted</body></html>",
        scheduler_page("12345", [confirmed], token="confirmed-token"),
    )

    try:
        options = await driver.get_schedule_options("12345")
        result = await driver.book_schedule(
            "12345",
            options["slots"][0]["id"],
        )
    finally:
        await client.aclose()

    assert [request.method for request in transport.requests] == [
        "GET",
        "GET",
        "POST",
        "GET",
    ]
    posted = parse_qs(
        transport.requests[2].content.decode("utf-8"),
        keep_blank_values=True,
    )
    assert posted == {
        "ScheduleViewModel.CallId": ["12345"],
        "ScheduleViewModel.StrmCode": ["616"],
        "ScheduleViewModel.CustomerName": ["Synthetic Resident"],
        "ScheduleViewModel.TechnicianEmail": ["technician@example.invalid"],
        "ScheduleViewModel.AvailabilityCalendarName": ["synthetic-calendar"],
        "ScheduleViewModel.CustomerEmail": ["resident@example.invalid"],
        "ScheduleViewModel.AppointmentSubject": ["Synthetic appointment"],
        "ScheduleViewModel.AppointmentDescription": ["Synthetic details"],
        "ScheduleViewModel.SelectedStartTime": ["2026-10-06T12:00:00"],
        "ScheduleViewModel.SelectedEndTime": ["2026-10-06T13:00:00"],
        "ScheduleViewModel.SelectedEventId": ["synthetic-booking-event"],
        "AdminMode": ["false"],
        "AdminToken": [""],
        "AdminRangeSteps": ["4"],
        "__RequestVerificationToken": ["fresh-token"],
    }
    assert transport.requests[2].headers["content-type"].startswith(
        "application/x-www-form-urlencoded; charset=utf-8"
    )
    assert result == {
        "call_id": "12345",
        "appointment": options["slots"][0],
    }
    assert "synthetic-booking-event" not in json.dumps(result)


@pytest.mark.asyncio
async def test_booking_rejects_slot_that_became_unavailable_before_post():
    available = schedule_slot(
        "synthetic-stale-event",
        "2026-10-06T12:00:00",
        "2026-10-06T13:00:00",
    )
    stale = schedule_slot(
        "synthetic-stale-event",
        "2026-10-06T12:00:00",
        "2026-10-06T13:00:00",
        available=False,
    )
    driver, client, transport = make_driver(
        scheduler_page("12345", [available]),
        scheduler_page("12345", [stale], token="fresh-token"),
    )

    try:
        options = await driver.get_schedule_options("12345")
        with pytest.raises(APIException) as raised:
            await driver.book_schedule("12345", options["slots"][0]["id"])
    finally:
        await client.aclose()

    assert raised.value.code == "help_schedule_slot_unavailable"
    assert [request.method for request in transport.requests] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_schedule_page_requires_csrf_before_booking_or_exposing_options():
    available = schedule_slot(
        "synthetic-event",
        "2026-10-06T12:00:00",
        "2026-10-06T13:00:00",
    )
    driver, client, transport = make_driver(
        scheduler_page("12345", [available], include_token=False)
    )

    try:
        with pytest.raises(APIException) as raised:
            await driver.get_schedule_options("12345")
    finally:
        await client.aclose()

    assert raised.value.code == "help_portal_invalid_response"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_booking_does_not_false_confirm_a_different_current_slot():
    selected = schedule_slot(
        "synthetic-selected-event",
        "2026-10-06T12:00:00",
        "2026-10-06T13:00:00",
    )
    different = schedule_slot(
        "synthetic-different-event",
        "2026-10-07T12:00:00",
        "2026-10-07T13:00:00",
        available=False,
        current=True,
    )
    driver, client, _ = make_driver(
        scheduler_page("12345", [selected], token="fresh-token"),
        "<html><body>Accepted</body></html>",
        scheduler_page("12345", [different], token="confirmed-token"),
    )
    first_driver, first_client, _ = make_driver(
        scheduler_page("12345", [selected])
    )
    try:
        options = await first_driver.get_schedule_options("12345")
    finally:
        await first_client.aclose()

    try:
        with pytest.raises(APIException) as raised:
            await driver.book_schedule("12345", options["slots"][0]["id"])
    finally:
        await client.aclose()

    assert raised.value.code == "help_write_unconfirmed"


@pytest.mark.asyncio
async def test_scheduler_response_size_limit_is_enforced():
    oversized = b"x" * (2 * 1024 * 1024 + 1)
    driver, client, _ = make_driver(
        (oversized, {"content-type": "text/html; charset=utf-8"})
    )

    try:
        with pytest.raises(APIException) as raised:
            await driver.get_schedule_options("12345")
    finally:
        await client.aclose()

    assert raised.value.code == "help_portal_invalid_response"
