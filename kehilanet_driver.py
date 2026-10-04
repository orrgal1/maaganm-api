"""Read-only access to Kehila-Net member pages."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from errors import APIException


_DEFAULT_BASE_URL = "https://www.maaganmk.co.il"
_PHONEBOOK_PATH = "/familytree/personlist.asp"
_ANNOUNCEMENTS_PATH = "/forum/forum/start.asp"
_DETAIL_PATH = "/forum/files/ViewMessage.asp"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CONTACTS = 500
_MAX_ANNOUNCEMENTS = 20
_TIMEOUT_MS = 20_000
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_HTTPS_RE = re.compile(r"https://[^\s<>\"']+", re.I)
_DETAIL_ONCLICK_RE = re.compile(
    r"^\s*top\.callMe\(\s*['\"](/forum/files/ViewMessage\.asp\?[^'\"]+)['\"]\s*\)\s*;\s*return\s+false\s*;?\s*$",
    re.I,
)


def _error(code: str, message: str) -> APIException:
    return APIException(status_code=502, code=code, message=message)


class _SessionExpired(Exception):
    pass


def _invalid_page() -> APIException:
    return _error("kehilanet_invalid_response", "The member service returned an invalid page.")


def _text(node: Any) -> str:
    return " ".join(node.stripped_strings)


def _query_string(parameters: dict[str, str]) -> str:
    try:
        return urlencode(parameters, encoding="cp1255", errors="strict")
    except UnicodeEncodeError:
        raise APIException(400, "kehilanet_invalid_input", "Invalid member page request.") from None


def _validate_query_limit(query: str, limit: int, maximum: int) -> None:
    if (
        not isinstance(query, str)
        or len(query) > 120
        or any(ord(character) < 32 or ord(character) == 127 for character in query)
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= maximum
    ):
        raise APIException(400, "kehilanet_invalid_input", "Invalid member page request.")


def _phonebook_contacts(soup: BeautifulSoup, limit: int) -> list[dict[str, Any]]:
    tables = soup.select("table#myTable")
    if len(tables) != 1:
        raise _invalid_page()
    table = tables[0]
    rows = table.find_all("tr", recursive=False)
    if not rows:
        tbody = table.find("tbody", recursive=False)
        rows = tbody.find_all("tr", recursive=False) if tbody else []
    if not rows:
        if soup.select('form input[name="SearchName"]'):
            return []
        raise _invalid_page()

    contacts: list[dict[str, Any]] = []
    for row in rows:
        cells = row.find_all("td", recursive=False)
        if not cells and row.find_all("th", recursive=False):
            continue
        if len(cells) != 6:
            raise _invalid_page()
        name = _text(cells[2])
        if not name:
            raise _invalid_page()
        phones: list[dict[str, str]] = []
        for phone_row in cells[3].find_all("tr"):
            pair = phone_row.find_all("td", recursive=False)
            if len(pair) != 2:
                raise _invalid_page()
            label, number = (_text(part) for part in pair)
            if not label or not number:
                raise _invalid_page()
            phones.append({"label": label, "number": number})
        emails = list(dict.fromkeys(_EMAIL_RE.findall(_text(cells[4]))))
        contacts.append({"name": name, "phones": phones, "emails": emails})
        if len(contacts) >= limit:
            break
    return contacts


def _detail_identity(onclick: str) -> tuple[str, str]:
    match = _DETAIL_ONCLICK_RE.fullmatch(onclick)
    if match is None:
        raise _invalid_page()
    parsed = urlsplit(match.group(1))
    if parsed.path.lower() != _DETAIL_PATH.lower() or parsed.fragment:
        raise _invalid_page()
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not {"forum_id", "msgID"}.issubset(query) or set(query) - {
        "forum_id", "msgID", "searchTXT"
    }:
        raise _invalid_page()
    forum_id, message_id = query["forum_id"], query["msgID"]
    if (
        len(forum_id) != 1
        or len(message_id) != 1
        or not re.fullmatch(r"[0-9]{1,32}", forum_id[0])
        or not re.fullmatch(r"[0-9]{1,32}", message_id[0])
    ):
        raise _invalid_page()
    return forum_id[0], message_id[0]


def _announcement_cards(soup: BeautifulSoup, limit: int) -> list[dict[str, Any]]:
    cards = soup.select(".featuresItem.section")
    if not cards:
        # The forum keeps its search form when a valid search finds no cards.
        if soup.select('form input[name="searchTXT"]'):
            return []
        raise _invalid_page()
    if all(card.select_one("a.showOverlay") is None for card in cards):
        if soup.select('form input[name="searchTXT"]'):
            return []
        raise _invalid_page()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        title_link = card.select_one("a.showOverlay")
        direct_links = card.find_all("a", recursive=False)
        dates = card.find_all("span", recursive=False)
        teaser = card.find("p", recursive=False)
        if title_link is None or len(direct_links) < 2 or not dates or teaser is None:
            raise _invalid_page()
        forum_id, message_id = _detail_identity(title_link.get("onclick", ""))
        item_id = f"{forum_id}:{message_id}"
        title = _text(title_link)
        date = _text(dates[0])
        category = _text(direct_links[1])
        if not item_id or not title or not date or not category or item_id in seen:
            raise _invalid_page()
        seen.add(item_id)
        items.append(
            {
                "id": item_id,
                "title": title,
                "date": date,
                "category": category,
                "teaser": _text(teaser),
                "content": "",
                "links": [],
            }
        )
        if len(items) >= limit:
            break
    return items


def _announcement_detail(soup: BeautifulSoup) -> tuple[str, list[str]]:
    cells = soup.select("td.dont-break-out")
    if len(cells) != 1:
        raise _invalid_page()
    cell = cells[0]
    content = "\n".join(part.strip() for part in cell.stripped_strings if part.strip())
    if not content:
        raise _invalid_page()
    candidates = [anchor.get("href", "") for anchor in cell.find_all("a", href=True)]
    candidates.extend(_HTTPS_RE.findall(content))
    links: list[str] = []
    for candidate in candidates:
        value = candidate.rstrip(".,;:)]}")
        parsed = urlsplit(value)
        if parsed.scheme.lower() == "https" and parsed.netloc and value not in links:
            links.append(value)
    return content, links


class KehilaNetDriver:
    """Keep one authenticated Chrome context and issue bounded read requests."""

    def __init__(self, base_url: str = _DEFAULT_BASE_URL) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("Kehila-Net base URL must be an HTTPS origin")
        self._base_url = base_url.rstrip("/")
        self._origin = (parsed.scheme, parsed.hostname, parsed.port)
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._session_key: str | None = None
        self._key_secret = os.urandom(32)

    async def search_phonebook(
        self, username: str, password: str, *, query: str, limit: int
    ) -> list[dict[str, Any]]:
        _validate_query_limit(query, limit, _MAX_CONTACTS)
        async with self._lock:
            try:
                await self._ensure_session(username, password)
                path = _PHONEBOOK_PATH + "?" + _query_string(
                    {"print": "1", "withpic": "1", "allfamily": "1", "SearchName": query}
                )
                soup = await self._get_authenticated_html(username, password, path)
                return _phonebook_contacts(soup, limit)
            except APIException:
                raise
            except Exception:
                raise _error("kehilanet_unavailable", "The member service is unavailable.") from None

    async def list_announcements(
        self, username: str, password: str, *, query: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        _validate_query_limit(query, limit, _MAX_ANNOUNCEMENTS)
        async with self._lock:
            try:
                await self._ensure_session(username, password)
                parameters = {"last": "1", "target": "1", "counterrefferer": "top_menu"}
                if query:
                    parameters["searchTXT"] = query
                soup = await self._get_authenticated_html(
                    username, password, _ANNOUNCEMENTS_PATH + "?" + _query_string(parameters)
                )
                items = _announcement_cards(soup, limit)
                for item in items:
                    forum_id, message_id = item["id"].split(":", 1)
                    detail_path = _DETAIL_PATH + "?" + urlencode(
                        {"forum_id": forum_id, "msgID": message_id}
                    )
                    item["content"], item["links"] = _announcement_detail(
                        await self._get_authenticated_html(username, password, detail_path)
                    )
                return items
            except APIException:
                raise
            except Exception:
                raise _error("kehilanet_unavailable", "The member service is unavailable.") from None

    async def _ensure_session(self, username: str, password: str) -> None:
        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
            raise APIException(503, "kehilanet_unavailable", "Member credentials are unavailable.")
        identity = f"{len(username)}:{username}{password}".encode("utf-8")
        session_key = hmac.new(self._key_secret, identity, hashlib.sha256).hexdigest()
        if self._context is not None and self._session_key == session_key:
            return
        await self._close_unlocked()
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                channel="chrome", headless=True
            )
            self._context = await self._browser.new_context()
            page = await self._context.new_page()
            try:
                await page.goto(self._base_url + "/", wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
                await page.locator('input[name="email"]').fill(username, timeout=_TIMEOUT_MS)
                await page.locator('input[name="password"]').fill(password, timeout=_TIMEOUT_MS)
                await page.locator('a[onclick="document.login.submit();"]').click(timeout=_TIMEOUT_MS)
                await page.wait_for_load_state("domcontentloaded", timeout=_TIMEOUT_MS)
            finally:
                await page.close()
            # The submitted form can briefly pass through the site's login check.
            for attempt in range(3):
                try:
                    verification = await self._get_html(
                        _PHONEBOOK_PATH + "?print=1&withpic=1&allfamily=1&SearchName="
                    )
                    if len(verification.select("table#myTable")) == 1:
                        break
                except (APIException, _SessionExpired):
                    pass
                if attempt < 2:
                    await asyncio.sleep(0.75)
            else:
                raise APIException(401, "kehilanet_auth_failed", "Member sign-in failed.")
            self._session_key = session_key
        except APIException:
            await self._close_unlocked()
            raise
        except Exception:
            await self._close_unlocked()
            raise _error("kehilanet_unavailable", "The member service is unavailable.") from None

    async def _get_authenticated_html(
        self, username: str, password: str, path: str
    ) -> BeautifulSoup:
        try:
            return await self._get_html(path)
        except _SessionExpired:
            await self._close_unlocked()
            await self._ensure_session(username, password)
            return await self._get_html(path)

    async def _get_html(self, path: str) -> BeautifulSoup:
        if self._context is None:
            raise _error("kehilanet_unavailable", "The member service is unavailable.")
        response = await self._context.request.get(
            self._base_url + path, timeout=_TIMEOUT_MS, max_redirects=3
        )
        final = urlsplit(response.url)
        if final.path.lower() == "/login.asp":
            raise _SessionExpired()
        if (
            response.status != 200
            or (final.scheme, final.hostname, final.port) != self._origin
            or "html" not in response.headers.get("content-type", "").lower()
        ):
            raise _invalid_page()
        declared_length = response.headers.get("content-length", "")
        if declared_length.isdigit() and int(declared_length) > _MAX_RESPONSE_BYTES:
            raise _invalid_page()
        body = await response.body()
        if len(body) > _MAX_RESPONSE_BYTES:
            raise _invalid_page()
        return BeautifulSoup(body, "html.parser")

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        self._session_key = None
        context, browser, playwright = self._context, self._browser, self._playwright
        self._context = self._browser = self._playwright = None
        if context is not None:
            await context.close()
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()


kehilanet_driver = KehilaNetDriver()
