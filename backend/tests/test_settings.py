"""Regression tests for the on-screen settings (client name + SharePoint
folder URL). Deterministic — no AI, no network."""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import app
from app import routes, settings_store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path/'test.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(routes, "SessionLocal", TestSession)
    monkeypatch.setattr(settings_store, "SessionLocal", TestSession)
    yield TestSession


client = TestClient(app)


def test_defaults_come_from_env_until_first_save(db):
    s = client.get("/api/settings").json()
    assert s["client_name"] == settings_store.DEFAULTS["client_name"]


def test_save_and_reload_roundtrip(db):
    r = client.put("/api/settings", json={
        "client_name": "Client XYZ",
        "sharepoint_folder_url": "https://corp.sharepoint.com/sites/xyz/Shared%20Documents/AP",
    })
    assert r.status_code == 200
    s = client.get("/api/settings").json()
    assert s["client_name"] == "Client XYZ"
    assert s["sharepoint_folder_url"].endswith("/AP")


def test_validation_rejects_bad_values(db):
    assert client.put("/api/settings", json={
        "client_name": "  ", "sharepoint_folder_url": "https://x.sharepoint.com/a",
    }).status_code == 400
    assert client.put("/api/settings", json={
        "client_name": "Client XYZ", "sharepoint_folder_url": "ftp://not-web",
    }).status_code == 400
    assert client.put("/api/settings", json={
        "client_name": "Client XYZ", "sharepoint_folder_url": None,
    }).status_code == 400


def test_upload_gate_follows_saved_client_name(db):
    client.put("/api/settings", json={
        "client_name": "Client XYZ",
        "sharepoint_folder_url": "https://corp.sharepoint.com/sites/xyz/AP",
    })
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("invoice.pdf", b"%PDF-fake")
    r = client.post("/api/runs",
                    data={"client": "Client ABC"},
                    files={"batch": ("b.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
    assert "Client XYZ" in r.json()["detail"]


def test_switches_list_names_every_switch_with_its_default(db):
    got = client.get("/api/settings/switches").json()
    by_key = {s["key"]: s for s in got["switches"]}
    assert {"claims_sharepoint_source", "claims_case_model", "claims_full_dump_grouping",
            "claims_agentic_investigation", "claims_shadow_investigation"} <= set(by_key)
    for s in by_key.values():
        assert s["label"] and s["description"] and isinstance(s["value"], bool)
    # Case model defaults on (env default "1"); the rest default off.
    assert by_key["claims_case_model"]["value"] is True
    assert by_key["claims_full_dump_grouping"]["value"] is False
    # Deployment facts are read-only and never carry a secret's value.
    assert any(d["label"] for d in got["deployment"])
    assert all(set(d) == {"label", "value"} for d in got["deployment"])


def test_switch_flip_is_saved_audited_and_refuses_unknown_keys(db):
    from app.models import AuditEvent

    r = client.put("/api/settings/switches", json={"claims_full_dump_grouping": True})
    assert r.status_code == 200
    by_key = {s["key"]: s for s in r.json()["switches"]}
    assert by_key["claims_full_dump_grouping"]["value"] is True
    from app import switches
    assert switches.on("claims_full_dump_grouping") is True
    s = db()
    events = s.query(AuditEvent).filter(AuditEvent.action == "settings_changed").all()
    s.close()
    assert len(events) == 1 and "claims_full_dump_grouping" in events[0].detail
    assert client.put("/api/settings/switches", json={"not_a_switch": True}).status_code == 400
    assert client.put("/api/settings/switches",
                      json={"claims_case_model": "maybe"}).status_code == 400


def test_a_run_keeps_the_switches_it_started_with(db):
    """A mid-flight run never changes rules: behavior reads the run's
    snapshot; the live setting only steers runs created after the flip."""
    from app import switches

    client.put("/api/settings/switches", json={"claims_shadow_investigation": True})
    snap = {"switches": switches.snapshot()}
    client.put("/api/settings/switches", json={"claims_shadow_investigation": False})
    assert switches.for_run(snap, "claims_shadow_investigation") is True
    assert switches.for_run({}, "claims_shadow_investigation") is False  # old run: live value
    assert switches.on("claims_shadow_investigation") is False


def test_settings_change_is_audited_with_before_and_after(db):
    from app.models import AuditEvent

    client.put("/api/settings", json={
        "client_name": "Client XYZ",
        "sharepoint_folder_url": "https://corp.sharepoint.com/sites/xyz/AP",
    })
    s = db()
    events = s.query(AuditEvent).filter(AuditEvent.action == "settings_changed").all()
    s.close()
    assert len(events) == 1
    assert "client_name" in events[0].detail and "Client XYZ" in events[0].detail
