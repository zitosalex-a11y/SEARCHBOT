#!/usr/bin/env python3
"""SearchBot - pull big sales out of a WhatsApp group chat export.

Reads the file WhatsApp produces with "Export chat" (a .txt, or the .zip it
sometimes comes in), finds every closed garage door job whose total is above a
threshold (default $1000), and writes:

  * <out>_sales.csv      - one row per qualifying job
  * <out>_customers.csv  - one row per customer (totals, job count, contact info)
  * <out>.xlsx           - both sheets in one workbook (only if openpyxl is installed)

Usage:
  python searchbot.py "WhatsApp Chat with Closing Reports.txt"
  python searchbot.py chat.zip --min 1500 --out big_customers
  python searchbot.py chat.zip --service all      # garage door AND hvac jobs
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Chat parsing
# --------------------------------------------------------------------------

# Invisible direction marks WhatsApp sprinkles into exports.
_INVISIBLE = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮﻿"), None)

# Android: "12/31/23, 9:15 PM - John: text"   iOS: "[12/31/23, 9:15:04 PM] John: text"
_MESSAGE_START = re.compile(
    r"^\[?(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s+"
    r"(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[APap]\.?\s?[Mm]\.?)?)\]?"
    r"\s*(?:-\s*)?(?P<sender>[^:]{1,80}?):\s(?P<text>.*)$"
)


@dataclass
class Message:
    date: str
    time: str
    sender: str
    text: str


def read_chat(path: Path) -> str:
    """Return the chat text from a .txt export or a .zip export."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            txt_names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not txt_names:
                raise SystemExit(f"No .txt chat file found inside {path}")
            raw = zf.read(txt_names[0])
    else:
        raw = path.read_bytes()
    return raw.decode("utf-8-sig", errors="replace")


def parse_messages(chat_text: str) -> list[Message]:
    """Split an export into messages, joining multi-line messages together."""
    messages: list[Message] = []
    for line in chat_text.splitlines():
        line = line.translate(_INVISIBLE)
        match = _MESSAGE_START.match(line)
        if match:
            messages.append(Message(match["date"], match["time"], match["sender"].strip(), match["text"]))
        elif messages:
            messages[-1].text += "\n" + line
    return messages


# --------------------------------------------------------------------------
# Report field extraction
# --------------------------------------------------------------------------

# Words that label the sale amount in a closing report (English + Hebrew).
TOTAL_LABELS = [
    "total", "grand total", "sale", "sold", "amount", "price", "paid", "payment",
    "charged", "collected", "invoice", "job total", "ticket",
    'סה"כ', "סהכ", "סכום", "מחיר", "שולם", "תשלום", "עלות",
]
CUSTOMER_LABELS = ["customer", "client", "name", "customer name", "client name", "לקוח", "שם", "שם לקוח"]
PHONE_LABELS = ["phone", "tel", "cell", "mobile", "number", "טלפון", "נייד", "פלאפון"]
ADDRESS_LABELS = ["address", "addr", "location", "כתובת", "עיר"]

_NUMBER = r"\d{1,3}(?:[,\s]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_CURRENCY_BEFORE = r"(?:\$|usd|us\$|₪|ils|nis)"
_CURRENCY_AFTER = r"(?:\$|usd|dollars?|bucks|₪|ils|nis|ש\"ח|שח|דולר)"
_AMOUNT_WITH_CURRENCY = re.compile(
    rf"{_CURRENCY_BEFORE}\s?(?P<a>{_NUMBER})(?P<ka>k\b)?"
    rf"|(?P<b>{_NUMBER})(?P<kb>k\b)?\s?{_CURRENCY_AFTER}",
    re.IGNORECASE,
)
_ANY_NUMBER = re.compile(rf"(?P<n>{_NUMBER})(?P<k>k\b)?", re.IGNORECASE)
_PHONE = re.compile(r"(?:\+|\()?\d[\d\s().-]{7,}\d")


def _to_float(number: str, thousands: str | None) -> float:
    value = float(re.sub(r"[,\s]", "", number))
    return value * 1000 if thousands else value


def _label_regex(labels: list[str]) -> re.Pattern:
    alternatives = "|".join(sorted((re.escape(l) for l in labels), key=len, reverse=True))
    # Label at line start (optionally after bullets/emoji), then ":" "-" "=" or whitespace.
    return re.compile(rf"^[\W_]*(?:{alternatives})\b\s*[:=\-–]?\s*(?P<value>.*)$", re.IGNORECASE)


