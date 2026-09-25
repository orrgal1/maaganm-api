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
) -> str:
    buttons = "".join(
        f'<button name="callsb" value="{action}">Action</button>' for action in actions
    )
    return f"""
      <tr>
        <td>{call_id}</td>
        <td>2026-01-02 03:04</td>
        <td>Service desk</td>
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


def portal_page(*rows: str, category_name: str = "General service") -> str:
    return f"""<!doctype html>
    <html><head><meta charset="windows-1255"></head><body>
      <form method="post">
        <input name="member" value="fixture-member">
        <input name="dira" value="residence-fixture">
        <select name="strm">
          <option value="">Choose</option>
          <option value="category-a">{category_name}</option>
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


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, tuple):
            content, headers = response
        else:
            content, headers = response.encode("utf-8"), {"content-type": "text/html; charset=utf-8"}
        return httpx.Response(200, content=content, headers=headers, request=request)


def make_driver(*responses):
    transport = ScriptedTransport(responses)
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return HelpPortalDriver("https://service.invalid", client), client, transport


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
        "categories": [{"id": "category-a", "name": "שירות כללי"}],
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
