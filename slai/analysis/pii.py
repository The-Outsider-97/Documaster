"""Personal-data detection with validation (checksums where the identifier has one). Pattern-based and local: it finds what looks
like personal data so a person can review and redact it; it does not claim to find all of it."""
from __future__ import annotations

import re
from typing import Any

from .model import Unit

PATTERNS: dict[str, str] = {
    "email": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    "phone": r"(?<![\w.])(?:\+|00)\d{1,3}[\s\-.()]*(?:\d[\s\-.()]*){7,12}\d|(?<![\w.])0\d{1,3}[\s\-.]?\d{3,4}[\s\-.]?\d{2,4}(?:[\s\-.]?\d{2,4})?(?![\w.])|(?<![\w.])\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\w.])",
    "iban": r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b",
    "card": r"\b(?:\d[ \-]?){13,19}\b",
    "bsn": r"\b\d{9}\b",
    "ipv4": r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
}
LABELS = {"email": "Email address", "phone": "Phone number", "iban": "Bank account (IBAN)", "card": "Payment card number",
          "bsn": "Possible Dutch citizen service number (BSN)", "ipv4": "IP address"}


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "").upper()
    if not 15 <= len(s) <= 34 or not s[:2].isalpha():
        return False
    moved = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in moved)
    return int(num) % 97 == 1


def _bsn_ok(s: str) -> bool:
    if len(s) != 9 or s == "000000000":
        return False
    total = sum(int(s[i]) * (9 - i) for i in range(8)) - int(s[8])
    return total % 11 == 0


def scan_text(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return not any(a < y and x < b for x, y in taken)

    order = ("email", "iban", "card", "phone", "bsn", "ipv4")
    for kind in order:
        for m in re.finditer(PATTERNS[kind], text):
            a, b = m.start(), m.end()
            s = m.group()
            digits = re.sub(r"\D", "", s)
            confidence = "pattern"
            if kind == "iban":
                if not _iban_ok(s):
                    continue
                confidence = "checksum"
            elif kind == "card":
                if not 13 <= len(digits) <= 19 or not _luhn(digits) or len(set(digits)) < 2:
                    continue
                confidence = "checksum"
            elif kind == "phone":
                if not 8 <= len(digits) <= 15:
                    continue
            elif kind == "bsn":
                if not _bsn_ok(s):
                    continue
                confidence = "checksum (weak: any 9-digit number passes 1 in 11)"
            elif kind == "email":
                confidence = "pattern"
            if not free(a, b):
                continue
            taken.append((a, b))
            found.append({"type": kind, "label": LABELS[kind], "text": s.strip(), "start": a, "end": b, "confidence": confidence})
    found.sort(key=lambda f: f["start"])
    return found


def scan(units: list[Unit], limit: int = 2000) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for u in units:
        for f in scan_text(u.text):
            items.append({**{k: f[k] for k in ("type", "label", "confidence")},
                          "text": f["text"], "evidence": u.evidence(f["start"], f["end"], quote=False)})
            if len(items) >= limit:
                break
    counts: dict[str, int] = {}
    for i in items:
        counts[i["type"]] = counts.get(i["type"], 0) + 1
    return {"total": len(items), "counts": counts, "items": items, "truncated": len(items) >= limit,
            "note": "Pattern-based detection with checksums where the identifier has one. Names and addresses are not detected; "
                    "review the document before relying on this."}
