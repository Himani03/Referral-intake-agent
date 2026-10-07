"""A deliberately plain mock EMR that only exists so the browser agent has something real to drive.

No API: the agent has to log in, search, and fill HTML forms like a front-desk user would.
Set EMR_FLAKY=0.3 to make 30% of referral submits save the record and then return a 503,
the nastiest failure mode for an agent (it looks failed but actually succeeded).
"""
from __future__ import annotations

import html
import os
import random
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import Cookie, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

DB = Path(os.environ.get("EMR_DB", "data/mock_emr.sqlite"))
SPECIALTIES = ["Cardiology", "Gastroenterology", "Ophthalmology", "Dermatology"]
SESSIONS: dict[str, str] = {}

app = FastAPI(title="Mock EMR")


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY, first TEXT, last TEXT, dob TEXT, phone TEXT,
            payer TEXT, member_id TEXT);
        CREATE TABLE IF NOT EXISTS referrals(id INTEGER PRIMARY KEY, patient_id INTEGER, specialty TEXT, icd10 TEXT,
            reason TEXT, urgency TEXT, provider TEXT, npi TEXT, source_fax TEXT UNIQUE, created_by TEXT);
        CREATE TABLE IF NOT EXISTS access_log(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, user TEXT,
            action TEXT, record TEXT);
        """
    )
    return con


def log(user: str, action: str, record: str = "") -> None:
    with closing(db()) as con, con:
        con.execute("INSERT INTO access_log(user, action, record) VALUES (?,?,?)", (user, action, record))


def page(title: str, body: str, user: str | None = None) -> HTMLResponse:
    nav = f'<nav>Signed in as <b>{html.escape(user)}</b> | <a href="/patients">Patients</a></nav><hr>' if user else ""
    return HTMLResponse(f"""<!doctype html><html><head><title>{title} - Mock EMR</title>
