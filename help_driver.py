import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote_from_bytes, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
import config

from errors import APIException


_BASE_URL = "https://help.mmm.org.il"
_PORTAL_PATH = "/hhopencall.pl"
_SCHEDULER_BASE_URL = "https://hh-add.mmm.org.il"
_SCHEDULER_PATH_PREFIX = "/ScheduleAppointment/"
_TIMEOUT = httpx.Timeout(20.0)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ATTACHMENTS = 5
_MAX_SCHEDULE_SLOTS = 1000
_SCHEDULE_FORM_FIELDS = (
    "ScheduleViewModel.CallId",
    "ScheduleViewModel.StrmCode",
    "ScheduleViewModel.CustomerName",
    "ScheduleViewModel.TechnicianEmail",
    "ScheduleViewModel.AvailabilityCalendarName",
    "ScheduleViewModel.CustomerEmail",
    "ScheduleViewModel.AppointmentSubject",
    "ScheduleViewModel.AppointmentDescription",
    "ScheduleViewModel.SelectedStartTime",
    "ScheduleViewModel.SelectedEndTime",
    "ScheduleViewModel.SelectedEventId",
    "AdminMode",
    "AdminToken",
    "AdminRangeSteps",
    "__RequestVerificationToken",
)
_MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024
_ACTIONS = ("complain", "hurryup", "close", "reopen")
_TRUE_VALUES = {"1", "true", "yes", "on", "open", "opened"}
_FALSE_VALUES = {"0", "false", "no", "off", "closed"}


@dataclass
class _Page:
    soup: BeautifulSoup
    calls: list["_CallRecord"]


@dataclass
class _CallRecord:
    public: dict[str, Any]
    state_value: str


@dataclass
class _HTTPResponse:
    content: bytes
    content_type: str
    final_url: str


@dataclass(frozen=True)
class _ScheduleSlot:
    source_event_id: str
    start: str
    end: str


@dataclass(frozen=True)
class _CurrentScheduleSlot:
    start: str
    end: str


@dataclass
class _SchedulePage:
    form_values: dict[str, str]
    slots: list[_ScheduleSlot]
    current: Optional[_CurrentScheduleSlot]
    supports_move: bool

