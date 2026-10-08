"""Read pay stub PDFs from the pay_stub_year_to_date folder.

Strategy: read the stub's tables by position (Description / Amount / YTD
columns), then cross-check every section against the stub's own totals and
check that gross - deductions - taxes = net pay. Anything that doesn't add up,
or can't be found, is flagged "needs review" for you to confirm or correct in
the app instead of being guessed."""
import hashlib
import re
from datetime import datetime

import pdfplumber

from .config import PAYSTUB_DIR
from .db import get_db, now_iso

NUM = re.compile(r"^\(?-?\$?[\d,]+\.\d{2}\)?$")
DATE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")

# Canonical fields shown in the app, in display order.
FIELDS = [
    ("gross", "Gross pay"),
    ("federal_tax", "Federal income tax"),
    ("state_tax", "State income tax"),
    ("local_tax", "Local tax"),
    ("social_security", "Social Security"),
    ("medicare", "Medicare"),
    ("other_taxes", "Other taxes"),
    ("k401_pretax", "401(k) pre-tax"),
    ("k401_roth", "401(k) Roth"),
    ("hsa", "HSA"),
    ("espp", "ESPP / ASPP"),
    ("other_pretax", "Other pre-tax deductions (benefits)"),
    ("other_posttax", "Other after-tax deductions"),
    ("net_pay", "Net pay (take-home)"),
    ("employer_401k", "Employer 401(k) contributions"),
]
FIELD_NAMES = dict(FIELDS)
DEDUCTION_FIELDS = ["federal_tax", "state_tax", "local_tax", "social_security", "medicare", "other_taxes",
                    "k401_pretax", "k401_roth", "hsa", "espp", "other_pretax", "other_posttax"]


def to_num(s):
    s = s.replace("$", "").replace(",", "")
    neg = s.startswith("(") or s.startswith("-")
    v = float(s.strip("()-"))
    return -v if neg else v


