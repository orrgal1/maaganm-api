from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from errors import APIException
from kehilanet_driver import (
    KehilaNetDriver,
    _announcement_cards,
    _announcement_detail,
    _detail_identity,
    _phonebook_contacts,
    _SessionExpired,
)


def html(source: str) -> BeautifulSoup:
    return BeautifulSoup(source, "html.parser")


PHONEBOOK = """
<table id="myTable">
  <tr><th>Column</th><th>Photo</th><th>Name</th><th>Phones</th><th>Email</th><th>Other</th></tr>
  <tr>
    <td>1</td><td></td><td><a>Synthetic Person</a></td>
    <td><table>
      <tr><td>Mobile</td><td>050-0000000</td></tr>
      <tr><td>Home</td><td>04-0000000</td></tr>
    </table></td>
    <td><a>one@example.invalid</a><br>two@example.invalid</td><td></td>
  </tr>
  <tr>
    <td>2</td><td></td><td><a>Another Person</a></td>
    <td></td><td></td><td></td>
  </tr>
</table>
"""


ANNOUNCEMENTS = """
<form method="get"><input name="searchTXT"></form>
<div class="featuresItem section">
  <div></div>
  <a class="showOverlay" onclick="top.callMe('/forum/files/ViewMessage.asp?forum_id=123&amp;msgID=456');return false;">Fixture notice</a>
  <a href="#">Community</a>
  <span>01/01/2026</span><br><span>Author</span>
  <p>A short preview.</p>
</div>
"""


DETAIL = """
<table><tr><td class="dont-break-out">
  <div>Fixture notice</div><div>Full synthetic announcement body.</div>
  <div><a href="https://example.invalid/details">More details</a></div>
  <div>https://example.invalid/details</div>
</td></tr></table>
"""


def test_phonebook_parses_direct_rows_and_nested_phone_pairs():
    contacts = _phonebook_contacts(html(PHONEBOOK), 5)
    assert contacts == [
        {
            "name": "Synthetic Person",
            "phones": [
                {"label": "Mobile", "number": "050-0000000"},
                {"label": "Home", "number": "04-0000000"},
            ],
            "emails": ["one@example.invalid", "two@example.invalid"],
        },
        {"name": "Another Person", "phones": [], "emails": []},
    ]
    assert len(_phonebook_contacts(html(PHONEBOOK), 1)) == 1


def test_phonebook_rejects_login_and_changed_row_layout():
    with pytest.raises(APIException, match="invalid page"):
        _phonebook_contacts(html('<form><input name="password"></form>'), 10)
    with pytest.raises(APIException, match="invalid page"):
        _phonebook_contacts(html('<table id="myTable"><tr><td>Odd</td></tr></table>'), 10)
    assert _phonebook_contacts(
        html('<form><input name="SearchName"></form><table id="myTable"><tbody></tbody></table>'),
        10,
    ) == []


def test_announcements_parse_stable_id_and_detail_content():
    items = _announcement_cards(html(ANNOUNCEMENTS), 1)
    assert items == [
        {
            "id": "123:456",
            "title": "Fixture notice",
            "date": "01/01/2026",
            "category": "Community",
            "teaser": "A short preview.",
            "content": "",
            "links": [],
        }
    ]
    content, links = _announcement_detail(html(DETAIL))
    assert "Full synthetic announcement body." in content
    assert links == ["https://example.invalid/details"]


def test_empty_forum_search_is_valid_but_login_or_unsafe_detail_is_not():
    assert _announcement_cards(html('<form><input name="searchTXT"></form>'), 5) == []
    assert _announcement_cards(
        html('<form><input name="searchTXT"></form><div class="featuresItem section">No results</div>'),
        5,
    ) == []
    with pytest.raises(APIException):
        _announcement_cards(html('<form><input name="password"></form>'), 5)
    with pytest.raises(APIException):
        _detail_identity("top.callMe('https://elsewhere.invalid/a');return false;")
    with pytest.raises(APIException):
        _detail_identity("top.callMe('/forum/files/ViewMessage.asp?forum_id=123&msgID=abc');return false;")
    assert _detail_identity(
        "top.callMe('/forum/files/ViewMessage.asp?forum_id=123&msgID=456&searchTXT=fixture');return false;"
    ) == ("123", "456")


@pytest.mark.asyncio
async def test_public_reads_use_only_bounded_get_paths(monkeypatch):
    driver = KehilaNetDriver()
    monkeypatch.setattr(driver, "_ensure_session", AsyncMock())
    paths: list[str] = []

    async def fake_get_html(path: str) -> BeautifulSoup:
        paths.append(path)
        if path.startswith("/familytree/"):
            return html(PHONEBOOK)
        if path.startswith("/forum/forum/"):
            return html(ANNOUNCEMENTS)
        return html(DETAIL)

    monkeypatch.setattr(driver, "_get_html", fake_get_html)
    contacts = await driver.search_phonebook("user", "password", query="שם", limit=1)
    items = await driver.list_announcements("user", "password", query="notice", limit=1)
    assert len(contacts) == len(items) == 1
    assert len(paths) == 3
    assert parse_qs(urlsplit(paths[0]).query, encoding="cp1255")["SearchName"] == ["שם"]
    assert parse_qs(urlsplit(paths[1]).query)["searchTXT"] == ["notice"]
    assert urlsplit(paths[2]).path == "/forum/files/ViewMessage.asp"
    assert items[0]["content"]


@pytest.mark.asyncio
async def test_invalid_limits_and_unknown_pages_fail_closed(monkeypatch):
    driver = KehilaNetDriver()
    monkeypatch.setattr(driver, "_ensure_session", AsyncMock())
    monkeypatch.setattr(driver, "_get_html", AsyncMock(return_value=html("<html>login</html>")))
    with pytest.raises(APIException) as raised:
        await driver.search_phonebook("user", "password", query="", limit=1)
    assert raised.value.code == "kehilanet_invalid_response"
    with pytest.raises(APIException) as raised:
        await driver.list_announcements("user", "password", limit=21)
    assert raised.value.code == "kehilanet_invalid_input"


@pytest.mark.asyncio
async def test_expired_session_reauthenticates_once(monkeypatch):
    driver = KehilaNetDriver()
    ensure = AsyncMock()
    close = AsyncMock()
    fetch = AsyncMock(side_effect=[_SessionExpired(), html(PHONEBOOK)])
    monkeypatch.setattr(driver, "_ensure_session", ensure)
    monkeypatch.setattr(driver, "_close_unlocked", close)
    monkeypatch.setattr(driver, "_get_html", fetch)
    contacts = await driver.search_phonebook("user", "password", query="", limit=1)
    assert len(contacts) == 1
    assert ensure.await_count == 2
    assert close.await_count == 1
    assert fetch.await_count == 2
