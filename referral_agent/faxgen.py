"""Generate SYNTHETIC referral faxes (no real patients) plus ground-truth JSON for evaluation.

Faxes are rendered as degraded images inside a PDF with no text layer, so an extractor
has to actually read pixels, like a real fax.
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .schema import npi_with_check_digit

FIRST = ["Maria", "James", "Priya", "Wei", "Aisha", "Carlos", "Emily", "Daniel", "Fatima", "Hiro", "Grace", "Omar"]
LAST = ["Alvarez", "Nguyen", "Patel", "Johnson", "Okafor", "Kim", "Rossi", "Haddad", "Chen", "Brooks", "Silva", "Tanaka"]
PAYERS = ["Blue Shield of CA", "Aetna PPO", "Medicare Part B", "UnitedHealthcare", "Kaiser Permanente", "Cigna HMO"]
PROVIDERS = ["Dr. Linda Park", "Dr. Samuel Reyes", "Dr. Anita Desai", "Dr. Mark Feldman", "Dr. Joy Mensah"]
CASES = {
    "Ophthalmology": [("H40.11", "Suspected primary open-angle glaucoma, elevated IOP"), ("E11.3293", "Diabetic retinopathy follow-up"), ("H25.13", "Bilateral cataracts, worsening night vision")],
    "Cardiology": [("I48.91", "New onset atrial fibrillation"), ("R07.9", "Chest pain on exertion, abnormal ECG"), ("I10", "Uncontrolled hypertension despite two agents")],
    "Gastroenterology": [("K21.9", "Chronic reflux not responding to PPI"), ("R19.5", "Positive FIT test, needs colonoscopy"), ("K50.90", "Suspected Crohn's disease")],
}


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    mac = "/System/Library/Fonts/Supplemental/" + ("Arial Bold.ttf" if bold else "Arial.ttf")
    for p in [f"/usr/share/fonts/truetype/dejavu/{name}", name, mac]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size)  # without a size this is a ~10px font and the fax is unreadable


def make_record(rng: random.Random) -> dict:
    specialty = rng.choice(list(CASES))
    picks = rng.sample(CASES[specialty], k=rng.choice([1, 1, 2]))
    dob = date(1940, 1, 1) + timedelta(days=rng.randint(0, 365 * 62))
    return {
        "patient_first_name": rng.choice(FIRST),
        "patient_last_name": rng.choice(LAST),
        "patient_dob": dob.isoformat(),
        "patient_phone": f"({rng.randint(200, 989)}) {rng.randint(200, 989)}-{rng.randint(1000, 9999)}",
        "insurance_payer": rng.choice(PAYERS),
        "insurance_member_id": f"{rng.choice('ABCXYZ')}{rng.choice('ABCXYZ')}{rng.randint(10000000, 99999999)}",
        "referring_provider": rng.choice(PROVIDERS),
        "referring_npi": npi_with_check_digit(str(rng.randint(100000000, 199999999))),
        "specialty": specialty,
        "icd10_codes": [c for c, _ in picks],
        "reason": "; ".join(r for _, r in picks),
        "urgency": rng.choice(["routine", "routine", "urgent"]),
    }


def render(rec: dict, rng: random.Random, noise: float) -> Image.Image:
    W, H = 1700, 2200  # 200 dpi letter
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    f, fb, fh = _font(34), _font(34, True), _font(54, True)
    y = 110
    d.text((120, y), "Bayview Family Medicine", font=fh, fill=0); y += 75
    d.text((120, y), "FAX: (555) 010-4400   SYNTHETIC TEST DOCUMENT", font=f, fill=60); y += 90
    d.text((120, y), "SPECIALTY REFERRAL REQUEST", font=fh, fill=0); y += 110
    dob = date.fromisoformat(rec["patient_dob"]).strftime("%m/%d/%Y")
    rows = [
        ("Patient Name:", f"{rec['patient_first_name']} {rec['patient_last_name']}"),
        ("Date of Birth:", dob),
        ("Phone:", rec["patient_phone"]),
        ("Insurance:", rec["insurance_payer"]),
        ("Member ID:", rec["insurance_member_id"]),
        ("Referring Provider:", rec["referring_provider"]),
        ("NPI:", rec["referring_npi"]),
        ("Refer To:", rec["specialty"]),
        ("Diagnosis (ICD-10):", ", ".join(rec["icd10_codes"])),
        ("Urgency:", rec["urgency"].upper()),
    ]
    for label, val in rows:
        d.text((120, y), label, font=fb, fill=0)
        d.text((560, y), val, font=f, fill=0)
        y += 70
    y += 30
    d.text((120, y), "Reason for Referral:", font=fb, fill=0); y += 60
    words, line = rec["reason"].split(), ""
    for w in words:
        if len(line) + len(w) > 70:
            d.text((160, y), line, font=f, fill=0); y += 55; line = ""
        line += w + " "
    d.text((160, y), line, font=f, fill=0)
    d.text((120, H - 220), "Signature: ____________________     Date: ___________", font=f, fill=0)
    d.text((120, H - 140), "CONFIDENTIAL: contains synthetic PHI for testing only", font=f, fill=80)

    # fax degradation: slight skew, blur, speckle, threshold
    img = img.rotate(rng.uniform(-1.2, 1.2) * noise, fillcolor=255, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.6 * noise))
    px = img.load()
    for _ in range(int(9000 * noise)):
        x, yy = rng.randrange(W), rng.randrange(H)
        px[x, yy] = 0 if rng.random() < 0.5 else 255
    img = img.point(lambda p: 0 if p < 150 else 255)
    return img


def generate(out_dir: Path, n: int, seed: int = 7, noise: float = 1.0) -> list[Path]:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        rec = make_record(rng)
        fax_id = f"FAX-{seed:02d}-{i:04d}"
        img = render(rec, rng, noise)
        pdf = out_dir / f"{fax_id}.pdf"
        img.save(pdf, "PDF", resolution=200)
        (out_dir / f"{fax_id}.truth.json").write_text(json.dumps(rec, indent=2))
        paths.append(pdf)
    return paths


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/faxes")
    ap.add_argument("-n", type=int, default=25)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--noise", type=float, default=1.0)
    a = ap.parse_args()
    for p in generate(Path(a.out), a.n, a.seed, a.noise):
        print(p)
