"""Drives a real browser against the mock EMR, including a flaky EMR that saves records and then returns 503."""
import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from referral_agent.audit import AuditLog, verify
from referral_agent.browser_agent import EmrAgent
from referral_agent.faxgen import generate
from referral_agent.pipeline import process


class TruthExtractor:
    """Reads the ground-truth JSON so these tests isolate the agent from OCR quality."""
    name = "truth"

    def extract(self, pdf: Path) -> dict:
        return json.loads(pdf.with_suffix(".truth.json").read_text())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    os.environ.update(EMR_DB=str(tmp / "emr.sqlite"), EMR_USER="frontdesk", EMR_PASSWORD="test-pass",
                      AUDIT_HMAC_KEY="test-key", EMR_FLAKY="0.5")
    from mock_emr.app import app  # import after env is set

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    faxes = generate(tmp / "faxes", n=6, seed=3, noise=0.3)
    yield {"tmp": tmp, "url": f"http://127.0.0.1:{port}", "faxes": faxes}
    server.should_exit = True


def referrals(env) -> list[tuple]:
    con = sqlite3.connect(env["tmp"] / "emr.sqlite")
    rows = con.execute("SELECT source_fax, specialty, icd10, npi FROM referrals").fetchall()
    con.close()
    return rows


def test_flaky_emr_never_creates_duplicates(env):
    audit = AuditLog(env["tmp"] / "audit.jsonl", actor="test")
    with EmrAgent(env["url"], audit) as agent:
        results = [process(p, TruthExtractor(), agent, audit, env["tmp"] / "review") for p in env["faxes"]]
    assert all(r["status"] == "submitted" for r in results), results
    rows = referrals(env)
    assert len(rows) == len(env["faxes"])  # one referral per fax, even with 503s after saving
    for pdf in env["faxes"]:
        truth = json.loads(pdf.with_suffix(".truth.json").read_text())
        row = next(r for r in rows if r[0] == pdf.stem)
        assert row[1] == truth["specialty"] and row[3] == truth["referring_npi"]
    assert verify(audit.path)[0]


def test_rerun_is_idempotent(env):
    audit = AuditLog(env["tmp"] / "audit.jsonl", actor="test")
    before = len(referrals(env))
    with EmrAgent(env["url"], audit) as agent:
        for p in env["faxes"]:
            process(p, TruthExtractor(), agent, audit, env["tmp"] / "review")
    assert len(referrals(env)) == before
    assert "skipped_duplicate" in audit.path.read_text()


def test_ambiguous_patient_goes_to_human(env):
    """Same last name + DOB as an existing patient but a different first name: never guess."""
    src = env["faxes"][0]
    truth = json.loads(src.with_suffix(".truth.json").read_text())
    imposter = env["tmp"] / "faxes2"
    imposter.mkdir(exist_ok=True)
    pdf = imposter / "FAX-IMPOSTER.pdf"
    pdf.write_bytes(src.read_bytes())
    pdf.with_suffix(".truth.json").write_text(json.dumps({**truth, "patient_first_name": "Zed"}))
    audit = AuditLog(env["tmp"] / "audit.jsonl", actor="test")
    with EmrAgent(env["url"], audit) as agent:
        r = process(pdf, TruthExtractor(), agent, audit, env["tmp"] / "review")
    assert r["status"] == "review" and "first name differs" in r["reason"]
    assert (env["tmp"] / "review" / "FAX-IMPOSTER.json").exists()


def test_invalid_extraction_never_reaches_emr(env):
    class BadOcr(TruthExtractor):
        def extract(self, pdf):
            return {**super().extract(pdf), "referring_npi": "1234567890"}

    audit = AuditLog(env["tmp"] / "audit.jsonl", actor="test")
    before = len(referrals(env))
    pdf = env["tmp"] / "faxes2" / "FAX-BADNPI.pdf"
    pdf.write_bytes(env["faxes"][1].read_bytes())
    pdf.with_suffix(".truth.json").write_text(env["faxes"][1].with_suffix(".truth.json").read_text())
    with EmrAgent(env["url"], audit) as agent:
        r = process(pdf, BadOcr(), agent, audit, env["tmp"] / "review")
    assert r["status"] == "review" and "referring_npi" in r["reason"]
    assert len(referrals(env)) == before
    assert "1234567890" not in audit.path.read_text()