class HelpPortalDriver:
    """Async driver for the service-center HTML form application."""

    def __init__(
        self,
        base_url: str = _BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
        scheduler_base_url: str = _SCHEDULER_BASE_URL,
        scheduler_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._scheduler_base_url = scheduler_base_url.rstrip("/") + "/"
        self._client = client or httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": "maaganm-api/1.0",
                "Accept-Language": "he-IL,he;q=0.9,en;q=0.7",
            },
        )
        self._scheduler_client = scheduler_client or self._client

    async def close(self) -> None:
        if not self._client.is_closed:
            await self._client.aclose()
        if self._scheduler_client is not self._client and not self._scheduler_client.is_closed:
            await self._scheduler_client.aclose()

    async def get_catalog(self, member_id: Any) -> dict[str, Any]:
        page = await self._fetch_page(member_id)
        return self._parse_catalog(page.soup)

    async def list_calls(self, member_id: Any) -> list[dict[str, Any]]:
        page = await self._fetch_page(member_id)
        return [dict(call.public) for call in page.calls]

    async def create_call(
        self,
        member_id: Any,
        *,
        category_id: Any,
        description: str,
        details: str,
        contact_id: Any,
        contact_name: str,
        contact_phone: str,
        contact_email: str,
        attachments: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        fresh = await self._fetch_page(member_id)
        catalog = self._parse_catalog(fresh.soup)
        category = str(category_id)
        contact = str(contact_id)

        if category not in {item["id"] for item in catalog["categories"]}:
            raise _error(400, "help_category_unavailable", "The requested service category is unavailable.")

        contact_values = self._option_values(fresh.soup, "cntct")
        if contact not in contact_values:
            raise _error(400, "help_contact_unavailable", "The requested service contact is unavailable.")

        scalar_values = {
            "member": member_id,
            "dira": catalog.get("residence_id", ""),
            "strm": category,
            "dscr": description,
            "rmrk": details,
            "cntct": contact,
            "cntct_name": contact_name,
            "cntct_cell": contact_phone,
            "cntct_email": contact_email,
        }
        encoded_scalars = {name: _encode_form_scalar(value) for name, value in scalar_values.items()}
        files: list[tuple[str, tuple[Any, ...]]] = [
            (name, (None, value)) for name, value in encoded_scalars.items()
        ]
        prepared_attachments = self._prepare_attachments(attachments)
        if prepared_attachments:
            files.extend(("imagefile", attachment) for attachment in prepared_attachments)
        else:
            files.append(("imagefile", ("", b"", "application/octet-stream")))

        response = await self._request("POST", _PORTAL_PATH, files=files)
        previous_ids = {call.public["id"] for call in fresh.calls}
        scheduler_call_id = self._scheduler_call_id_from_url(response.final_url)
        if category == "616" and scheduler_call_id is not None:
            result = await self._fetch_page(member_id)
            created = self._confirm_created_call(
                result,
                previous_ids,
                description,
                details,
                expected_id=scheduler_call_id,
            )
            public = dict(created.public)
            public["scheduling"] = {"mode": "calendar", "call_id": scheduler_call_id}
            return public

        result = self._parse_page(response.content, response.content_type)
        created = self._confirm_created_call(result, previous_ids, description, details)
        public = dict(created.public)
        handler = _normalize_text(public.get("handler", ""))
        public["scheduling"] = (
            {"mode": "assigned", "handler": handler}
            if handler
            else {"mode": "unknown"}
        )
        return public

    async def get_schedule_options(self, call_id: str) -> dict[str, Any]:
        target_id = _validated_schedule_call_id(call_id)
        page = await self._fetch_schedule_page(target_id)
        result: dict[str, Any] = {
            "call_id": target_id,
            "slots": [_public_schedule_slot(slot) for slot in page.slots],
        }
        if page.current is not None:
            result["current"] = _public_current_schedule_slot(page.current)
        return result

    async def get_schedule_replacements(
        self,
        member_id: Any,
        call_id: str,
    ) -> dict[str, Any]:
        target_id = _validated_schedule_call_id(call_id)
        call = self._find_call(await self._fetch_page(member_id), target_id)
        schedule = await self._fetch_optional_schedule_page(target_id)
        if schedule is None or schedule.form_values["ScheduleViewModel.StrmCode"] != "616":
            return {
                "call_id": target_id,
                "scheduling": _call_scheduling(call),
            }
        if (
            not call.public["is_open"]
            or schedule.current is None
            or not schedule.supports_move
        ):
            raise _error(
                400,
                "help_reschedule_unavailable",
                "The appointment cannot be rescheduled.",
            )
        return {
            "call_id": target_id,
            "scheduling": {"mode": "calendar"},
            "appointment": {
                **_public_current_schedule_slot(schedule.current),
                "status": "scheduled",
            },
            "replacement_slots": [
                _public_schedule_slot(slot) for slot in schedule.slots
            ],
        }

    async def move_schedule(
        self,
        member_id: Any,
        call_id: str,
        slot_id: str,
    ) -> dict[str, Any]:
        target_id = _validated_schedule_call_id(call_id)
        target_slot_id = _validated_public_slot_id(slot_id)
        call = self._find_call(await self._fetch_page(member_id), target_id)
        fresh = await self._fetch_optional_schedule_page(target_id)
        if (
            not call.public["is_open"]
            or fresh is None
            or fresh.form_values["ScheduleViewModel.StrmCode"] != "616"
            or fresh.current is None
            or not fresh.supports_move
        ):
            raise _error(
                400,
                "help_reschedule_unavailable",
                "The appointment cannot be rescheduled.",
            )
        selected = next(
            (slot for slot in fresh.slots if _schedule_slot_id(slot) == target_slot_id),
            None,
        )
        if selected is None or (
            selected.start == fresh.current.start and selected.end == fresh.current.end
        ):
            raise _error(
                400,
                "help_schedule_slot_unavailable",
                "The requested appointment slot is unavailable.",
            )

        payload = dict(fresh.form_values)
        payload["ScheduleViewModel.SelectedStartTime"] = selected.start
        payload["ScheduleViewModel.SelectedEndTime"] = selected.end
        payload["ScheduleViewModel.SelectedEventId"] = selected.source_event_id
        await self._request(
            "POST",
            self._schedule_path(target_id),
            scheduler=True,
            content=urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        )

        confirmed = await self._fetch_optional_schedule_page(target_id)
        current = confirmed.current if confirmed is not None else None
        if (
            confirmed is None
            or not confirmed.supports_move
            or current is None
            or current.start != selected.start
            or current.end != selected.end
        ):
            raise _error(
                502,
                "help_write_unconfirmed",
                "The service-center change could not be confirmed.",
            )
        return {
            "call_id": target_id,
            "appointment": {
                "id": target_slot_id,
                **_public_current_schedule_slot(current),
                "status": "scheduled",
            },
            "status": "rescheduled",
        }


    async def book_schedule(self, call_id: str, slot_id: str) -> dict[str, Any]:
        target_id = _validated_schedule_call_id(call_id)
        target_slot_id = _validated_public_slot_id(slot_id)
        fresh = await self._fetch_schedule_page(target_id)
        if fresh.current is not None:
            raise _error(
                400,
                "help_schedule_already_booked",
                "The service call already has an appointment.",
            )
        selected = next(
            (slot for slot in fresh.slots if _schedule_slot_id(slot) == target_slot_id),
            None,
        )
        if selected is None:
            raise _error(
                400,
                "help_schedule_slot_unavailable",
                "The requested appointment slot is unavailable.",
            )

        payload = dict(fresh.form_values)
        payload["ScheduleViewModel.SelectedStartTime"] = selected.start
        payload["ScheduleViewModel.SelectedEndTime"] = selected.end
        payload["ScheduleViewModel.SelectedEventId"] = selected.source_event_id
        await self._request(
            "POST",
            self._schedule_path(target_id),
            scheduler=True,
            content=urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        )

        confirmed = await self._fetch_schedule_page(target_id)
        current = confirmed.current
        if current is None or current.start != selected.start or current.end != selected.end:
            raise _error(
                502,
                "help_write_unconfirmed",
                "The service-center change could not be confirmed.",
            )
        return {
            "call_id": target_id,
            "appointment": {
                "id": target_slot_id,
                **_public_current_schedule_slot(current),
            },
        }

    async def act_on_call(
        self,
        member_id: Any,
        *,
        call_id: Any,
        action: str,
        text: str,
    ) -> dict[str, Any]:
        if action not in _ACTIONS:
            raise _error(400, "help_action_unavailable", "The requested call action is unavailable.")

        fresh = await self._fetch_page(member_id)
        target_id = str(call_id)
        before = next((call for call in fresh.calls if call.public["id"] == target_id), None)
        if before is None:
            raise _error(404, "help_call_unavailable", "The requested service call is unavailable.")
        if action not in before.public["available_actions"]:
            raise _error(400, "help_action_unavailable", "The requested call action is unavailable.")
        if (action == "close" and not before.public["is_open"]) or (
            action == "reopen" and before.public["is_open"]
        ):
            raise _error(400, "help_action_unavailable", "The requested call action is unavailable.")

        payload = {
            "callactiontext": text,
            "callid": target_id,
            "callisopen": before.state_value,
            "callsb": action,
            "member": member_id,
        }
        encoded_payload = _encode_form_payload(payload)
        response = await self._request(
            "POST",
            _PORTAL_PATH,
            content=encoded_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        result = self._parse_page(response.content, response.content_type)
        after = next((call for call in result.calls if call.public["id"] == target_id), None)
        if after is None or not self._action_confirmed(before, after, action, str(text)):
            raise _error(502, "help_write_unconfirmed", "The service-center change could not be confirmed.")
        return dict(after.public)

    async def _fetch_page(self, member_id: Any) -> _Page:
        response = await self._request(
            "POST",
            _PORTAL_PATH,
            content=_encode_form_payload({"member": member_id}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._parse_page(response.content, response.content_type)

    async def _fetch_schedule_page(self, call_id: str) -> _SchedulePage:
        page = await self._fetch_optional_schedule_page(call_id)
        if page is None:
            raise _invalid_response()
        return page

    async def _fetch_optional_schedule_page(
        self,
        call_id: str,
    ) -> Optional[_SchedulePage]:
        response = await self._request(
            "GET",
            self._schedule_path(call_id),
            scheduler=True,
        )
        final_path = urlsplit(response.final_url).path.rstrip("/")
        if final_path == "/Error":
            return None
        if final_path != self._schedule_path(call_id).rstrip("/"):
            raise _invalid_response()
        return self._parse_schedule_page(response.content, response.content_type, call_id)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        scheduler: bool = False,
        **kwargs: Any,
    ) -> _HTTPResponse:
        base_url = self._scheduler_base_url if scheduler else self._base_url
        client = self._scheduler_client if scheduler else self._client
        url = urljoin(base_url, path.lstrip("/"))
        try:
            async with client.stream(
                method, url, timeout=_TIMEOUT, follow_redirects=True, **kwargs
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise _error(502, "help_portal_unavailable", "The service center is unavailable.")
                if scheduler and _url_origin(urlsplit(str(response.url))) != _url_origin(
                    urlsplit(self._scheduler_base_url)
                ):
                    raise _invalid_response()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise _error(
                            502,
                            "help_portal_invalid_response",
                            "Service center returned an invalid response.",
                        )
                final_url = str(response.url.copy_with(query=None, fragment=None))
                return _HTTPResponse(
                    content=bytes(body),
                    content_type=response.headers.get("content-type", ""),
                    final_url=final_url,
                )
        except APIException:
            raise
        except httpx.HTTPError as exc:
            raise _error(502, "help_portal_unavailable", "The service center is unavailable.") from exc

    def _confirm_created_call(
        self,
        page: _Page,
        previous_ids: set[str],
        description: str,
        details: str,
        *,
        expected_id: Optional[str] = None,
    ) -> _CallRecord:
        if expected_id is None:
            candidates = [
                call for call in page.calls if call.public["id"] not in previous_ids
            ]
        else:
            candidates = [
                call
                for call in page.calls
                if call.public["id"] == expected_id
                and call.public["id"] not in previous_ids
            ]
        normalized_description = _normalize_text(description)
        normalized_details = _normalize_text(details)
        if (
            len(candidates) != 1
            or _normalize_text(candidates[0].public["description"])
            != normalized_description
            or (
                normalized_details
                and normalized_details
                not in _normalize_text(
                    f"{candidates[0].public['description']} {candidates[0].public['note']}"
                )
            )
        ):
            raise _error(
                502,
                "help_write_unconfirmed",
                "The service-center change could not be confirmed.",
            )
        return candidates[0]

    @staticmethod
    def _find_call(page: _Page, call_id: str) -> _CallRecord:
        call = next(
            (record for record in page.calls if record.public["id"] == call_id),
            None,
        )
        if call is None:
            raise _error(
                404,
                "help_call_unavailable",
                "The requested service call is unavailable.",
            )
        return call

    def _schedule_path(self, call_id: str) -> str:
        return f"{_SCHEDULER_PATH_PREFIX}{call_id}"

    def _scheduler_call_id_from_url(self, value: str) -> Optional[str]:
        parsed = urlsplit(value)
        expected = urlsplit(self._scheduler_base_url)
        if _url_origin(parsed) != _url_origin(expected):
            return None
        match = re.fullmatch(r"/ScheduleAppointment/([0-9]+)", parsed.path)
        return match.group(1) if match else None

    def _parse_schedule_page(
        self,
        content: bytes,
        content_type: str,
        call_id: str,
    ) -> _SchedulePage:
        soup = BeautifulSoup(_decode_html(content, content_type), "html.parser")
        token = soup.find(attrs={"name": "__RequestVerificationToken"})
        form = token.find_parent("form") if token is not None else None
        if form is None:
            raise _invalid_response()

        form_values: dict[str, str] = {}
        for name in _SCHEDULE_FORM_FIELDS:
            control = form.find(attrs={"name": name})
            if control is None or control.has_attr("disabled"):
                raise _invalid_response()
            form_values[name] = _control_value(control)
        if (
            form_values["ScheduleViewModel.CallId"] != call_id
            or not form_values["__RequestVerificationToken"]
        ):
            raise _invalid_response()

        raw_slots, raw_current = _extract_schedule_declarations(soup)
        if len(raw_slots) > _MAX_SCHEDULE_SLOTS:
            raise _invalid_response()
        slots: list[_ScheduleSlot] = []
        identities: set[tuple[str, str, str]] = set()
        for item in raw_slots:
            if not isinstance(item, dict):
                raise _invalid_response()
            source_event_id = item.get("sourceEventId")
            start = item.get("start")
            end = item.get("end")
            required_keys = {"sourceEventId", "start", "end", "isAvailable", "isCurrent"}
            if (
                not required_keys.issubset(item)
                or not isinstance(source_event_id, str)
                or not source_event_id
                or not isinstance(start, str)
                or not start
                or not isinstance(end, str)
                or not end
                or item.get("isAvailable") is not True
                or item.get("isCurrent") is not False
            ):
                raise _invalid_response()
            identity = (source_event_id, start, end)
            if identity in identities:
                raise _invalid_response()
            identities.add(identity)
            slots.append(_ScheduleSlot(source_event_id=source_event_id, start=start, end=end))

        current = None
        if raw_current is not None:
            current_keys = {"id", "start", "end", "isCurrent", "isAvailable", "editable"}
            if (
                not isinstance(raw_current, dict)
                or not current_keys.issubset(raw_current)
                or raw_current.get("id") != "__current_slot__"
                or not isinstance(raw_current.get("start"), str)
                or not raw_current["start"]
                or not isinstance(raw_current.get("end"), str)
                or not raw_current["end"]
                or raw_current.get("isCurrent") is not True
                or raw_current.get("isAvailable") is not False
                or raw_current.get("editable") is not False
            ):
                raise _invalid_response()
            current = _CurrentScheduleSlot(start=raw_current["start"], end=raw_current["end"])

        update_control = form.find(id="btnSubmitSchedule")
        form_action = str(form.get("action", "")).strip()
        form_action_path = urlsplit(form_action).path if form_action else self._schedule_path(call_id)
        supports_move = bool(
            current is not None
            and str(form.get("method", "get")).strip().lower() == "post"
            and form_action_path == self._schedule_path(call_id)
            and update_control is not None
            and not update_control.get("formaction")
            and _normalize_text(update_control.get_text(" ", strip=True)) == "עדכון מועד"
            and "בחירת מועד חדש תבטל את המועד הנוכחי"
            in _normalize_text(form.get_text(" ", strip=True))
        )
        return _SchedulePage(
            form_values=form_values,
            slots=slots,
            current=current,
            supports_move=supports_move,
        )

    def _parse_page(self, content: bytes, content_type: str) -> _Page:
        soup = BeautifulSoup(_decode_html(content, content_type), "html.parser")
        if soup.find(attrs={"name": "member"}) is None:
            raise _error(
                502,
                "help_portal_invalid_response",
                "Service center returned an invalid response.",
            )
        calls = self._parse_calls(soup)
        return _Page(soup=soup, calls=calls)

    def _parse_catalog(self, soup: BeautifulSoup) -> dict[str, Any]:
        category_select = soup.find("select", attrs={"name": "strm"})
        contact_controls = soup.find_all(attrs={"name": "cntct"})
        if category_select is None or not contact_controls:
            raise _error(
                502,
                "help_portal_invalid_response",
                "Service center returned an invalid response.",
            )

        categories = self._parse_options(category_select, include_metadata=False)
        contacts = self._parse_contacts(soup, contact_controls)
        if not categories:
            raise _error(
                502,
                "help_portal_invalid_response",
                "Service center returned an invalid response.",
            )
        for category in categories:
            category["scheduling"] = _category_scheduling(category["id"])

        result: dict[str, Any] = {"categories": categories, "contacts": contacts}
        residence = _field_value(soup, "dira")
        if residence:
            result["residence_id"] = residence
        return result

    @staticmethod
    def _parse_options(select: Any, *, include_metadata: bool) -> list[dict[str, str]]:
        parsed: list[dict[str, str]] = []
        for option in select.find_all("option"):
            value = str(option.get("value", "")).strip()
            if not value or option.has_attr("disabled"):
                continue
            name = option.get_text(" ", strip=True)
            if not name:
                continue
            item = {"id": value, "name": name}
            if include_metadata:
                metadata = {
                    "phone": ("data-phone", "data-cell", "data-contact-phone"),
                    "email": ("data-email", "data-contact-email"),
                }
                for key, attributes in metadata.items():
                    field = next((str(option.get(attr, "")).strip() for attr in attributes if option.get(attr)), "")
                    if field:
                        item[key] = field
            parsed.append(item)
        return parsed

    @classmethod
    def _parse_contacts(cls, soup: BeautifulSoup, controls: Sequence[Any]) -> list[dict[str, str]]:
        if len(controls) == 1 and getattr(controls[0], "name", None) == "select":
            return cls._parse_options(controls[0], include_metadata=True)

        parsed: list[dict[str, str]] = []
        for control in controls:
            if getattr(control, "name", None) == "select" or control.has_attr("disabled"):
                continue
            value = _control_value(control)
            if not value:
                continue
            name = _contact_label(soup, control)
            if not name:
                continue
            item = {"id": value, "name": name}
            metadata = {
                "phone": ("data-phone", "data-cell", "data-contact-phone"),
                "email": ("data-email", "data-contact-email"),
            }
            for key, attributes in metadata.items():
                field = next(
                    (str(control.get(attr, "")).strip() for attr in attributes if control.get(attr)),
                    "",
                )
                if field:
                    item[key] = field
            if control.has_attr("checked"):
                entered_name = _field_value(soup, "cntct_name")
                if entered_name:
                    item["name"] = entered_name
                for key, field_name in (("phone", "cntct_cell"), ("email", "cntct_email")):
                    field = _field_value(soup, field_name)
                    if field:
                        item[key] = field
            parsed.append(item)
        return parsed

    @staticmethod
    def _option_values(soup: BeautifulSoup, name: str) -> set[str]:
        controls = soup.find_all(attrs={"name": name})
        values: set[str] = set()
        for control in controls:
            if control.has_attr("disabled"):
                continue
            if getattr(control, "name", None) == "select":
                values.update(
                    str(option.get("value", "")).strip()
                    for option in control.find_all("option")
                    if not option.has_attr("disabled")
                )
            else:
                values.add(_control_value(control))
        return values

    def _parse_calls(self, soup: BeautifulSoup) -> list[_CallRecord]:
        records: dict[str, _CallRecord] = {}
        order: list[str] = []
        call_fields = soup.find_all(attrs={"name": "callid"})
        for call_field in call_fields:
            call_id = _control_value(call_field)
            if not call_id:
                continue
            row = call_field.find_parent("tr")
            container = row or call_field.find_parent("form") or call_field.parent
            if container is None:
                continue

            state_control = container.find(attrs={"name": "callisopen"})
            if state_control is None and row is not None:
                state_control = row.find(attrs={"name": "callisopen"})
            state_value = _control_value(state_control) if state_control is not None else ""
            actions = _available_actions(container)
            state = _parse_open_state(state_value, actions)
            if state is None:
                raise _error(
                    502,
                    "help_portal_invalid_response",
                    "Service center returned an invalid response.",
                )

            fields = {
                "opened_at": _semantic_value(container, "opened_at", ("callopened", "call_opened_at")),
                "handler": _semantic_value(container, "handler", ("callhandler", "call_handler")),
                "description": _semantic_value(
                    container, "description", ("calldescription", "call_description", "dscr")
                ),
                "note": _semantic_value(container, "note", ("callnote", "call_note", "rmrk")),
            }
            if not all(fields.values()) and row is not None:
                fallback = _row_text_values(row, call_id)
                for key, value in zip(("opened_at", "handler", "description", "note"), fallback):
                    if not fields[key]:
                        fields[key] = value

            if call_id not in records:
                public = {
                    "id": call_id,
                    **fields,
                    "is_open": state,
                    "available_actions": actions,
                }
                records[call_id] = _CallRecord(public=public, state_value=state_value)
                order.append(call_id)
            else:
                existing = records[call_id]
                existing.public["available_actions"] = list(
                    dict.fromkeys(existing.public["available_actions"] + actions)
                )
                for key, value in fields.items():
                    if not existing.public[key] and value:
                        existing.public[key] = value
                if not existing.state_value and state_value:
                    existing.state_value = state_value
        return [records[call_id] for call_id in order]

    @staticmethod
    def _prepare_attachments(
        attachments: Sequence[Mapping[str, Any]],
    ) -> list[tuple[str, bytes, str]]:
        if len(attachments) > _MAX_ATTACHMENTS:
            raise _error(400, "help_attachment_invalid", "The service-call attachments are invalid.")
        prepared: list[tuple[str, bytes, str]] = []
        total = 0
        for attachment in attachments:
            content = attachment.get("content")
            if not isinstance(content, bytes):
                raise _error(400, "help_attachment_invalid", "The service-call attachments are invalid.")
            total += len(content)
            if total > _MAX_ATTACHMENT_BYTES:
                raise _error(400, "help_attachment_invalid", "The service-call attachments are invalid.")
            filename = _safe_basename(str(attachment.get("filename", "")))
            content_type = str(attachment.get("content_type", "application/octet-stream"))
            prepared.append((filename, content, content_type))
        return prepared

    @staticmethod
    def _action_confirmed(
        before: _CallRecord,
        after: _CallRecord,
        action: str,
        text: str,
    ) -> bool:
        if action == "close":
            return not after.public["is_open"] and "reopen" in after.public["available_actions"]
        if action == "reopen":
            return after.public["is_open"] and "close" in after.public["available_actions"]
        normalized_text = _normalize_text(text)
        if not normalized_text:
            return False
        before_text = _normalize_text(f"{before.public['description']} {before.public['note']}")
        after_text = _normalize_text(f"{after.public['description']} {after.public['note']}")
        return normalized_text not in before_text and normalized_text in after_text


def _validated_schedule_call_id(value: Any) -> str:
    call_id = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,32}", call_id):
        raise _error(
            400,
            "help_call_unavailable",
            "The requested service call is unavailable.",
        )
    return call_id


def _extract_schedule_declarations(soup: BeautifulSoup) -> tuple[list[Any], Any]:
    decoder = json.JSONDecoder()
    available: list[Any] = []
    currents: list[Any] = []
    patterns = (
        (re.compile(r"\b(?:const|let|var)\s+availableSlots\s*=\s*"), available),
        (re.compile(r"\b(?:const|let|var)\s+currentSlotEvent\s*=\s*"), currents),
    )
    for script in soup.find_all("script"):
        source = script.string if script.string is not None else script.get_text()
        for pattern, declarations in patterns:
            for match in pattern.finditer(source):
                try:
                    value, _ = decoder.raw_decode(source, match.end())
                except (json.JSONDecodeError, TypeError) as exc:
                    raise _invalid_response() from exc
                declarations.append(value)
    if len(available) != 1 or len(currents) != 1 or not isinstance(available[0], list):
        raise _invalid_response()
    return available[0], currents[0]


def _validated_public_slot_id(value: Any) -> str:
    slot_id = str(value).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", slot_id):
        raise _error(
            400,
            "help_schedule_slot_unavailable",
            "The requested appointment slot is unavailable.",
        )
    return slot_id


def _schedule_slot_id(slot: _ScheduleSlot) -> str:
    identity = json.dumps(
        [slot.source_event_id, slot.start, slot.end],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _public_schedule_slot(slot: _ScheduleSlot) -> dict[str, str]:
    return {
        "id": _schedule_slot_id(slot),
        "start": slot.start,
        "end": slot.end,
    }

def _public_current_schedule_slot(slot: _CurrentScheduleSlot) -> dict[str, str]:
    return {"start": slot.start, "end": slot.end}


def _call_scheduling(call: _CallRecord) -> dict[str, str]:
    handler = _normalize_text(call.public.get("handler", ""))
    if handler:
        return {"mode": "assigned", "handler": handler}
    return {"mode": "unknown"}


def _category_scheduling(category_id: str) -> dict[str, str]:
    if category_id == "616":
        return {"mode": "calendar"}
    if category_id == "624":
        return {
            "mode": "contact",
            "phone": "077-7076023",
            "extension": "2",
        }
    return {"mode": "unknown"}


def _url_origin(value: Any) -> tuple[str, str, Optional[int]]:
    return (
        str(value.scheme).lower(),
        str(value.hostname or "").lower(),
        value.port,
    )


def _invalid_response() -> APIException:
    return _error(
        502,
        "help_portal_invalid_response",
        "Service center returned an invalid response.",
    )


def _normalize_text(value: Any) -> str:
    return " ".join(str(value).split())


def _encode_form_scalar(value: Any) -> bytes:
    try:
        return str(value).encode("windows-1255")
    except UnicodeEncodeError as exc:
        raise _error(400, "help_invalid_input", "The request contains invalid input.") from exc


def _encode_form_payload(values: Mapping[str, Any]) -> bytes:
    return b"&".join(
        (
            quote_from_bytes(_encode_form_scalar(name), safe=b"-._~")
            + "="
            + quote_from_bytes(_encode_form_scalar(value), safe=b"-._~")
        ).encode("ascii")
        for name, value in values.items()
    )


def _decode_html(content: bytes, content_type: str) -> str:
    encodings: list[str] = []
    header_match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.IGNORECASE)
    if header_match:
        encodings.append(header_match.group(1))
    prefix = content[:4096]
    meta_match = re.search(
        br"<meta[^>]+charset\s*=\s*[\"']?\s*([^\s\"'/>;]+)", prefix, re.IGNORECASE
    )
    if meta_match:
        encodings.append(meta_match.group(1).decode("ascii", "ignore"))
    http_equiv_match = re.search(
        br"<meta[^>]+content\s*=\s*[\"'][^\"']*charset\s*=\s*([^\s\"';>]+)",
        prefix,
        re.IGNORECASE,
    )
    if http_equiv_match:
        encodings.append(http_equiv_match.group(1).decode("ascii", "ignore"))
    encodings.extend(("utf-8", "windows-1255"))
    for encoding in dict.fromkeys(item.strip() for item in encodings if item.strip()):
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise _error(502, "help_portal_invalid_response", "Service center returned an invalid response.")


def _field_value(scope: Any, name: str) -> str:
    field = scope.find(attrs={"name": name})
    return _control_value(field) if field is not None else ""


def _control_value(control: Any) -> str:
    if control is None:
        return ""
    if getattr(control, "name", None) == "select":
        selected = control.find("option", selected=True) or control.find("option")
        return str(selected.get("value", "")).strip() if selected else ""
    if getattr(control, "name", None) == "textarea":
        return control.get_text(" ", strip=True)
    return str(control.get("value", "")).strip()


def _contact_label(soup: BeautifulSoup, control: Any) -> str:
    for attribute in ("data-name", "data-contact-name"):
        value = str(control.get(attribute, "")).strip()
        if value:
            return value
    control_id = str(control.get("id", "")).strip()
    if control_id:
        label = soup.find("label", attrs={"for": control_id})
        if label is not None:
            value = label.get_text(" ", strip=True)
            if value:
                return value
    parent = control.parent
    if getattr(parent, "name", None) == "label":
        value = parent.get_text(" ", strip=True)
        if value:
            return value
    for sibling in control.next_siblings:
        sibling_name = getattr(sibling, "name", None)
        if sibling_name == "br":
            break
        if sibling_name and (
            sibling.get("name") == "cntct"
            or sibling.find(attrs={"name": "cntct"}) is not None
        ):
            break
        value = sibling.get_text(" ", strip=True) if sibling_name else str(sibling).strip()
        if value:
            return value
    return ""


def _available_actions(container: Any) -> list[str]:
    actions: list[str] = []
    for control in container.find_all(attrs={"name": "callsb"}):
        if getattr(control, "name", None) == "select":
            values = [str(option.get("value", "")).strip() for option in control.find_all("option")]
        else:
            values = [str(control.get("value", "")).strip()]
        for value in values:
            if value in _ACTIONS and value not in actions and not control.has_attr("disabled"):
                actions.append(value)
    return actions


def _parse_open_state(value: str, actions: Sequence[str]) -> Optional[bool]:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    if "reopen" in actions and "close" not in actions:
        return False
    if "close" in actions and "reopen" not in actions:
        return True
    return None


def _semantic_value(container: Any, key: str, names: Sequence[str]) -> str:
    attr_name = key.replace("_", "-")
    direct = container.get(f"data-{attr_name}")
    if direct is not None:
        return str(direct).strip()
    marked = container.find(attrs={"data-field": key}) or container.find(
        attrs={"data-field": attr_name}
    )
    if marked is not None:
        return marked.get_text(" ", strip=True)
    class_marked = container.find(class_=lambda value: value and key in str(value).split())
    if class_marked is not None:
        return class_marked.get_text(" ", strip=True)
    for name in names:
        value = _field_value(container, name)
        if value:
            return value
    return ""


def _row_text_values(row: Any, call_id: str) -> list[str]:
    values: list[str] = []
    for cell in row.find_all("td", recursive=False):
        clone = BeautifulSoup(str(cell), "html.parser")
        for removable in clone.find_all(("form", "script", "style", "button", "input", "select", "textarea")):
            removable.decompose()
        text = clone.get_text(" ", strip=True)
        if text and text != call_id:
            values.append(text)
    return values[:4]


def _safe_basename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    basename = "".join(character for character in basename if 32 <= ord(character) != 127)
    if basename in {"", ".", ".."}:
        return "attachment"
    return basename


def _error(status_code: int, code: str, message: str) -> APIException:
    return APIException(status_code=status_code, code=code, message=message)


help_portal_driver = HelpPortalDriver(
    getattr(config, "HELP_BASE_URL", _BASE_URL),
    scheduler_base_url=getattr(
        config,
        "HELP_SCHEDULER_BASE_URL",
        _SCHEDULER_BASE_URL,
    ),
)