def _lines(words, tol=3.0):
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        for ln in lines:
            if abs(ln["top"] - w["top"]) <= tol:
                ln["words"].append(w)
                break
        else:
            lines.append({"top": w["top"], "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
        ln["text"] = " ".join(w["text"] for w in ln["words"])
    lines.sort(key=lambda ln: ln["top"])
    return lines


def _parse_tables(page):
    """Find every 'Description ... Amount ... YTD' table block and read its rows."""
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    lines = _lines(words)
    sections = []
    for li, ln in enumerate(lines):
        descs = [w for w in ln["words"] if w["text"] == "Description"]
        if not descs:
            continue
        for di, dw in enumerate(descs):
            x_lo = dw["x0"] - 1
            x_hi = descs[di + 1]["x0"] - 1 if di + 1 < len(descs) else page.width
            hdr = [w for w in ln["words"] if x_lo <= w["x0"] < x_hi]
            amount_col = next((w for w in hdr if w["text"] in ("Amount", "Current")), None)
            ytd_col = next((w for w in hdr if w["text"] in ("YTD", "Year-to-Date", "Year-To-Date")), None)
            if not amount_col and not ytd_col:
                continue
            other_hdr = [w for w in hdr if w is not dw]
            desc_right = min(w["x0"] for w in other_hdr) - 2 if other_hdr else x_hi
            # Section title: words in this block on the line(s) just above the header
            title = ""
            for prev in reversed(lines[max(0, li - 2):li]):
                tw = [w for w in prev["words"] if x_lo - 40 <= (w["x0"] + w["x1"]) / 2 < x_hi]
                if tw and not any(NUM.match(w["text"]) for w in tw):
                    title = " ".join(w["text"] for w in tw)
                    break
            rows, total = [], None
            for nxt in lines[li + 1:]:
                bw = [w for w in nxt["words"] if x_lo <= w["x0"] < x_hi]
                if not bw:
                    continue
                if any(w["text"] == "Description" for w in bw):
                    break
                label = " ".join(w["text"] for w in bw if w["x1"] <= desc_right + 1 and not NUM.match(w["text"]))
                cur = ytd = None
                for w in bw:
                    if not NUM.match(w["text"]):
                        continue
                    if amount_col and abs(w["x1"] - amount_col["x1"]) < 18:
                        cur = to_num(w["text"])
                    elif ytd_col and abs(w["x1"] - ytd_col["x1"]) < 18:
                        ytd = to_num(w["text"])
                if cur is None and ytd is None:
                    if rows or label.lower() == title.lower():
                        break  # end of table
                    continue
                if title and label.lower() == title.lower():
                    total = (cur, ytd)
                    break
                rows.append({"label": label, "current": cur, "ytd": ytd})
            sections.append({"title": title, "rows": rows, "total": total})
    return sections, lines


SUMMARY_LABELS = [
    ("gross", ["earnings", "gross pay", "gross earnings", "total earnings"]),
    ("pretax", ["pre tax deductions", "pre-tax deductions"]),
    ("taxes", ["employee taxes", "taxes", "total taxes"]),
    ("posttax", ["post tax deductions", "post-tax deductions", "after-tax deductions"]),
    ("net", ["take home pay", "net pay", "take home"]),
]


def _parse_summary(lines):
    """The 'Current / Year-To-Date' summary row at the top of Workday stubs."""
    for i, ln in enumerate(lines):
        first = ln["words"][0]["text"].lower()
        if first != "current":
            continue
        nums = [w for w in ln["words"] if NUM.match(w["text"])]
        if len(nums) < 3:
            continue
        ytd_line = next((l2 for l2 in lines[i + 1:i + 3] if l2["words"][0]["text"].lower().startswith("year")), None)
        ytd_nums = [w for w in ytd_line["words"] if NUM.match(w["text"])] if ytd_line else []
        hdr = lines[i - 1]["text"].lower() if i else ""
        found = []
        for key, names in SUMMARY_LABELS:
            pos = min((hdr.find(n) for n in names if hdr.find(n) >= 0), default=-1)
            if pos >= 0:
                found.append((pos, key))
        found.sort()
        keys = [k for _, k in found]
        if len(keys) != len(nums):
            return None
        out = {}
        for j, k in enumerate(keys):
            out[k] = (to_num(nums[j]["text"]), to_num(ytd_nums[j]["text"]) if j < len(ytd_nums) else None)
        return out
    return None


def classify(section_title: str, label: str):
    s, l = section_title.lower(), label.lower()
    if "taxable" in s or "taxable" in l:
        return None  # informational taxable-wages table
    if "employer" in s:
        return "employer_401k" if re.search(r"401\(?k\)?|403\(?b\)?", l) else "employer_other"
    if "tax" in s and "deduction" not in s:
        if "medicare" in l:
            return "medicare"
        if re.search(r"oasdi|social security|\bfica\b|\bss\b", l):
            return "social_security"
        if re.search(r"local|city|county|school|municipal", l):
            return "local_tax"
        if "federal" in l or "fed " in l or l.startswith("fit"):
            return "federal_tax"
        if "state" in l or re.search(r"\b[A-Z]{2}\b", label):
            return "state_tax"
        return "other_taxes"
    if "deduction" in s:
        pre = "pre" in s
        if "roth" in l:
            return "k401_roth"
        if re.search(r"401\(?k\)?|403\(?b\)?|457|retirement", l):
            return "k401_pretax" if pre else "k401_roth"
        if re.search(r"\bhsa\b|health savings", l):
            return "hsa"
        if re.search(r"espp|aspp|stock purchase", l):
            return "espp"
        return "other_pretax" if pre else "other_posttax"
    if "earning" in s:
        return "earning"
    return None


def parse_pdf(path):
    notes = []
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        sections, lines = _parse_tables(page)
    summary = _parse_summary(lines)

    # Header info
    employer = re.match(r"^(.*?)(?=\s\d)", text.strip().splitlines()[0])
    employer = employer.group(1).strip() if employer else None
    dates = []
    for ln in text.splitlines():
        if "Earnings" in ln and dates:
            break
        dates += DATE.findall(ln)
    period_start = period_end = pay_date = None
    if len(dates) >= 3:
        period_start, period_end, pay_date = [datetime.strptime(d, "%m/%d/%Y").date().isoformat() for d in dates[:3]]
    else:
        notes.append("Could not find pay period / payment dates.")

    out_lines = []
    fields = {k: [0.0, 0.0] for k, _ in FIELDS}
    found = set()
    section_ok = {}
    for sec in sections:
        s_cur = s_ytd = 0.0
        for r in sec["rows"]:
            out_lines.append({"section": sec["title"], **r})
            f = classify(sec["title"], r["label"])
            s_cur += r["current"] or 0
            s_ytd += r["ytd"] or 0
            if f in fields:
                fields[f][0] += r["current"] or 0
                fields[f][1] += r["ytd"] or 0
                found.add(f)
        if sec["total"]:
            ok = abs(s_cur - (sec["total"][0] or 0)) < 0.02 and abs(s_ytd - (sec["total"][1] or 0)) < 0.02
            section_ok[sec["title"]] = ok
            if not ok:
                notes.append(f"Lines in '{sec['title']}' don't add up to the stub's total.")

    if summary:
        if "gross" in summary:
            fields["gross"] = list(summary["gross"])
            found.add("gross")
        if "net" in summary:
            fields["net_pay"] = list(summary["net"])
            found.add("net_pay")
    else:
        notes.append("Could not find the summary (gross / net pay) row.")
        for sec in sections:
            if "earning" in sec["title"].lower() and sec["total"]:
                fields["gross"] = list(sec["total"])
                found.add("gross")

    # Cross-checks
    checks_ok = True
    for i, period in ((0, "this period"), (1, "year-to-date")):
        if "gross" in found and "net_pay" in found:
            ded = sum(fields[f][i] for f in DEDUCTION_FIELDS)
            if abs(fields["gross"][i] - ded - fields["net_pay"][i]) > 0.02:
                checks_ok = False
                notes.append(f"Gross minus deductions doesn't equal net pay ({period}).")
        else:
            checks_ok = False
    if summary:
        for key, secword in (("taxes", "tax"), ("pretax", "pre"), ("posttax", "post")):
            if key not in summary:
                continue
            fs = {"taxes": ["federal_tax", "state_tax", "local_tax", "social_security", "medicare", "other_taxes"],
                  "pretax": ["k401_pretax", "hsa", "other_pretax"],
                  "posttax": ["k401_roth", "espp", "other_posttax"]}[key]
            for i in (0, 1):
                if abs(sum(fields[f][i] for f in fs) - (summary[key][i] or 0)) > 0.02:
                    checks_ok = False
                    notes.append(f"{key} lines don't match the stub's {key} total.")
                    break

    confidence = {}
    for f, _ in FIELDS:
        if f in ("gross", "net_pay"):
            confidence[f] = "high" if (f in found and checks_ok) else ("low" if f in found else "missing")
        elif f == "employer_401k":
            confidence[f] = "high"  # informational only; absent means 0
        else:
            # A deduction line that wasn't present is confidently 0 only if all totals reconcile.
            confidence[f] = "high" if checks_ok else ("low" if f in found else "missing")
    status = "confirmed" if checks_ok and all(c == "high" for c in confidence.values()) and pay_date else "needs_review"
    return {
        "employer": employer, "period_start": period_start, "period_end": period_end, "pay_date": pay_date,
        "lines": out_lines, "fields": fields, "confidence": confidence, "status": status, "notes": notes,
    }


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_folder(force: bool = False) -> dict:
    """Import any new PDFs in pay_stub_year_to_date. Already-imported files
    (same content) are skipped unless force=True (re-parse; keeps your edits only if not forced)."""
    PAYSTUB_DIR.mkdir(exist_ok=True)
    imported, skipped, errors = [], [], []
    with get_db() as conn:
        for path in sorted(PAYSTUB_DIR.glob("*.pdf")):
            h = file_hash(path)
            existing = conn.execute("SELECT id FROM paystubs WHERE file_hash=?", (h,)).fetchone()
            if existing and not force:
                skipped.append(path.name)
                continue
            try:
                p = parse_pdf(path)
            except Exception as e:  # noqa: BLE001 - report any unreadable PDF
                errors.append(f"{path.name}: could not read ({type(e).__name__})")
                continue
            if existing:
                conn.execute("DELETE FROM paystub_lines WHERE paystub_id=?", (existing["id"],))
                conn.execute("DELETE FROM paystub_fields WHERE paystub_id=?", (existing["id"],))
                conn.execute("DELETE FROM paystubs WHERE id=?", (existing["id"],))
            cur = conn.execute(
                "INSERT INTO paystubs(file_name,file_hash,employer,pay_date,period_start,period_end,status,"
                "parse_notes,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (path.name, h, p["employer"], p["pay_date"], p["period_start"], p["period_end"], p["status"],
                 "\n".join(p["notes"]), now_iso()))
            sid = cur.lastrowid
            for ln in p["lines"]:
                conn.execute("INSERT INTO paystub_lines(paystub_id,section,label,current,ytd) VALUES(?,?,?,?,?)",
                             (sid, ln["section"], ln["label"], ln["current"], ln["ytd"]))
            for f, (cur_v, ytd_v) in p["fields"].items():
                conn.execute("INSERT INTO paystub_fields(paystub_id,field,current,ytd,confidence) VALUES(?,?,?,?,?)",
                             (sid, f, round(cur_v, 2), round(ytd_v, 2), p["confidence"][f]))
            imported.append({"file": path.name, "status": p["status"], "notes": p["notes"]})
    return {"imported": imported, "skipped": skipped, "errors": errors}


def list_paystubs(conn):
    out = []
    for s in conn.execute("SELECT * FROM paystubs ORDER BY pay_date DESC"):
        s = dict(s)
        s["fields"] = {r["field"]: dict(r) for r in conn.execute(
            "SELECT field,current,ytd,confidence FROM paystub_fields WHERE paystub_id=?", (s["id"],))}
        s["lines"] = [dict(r) for r in conn.execute(
            "SELECT section,label,current,ytd FROM paystub_lines WHERE paystub_id=? ORDER BY id", (s["id"],))]
        out.append(s)
    return out
