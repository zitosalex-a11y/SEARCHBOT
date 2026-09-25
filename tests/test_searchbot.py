import csv
import tempfile
import unittest
from pathlib import Path

import searchbot

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "sample_chat.txt"


class ParseTests(unittest.TestCase):
    def test_android_and_ios_formats(self):
        text = (
            "12/31/23, 9:15 PM - John: hi\nsecond line\n"
            "‎[31.12.2023, 21:15:04] Ron Cohen: Total: $2,000\n"
        )
        msgs = searchbot.parse_messages(text)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].text, "hi\nsecond line")
        self.assertEqual(msgs[1].sender, "Ron Cohen")

    def test_amount_formats(self):
        cases = {
            "Total: $4,850": 4850,
            "paid 1200$ cash": 1200,
            "Total: 1,500.00 USD": 1500,
            "Sale: 2.5k": 2500,
            "סה\"כ: 3,200 ₪": 3200,
            "call me 555-123-4567 at 1000 main st": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(searchbot.extract_amount(text), expected)

    def test_labelled_total_beats_other_amounts(self):
        text = "Parts: $2,000\nDiscount $100\nTotal: $1,900"
        self.assertEqual(searchbot.extract_amount(text), 1900)

    def test_hebrew_report(self):
        text = "לקוח: דני כהן\nטלפון: 050-1234567\nשירות: תיקון דלת מוסך\nנסגר\nסה\"כ: 1500 ש\"ח"
        [sale] = searchbot.find_sales(searchbot.parse_messages("1.3.24, 10:00 - רון: " + text), 1000)
        self.assertEqual(sale.customer, "דני כהן")
        self.assertEqual(sale.phone, "050-1234567")
        self.assertEqual(sale.total, 1500)


class ClosingReportTests(unittest.TestCase):
    REPORT = (
        "3/2/24, 2:05 PM - Mike: Customer: Robert Lee\n"
        "Phone: 555 222 3333\n"
        "Address: 400 Elm Rd, Frisco TX\n"
        "Service: Garage door spring\n"
        "Preferred Date And Time: 3/2 2pm-4pm\n"
        "Notes: waiting for parts 2 weeks, gate code 4455\n"
        "\n"
        "Closed\n"
        "Deposit: 500$\n"
        "Balance: 1,300$\n"
        "Total: 1,800$\n"
        "CP parts: 150$\n"
        "Company parts: 220$\n"
    )

    def test_team_report_format(self):
        [sale] = searchbot.find_sales(searchbot.parse_messages(self.REPORT), 1000)
        self.assertEqual(sale.customer, "Robert Lee")
        self.assertEqual(sale.phone, "555 222 3333")
        self.assertEqual(sale.address, "400 Elm Rd, Frisco TX")
        self.assertEqual(sale.service, "Garage door spring")
        self.assertEqual(sale.total, 1800)  # the Total line, not the bigger-looking numbers
        self.assertEqual(sale.deposit, 500)
        self.assertEqual(sale.balance, 1300)
        self.assertEqual(sale.parts, 370)  # CP + company parts
        self.assertEqual(sale.parts_detail, "CP parts: 150$ | Company parts: 220$")

    def test_callbacks_and_in_progress_are_skipped(self):
        callback = self.REPORT.replace("Closed\n", "Callback\n")
        in_progress = self.REPORT.replace("Closed\n", "In progress\n")
        no_total = self.REPORT.replace("Total: 1,800$\n", "")
        for text in (callback, in_progress, no_total):
            with self.subTest(text=text.splitlines()[7:9]):
                self.assertEqual(searchbot.find_sales(searchbot.parse_messages(text), 1000), [])

    def test_closed_with_extra_text(self):
        text = self.REPORT.replace("Closed\n", "CLOSED ✅ paid cash\n")
        self.assertEqual(len(searchbot.find_sales(searchbot.parse_messages(text), 1000)), 1)

    def test_only_garage_door_jobs_by_default(self):
        hvac = self.REPORT.replace("Service: Garage door spring", "Service: HVAC - AC not cooling")
        no_service = self.REPORT.replace("Service: Garage door spring\n", "")
        for text in (hvac, no_service):
            with self.subTest(service=text.splitlines()[3]):
                self.assertEqual(searchbot.find_sales(searchbot.parse_messages(text), 1000), [])
        # --service all / hvac
        self.assertEqual(len(searchbot.find_sales(searchbot.parse_messages(hvac), 1000, service=None)), 1)
        self.assertEqual(len(searchbot.find_sales(searchbot.parse_messages(hvac), 1000, service="hvac")), 1)

    def test_service_type(self):
        cases = {
            "Garage door repair": "garage", "Broken spring": "garage", "Opener install": "garage",
            "HVAC": "hvac", "AC not cooling": "hvac", "A/C repair": "hvac", "Furnace": "hvac",
            "Dryer vent cleaning": "", "": "",
        }
        for service, expected in cases.items():
            with self.subTest(service=service):
                self.assertEqual(searchbot.service_type(service), expected)

    def test_total_without_currency_sign(self):
        text = "Customer: A\nClosed\nTotal: 2500\nParts: 300"
        self.assertEqual(searchbot.extract_amount(text), 2500)
        self.assertEqual(searchbot.extract_parts(text)[0], 300)

    def test_exactly_1000_is_not_over(self):
        msgs = searchbot.parse_messages("3/5/24, 9:00 AM - Sam: Customer: A\nClosed\nTotal: 1000$")
        self.assertEqual(searchbot.find_sales(msgs, 1000), [])


class EndToEndTests(unittest.TestCase):
    def test_sample_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"
            self.assertEqual(searchbot.main([str(SAMPLE), "--out", str(out)]), 0)
            with open(f"{out}_customers.csv", encoding="utf-8-sig") as f:
                customers = list(csv.DictReader(f))
            with open(f"{out}_sales.csv", encoding="utf-8-sig") as f:
                sales = list(csv.DictReader(f))

        # HVAC jobs, $350 job, exactly-$1000 job, callback, in-progress job and chat are excluded
        self.assertEqual(len(sales), 3)
        self.assertEqual([c["customer"] for c in customers], ["John Smith", "Robert Lee"])
        john = customers[0]
        self.assertEqual(john["jobs"], "2")  # merged by phone despite different formatting
        self.assertEqual(float(john["total_spent"]), 6350)
        self.assertEqual(float(john["total_parts"]), 1280)


if __name__ == "__main__":
    unittest.main()
