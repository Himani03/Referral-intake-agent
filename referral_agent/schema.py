"""Referral schema. Everything an extractor returns must pass through here before an agent touches the EMR."""
from __future__ import annotations

import re
from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, field_validator

ICD10_RE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
PHONE_RE = re.compile(r"^\(\d{3}\) \d{3}-\d{4}$")


class Urgency(str, Enum):
    routine = "routine"
    urgent = "urgent"


def npi_is_valid(npi: str) -> bool:
    """NPI check digit uses Luhn over '80840' + first 9 digits."""
    if not re.fullmatch(r"\d{10}", npi):
        return False
    digits = [int(c) for c in "80840" + npi[:9]]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(npi[9])


def npi_with_check_digit(first9: str) -> str:
    for c in "0123456789":
        if npi_is_valid(first9 + c):
            return first9 + c
    raise ValueError("unreachable")


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return raw
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


class Referral(BaseModel):
    patient_first_name: str = Field(min_length=1)
    patient_last_name: str = Field(min_length=1)
    patient_dob: date
    patient_phone: str
    insurance_payer: str = Field(min_length=2)
    insurance_member_id: str = Field(min_length=4)
    referring_provider: str = Field(min_length=3)
    referring_npi: str
    specialty: str = Field(min_length=3)
    icd10_codes: list[str] = Field(min_length=1)
    reason: str = Field(min_length=5)
    urgency: Urgency

    @field_validator("patient_first_name", "patient_last_name", "insurance_payer", "referring_provider", "specialty", "reason")
    @classmethod
    def strip(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("patient_dob")
    @classmethod
    def dob_in_past(cls, v: date) -> date:
        if v >= date.today() or v.year < 1900:
            raise ValueError("DOB out of range")
        return v

    @field_validator("patient_phone", mode="before")
    @classmethod
    def phone(cls, v: str) -> str:
        v = normalize_phone(str(v))
        if not PHONE_RE.match(v):
            raise ValueError("phone must have 10 digits")
        return v

    @field_validator("referring_npi", mode="before")
    @classmethod
    def npi(cls, v: str) -> str:
        v = re.sub(r"\D", "", str(v))
        if not npi_is_valid(v):
            raise ValueError("NPI fails check digit")
        return v

    @field_validator("icd10_codes", mode="before")
    @classmethod
    def icd(cls, v) -> list[str]:
        if isinstance(v, str):
            v = re.split(r"[,;\s]+", v)
        codes = [c.strip().upper() for c in v if c and c.strip()]
        bad = [c for c in codes if not ICD10_RE.match(c)]
        if bad:
            raise ValueError(f"invalid ICD-10 codes: {bad}")
        return codes

    @field_validator("urgency", mode="before")
    @classmethod
    def urg(cls, v: str) -> str:
        return str(v).strip().lower()


FIELDS = list(Referral.model_fields.keys())
