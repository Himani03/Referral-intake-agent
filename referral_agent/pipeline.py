"""fax PDF -> extract -> validate -> (EMR agent | human review queue), with every step audited.

python -m referral_agent.pipeline --faxes data/faxes --extractor ocr --emr http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from .audit import AuditLog
from .browser_agent import EmrAgent, NeedsHumanReview
from .extractors import get_extractor
from .schema import Referral


def queue_for_review(review_dir: Path, fax_id: str, reason: str, raw: dict) -> None:
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / f"{fax_id}.json").write_text(json.dumps({"fax_id": fax_id, "reason": reason, "extracted": raw}, indent=2, default=str))


def process(pdf: Path, extractor, agent: EmrAgent, audit: AuditLog, review_dir: Path) -> dict:
    fax_id = pdf.stem
    t0 = time.perf_counter()
    try:
        raw = extractor.extract(pdf)
    except Exception as e:  # API error, unparseable model output: fail closed for this fax, keep going
        audit.record("extract", "failed", fax_id=fax_id, extractor=extractor.name, error=type(e).__name__)
        queue_for_review(review_dir, fax_id, f"extraction failed: {type(e).__name__}", {})
        return {"fax_id": fax_id, "status": "review", "reason": "extraction failed"}
    audit.record("extract", fax_id=fax_id, extractor=extractor.name, fields_found=len(raw),
                 ms=round((time.perf_counter() - t0) * 1000))
    try:
        ref = Referral(**raw)
    except ValidationError as e:
        bad = sorted({str(err["loc"][0]) for err in e.errors()})
        audit.record("validate", "failed", fax_id=fax_id, fields=bad)  # field names only, never values
        queue_for_review(review_dir, fax_id, f"validation failed: {', '.join(bad)}", raw)
        return {"fax_id": fax_id, "status": "review", "reason": f"invalid: {', '.join(bad)}"}
    audit.record("validate", fax_id=fax_id)
    try:
        rid = agent.submit(ref, fax_id)
    except NeedsHumanReview as e:
        audit.record("review.queued", fax_id=fax_id, reason=e.reason)
        queue_for_review(review_dir, fax_id, e.reason, raw)
        return {"fax_id": fax_id, "status": "review", "reason": e.reason}
    return {"fax_id": fax_id, "status": "submitted", "referral_id": rid,
            "seconds": round(time.perf_counter() - t0, 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--faxes", default="data/faxes")
    ap.add_argument("--extractor", default="ocr", choices=["ocr", "claude", "ollama"])
    ap.add_argument("--emr", default="http://127.0.0.1:8000")
    ap.add_argument("--audit", default="data/audit.jsonl")
    ap.add_argument("--review", default="data/review_queue")
    ap.add_argument("--headed", action="store_true", help="watch the browser work")
    a = ap.parse_args()

    audit = AuditLog(Path(a.audit), actor=f"intake-agent/{a.extractor}")
    extractor = get_extractor(a.extractor)
    results = []
    with EmrAgent(a.emr, audit, headless=not a.headed) as agent:
        for pdf in sorted(Path(a.faxes).glob("*.pdf")):
            r = process(pdf, extractor, agent, audit, Path(a.review))
            print(json.dumps(r))
            results.append(r)
    print("summary:", dict(Counter(r["status"] for r in results)))


if __name__ == "__main__":
    main()