_TOTAL_RE = _label_regex(TOTAL_LABELS)
_JOB_TOTAL_RE = _label_regex(["total", "grand total", "job total", 'סה"כ', "סהכ"])
_DEPOSIT_RE = _label_regex(["deposit", "dep", "מקדמה"])
_BALANCE_RE = _label_regex(["balance", "balance due", "remaining", "יתרה"])
_SERVICE_RE = _label_regex(["service", "שירות"])
# Any line whose label mentions parts: "Parts:", "CP parts:", "cp/company parts:", "Total parts:".
_PARTS_RE = re.compile(r"^[\W_]*(?P<label>[^:\n]{0,30}?\b(?:parts?|חלקים)\b[^:\n\d]{0,20}?)\s*[:=]\s*(?P<value>.*)$", re.IGNORECASE)
_CUSTOMER_RE = _label_regex(CUSTOMER_LABELS)
_PHONE_RE = _label_regex(PHONE_LABELS)
_ADDRESS_RE = _label_regex(ADDRESS_LABELS)
_CUSTOMER_INLINE_RE = re.compile(
    r"\b(?:%s)\s*:\s*(?P<value>[^,;|\n]+)"
    % "|".join(sorted((re.escape(l) for l in CUSTOMER_LABELS), key=len, reverse=True)),
    re.IGNORECASE,
)


def _amount_in(value: str) -> float | None:
    """First amount in a label's value: prefer one with a currency sign, else any number."""
    cur = _AMOUNT_WITH_CURRENCY.search(value)
    if cur:
        return _to_float(cur["a"] or cur["b"], cur["ka"] or cur["kb"])
    num = _ANY_NUMBER.search(value)
    return _to_float(num["n"], num["k"]) if num else None


def _labelled_amount(text: str, pattern: re.Pattern) -> float | None:
    """Amount on the first line with this label (or on the next line, if the label line is empty)."""
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if _PARTS_RE.match(line):  # "Total parts: 300" is parts, not the job total
            continue
        m = pattern.match(line)
        if not m:
            continue
        value = m["value"] or next((l for l in lines[i + 1:] if l), "")
        amount = _amount_in(value)
        if amount is not None:
            return amount
    return None


def extract_amount(text: str) -> float | None:
    """Find the sale amount of a report.

    Priority: the "Total:" line; then a number on another sale line ("Sale:/Paid:/
    Price:"); then the largest amount written with a currency sign ($1,200 / 1200$).
    Plain numbers with no label and no currency are ignored so phone numbers and
    dates don't count.
    """
    total = _labelled_amount(text, _JOB_TOTAL_RE)
    if total is not None:
        return total

    labelled = [
        a for line in text.splitlines()
        if not _PARTS_RE.match(line.strip()) and not _DEPOSIT_RE.match(line.strip())
        and not _BALANCE_RE.match(line.strip())
        and (m := _TOTAL_RE.match(line.strip())) and (a := _amount_in(m["value"])) is not None
    ]
    if labelled:
        return max(labelled)

    with_currency = [
        _to_float(m["a"] or m["b"], m["ka"] or m["kb"]) for m in _AMOUNT_WITH_CURRENCY.finditer(text)
    ]
    return max(with_currency) if with_currency else None


def extract_parts(text: str) -> tuple[float | None, str]:
    """Parts cost and the raw parts lines.

    Handles "Parts: 250", "CP parts: $120", "Company parts: 300", and several
    parts lines in one report (they are added up).
    """
    amounts: list[float] = []
    details: list[str] = []
    for line in text.splitlines():
        m = _PARTS_RE.match(line.strip())
        if not m:
            continue
        details.append(line.strip())
        in_line = [_to_float(c["a"] or c["b"], c["ka"] or c["kb"]) for c in _AMOUNT_WITH_CURRENCY.finditer(m["value"])]
        if not in_line:
            in_line = [_to_float(n["n"], n["k"]) for n in _ANY_NUMBER.finditer(m["value"])]
        amounts.extend(in_line)
    return (sum(amounts) if amounts else None), " | ".join(details)


def _labelled_value(text: str, pattern: re.Pattern) -> str:
    for line in text.splitlines():
        m = pattern.match(line.strip())
        if m and m["value"].strip():
            return m["value"].strip()
    return ""


def extract_phone(text: str) -> str:
    labelled = _labelled_value(text, _PHONE_RE)
    source = labelled or text
    m = _PHONE.search(source)
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def extract_customer(text: str) -> str:
    name = _labelled_value(text, _CUSTOMER_RE)
    if not name:
        # One-line reports: "Closing - Name: Robert Lee, 555 222 3333, paid 1200$"
        m = _CUSTOMER_INLINE_RE.search(text)
        name = m["value"].strip() if m else ""
    # Drop a trailing phone number if the name line has one ("John Smith 050-1234567").
    return _PHONE.sub("", name).strip(" ,-|") if name else ""


# --------------------------------------------------------------------------
# Sales / customers
# --------------------------------------------------------------------------

