"""The run diary: naming failures, hiding secrets, and counting patterns.

The bug that made this module necessary: the AI proxy rejected every
request with HTTP 401, each document's failure was caught individually,
and the run still reached "ready". Nobody saw a 401 anywhere. So these
tests care about three things — that a failure gets a name a reviewer can
act on, that recording one can never itself break a run, and that "all of
them failed" is reported as one event rather than N unrelated ones.
"""
from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import routes, telemetry
from app.db import Base
from app.main import app
from app.models import Document, Run, RunEvent
from app.pipeline import runner


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'test.sqlite3'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(routes, "SessionLocal", TestSession)
    s = TestSession()
    s.add(Run(id="r1", client="Client ABC", status="ready"))
    s.commit()
    yield TestSession
    s.close()


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://proxy.example.com/chat/completions")
    return httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(status, request=request))


# --- naming the failure -----------------------------------------------------

def test_a_401_is_reported_as_a_credential_problem():
    """The failure this module exists for. "HTTPStatusError" sent everyone
    looking at the documents; "the service did not accept our credentials"
    sends them to the key."""
    reason = telemetry.describe_failure(_http_error(401))
    assert "credentials" in reason
    assert "key" in reason


def test_statuses_that_mean_different_things_read_differently():
    rate_limited = telemetry.describe_failure(_http_error(429))
    forbidden = telemetry.describe_failure(_http_error(403))
    missing = telemetry.describe_failure(_http_error(404))
    assert "rate-limiting" in rate_limited      # wait and retry
    assert "not permitted" in forbidden          # the key lacks access
    assert "address is wrong" in missing         # the setup is wrong
    assert len({rate_limited, forbidden, missing}) == 3


def test_a_status_on_the_exception_itself_is_found():
    """Not every client wraps a response object; some set status_code."""

    class ProviderError(Exception):
        status_code = 401

    assert "credentials" in telemetry.describe_failure(ProviderError("nope"))


def test_network_failures_are_translated_for_a_non_engineer():
    dns = telemetry.describe_failure(
        httpx.ConnectError("[Errno 11001] getaddrinfo failed"))
    tls = telemetry.describe_failure(RuntimeError("certificate verify failed"))
    assert "VPN" in dns
    assert "certificate" in tls


def test_a_cut_connection_is_not_reported_as_a_bad_certificate():
    """The regression this test exists for: every Python TLS error signs
    its message with "(_ssl.c:NNNN)", so a classifier matching the bare
    word "ssl" called a dropped stream a rejected certificate — and sent
    reviewers to IT over a certificate that was never in question."""
    cut = telemetry.describe_failure(
        httpx.ReadError("EOF occurred in violation of protocol (_ssl.c:2426)"))
    assert "certificate" not in cut
    assert "trying again" in cut


def test_a_real_rejected_certificate_still_says_so():
    """The other half of the same fix: tightening the match must not stop
    an actual trust failure from being named."""
    for exc in (
        httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate "
                           "verify failed: unable to get local issuer "
                           "certificate (_ssl.c:1006)"),
        ssl.SSLCertVerificationError("unable to get local issuer certificate"),
    ):
        assert "certificate" in telemetry.describe_failure(exc)


def test_a_broken_stream_never_reaches_a_reviewer_as_jargon():
    """A gateway hanging up mid-reply arrives as ReadError with no useful
    message at all, or as RemoteProtocolError. Both used to be shown to
    the reviewer as those very words."""
    for exc in (httpx.ReadError(""),
                httpx.ReadError("[Errno 54] Connection reset by peer"),
                httpx.RemoteProtocolError("Server disconnected without "
                                          "sending a response.")):
        reason = telemetry.describe_failure(exc)
        assert type(exc).__name__ not in reason
        assert "trying again" in reason


