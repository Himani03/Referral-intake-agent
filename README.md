# 📠 Referral Intake Agent

**An AI agent that reads faxed medical referrals and types them into an EMR, built around one question: what happens when something goes wrong?**

In 2026, specialty clinics still receive most of their referrals by fax. Someone at the front desk squints at a blurry page, finds or registers the patient in the EMR, and retypes every field by hand. This project automates that whole loop with a vision model and a browser agent. It is designed to **fail closed**: whenever it is unsure, a human decides instead of the chart quietly getting a wrong value.

> All patient data in this repo is **synthetic**. Nothing here has ever touched real PHI.

---

## The headline result

25 synthetic faxes, degraded to look like real fax output (skew, blur, speckle, no text layer):

| Metric | OCR baseline (Tesseract) | Local vision LLM (qwen2.5-VL 3B, free, runs on a laptop) |
|---|---|---|
| Mean field accuracy | 93.7% | **99.7%** |
| ICD-10 field accuracy | 56% | **100%** |
| Auto-submit rate (passed validation) | 56% | **96%** |
| **Auto-submit precision** (passed validation *and* every field right) | 71.4% | **100%** |

**Auto-submit precision is the number that matters.** It answers "when the agent enters a referral with no human in the loop, how often is everything in it correct?" With OCR, 4 of the 14 faxes that passed validation still carried a wrong first name, phone digit or member ID. No format check can catch errors like that, so they would land silently in a patient's chart.

The vision model got all 24 of its auto-submitted faxes completely right. On the 25th it misread one NPI digit, and the NPI check digit caught it, so the fax went to a human instead of into the chart. **The safety net caught the one error the model made.**

<sub>The OCR numbers come from the original Linux run. The vision LLM numbers come from the same generator and seed rendered on macOS. Reproduce both with the commands below.</sub>

---

## How it works

```
fax PDF ──► extractor (Tesseract OCR | local vision LLM via Ollama | Claude vision)
              │
              ▼
        schema validation (NPI check digit, ICD-10 format, DOB, phone)
              │ fails ────────────────► human review queue
              ▼ passes
        browser agent (Playwright) ──► mock EMR (login, search, register, refer)
              │ ambiguous patient / submit keeps failing ──► human review queue
              ▼
        tamper-evident audit log (hash chain, no PHI)
```

| Module | What it does |
|---|---|
| `faxgen` | Renders fake referral faxes as degraded images inside PDFs with no text layer, plus ground-truth JSON for scoring. |
| `extractors` | Turn a fax into fields. `ocr` is Tesseract plus label parsing. `ollama` runs a local vision model, which is free and offline. `claude` uses the Anthropic API. All three share one prompt and one output schema. |
| `schema` | Validates every field before the agent is allowed to act. The NPI Luhn check and the ICD-10 format catch the classic OCR misreads (O vs 0, dropped digits). |
| `mock_emr` | A deliberately plain HTML EMR with **no API**, so the agent has to work the UI the way a person would. `EMR_FLAKY=0.3` makes 30% of submits save the record and *then* return a 503. |
| `browser_agent` | Logs in, matches or registers the patient, fills in the referral form, and confirms. |
| `audit` | Writes one JSONL line per action, chained by SHA-256. Patients appear only as keyed HMAC pseudonyms. |

---

## When things go wrong (the interesting part)

| Situation | What the agent does |
|---|---|
| Extracted value fails validation | Never touches the EMR. Goes to the review queue with the names of the failing fields. |
| Extractor crashes or returns garbage | That fax goes to review and the batch keeps running. |
| No patient with that last name + DOB | Registers a new patient. |
| One match, same first name | Uses it. |
| Several matches, or the first name differs | **Stops.** A wrong-patient error is the worst outcome, so a human decides. |
| EMR saves the referral but returns 503 | Looks up the fax ID before retrying, finds the saved record, and logs `recovered`. No duplicate. |
| Same fax processed twice | Detected by fax ID and skipped. |
| Submit keeps failing | Retries with exponential backoff, then sends the fax to the review queue with a failure screenshot. |

**End-to-end run** (local vision LLM, `EMR_FLAKY=0.3`): 24 referrals submitted, 1 sent to review (the misread NPI), 3 save-then-503 events recovered, **0 duplicates**, 99 audit entries verified, **0 patient names in the log**.

---

## Run it

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env                  # edit, then load it: set -a; source .env; set +a

python -m referral_agent.faxgen -n 25                       # synthetic faxes into data/faxes

# pick an extractor to score
ollama pull qwen2.5vl:3b && python -m referral_agent.evaluate --extractor ollama   # free, local
python -m referral_agent.evaluate --extractor ocr           # needs: brew install tesseract
python -m referral_agent.evaluate --extractor claude        # needs ANTHROPIC_API_KEY

uvicorn mock_emr.app:app --port 8000 &                      # the mock EMR
python -m referral_agent.pipeline --extractor ollama --headed   # watch the agent drive the browser
python -c "from referral_agent.audit import verify; print(verify('data/audit.jsonl'))"

pytest -q                                                   # 15 tests, including a real browser against a flaky EMR
```

---

## Security notes

This is a demo, not a HIPAA-compliant system, but it is built the way one should start:

- **Secrets come only from the environment** (EMR login, API key, audit HMAC key). In production they would come from a secret manager. None of them appear in code or logs.
- **The audit log records who did what, to which record, and when, without storing PHI.** Editing, reordering or removing any line breaks the chain. A hash chain alone can't detect lines cut off the *end*, so in production the latest hash would also be anchored somewhere external.
- **Review-queue files and failure screenshots do contain PHI.** They stay under `data/`, which is gitignored and would live on encrypted, access-controlled storage in real use.
- **The agent fails closed.** Anything uncertain goes to a person, not into the chart.

---

## What I'd build next

1. **Score Claude vision on the same set** and compare a 3B local model against a frontier model on harder, noisier faxes, where the gap should show.
2. **Per-field confidence:** have the model flag fields it is unsure of and route only those to review.
3. **Cross-check against the EMR** (for example, does the phone on file match the fax?) to catch the errors that validation can't.
4. **Run several agents concurrently** behind a queue, with per-clinic rate limits.

---

<sub>Built with Python, Playwright, FastAPI, Pydantic, PyMuPDF, Pillow, Tesseract, Ollama and the Anthropic SDK.</sub>
