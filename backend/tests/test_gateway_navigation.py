"""Finding a SharePoint folder on a server with no resolve-folder tool.

The enterprise gateway does not accept a folder URL. It offers the steps
a person takes by hand — get the site, list its libraries, list the items
— so the app has to read the address the reviewer pasted and walk them.

That path used to run for the first time on the enterprise gateway itself,
over a VPN, where the only feedback was a failed run. These tests run it
against a fake with the same shape and the same awkwardnesses.
"""
from __future__ import annotations

import pytest

from app.docsource import (
    RealMcpSource,
    SourceUnavailable,
    _match_library,
    parse_sharepoint_folder_url,
)

# What a reviewer actually copies out of the address bar.
PLAIN = ("https://contoso.sharepoint.com/sites/ClientABC/"
         "Shared%20Documents/AP%20Reference")
VIEW_PAGE = ("https://contoso.sharepoint.com/sites/ClientABC/Shared%20Documents/"
             "Forms/AllItems.aspx?id=%2Fsites%2FClientABC%2FShared%20Documents"
             "%2FAP%20Reference&viewid=7f3a1b2c")


@pytest.fixture()
def gateway_source(gateway_url, monkeypatch):
    monkeypatch.setenv("MCP_URL", gateway_url)
    for var in ("MCP_AUTH_HEADER", "MCP_AUTH_VALUE", "MCP_TOOL_RESOLVE_FOLDER",
                "MCP_TOOL_LIST_ITEMS", "MCP_TOOL_GET_DOCUMENT"):
        monkeypatch.delenv(var, raising=False)
    return lambda url=PLAIN: RealMcpSource(url)


# --- reading the pasted address --------------------------------------------

def test_a_plain_folder_address_is_split_into_its_parts():
    address = parse_sharepoint_folder_url(PLAIN)
    assert address["site_path"] == "/sites/ClientABC"
    assert address.site_url == "https://contoso.sharepoint.com/sites/ClientABC"
    # The browser says "Shared Documents"; the API calls it "Documents".
    assert address["browser_library"] == "Shared Documents"
    assert address["library"] == "Documents"
    assert address["folder_path"] == "AP Reference"


def test_the_address_of_a_folder_you_clicked_into_is_understood():
    """Once you click a folder, SharePoint shows a Forms/AllItems.aspx
    address whose visible path stops at the library — the real folder is
    hidden in the "id" query parameter. Reading the visible path would
    silently list the library root instead of the folder."""
    assert parse_sharepoint_folder_url(VIEW_PAGE) == \
        parse_sharepoint_folder_url(PLAIN)


def test_teams_sites_work_the_same_way():
    address = parse_sharepoint_folder_url(
        "https://contoso.sharepoint.com/teams/Finance/Documents/AP")
    assert address["site_path"] == "/teams/Finance"
    assert address["library"] == "Documents"
    assert address["folder_path"] == "AP"


def test_a_library_root_with_no_subfolder_is_allowed():
    address = parse_sharepoint_folder_url(
        "https://contoso.sharepoint.com/sites/ClientABC/Shared Documents")
    assert address["library"] == "Documents"
    assert address["folder_path"] == ""


@pytest.mark.parametrize("bad,expected", [
    ("", "not a full web address"),
    ("sites/ClientABC/Documents", "not a full web address"),
    ("https://contoso.sharepoint.com/", "does not include a site"),
    ("https://contoso.sharepoint.com/sites/ClientABC", "does not name a document library"),
    ("https://contoso.sharepoint.com/sites/X/Shared Documents/Forms/AllItems.aspx",
     "points at a list view"),
])
def test_an_unusable_address_says_what_to_copy_instead(bad, expected):
    """A misread address means listing the WRONG folder, and checking this
    month's invoices against the wrong listing is worse than stopping."""
    with pytest.raises(SourceUnavailable) as exc:
        parse_sharepoint_folder_url(bad)
    assert expected in str(exc.value)


def test_the_library_is_matched_by_either_of_its_two_names():
    libraries = {"value": [{"id": "b!assets", "name": "Site Assets"},
                           {"id": "b!docs", "name": "Documents"}]}
    assert _match_library(libraries, "Documents", "Shared Documents")["id"] == "b!docs"
    assert _match_library(libraries, "Nope", "Nope") is None