@dataclass
class Sale:
    date: str
    time: str
    sent_by: str
    customer: str
    phone: str
    address: str
    service: str
    total: float
    parts: float | None
    parts_detail: str
    deposit: float | None
    balance: float | None
    report: str


@dataclass
class Customer:
    customer: str
    phone: str = ""
    address: str = ""
    jobs: int = 0
    total_spent: float = 0.0
    total_parts: float = 0.0
    biggest_sale: float = 0.0
    last_job_date: str = ""
    dates: list[str] = field(default_factory=list)


# A line saying the job is closed: "Closed", "CLOSED ✅", "Closed - paid cash", "נסגר".
_CLOSED_LINE = re.compile(r"^[\W_]*(?:job\s+)?(?:closed|נסגר|סגור)\b", re.IGNORECASE)


def is_customer_report(text: str) -> bool:
    """A job report of any status (starts the team template with a Customer: line)."""
    return any(_CUSTOMER_RE.match(line.strip()) for line in text.splitlines())


def is_closed_report(text: str) -> bool:
    """A finished job in the team template: Customer: line + "Closed" line + Total: line.

    Callbacks and in-progress jobs don't have the "Closed" line, so they are skipped.
    """
    return (
        is_customer_report(text)
        and any(_CLOSED_LINE.match(line.strip()) for line in text.splitlines())
        and _labelled_amount(text, _JOB_TOTAL_RE) is not None
    )


# Words in the "Service:" line that tell which kind of job it is.
SERVICE_TYPES = {
    "garage": [
        "garage", "door", "doors", "spring", "springs", "torsion", "opener", "openers",
        "cable", "cables", "roller", "rollers", "track", "tracks", "panel", "panels", "gdo",
        "מוסך", "דלת", "קפיץ", "מנוע",
    ],
    "hvac": [
        "hvac", "ac", "a/c", "a\\c", "air condition", "air conditioner", "air conditioning",
        "furnace", "heat", "heating", "heater", "heat pump", "duct", "ducts", "cooling",
        "thermostat", "compressor", "condenser", "mini split", "minisplit", "coil", "freon",
        "מזגן", "מיזוג", "חימום",
    ],
}


def _word_regex(words: list[str]) -> re.Pattern:
    alternatives = "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


_SERVICE_TYPE_RES = {name: _word_regex(words) for name, words in SERVICE_TYPES.items()}


def service_type(service: str) -> str:
    """"garage", "hvac", ... for a Service: line, or "" when it's unclear or mixed."""
    matches = [name for name, pattern in _SERVICE_TYPE_RES.items() if pattern.search(service)]
    return matches[0] if len(matches) == 1 else ""


def find_sales(
    messages: list[Message], minimum: float, keyword: str | None = None, service: str | None = "garage"
) -> list[Sale]:
    """Closed jobs over `minimum`. `service` keeps only that job type ("garage", "hvac"); None keeps all."""
    sales = []
    for msg in messages:
        if keyword and keyword.lower() not in msg.text.lower():
            continue
        if not is_closed_report(msg.text):
            continue
        if service and service_type(_labelled_value(msg.text, _SERVICE_RE)) != service:
            continue
        amount = _labelled_amount(msg.text, _JOB_TOTAL_RE)
        if amount is None or amount <= minimum:
            continue
        parts, parts_detail = extract_parts(msg.text)
        sales.append(Sale(
            date=msg.date,
            time=msg.time,
            sent_by=msg.sender,
            customer=extract_customer(msg.text),
            phone=extract_phone(msg.text),
            address=_labelled_value(msg.text, _ADDRESS_RE),
            service=_labelled_value(msg.text, _SERVICE_RE),
            total=amount,
            parts=parts,
            parts_detail=parts_detail,
            deposit=_labelled_amount(msg.text, _DEPOSIT_RE),
            balance=_labelled_amount(msg.text, _BALANCE_RE),
            report=msg.text.strip(),
        ))
    return sales


def group_customers(sales: list[Sale]) -> list[Customer]:
    """Merge sales that belong to the same customer (same phone, else same name)."""
    customers: dict[str, Customer] = {}
    for sale in sales:
        digits = re.sub(r"\D", "", sale.phone)
        key = digits[-9:] if digits else sale.customer.lower().strip() or f"unknown-{id(sale)}"
        c = customers.setdefault(key, Customer(customer=sale.customer or "(no name in report)"))
        if not c.phone and sale.phone:
            c.phone = sale.phone
        if not c.address and sale.address:
            c.address = sale.address
        if c.customer == "(no name in report)" and sale.customer:
            c.customer = sale.customer
        c.jobs += 1
        c.total_spent += sale.total
        c.total_parts += sale.parts or 0
        c.biggest_sale = max(c.biggest_sale, sale.total)
        c.dates.append(sale.date)
        c.last_job_date = sale.date
    return sorted(customers.values(), key=lambda c: c.total_spent, reverse=True)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

