import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote_from_bytes, urljoin

import httpx
from bs4 import BeautifulSoup
import config

from errors import APIException


_BASE_URL = "https://help.mmm.org.il"
_PORTAL_PATH = "/hhopencall.pl"
_TIMEOUT = httpx.Timeout(20.0)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ATTACHMENTS = 5
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


class HelpPortalDriver:
    """Async driver for the service-center HTML form application."""

    def __init__(
        self,
        base_url: str = _BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._client = client or httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": "maaganm-api/1.0",
                "Accept-Language": "he-IL,he;q=0.9,en;q=0.7",
            },
        )

    async def close(self) -> None:
        if not self._client.is_closed:
            await self._client.aclose()

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

        content, content_type = await self._request("POST", _PORTAL_PATH, files=files)
        result = self._parse_page(content, content_type)
        previous_ids = {call.public["id"] for call in fresh.calls}
        created = [
            call for call in result.calls if call.public["id"] not in previous_ids
        ]
        normalized_description = _normalize_text(description)
        normalized_details = _normalize_text(details)
        if (
            len(created) != 1
            or _normalize_text(created[0].public["description"])
            != normalized_description
            or (
                normalized_details
                and normalized_details
                not in _normalize_text(
                    f"{created[0].public['description']} {created[0].public['note']}"
                )
            )
        ):
            raise _error(502, "help_write_unconfirmed", "The service-center change could not be confirmed.")
        return dict(created[0].public)

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
        content, content_type = await self._request(
            "POST",
            _PORTAL_PATH,
            content=encoded_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        result = self._parse_page(content, content_type)
        after = next((call for call in result.calls if call.public["id"] == target_id), None)
        if after is None or not self._action_confirmed(before, after, action, str(text)):
            raise _error(502, "help_write_unconfirmed", "The service-center change could not be confirmed.")
        return dict(after.public)

    async def _fetch_page(self, member_id: Any) -> _Page:
        content, content_type = await self._request(
            "POST",
            _PORTAL_PATH,
            content=_encode_form_payload({"member": member_id}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._parse_page(content, content_type)

    async def _request(self, method: str, path: str, **kwargs: Any) -> tuple[bytes, str]:
        url = urljoin(self._base_url, path.lstrip("/"))
        try:
            async with self._client.stream(
                method, url, timeout=_TIMEOUT, follow_redirects=True, **kwargs
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise _error(502, "help_portal_unavailable", "The service center is unavailable.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise _error(
                            502,
                            "help_portal_invalid_response",
                            "Service center returned an invalid response.",
                        )
                return bytes(body), response.headers.get("content-type", "")
        except APIException:
            raise
        except httpx.HTTPError as exc:
            raise _error(502, "help_portal_unavailable", "The service center is unavailable.") from exc

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


help_portal_driver = HelpPortalDriver(getattr(config, "HELP_BASE_URL", _BASE_URL))