<style>body{{font-family:sans-serif;max-width:760px;margin:24px auto}} label{{display:block;margin-top:10px}}
input,select,textarea{{width:100%;padding:6px}} table{{width:100%;border-collapse:collapse}} td,th{{border:1px solid #ccc;padding:6px}}
.ok{{background:#e6f4ea;padding:10px}} .err{{background:#fde8e8;padding:10px}}</style></head>
<body>{nav}<h1>{title}</h1>{body}</body></html>""")


def current_user(sid: str | None) -> str | None:
    return SESSIONS.get(sid or "")


@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/login", 303)


@app.get("/login", response_class=HTMLResponse)
def login_form(error: str = ""):
    err = '<p class="err" role="alert">Invalid credentials</p>' if error else ""
    return page("Sign in", f"""{err}<form method="post" action="/login">
<label for="username">Username</label><input id="username" name="username" autocomplete="username">
<label for="password">Password</label><input id="password" name="password" type="password">
<button type="submit">Sign in</button></form>""")


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    expected = (os.environ.get("EMR_USER", "frontdesk"), os.environ.get("EMR_PASSWORD", "change-me"))
    if not (secrets.compare_digest(username, expected[0]) and secrets.compare_digest(password, expected[1])):
        log(username, "login.failed")
        return RedirectResponse("/login?error=1", 303)
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = username
    log(username, "login")
    r = RedirectResponse("/patients", 303)
    r.set_cookie("sid", sid, httponly=True, samesite="strict")
    return r


@app.get("/patients", response_class=HTMLResponse)
def patients(last: str = "", dob: str = "", sid: str | None = Cookie(None)):
    user = current_user(sid)
    if not user:
        return RedirectResponse("/login", 303)
    rows = ""
    if last:
        with closing(db()) as con:
            res = con.execute("SELECT * FROM patients WHERE lower(last)=lower(?) AND (?='' OR dob=?)", (last, dob, dob)).fetchall()
        log(user, "patient.search", f"{len(res)} results")
        rows = "".join(
            f'<tr data-patient-id="{p["id"]}"><td>{html.escape(p["first"])} {html.escape(p["last"])}</td><td>{p["dob"]}</td>'
            f'<td><a href="/patients/{p["id"]}/referrals/new">New referral</a></td></tr>' for p in res
        ) or '<tr><td colspan="3" id="no-results">No matching patients</td></tr>'
        rows = f"<table><tr><th>Name</th><th>DOB</th><th></th></tr>{rows}</table>"
    return page("Patients", f"""<form method="get" action="/patients">
<label for="last">Last name</label><input id="last" name="last" value="{html.escape(last)}">
<label for="dob">Date of birth</label><input id="dob" name="dob" type="date" value="{html.escape(dob)}">
<button type="submit">Search</button></form><p><a href="/patients/new">Register new patient</a></p>{rows}""", user)


@app.get("/patients/new", response_class=HTMLResponse)
def new_patient_form(sid: str | None = Cookie(None)):
    user = current_user(sid)
    if not user:
        return RedirectResponse("/login", 303)
    return page("Register patient", """<form method="post" action="/patients">
<label for="first">First name</label><input id="first" name="first" required>
<label for="last">Last name</label><input id="last" name="last" required>
<label for="dob">Date of birth</label><input id="dob" name="dob" type="date" required>
<label for="phone">Phone</label><input id="phone" name="phone" required>
<label for="payer">Insurance payer</label><input id="payer" name="payer" required>
<label for="member_id">Member ID</label><input id="member_id" name="member_id" required>
<button type="submit">Register</button></form>""", user)


@app.post("/patients")
def create_patient(first: str = Form(...), last: str = Form(...), dob: str = Form(...), phone: str = Form(...),
                   payer: str = Form(...), member_id: str = Form(...), sid: str | None = Cookie(None)):
    user = current_user(sid)
    if not user:
        return RedirectResponse("/login", 303)
    with closing(db()) as con, con:
        cur = con.execute("INSERT INTO patients(first,last,dob,phone,payer,member_id) VALUES (?,?,?,?,?,?)",
                          (first, last, dob, phone, payer, member_id))
        pid = cur.lastrowid
    log(user, "patient.create", f"patient:{pid}")
    return RedirectResponse(f"/patients/{pid}/referrals/new", 303)


@app.get("/patients/{pid}/referrals/new", response_class=HTMLResponse)
def new_referral_form(pid: int, sid: str | None = Cookie(None)):
    user = current_user(sid)
    if not user:
        return RedirectResponse("/login", 303)
    opts = "".join(f"<option>{s}</option>" for s in SPECIALTIES)
    return page("New referral", f"""<form method="post" action="/patients/{pid}/referrals">
<label for="specialty">Specialty</label><select id="specialty" name="specialty"><option value="">Select...</option>{opts}</select>
<label for="icd10">Diagnosis codes (ICD-10, comma separated)</label><input id="icd10" name="icd10" required>
<label for="reason">Reason for referral</label><textarea id="reason" name="reason" required></textarea>
<fieldset><legend>Urgency</legend>
<label><input type="radio" name="urgency" value="routine"> Routine</label>
<label><input type="radio" name="urgency" value="urgent"> Urgent</label></fieldset>
<label for="provider">Referring provider</label><input id="provider" name="provider" required>
<label for="npi">Referring NPI</label><input id="npi" name="npi" required>
<label for="source_fax">Source fax ID</label><input id="source_fax" name="source_fax" required>
<button type="submit">Submit referral</button></form>""", user)


@app.post("/patients/{pid}/referrals")
def create_referral(pid: int, request: Request, specialty: str = Form(...), icd10: str = Form(...), reason: str = Form(...),
                    urgency: str = Form(...), provider: str = Form(...), npi: str = Form(...), source_fax: str = Form(...),
                    sid: str | None = Cookie(None)):
    user = current_user(sid)
    if not user:
        return RedirectResponse("/login", 303)
    with closing(db()) as con, con:
        try:
            cur = con.execute(
                "INSERT INTO referrals(patient_id,specialty,icd10,reason,urgency,provider,npi,source_fax,created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, specialty, icd10, reason, urgency, provider, npi, source_fax, user))
            rid = cur.lastrowid
        except sqlite3.IntegrityError:
            return page("Duplicate", f'<p class="err" role="alert">A referral from fax {html.escape(source_fax)} already exists.</p>', user)
    log(user, "referral.create", f"referral:{rid}")
    if random.random() < float(os.environ.get("EMR_FLAKY", "0")):
        return HTMLResponse("<h1>503 Service Unavailable</h1>", status_code=503)
    return RedirectResponse(f"/referrals/{rid}", 303)


@app.get("/referrals", response_class=HTMLResponse)
def find_referral(source_fax: str = "", sid: str | None = Cookie(None)):
    user = current_user(sid)
    if not user:
        return RedirectResponse("/login", 303)
    with closing(db()) as con:
        r = con.execute("SELECT id FROM referrals WHERE source_fax=?", (source_fax,)).fetchone()
    body = f'<p id="found" data-referral-id="{r["id"]}">Referral #{r["id"]}</p>' if r else '<p id="not-found">None</p>'
    return page("Referral lookup", body, user)


@app.get("/referrals/{rid}", response_class=HTMLResponse)
def show_referral(rid: int, sid: str | None = Cookie(None)):
    user = current_user(sid)
    if not user:
        return RedirectResponse("/login", 303)
    with closing(db()) as con:
        r = con.execute("SELECT * FROM referrals WHERE id=?", (rid,)).fetchone()
    log(user, "referral.view", f"referral:{rid}")
    return page("Referral created", f'<p class="ok" id="confirmation" data-referral-id="{rid}">Referral #{rid} created for '
                f'{html.escape(r["specialty"])} ({html.escape(r["urgency"])}).</p>', user)