# --- walking the gateway ----------------------------------------------------

def test_the_folder_is_found_without_any_resolve_tool(gateway_source):
    """The whole point: this server has no resolve-folder tool at all."""
    names = gateway_source().list_names()
    assert names, "expected the reference folder to list"
    assert all(isinstance(n, str) for n in names)


def test_a_document_downloads_end_to_end_through_the_gateway(gateway_source):
    from fake_mcp import gateway_server

    source = gateway_source()
    names = source.list_names()
    data = source.get_reference(names[0])
    assert data == (gateway_server.REFERENCE_DIR / names[0]).read_bytes()


def test_the_view_page_address_reaches_the_same_folder(gateway_source):
    assert gateway_source(VIEW_PAGE).list_names() == gateway_source(PLAIN).list_names()


def test_several_tools_mentioning_site_do_not_confuse_the_lookup(gateway_source):
    """This gateway offers sp_get_sharepoint_site, sp_search_site_content
    and sp_get_site_permissions. Matching on the bare word "site" cannot
    tell them apart — the real gateway produced exactly this ambiguity,
    and the fix was asking for the specific spelling first."""
    names = gateway_source().list_names()  # must not raise "3 tools match"
    assert names


def test_every_page_of_a_long_folder_is_read(gateway_source):
    """The gateway paginates both list tools with skip_token. Reading only
    the first page looks exactly like a short folder — nothing complains,
    the run just never sees some of the files. That could mean missing the
    payment listing, or matching an older one that happened to fit on
    page one. The fake serves ONE file per page to force the issue."""
    from fake_mcp import gateway_server

    on_disk = sorted(f.name for f in gateway_server.REFERENCE_DIR.iterdir()
                     if f.is_file())
    assert len(on_disk) > 1, "this test needs more than one sample file"
    assert gateway_source().list_names() == on_disk


def test_the_library_list_is_read_from_this_gateways_own_envelope(gateway_source):
    """It answers under "libraries", not the "value" Graph-shaped servers
    use. Not recognising that read as "this site has no document
    libraries" — a wrong answer that looks like a real one, and it left
    the next step with no drive id."""
    from app.docsource import _match_library, _unwrap_items

    payload = gateway_server_libraries()
    assert len(_unwrap_items(payload)) == 2
    assert _match_library(payload, "Documents", "Shared Documents")["id"]


def gateway_server_libraries():
    from fake_mcp import gateway_server

    return gateway_server.list_document_libraries(
        site_id=gateway_server.SITE_ID)


def test_an_unknown_envelope_name_still_yields_its_one_list():
    """The next gateway will invent another name. One list and no
    ambiguity is enough to go on."""
    from app.docsource import _unwrap_items

    assert _unwrap_items({"somethingNew": [{"name": "a"}]}) == [{"name": "a"}]
    # ...but two lists ARE ambiguous, so nothing is guessed.
    assert _unwrap_items({"a": [{"x": 1}], "b": [{"y": 2}]}) == []


def test_a_missing_drive_id_names_the_step_that_should_have_supplied_it(
        gateway_source, monkeypatch):
    """list_library_items REQUIRES drive_id. Letting the call go anyway
    returns "'drive_id' is a required property", which names the symptom
    and not the step that failed to produce it.

    The library must MATCH and still carry no id — returning no match at
    all stops one branch earlier, at "no such library", and never reaches
    this check.
    """
    from app import docsource

    monkeypatch.setattr(docsource, "_match_library",
                        lambda *a, **k: {"name": "Documents"})  # matched, but no id
    with pytest.raises(SourceUnavailable) as exc:
        gateway_source().list_names()
    message = str(exc.value)
    assert "drive_id" in message                      # what is missing
    assert "document library lookup" in message       # ...and which step owed it


