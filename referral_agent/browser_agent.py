"""Playwright agent that enters a validated referral into the EMR the way a front-desk user would.

Safety choices worth talking through in an interview:
- It never guesses a patient. Zero matches -> register a new patient. Exactly one match with the same
  first name -> use it. Anything else (several matches, first name differs) -> stop and send to a human.
  Wrong-patient errors are the worst thing this agent could do.
- Submits are idempotent. The fax ID is stored on the referral, and before every retry the agent looks
  it up first. That covers the case where the EMR saved the record but returned an error.
- Credentials come from the environment only and are never logged. The audit log gets pseudonyms, not PHI.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from .audit import AuditLog, pseudonym
from .schema import Referral


class NeedsHumanReview(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class EmrAgent:
    def __init__(self, base_url: str, audit: AuditLog, headless: bool = True, max_attempts: int = 3,
                 failure_dir: Path = Path("data/failures")):
        self.base = base_url.rstrip("/")
        self.audit = audit
        self.headless = headless
        self.max_attempts = max_attempts
        self.failure_dir = failure_dir

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.page: Page = self._browser.new_page()
        self.page.set_default_timeout(10_000)
        self.login()
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()

    def login(self) -> None:
        user, pw = os.environ.get("EMR_USER"), os.environ.get("EMR_PASSWORD")
        if not user or not pw:
            raise RuntimeError("Set EMR_USER and EMR_PASSWORD in the environment.")
        p = self.page
        p.goto(f"{self.base}/login")
        p.get_by_label("Username").fill(user)
        p.get_by_label("Password").fill(pw)
        p.get_by_role("button", name="Sign in").click()
        if p.get_by_role("alert").count():
            self.audit.record("emr.login", "failed")
            raise RuntimeError("EMR login failed")
        self.audit.record("emr.login")

    # --- lookups -------------------------------------------------------------
    def existing_referral(self, fax_id: str) -> int | None:
        self.page.goto(f"{self.base}/referrals?source_fax={fax_id}")
        found = self.page.locator("#found")
        return int(found.get_attribute("data-referral-id")) if found.count() else None

    def find_or_create_patient(self, ref: Referral) -> int:
        p, subject = self.page, pseudonym(f"{ref.patient_last_name}|{ref.patient_dob}")
        p.goto(f"{self.base}/patients")
        p.get_by_label("Last name").fill(ref.patient_last_name)
        p.get_by_label("Date of birth").fill(ref.patient_dob.isoformat())
        p.get_by_role("button", name="Search").click()
        rows = p.locator("tr[data-patient-id]")
        n = rows.count()
        if n == 0:
            pid = self._register(ref)
            self.audit.record("emr.patient.create", subject=subject, patient_id=pid)
            return pid
        if n > 1:
            self.audit.record("emr.patient.match", "ambiguous", subject=subject, candidates=n)
            raise NeedsHumanReview(f"{n} patients share last name and DOB")
        name = rows.first.locator("td").first.inner_text().split()[0]
        if name.lower() != ref.patient_first_name.lower():
            self.audit.record("emr.patient.match", "mismatch", subject=subject)
            raise NeedsHumanReview("patient found by last name and DOB but first name differs")
        pid = int(rows.first.get_attribute("data-patient-id"))
        self.audit.record("emr.patient.match", subject=subject, patient_id=pid)
        return pid

    def _register(self, ref: Referral) -> int:
        p = self.page
        p.goto(f"{self.base}/patients/new")
        p.get_by_label("First name").fill(ref.patient_first_name)
        p.get_by_label("Last name").fill(ref.patient_last_name)
        p.get_by_label("Date of birth").fill(ref.patient_dob.isoformat())
        p.get_by_label("Phone").fill(ref.patient_phone)
        p.get_by_label("Insurance payer").fill(ref.insurance_payer)
        p.get_by_label("Member ID").fill(ref.insurance_member_id)
        p.get_by_role("button", name="Register").click()
        p.wait_for_url("**/referrals/new")
        return int(p.url.split("/patients/")[1].split("/")[0])

    # --- submit --------------------------------------------------------------
    def _fill_and_submit(self, pid: int, ref: Referral, fax_id: str) -> int | None:
        p = self.page
        p.goto(f"{self.base}/patients/{pid}/referrals/new")
        options = p.locator("#specialty option").all_inner_texts()
        if ref.specialty not in options:
            raise NeedsHumanReview(f"specialty '{ref.specialty}' not offered by this EMR")
        p.get_by_label("Specialty").select_option(ref.specialty)
        p.get_by_label("Diagnosis codes").fill(", ".join(ref.icd10_codes))
        p.get_by_label("Reason for referral").fill(ref.reason)
        p.get_by_label(ref.urgency.value.capitalize(), exact=True).check()
        p.get_by_label("Referring provider").fill(ref.referring_provider)
        p.get_by_label("Referring NPI").fill(ref.referring_npi)
        p.get_by_label("Source fax ID").fill(fax_id)
        with p.expect_navigation() as nav:
            p.get_by_role("button", name="Submit referral").click()
        resp = nav.value
        if resp is None or resp.status >= 500:
            return None
        conf = p.locator("#confirmation")
        return int(conf.get_attribute("data-referral-id")) if conf.count() else None

    def submit(self, ref: Referral, fax_id: str) -> int:
        existing = self.existing_referral(fax_id)
        if existing:
            self.audit.record("emr.referral.submit", "skipped_duplicate", fax_id=fax_id, referral_id=existing)
            return existing
        pid = self.find_or_create_patient(ref)
        for attempt in range(1, self.max_attempts + 1):
            try:
                rid = self._fill_and_submit(pid, ref, fax_id)
            except NeedsHumanReview:
                raise
            except Exception as e:  # timeouts, navigation errors
                self.audit.record("emr.referral.submit", "error", fax_id=fax_id, attempt=attempt, error=type(e).__name__)
                rid = None
            if rid:
                self.audit.record("emr.referral.submit", fax_id=fax_id, attempt=attempt, referral_id=rid, patient_id=pid)
                return rid
            # unknown outcome: the EMR may have saved it anyway, so check before trying again
            existing = self.existing_referral(fax_id)
            if existing:
                self.audit.record("emr.referral.submit", "recovered", fax_id=fax_id, attempt=attempt, referral_id=existing)
                return existing
            self.audit.record("emr.referral.submit", "retrying", fax_id=fax_id, attempt=attempt)
            time.sleep(0.5 * 2 ** (attempt - 1))
        self._capture_failure(fax_id)
        raise NeedsHumanReview(f"submit failed after {self.max_attempts} attempts")

    def _capture_failure(self, fax_id: str) -> None:
        # Screenshots contain PHI: they stay inside data/ (gitignored, encrypted disk in real use).
        self.failure_dir.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(self.failure_dir / f"{fax_id}.png"))
