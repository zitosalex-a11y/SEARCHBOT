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
        text = "דוח סגירה\nלקוח: דני כהן\nטלפון: 050-1234567\nסה\"כ: 1500 ש\"ח"
        [sale] = searchbot.find_sales(searchbot.parse_messages("1.3.24, 10:00 - רון: " + text), 1000)
        self.assertEqual(sale.customer, "דני כהן")
        self.assertEqual(sale.phone, "050-1234567")
        self.assertEqual(sale.amount, 1500)


class EndToEndTests(unittest.TestCase):
    def test_sample_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"
            self.assertEqual(searchbot.main([str(SAMPLE), "--out", str(out)]), 0)
            with open(f"{out}_customers.csv", encoding="utf-8-sig") as f:
                customers = list(csv.DictReader(f))
            with open(f"{out}_sales.csv", encoding="utf-8-sig") as f:
                sales = list(csv.DictReader(f))

        self.assertEqual(len(sales), 3)  # $350 job and the "1000 main st" chat are excluded
        self.assertEqual([c["customer"] for c in customers], ["John Smith", "Robert Lee"])
        john = customers[0]
        self.assertEqual(john["jobs"], "2")  # merged by phone despite different formatting
        self.assertEqual(float(john["total_spent"]), 6350)


if __name__ == "__main__":
    unittest.main()
