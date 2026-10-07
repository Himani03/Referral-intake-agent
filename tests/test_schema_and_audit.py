import json
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from referral_agent.audit import AuditLog, pseudonym, verify
from referral_agent.schema import Referral, npi_is_valid, npi_with_check_digit

GOOD = dict(
    patient_first_name="Maria", patient_last_name="Alvarez", patient_dob="1961-04-09",
    patient_phone="555.201.3344", insurance_payer="Aetna PPO", insurance_member_id="AX12345678",
    referring_provider="Dr. Linda Park", referring_npi="1234567893", specialty="Cardiology",
    icd10_codes="I48.91, R07.9", reason="New onset atrial fibrillation", urgency="URGENT",
)


def test_valid_referral_normalizes():
    r = Referral(**GOOD)
    assert r.patient_phone == "(555) 201-3344"
    assert r.icd10_codes == ["I48.91", "R07.9"]
    assert r.urgency.value == "urgent"


def test_npi_check_digit():
    assert npi_is_valid("1234567893")  # CMS published example
    assert not npi_is_valid("1234567890")
    assert npi_is_valid(npi_with_check_digit("123456789"))


@pytest.mark.parametrize("field,value", [
    ("referring_npi", "1234567890"),          # one digit off: OCR misread
    ("icd10_codes", "RO7.9"),                 # letter O instead of zero
    ("patient_phone", "555-201-334"),         # dropped digit
    ("patient_dob", (date.today() + timedelta(days=1)).isoformat()),
    ("urgency", "asap"),
])
def test_validation_catches_ocr_style_errors(field, value):
    with pytest.raises(ValidationError):
        Referral(**{**GOOD, field: value})


@pytest.fixture
def audit(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-key")
    return AuditLog(tmp_path / "audit.jsonl", actor="test")


def test_audit_chain_verifies(audit):
    for i in range(5):
        audit.record("emr.referral.submit", subject=pseudonym("Alvarez|1961-04-09"), n=i)
    ok, msg = verify(audit.path)
    assert ok, msg


def test_audit_detects_edit(audit):
    for i in range(3):
        audit.record("step", n=i)
    lines = audit.path.read_text().splitlines()
    e = json.loads(lines[1]); e["outcome"] = "failed"
    lines[1] = json.dumps(e, sort_keys=True)
    audit.path.write_text("\n".join(lines) + "\n")
    ok, msg = verify(audit.path)
    assert not ok and "line 2" in msg


def test_audit_detects_deleted_line(audit):
    for i in range(3):
        audit.record("step", n=i)
    lines = audit.path.read_text().splitlines()
    audit.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    ok, msg = verify(audit.path)
    assert not ok and "chain broken" in msg


def test_audit_never_contains_phi(audit):
    audit.record("emr.patient.create", subject=pseudonym("Alvarez|1961-04-09"))
    text = audit.path.read_text()
    assert "Alvarez" not in text and "1961" not in text
    assert pseudonym("alvarez|1961-04-09 ") == pseudonym("Alvarez|1961-04-09")  # stable lookup