def test_a_broken_stream_is_blamed_on_the_system_not_the_document():
    """The costly half of the bug. checks.py asks is_service_failure to
    decide whether to say "a problem with the system" or to point at the
    document. An unregistered reason quietly answers "the document", and
    a reviewer rescans a perfectly good invoice over a gateway hiccup."""
    for exc in (httpx.ReadError(""),
                httpx.ReadError("EOF occurred in violation of protocol (_ssl.c:2426)"),
                httpx.RemoteProtocolError("Server disconnected")):
        assert telemetry.is_service_failure(telemetry.describe_failure(exc))


def test_a_handshake_failure_is_not_dressed_up_as_a_certificate_problem():
    """A protocol-version mismatch is a TLS error but not a trust error.
    It should fall through to the general unreachable advice."""
    reason = telemetry.describe_failure(
        httpx.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version "
                           "number (_ssl.c:1000)"))
    assert "certificate" not in reason
    assert telemetry.is_service_failure(reason)


def test_an_unrecognisable_failure_falls_back_to_its_type():
    assert telemetry.describe_failure(ValueError("boom")) == "ValueError"


def test_a_failure_buried_in_an_exception_group_is_still_named():
    """The MCP SDK runs its transport in an anyio task group, so every
    connection failure arrives wrapped. Describing only the wrapper gave
    the reviewer the word "ExceptionGroup" — the exact uselessness this
    module was written to remove."""
    buried = ExceptionGroup("unhandled errors in a TaskGroup", [
        ExceptionGroup("inner", [
            httpx.ConnectError("[Errno 8] nodename nor servname provided"),
        ]),
    ])
    assert "VPN" in telemetry.describe_failure(buried)


def test_a_failure_behind_a_raise_from_chain_is_still_named():
    try:
        try:
            raise _http_error(401)
        except Exception as inner:
            raise RuntimeError("the sign-in step failed") from inner
    except RuntimeError as exc:
        assert "credentials" in telemetry.describe_failure(exc)


def test_an_unrecognisable_group_names_its_contents_not_the_wrapper():
    group = ExceptionGroup("unhandled errors in a TaskGroup",
                           [ValueError("something odd")])
    assert telemetry.describe_failure(group) == "ValueError"


# --- keeping secrets out ----------------------------------------------------

def test_temporary_download_urls_never_reach_the_diary(db):
    """Download links are bearer-like: whoever holds one can fetch the
    document. They appear inside exception text, so nothing recorded keeps
    them — not the message, not the technical detail."""
    session = db()
    telemetry.record(
        db=session, run_id="r1", stage="check", level=telemetry.ERROR,
        code="X", message="failed fetching https://sp.example.com/d?token=SECRET",
        detail="GET https://sp.example.com/d?token=SECRET -> 403")
    event = session.query(RunEvent).one()
    assert "SECRET" not in event.message and "SECRET" not in event.detail
    assert "<link>" in event.message and "<link>" in event.detail
    session.close()


def test_long_detail_is_truncated_rather_than_stored_whole(db):
    session = db()
    telemetry.record(db=session, run_id="r1", stage="sort",
                     level=telemetry.INFO, code="X", message="m",
                     detail="y" * 5000)
    assert len(session.query(RunEvent).one().detail) == telemetry.MAX_DETAIL
    session.close()


# --- telemetry must never be the thing that breaks a run --------------------

def test_a_diary_write_that_fails_does_not_raise(caplog):
    """Telemetry that can fail a run is worse than no telemetry."""

    class BrokenSession:
        def get_bind(self):
            raise RuntimeError("database is locked")

    telemetry.record(BrokenSession(), "r1", "sort", telemetry.ERROR,
                     "X", "message")  # must not raise
    assert "could not save run event" in caplog.text