SALE_COLUMNS = [
    "date", "customer", "phone", "address", "service", "total", "parts", "parts_detail",
    "deposit", "balance", "sent_by", "time", "report",
]
CUSTOMER_COLUMNS = [
    "customer", "phone", "address", "jobs", "total_spent", "total_parts", "biggest_sale", "last_job_date",
]


def _rows(items, columns):
    return [[getattr(item, col) for col in columns] for item in items]


def write_csv(path: Path, columns: list[str], rows: list[list]) -> None:
    # utf-8-sig so Excel opens Hebrew / emoji correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)


def write_xlsx(path: Path, sales: list[Sale], customers: list[Customer]) -> bool:
    try:
        from openpyxl import Workbook
    except ImportError:
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = "Customers"
    ws.append(CUSTOMER_COLUMNS)
    for row in _rows(customers, CUSTOMER_COLUMNS):
        ws.append(row)
    ws2 = wb.create_sheet("Sales")
    ws2.append(SALE_COLUMNS)
    for row in _rows(sales, SALE_COLUMNS):
        ws2.append(row)
    wb.save(path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find sales above a threshold in a WhatsApp chat export.")
    parser.add_argument("chat", type=Path, help="WhatsApp export (.txt or .zip)")
    parser.add_argument("--min", type=float, default=1000, help="only sales ABOVE this amount (default 1000)")
    parser.add_argument("--out", default="good_customers", help="output file name prefix (default good_customers)")
    parser.add_argument(
        "--service", default="garage", choices=[*SERVICE_TYPES, "all"],
        help="which jobs to keep, by the Service: line (default garage; 'all' keeps every job)",
    )
    parser.add_argument("--keyword", help='only messages containing this word, e.g. "closing" or "report"')
    parser.add_argument("--sender", help="only messages from senders whose name contains this text")
    parser.add_argument("--since", help="only messages on/after this date, same format as the export (e.g. 1/1/24)")
    args = parser.parse_args(argv)

    if not args.chat.exists():
        parser.error(f"file not found: {args.chat}")

    messages = parse_messages(read_chat(args.chat))
    if not messages:
        print("No messages found - is this a WhatsApp 'Export chat' file?", file=sys.stderr)
        return 1
    if args.sender:
        messages = [m for m in messages if args.sender.lower() in m.sender.lower()]
    if args.since:
        messages = messages[_first_index_on_or_after(messages, args.since):]

    service = None if args.service == "all" else args.service
    sales = find_sales(messages, args.min, args.keyword, service)
    customers = group_customers(sales)

    out = Path(args.out)
    write_csv(out.with_name(out.name + "_sales.csv"), SALE_COLUMNS, _rows(sales, SALE_COLUMNS))
    write_csv(out.with_name(out.name + "_customers.csv"), CUSTOMER_COLUMNS, _rows(customers, CUSTOMER_COLUMNS))
    wrote_xlsx = write_xlsx(out.with_name(out.name + ".xlsx"), sales, customers)

    print(f"Scanned {len(messages)} messages.")
    open_jobs = sum(1 for m in messages if is_customer_report(m.text) and not is_closed_report(m.text))
    print(f"Skipped {open_jobs} reports without a Closed + Total: line (callbacks, in progress, ...).")
    if service:
        other = sum(
            1 for m in messages
            if is_closed_report(m.text) and service_type(_labelled_value(m.text, _SERVICE_RE)) != service
        )
        print(f"Skipped {other} closed jobs that are not {service} jobs (by the Service: line).")
    print(f"Found {len(sales)} sales above ${args.min:,.0f} from {len(customers)} customers.")
    for c in customers[:10]:
        print(f"  {c.customer:<30} {c.phone:<18} ${c.total_spent:>10,.2f}  ({c.jobs} job{'s' if c.jobs != 1 else ''})")
    print(f"\nSaved: {out.name}_customers.csv, {out.name}_sales.csv" + (f", {out.name}.xlsx" if wrote_xlsx else ""))
    return 0


def _first_index_on_or_after(messages: list[Message], since: str) -> int:
    """Index of the first message on/after `since` (the export is chronological)."""
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%d/%m/%y", "%d/%m/%Y", "%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            target = datetime.strptime(since, fmt)
            break
        except ValueError:
            continue
    else:
        raise SystemExit(f"Could not understand --since date: {since}")
    for i, msg in enumerate(messages):
        try:
            if datetime.strptime(msg.date, fmt) >= target:
                return i
        except ValueError:
            continue
    return len(messages)


if __name__ == "__main__":
    sys.exit(main())
