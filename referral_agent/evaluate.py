"""Score an extractor against the synthetic ground truth.

The number that matters most is not average field accuracy. It is: of the faxes that PASSED validation
(the ones the agent would enter without a human), how many had every field right? A wrong value that
slips through validation is a silent error in someone's chart.

python -m referral_agent.evaluate --faxes data/faxes --extractor ocr
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from pydantic import ValidationError

from .extractors import get_extractor
from .schema import FIELDS, Referral


def norm(field: str, v) -> object:
    if v is None:
        return None
    if field == "icd10_codes":
        return sorted(str(c).upper() for c in (v if isinstance(v, list) else str(v).replace(",", " ").split()))
    return " ".join(str(v).split()).lower()


def score(faxes: Path, extractor_name: str) -> dict:
    ex = get_extractor(extractor_name)
    per_field = defaultdict(lambda: [0, 0])
    passed = passed_and_correct = total = 0
    for pdf in sorted(faxes.glob("*.pdf")):
        truth = json.loads(pdf.with_suffix(".truth.json").read_text())
        raw = ex.extract(pdf)
        total += 1
        try:
            got = Referral(**raw).model_dump(mode="json")
            passed += 1
            valid = True
        except ValidationError:
            got, valid = raw, False
        all_ok = True
        for f in FIELDS:
            ok = norm(f, got.get(f)) == norm(f, truth[f])
            per_field[f][0] += ok
            per_field[f][1] += 1
            all_ok &= ok
        if valid and all_ok:
            passed_and_correct += 1
    field_acc = {f: round(c / n, 3) for f, (c, n) in per_field.items()}
    return {
        "extractor": extractor_name,
        "faxes": total,
        "mean_field_accuracy": round(sum(field_acc.values()) / len(field_acc), 3),
        "auto_submit_rate": round(passed / total, 3),
        "auto_submit_precision": round(passed_and_correct / passed, 3) if passed else None,
        "per_field": field_acc,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--faxes", default="data/faxes")
    ap.add_argument("--extractor", default="ocr", choices=["ocr", "claude", "ollama"])
    a = ap.parse_args()
    print(json.dumps(score(Path(a.faxes), a.extractor), indent=2))