def test_a_failed_diary_write_cannot_destroy_the_run_it_was_describing(db, caplog):
    """The nastiest shape of "telemetry must not change the run".

    Writing the diary through the pipeline's own session meant a failed
    insert rolled that session back — throwing away whatever business
    data was pending. A run's finished outputs would silently become {}
    while the run still went on to report itself ready.
    """
    session = db()
    run = session.get(Run, "r1")
    run.outputs = {"listing_rows": ["a row the reviewer needs"]}  # pending, uncommitted

    class UnwritableEvent:
        """A bind that accepts a session but refuses the insert."""

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            raise RuntimeError("database is locked")

    telemetry.record(_SessionWithBind(session, UnwritableEvent(None)), "r1",
                     "output", telemetry.INFO, "STAGE_DONE", "built")

    assert "could not save run event" in caplog.text
    session.commit()
    assert session.get(Run, "r1").outputs == {"listing_rows": ["a row the reviewer needs"]}
    session.close()


class _SessionWithBind:
    """The caller's session, but handing out a bind that cannot be used."""

    def __init__(self, real, bind):
        self._real, self._bind = real, bind

    def get_bind(self):
        return self._bind


def test_the_diary_does_not_commit_the_callers_half_finished_work(db):
    """Telemetry used to commit the pipeline's session as a side effect,
    publishing whatever happened to be pending at that moment."""
    session = db()
    run = session.get(Run, "r1")
    run.status = "half-way"          # pending, deliberately not committed
    telemetry.record(session, "r1", "sort", telemetry.INFO, "X", "message")

    other = db()                     # a different session sees only committed data
    assert other.get(Run, "r1").status == "ready"
    assert other.query(RunEvent).count() == 1   # ...but the event IS saved
    other.close()
    session.close()


def test_record_failure_returns_the_same_reason_it_stores(db):
    """doc.error and the diary must not disagree about what happened."""
    session = db()
    reason = telemetry.record_failure(session, "r1", "sort", "DOCUMENT_FAILED",
                                      "Could not sort invoice_01.pdf",
                                      _http_error(401), document_id="docA")
    event = session.query(RunEvent).one()
    assert reason in event.message
    assert event.document_id == "docA"
    assert "HTTPStatusError" in event.detail  # the engineer's version survives
    session.close()


# --- turning N small failures back into one diagnosable one -----------------

def test_every_document_failing_the_same_way_is_one_loud_event(db):
    session = db()
    reason = telemetry.describe_failure(_http_error(401))
    halt = runner._record_stage_end(session, "r1", "sort", 22, [reason] * 22, 0.0)
    event = session.query(RunEvent).one()
    assert event.level == telemetry.ERROR
    assert event.code == "ALL_DOCUMENTS_FAILED"
    assert "EVERY document (22)" in event.message
    # The point of the whole exercise: say where to look.
    assert "not a problem with the uploaded files" in event.message
    # ...and the stage asks for the run to be abandoned.
    assert "credentials" in halt
    session.close()


def test_a_partial_failure_is_a_warning_and_does_not_stop_the_run(db):
    session = db()
    halt = runner._record_stage_end(session, "r1", "extract", 10, ["blurry"] * 3, 0.0)
    event = session.query(RunEvent).one()
    assert event.level == telemetry.WARNING
    assert "3 of 10" in event.message
    assert "other 7 continued" in event.message
    assert halt == ""  # some documents worked, so the batch is still worth finishing
    session.close()


def test_a_clean_stage_records_that_it_was_clean(db):
    session = db()
    assert runner._record_stage_end(session, "r1", "sort", 5, [], 0.0) == ""
    event = session.query(RunEvent).one()
    assert event.level == telemetry.INFO
    assert event.code == "STAGE_DONE"
    session.close()


def test_a_stage_with_nothing_to_do_neither_records_nor_halts(db):
    """A batch of pure receipts has nothing to extract. That is normal."""
    session = db()
    assert runner._record_stage_end(session, "r1", "extract", 0, [], 0.0) == ""
    assert session.query(RunEvent).count() == 0
    session.close()


