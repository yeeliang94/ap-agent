"""The run diary: what happened, when, and why — in plain sentences.

Two things were invisible before this module existed. A stage could fail
for every single document and the run would still finish "ready", because
each failure was swallowed per document. And when a failure DID surface,
it surfaced as an exception class name, which tells a reviewer nothing.

So every notable moment is recorded once, here, and lands in two places:

  - the server log, for whoever is debugging
  - a run_events row, which the review screen shows

Recording in one call is deliberate: two separate calls drift, and the
log and the screen then disagree about what happened.

Nothing recorded here may contain a secret. Model keys never appear in
exception text, but temporary download URLs do, and those are bearer-like
— so every message and detail passes through redact_links first.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("telemetry")

# Levels, in the order a reviewer cares about them.
INFO, WARNING, ERROR = "info", "warning", "error"

_LOG_LEVELS = {INFO: logging.INFO, WARNING: logging.WARNING, ERROR: logging.ERROR}

# How much of a raw failure is worth keeping. Long enough for a stack-free
# reason, short enough that the screen stays readable and the database
# does not fill with model dumps.
MAX_DETAIL = 600

_LINK_PATTERN = re.compile(r"https?://\S+")


def redact_links(text: str) -> str:
    """Replace any URL with <link>.

    Temporary SharePoint download URLs are bearer-like: anyone holding one
    can fetch the document. They turn up inside exception text, so nothing
    that might reach a screen or a log file keeps them.
    """
    return _LINK_PATTERN.sub("<link>", text)


# --- naming a failure -------------------------------------------------------
# Status codes an HTTP-speaking dependency (the AI proxy, the SharePoint
# gateway) returns, in words a reviewer can act on. The point is that
# "401" and "429" mean completely different things to the person reading:
# one is a broken setup, the other is "wait and try again".
_STATUS_REASONS = {
    400: "the service rejected the request as malformed",
    401: ("the service did not accept our credentials — the API key is "
          "missing, expired, or not entitled to this service"),
    403: ("the credentials were recognised but are not permitted to do "
          "this — the key or account lacks access"),
    404: ("the service address is wrong — that endpoint does not exist"),
    408: "the service did not answer in time",
    413: "the request was too large for the service to accept",
    429: ("the service is rate-limiting us — too many requests too "
          "quickly, or a quota is exhausted"),
    500: "the service hit an internal error",
    502: "a gateway in front of the service returned a bad response",
    503: "the service is temporarily unavailable",
    504: "a gateway in front of the service timed out",
}

# Markers of a name-resolution failure, across platforms: Windows raises
# WSAHOST_NOT_FOUND (11001), Unix EAI_NONAME (8).
_DNS_MARKERS = ("getaddrinfo", "11001", "nodename nor servname",
                "name or service not known", "temporary failure in name resolution")

# What a REJECTED CERTIFICATE actually looks like. Deliberately NOT the
# bare word "ssl": every Python TLS error signs off with the C file and
# line that raised it — "(_ssl.c:2426)" — so matching "ssl" matches every
# TLS-layer failure there is, a cut connection included. That sent
# reviewers to IT about certificates when the gateway had merely hung up.
_CERT_MARKERS = ("certificate", "unable to get local issuer", "unknown ca",
                 "self-signed", "self signed", "hostname mismatch")

# A connection that opened and then broke part-way through, as opposed to
# one that never opened at all. Kept apart from the unreachable case
# because the advice differs: this one is usually worth simply retrying.
# Matched on class names as well as text, since httpx raises ReadError
# with an empty message when the far end just goes quiet.
_CUT_CLASSES = ("ReadError", "WriteError", "ProtocolError",
                "IncompleteRead", "ChunkedEncoding")
_CUT_MARKERS = ("reset by peer", "server disconnected", "eof occurred",
                "connection broken", "connection aborted")

_NO_DNS = ("the server name could not be looked up — this usually means "
           "the VPN or corporate network is not connected")
_BAD_CERT = ("the server's security certificate was rejected — the "
             "corporate certificate may not be trusted by this app")
_TOO_SLOW = "the service did not answer in time"
_REFUSED = "the server refused the connection"
_UNREACHABLE = ("the server could not be reached — check the address, the "
                "VPN, and whether a proxy is required")
_STREAM_CUT = ("the connection to the service broke part-way through — "
               "usually a temporary network or gateway problem, so the run "
               "is worth trying again")

# Every reason above is a DEPENDENCY failing, never the document. Kept as
# a set so callers can tell the two apart: telling a reviewer that a
# corrupt scan is "a problem with the system" sends them to IT instead of
# to the file, and telling them a dead VPN is a bad scan wastes an
# afternoon on a perfectly good invoice.
SERVICE_REASONS = frozenset(
    list(_STATUS_REASONS.values())
    + [_NO_DNS, _BAD_CERT, _TOO_SLOW, _REFUSED, _UNREACHABLE, _STREAM_CUT])


def is_service_failure(reason: str) -> bool:
    """Did a dependency fail, rather than the document itself?

    Takes the sentence rather than the exception because the pipeline
    stores the sentence: doc.error is what the checking stage has to work
    from, long after the exception is gone.
    """
    return reason.strip().rstrip(".") in {r.rstrip(".") for r in SERVICE_REASONS}


def is_transient(exc: BaseException) -> bool:
    """Is this worth simply trying again?

    True only for a connection that broke part-way through. A refusal, a
    dead name, or a rejected certificate will fail identically on the
    next attempt, and retrying those just makes a person wait longer for
    the same answer.
    """
    return describe_failure(exc) == _STREAM_CUT


def _status_of(exc: Exception) -> int | None:
    """The HTTP status behind an exception, however the library exposes it."""
    for attribute in ("status_code", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and 100 <= value < 600:
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _related(exc: BaseException, depth: int = 0, seen: set | None = None) -> list:
    """This exception and every one hiding inside it, outermost first.

    The MCP SDK runs its transport inside an anyio task group, so a DNS
    miss or a rejected certificate reaches us wrapped in an
    ExceptionGroup. Describing only the outer one produces the useless
    word "ExceptionGroup" — which is the same failure this whole module
    exists to stop.
    """
    seen = seen if seen is not None else set()
    if depth > 6 or id(exc) in seen:
        return []
    seen.add(id(exc))
    out = [exc]
    for inner in getattr(exc, "exceptions", None) or []:   # ExceptionGroup
        out += _related(inner, depth + 1, seen)
    for chained in (exc.__cause__, exc.__context__):       # "raise X from Y"
        if chained is not None:
            out += _related(chained, depth + 1, seen)
    return out


def _describe_one(exc: BaseException) -> str:
    """A specific reason for ONE exception, or "" if it says nothing useful."""
    status = _status_of(exc)
    if status is not None:
        return _STATUS_REASONS.get(
            status, f"the service answered with an unexpected HTTP {status}")

    text = str(exc).lower()
    # Matched on the class NAME so this covers httpx and httpx2 alike
    # without importing either.
    name = type(exc).__name__
    if any(marker in text for marker in _DNS_MARKERS):
        return _NO_DNS
    # "certificate" in the class name covers ssl.SSLCertVerificationError
    # and ssl.CertificateError, which carry the detail in their args.
    if (any(marker in text for marker in _CERT_MARKERS)
            or "certificate" in name.lower()):
        return _BAD_CERT
    if "Timeout" in name or "timed out" in text:
        return _TOO_SLOW
    if "refus" in text:
        return _REFUSED
    if "ConnectError" in name or "ConnectionError" in name:
        return _UNREACHABLE
    # Last, so that a failure which never got a connection up keeps its
    # more specific reason above rather than being called a broken stream.
    if (any(cls in name for cls in _CUT_CLASSES)
            or any(marker in text for marker in _CUT_MARKERS)):
        return _STREAM_CUT
    return ""


def describe_failure(exc: BaseException) -> str:
    """A short sentence naming what went wrong, safe to show a reviewer.

    Deliberately does NOT return the exception text: that is written for
    an engineer, often quotes a URL, and is the reason failed runs used to
    read "ConnectError" and leave everyone guessing. The raw text still
    goes to the log, via record()'s detail.
    """
    for candidate in _related(exc):
        described = _describe_one(candidate)
        if described:
            return described
    # Nothing recognisable. Name the innermost exception rather than the
    # wrapper: "ExceptionGroup" tells nobody anything, and the class it
    # wrapped at least names the subsystem that failed.
    specific = [e for e in _related(exc) if not getattr(e, "exceptions", None)]
    return type(specific[-1] if specific else exc).__name__


def record(db, run_id: str, stage: str, level: str, code: str, message: str,
           detail: str = "", document_id: str = "") -> None:
    """Write one moment of a run's life to the log AND to its diary.

    Never raises: telemetry that can fail a run is worse than no
    telemetry. A diary entry that cannot be saved is logged and dropped.
    """
    from sqlalchemy.orm import Session

    from .models import RunEvent

    message = redact_links(message)[:MAX_DETAIL]
    detail = redact_links(detail)[:MAX_DETAIL]
    log.log(_LOG_LEVELS.get(level, logging.INFO),
            "run %s [%s/%s] %s%s", run_id, stage, code, message,
            f" -- {detail}" if detail else "")
    try:
        # A SEPARATE session on the caller's engine, and this matters more
        # than it looks. Writing through the pipeline's own session would
        # commit whatever business data it happens to have pending, and —
        # far worse — rolling back on failure would DISCARD that pending
        # data. A diary entry that could not be saved would then silently
        # throw away a run's finished outputs. Telemetry observes the run;
        # it must never be able to change it.
        with Session(db.get_bind()) as own:
            own.add(RunEvent(run_id=run_id, stage=stage, level=level, code=code,
                             message=message, detail=detail,
                             document_id=document_id))
            own.commit()
    except Exception:
        log.exception("could not save run event for %s", run_id)


def record_failure(db, run_id: str, stage: str, code: str, what: str,
                   exc: Exception, document_id: str = "") -> str:
    """Record an exception as a named reason. Returns that reason.

    Callers use the return value for the field a person actually reads —
    doc.error or run.error — so the screen and the diary say the same
    thing rather than one saying "ReadError" and the other explaining it.
    """
    reason = describe_failure(exc)
    record(db, run_id, stage, ERROR, code, f"{what}: {reason}",
           detail=f"{type(exc).__name__}: {exc}", document_id=document_id)
    return reason