def test_an_unexpected_required_argument_is_not_blamed_on_the_library(
        gateway_source, monkeypatch):
    """The library wording is only right for a library id. Any other
    missing argument needs its own explanation, or it sends the reviewer
    to check a SharePoint folder that was never the problem."""
    from app import mcp_client

    monkeypatch.setattr(mcp_client.McpSession, "required_arguments",
                        lambda self, tool: {"tenant_id"})
    with pytest.raises(SourceUnavailable) as exc:
        gateway_source().list_names()
    message = str(exc.value)
    assert "tenant_id" in message
    assert "document library lookup" not in message
    assert "MCP_TOOL_LIST_ITEMS" in message           # ...and what to try


# --- paging that goes wrong -------------------------------------------------

class _StubSession:
    """A server that answers with whatever pages it was handed."""

    def __init__(self, pages, accepted=None):
        self.pages, self.accepted, self.calls = pages, accepted, []

    def accepted_arguments(self, tool):
        return self.accepted

    async def call(self, tool, arguments):
        self.calls.append(dict(arguments))
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]


def _walk(session):
    import asyncio

    from app.docsource import _all_pages

    return asyncio.run(_all_pages(session, "list_items", {"drive_id": "d"},
                                  "list the folder"))


def test_a_repeated_continuation_token_stops_the_run(gateway_source):
    """A server that keeps handing back the same token is handing back the
    same page. Treating that as "finished" returns a duplicated, truncated
    folder and calls it complete — which is the one outcome paging exists
    to prevent."""
    stuck = {"items": [{"name": "a.xlsx", "id": "1"}], "skip_token": "SAME"}
    with pytest.raises(SourceUnavailable) as exc:
        _walk(_StubSession([stuck], accepted={"drive_id", "skip_token"}))
    assert "same continuation token twice" in str(exc.value)


def test_the_next_page_is_asked_for_under_the_name_this_tool_accepts():
    """Servers need not answer with the name they ask for, and arguments a
    tool does not declare are dropped before sending — so asking under the
    wrong name re-requests page one politely, forever."""
    pages = [{"items": [{"name": "a.xlsx", "id": "1"}], "nextSkipToken": "P2"},
             {"items": [{"name": "b.xlsx", "id": "2"}]}]
    session = _StubSession(pages, accepted={"drive_id", "continuation_token"})
    assert len(_walk(session)) == 2
    # Answered with nextSkipToken; asked with continuation_token.
    assert session.calls[1]["continuation_token"] == "P2"
    assert "nextSkipToken" not in session.calls[1]


def test_more_pages_with_no_way_to_ask_for_them_is_a_failure():
    """Not a short folder — an unreadable one."""
    pages = [{"items": [{"name": "a.xlsx", "id": "1"}], "skip_token": "P2"}]
    with pytest.raises(SourceUnavailable) as exc:
        _walk(_StubSession(pages, accepted={"drive_id"}))  # no cursor argument
    assert "no argument to ask for the next page" in str(exc.value)


def test_a_diagnostic_list_is_never_mistaken_for_the_folder_contents():
    """Turning error text into "file names" would produce a folder full of
    documents that do not exist."""
    from app.docsource import _unwrap_items

    assert _unwrap_items({"errors": ["access denied"]}) == []
    assert _unwrap_items({"warnings": [{"code": "x"}]}) == []
    # A bare list of strings under an unknown key is messages, not files.
    assert _unwrap_items({"somethingNew": ["not a file record"]}) == []
    # ...but real entries under an unknown key are still read.
    assert _unwrap_items({"somethingNew": [{"name": "a.xlsx"}]}) == [{"name": "a.xlsx"}]


def test_a_folder_that_is_not_there_says_so_plainly(gateway_source):
    source = gateway_source(
        "https://contoso.sharepoint.com/sites/ClientABC/Shared Documents/Nope")
    with pytest.raises(SourceUnavailable) as exc:
        source.list_names()
    assert "itemNotFound" in str(exc.value)


def test_a_wrong_library_name_lists_what_the_site_actually_has(gateway_source):
    source = gateway_source(
        "https://contoso.sharepoint.com/sites/ClientABC/Invoices/AP Reference")
    with pytest.raises(SourceUnavailable) as exc:
        source.list_names()
    message = str(exc.value)
    assert "no document library called 'Invoices'" in message
    assert "Documents" in message  # ...and what it does have
