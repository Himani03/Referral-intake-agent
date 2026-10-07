"""Extractors turn a fax PDF into a raw field dict. Validation happens later in schema.Referral.

- OcrExtractor: Tesseract + label parsing. Runs fully offline; the baseline.
- ClaudeVisionExtractor: sends page images to Claude and asks for JSON. Needs ANTHROPIC_API_KEY.
- OllamaVisionExtractor: same prompt against a local vision model served by Ollama. Free and offline.
"""
from __future__ import annotations

import base64
import difflib
import io
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import pymupdf
from PIL import Image

from .schema import FIELDS


def pdf_to_images(pdf: Path, dpi: int = 200) -> list[Image.Image]:
    out = []
    with pymupdf.open(pdf) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
            out.append(Image.frombytes("L", (pix.width, pix.height), pix.samples))
    return out


class OcrExtractor:
    name = "ocr"

    LABELS = {
        "patient name": "_name",
        "date of birth": "patient_dob",
        "phone": "patient_phone",
        "insurance": "insurance_payer",
        "member id": "insurance_member_id",
        "referring provider": "referring_provider",
        "npi": "referring_npi",
        "refer to": "specialty",
        "diagnosis (icd-10)": "icd10_codes",
        "urgency": "urgency",
    }

    def ocr(self, img: Image.Image) -> str:
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            img.save(f.name)
            return subprocess.run(["tesseract", f.name, "-", "--psm", "6"], capture_output=True, text=True, check=True).stdout

    @staticmethod
    def clean(v: str) -> str:
        """Drop speckle junk OCR picks up at the edges of a value (': ', ' ~', ' ;', '- ')."""
        toks = v.split()
        while toks and not any(c.isalnum() for c in toks[0]):
            toks.pop(0)
        while toks and not any(c.isalnum() for c in toks[-1]):
            toks.pop()
        v = " ".join(toks)
        return v.strip(" .,:;~-_|'\"")

    def match_label(self, head: str) -> str | None:
        head = re.sub(r"[^a-z0-9() -]", "", head.lower()).strip()
        best = difflib.get_close_matches(head, list(self.LABELS), n=1, cutoff=0.8)
        return self.LABELS[best[0]] if best else None

    def extract(self, pdf: Path) -> dict:
        text = "\n".join(self.ocr(im) for im in pdf_to_images(pdf))
        out: dict = {}
        lines = [l.strip() for l in text.splitlines()]
        for i, line in enumerate(lines):
            low = line.lower()
            if ":" in line:
                head, val = line.split(":", 1)
                key = self.match_label(head)
                if key:
                    out.setdefault(key, self.clean(val))
            if difflib.SequenceMatcher(None, low[:20], "reason for referral:").ratio() > 0.8:
                rest = []
                for nxt in lines[i + 1:]:
                    if not nxt or nxt.lower().startswith("signature"):
                        if rest:
                            break
                        continue
                    rest.append(nxt)
                out["reason"] = self.clean(" ".join(rest))
        if "_name" in out:
            parts = [p for p in out.pop("_name").split() if any(c.isalpha() for c in p)]
            if parts:
                out["patient_first_name"], out["patient_last_name"] = parts[0], " ".join(parts[1:])
        if "patient_dob" in out:
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", out["patient_dob"])
            if m:
                out["patient_dob"] = datetime(int(m[3]), int(m[1]), int(m[2])).date().isoformat()
        if "urgency" in out:
            out["urgency"] = out["urgency"].strip(" .").lower()
        if "referring_npi" in out:
            out["referring_npi"] = out["referring_npi"].replace("O", "0").replace("o", "0")
        return out


PROMPT = f"""You are reading a faxed specialty referral. Return ONLY a JSON object with exactly these keys:
{", ".join(FIELDS)}.
Rules: patient_dob as YYYY-MM-DD. icd10_codes as a list of strings. urgency is "routine" or "urgent".
referring_npi as 10 digits. Copy values exactly as written; if a value is unreadable use null. Do not guess."""


class ClaudeVisionExtractor:
    name = "claude"

    def __init__(self, model: str | None = None):
        import anthropic  # imported lazily so the offline path has no dependency on it

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("Set ANTHROPIC_API_KEY in your environment (never in code).")
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

    def extract(self, pdf: Path) -> dict:
        content = []
        for im in pdf_to_images(pdf, dpi=150):
            buf = io.BytesIO()
            im.save(buf, "PNG")
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                         "data": base64.b64encode(buf.getvalue()).decode()}})
        content.append({"type": "text", "text": PROMPT})
        msg = self.client.messages.create(model=self.model, max_tokens=1024, messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
        return {k: v for k, v in data.items() if v is not None}


class OllamaVisionExtractor:
    name = "ollama"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("OLLAMA_MODEL", "qwen2.5vl:3b")
        self.url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat"

    def extract(self, pdf: Path) -> dict:
        import urllib.request

        images = []
        for im in pdf_to_images(pdf, dpi=150):
            buf = io.BytesIO()
            im.save(buf, "PNG")
            images.append(base64.b64encode(buf.getvalue()).decode())
        body = {"model": self.model, "stream": False, "format": "json", "options": {"temperature": 0},
                "messages": [{"role": "user", "content": PROMPT, "images": images}]}
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            text = json.loads(r.read())["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
        return {k: v for k, v in data.items() if v is not None}


def get_extractor(name: str):
    return {"ocr": OcrExtractor, "claude": ClaudeVisionExtractor, "ollama": OllamaVisionExtractor}[name]()