def test_a_missing_reference_file_is_a_warning_not_silence(db):
    """No policy sheet means the spending-cap check simply does not
    happen. That degradation was invisible; now it is stated."""
    session = db()
    runner._record_reference_files(
        session, "r1",
        {"payment_listing": "FY2026 Payment Listing.xlsx",
         "policy_sheet": None, "bank_template": None}, 0.0)
    warnings = session.query(RunEvent).filter(RunEvent.level == "warning").all()
    assert {w.code for w in warnings} == {"REFERENCE_MISSING"}
    assert len(warnings) == 2
    assert any("policy sheet" in w.message for w in warnings)
    session.close()


# --- abandoning a run that cannot succeed -----------------------------------

async def _listing_ready(*_args, **_kwargs):
    """Keep sort-stage tests off the unrelated listing-model path."""
    return []


def test_a_run_whose_every_document_fails_sorting_stops_there(db, monkeypatch):
    """The 401 case, end to end.

    Before, every document failed individually, each failure was absorbed,
    and the run reached "ready" — so the AI was paid to extract and check
    documents nobody had managed to look at. Now the run ends at the stage
    that failed, with the reason on it.
    """
    session = db()
    for i in range(3):
        session.add(Document(id=f"d{i}", run_id="r1", filename=f"inv_{i}.pdf",
                             fields={}, confidence={}, corrections={}))
    session.commit()
    session.close()

    monkeypatch.setattr(runner, "SessionLocal", db)
    monkeypatch.setattr(runner.reference, "snapshot_references",
                        lambda *a, **k: {"payment_listing": "listing.xlsx",
                                         "policy_sheet": None, "bank_template": None})
    monkeypatch.setattr(runner.reference, "load_listing_notes", _listing_ready)
    monkeypatch.setattr(runner, "document_to_pngs", lambda *a, **k: [b"png"])

    async def always_401(*a, **k):
        raise _http_error(401)

    monkeypatch.setattr(runner, "sort_document", always_401)

    # The stages that must never be reached — and never paid for.
    async def must_not_run(*a, **k):
        raise AssertionError("a later stage ran after every document failed")

    monkeypatch.setattr(runner, "extract_all", must_not_run)
    monkeypatch.setattr(runner, "run_checks", must_not_run)

    asyncio.run(runner.process_run("r1", Path("/nonexistent")))

    check = db()
    run = check.get(Run, "r1")
    assert run.status == "failed"
    assert "credentials" in run.error
    assert "Every one of the 3 document(s) failed the sort stage" in run.error
    codes = [e.code for e in check.query(RunEvent).all()]
    assert "ALL_DOCUMENTS_FAILED" in codes
    assert "RUN_HALTED" in codes
    check.close()


def test_a_run_where_only_some_documents_fail_still_finishes(db, monkeypatch):
    """The tolerance that made the bug invisible is still correct behaviour
    when it is genuinely one bad file — so it must survive the fix."""
    session = db()
    for i in range(3):
        session.add(Document(id=f"d{i}", run_id="r1", filename=f"inv_{i}.pdf",
                             fields={}, confidence={}, corrections={}))
    session.commit()
    session.close()

    monkeypatch.setattr(runner, "SessionLocal", db)
    monkeypatch.setattr(runner.reference, "snapshot_references",
                        lambda *a, **k: {"payment_listing": "listing.xlsx",
                                         "policy_sheet": None, "bank_template": None})
    monkeypatch.setattr(runner.reference, "load_listing_notes", _listing_ready)
    monkeypatch.setattr(runner, "document_to_pngs", lambda *a, **k: [b"png"])

    async def fails_only_the_first(path, png):
        if str(path).endswith("inv_0.pdf"):
            raise ValueError("corrupt scan")
        return SimpleNamespace(kind="invoice")

    async def no_op_extract(docs, workspace, on_progress):
        for d in docs:
            if d.kind in ("invoice", "claim"):
                d.status = "extracted"
                on_progress()

    async def no_flags(*a, **k):
        return []

    async def empty_outputs(*a, **k):
        return {}

    monkeypatch.setattr(runner, "sort_document", fails_only_the_first)
    monkeypatch.setattr(runner, "extract_all", no_op_extract)
    monkeypatch.setattr(runner, "run_checks", no_flags)
    monkeypatch.setattr(runner.output, "build_outputs", empty_outputs)

    asyncio.run(runner.process_run("r1", Path("/nonexistent")))

    check = db()
    assert check.get(Run, "r1").status == "ready"
    codes = [e.code for e in check.query(RunEvent).all()]
    assert "SOME_DOCUMENTS_FAILED" in codes  # said out loud, but not fatal
    assert "RUN_HALTED" not in codes
    check.close()


# --- the API the screen reads -----------------------------------------------

client = TestClient(app)


def test_the_events_endpoint_can_return_only_the_problems(db):
    session = db()
    telemetry.record(session, "r1", "sort", telemetry.INFO, "STAGE_DONE", "fine")
    telemetry.record(session, "r1", "sort", telemetry.WARNING, "SOME", "hmm")
    telemetry.record(session, "r1", "check", telemetry.ERROR, "BAD", "broken")
    session.close()

    assert len(client.get("/api/runs/r1/events").json()) == 3
    problems = client.get("/api/runs/r1/events?level=problems").json()
    assert [e["level"] for e in problems] == ["warning", "error"]
    assert client.get("/api/runs/nope/events").status_code == 404


def test_a_dependency_failure_and_a_bad_file_are_told_apart():
    """checks.py words the reviewer's flag from this. Calling a corrupt
    scan "a problem with the system" sends them to IT over a file they
    could simply rescan; calling a dead VPN a bad scan wastes an
    afternoon on a perfectly good invoice."""
    assert telemetry.is_service_failure(telemetry.describe_failure(_http_error(401)))
    assert telemetry.is_service_failure(
        telemetry.describe_failure(httpx.ConnectError("getaddrinfo failed")))
    # A corrupt PDF is not a service failure.
    assert not telemetry.is_service_failure(
        telemetry.describe_failure(ValueError("cannot open broken document")))
    assert not telemetry.is_service_failure("")


# --- requests from other websites -------------------------------------------

def test_a_write_from_another_website_is_refused(db):
    """CORS stops a page READING our answers; it does not stop it SENDING
    a request. Any page a reviewer visits can post an ordinary form at
    127.0.0.1 and the browser delivers it."""
    r = client.post("/api/sharepoint/disconnect",
                    headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403
    assert "another website" in r.json()["detail"]


def test_the_apps_own_page_is_not_refused(db):
    r = client.post("/api/sharepoint/disconnect",
                    headers={"Origin": "http://testserver"})
    assert r.status_code != 403


def test_the_dev_server_origin_is_still_allowed(db):
    r = client.post("/api/sharepoint/disconnect",
                    headers={"Origin": "http://localhost:5173"})
    assert r.status_code != 403


def test_reading_is_never_blocked_by_origin(db):
    """A GET changes nothing, and blocking it would break the app loading."""
    assert client.get("/api/runs",
                      headers={"Origin": "https://evil.example.com"}).status_code == 200


def test_every_run_summary_carries_its_problem_counts(db):
    """So a run that reached "ready" WITH errors cannot be read as clean
    on any screen, without fetching the diary itself."""
    session = db()
    telemetry.record(session, "r1", "sort", telemetry.ERROR, "BAD", "broken")
    telemetry.record(session, "r1", "sort", telemetry.WARNING, "MEH", "hmm")
    telemetry.record(session, "r1", "sort", telemetry.INFO, "OK", "fine")
    session.close()

    summary = client.get("/api/runs/r1").json()
    assert summary["status"] == "ready"
    assert (summary["errors"], summary["warnings"]) == (1, 1)
