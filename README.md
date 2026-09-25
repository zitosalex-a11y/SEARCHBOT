# SearchBot: find your big customers in a WhatsApp group

SearchBot reads a WhatsApp chat **export file** and finds every closing report with a sale above $1000 (you can change the amount). It then writes a list of those customers to a spreadsheet.

It never connects to WhatsApp, so your account can't be banned for using it.

## 1. Export the group chat
- **Android:** open the group → ⋮ → More → Export chat → **Without media**
- **iPhone:** open the group → tap the group name → Export Chat → **Without Media**

Send the file to your computer. It will be a `.txt` or `.zip` file. SearchBot reads either one.

## 2. Run it
You need Python 3.9 or newer. SearchBot uses only Python's standard library.

```bash
python searchbot.py "WhatsApp Chat with Closing Reports.zip"
```

Options:

| Option | Meaning |
|---|---|
| `--min 1500` | only sales above $1500 (default: 1000) |
| `--out big_customers` | name for the output files |
| `--keyword closing` | only read messages that contain the word "closing" |
| `--sender Mike` | only read reports sent by Mike |
| `--since 1/1/24` | only read messages from this date on (write the date the way your export does) |

## 3. Output files
- `good_customers_customers.csv`: one row per customer, with name, phone, address, number of jobs, total spent, biggest sale and last job date. The biggest spenders come first.
- `good_customers_sales.csv`: one row per qualifying job, with the full report text.
- `good_customers.xlsx`: both lists as two sheets in one Excel file. This file is only created if `openpyxl` is installed (`pip install openpyxl`).

The CSV files open directly in Excel or Google Sheets. Hebrew text and emoji display correctly.

## How it reads a report
- **Amount:** SearchBot first looks for a number on a line that starts with Total / Sale / Paid / Amount / Price / סה"כ / סכום / מחיר / שולם. If there is no such line, it takes the largest amount written with a currency sign (`$4,850`, `1200$`, `1,500 USD`, `2.5k$`, `₪3,200`). Plain numbers with no label and no currency sign are skipped, so phone numbers and street numbers are not counted as sales.
- **Customer:** read from `Customer:` / `Client:` / `Name:` / `לקוח:` / `שם:`.
- **Phone and address:** read from `Phone:` / `טלפון:` and `Address:` / `כתובת:`. If there is no `Phone:` label, SearchBot uses the first phone-like number in the message.
- **Repeat customers:** jobs with the same phone number (or, if there is no phone, the same name) are combined into one customer.

If your team labels reports with other words, add those words to the lists at the top of `searchbot.py` (`TOTAL_LABELS`, `CUSTOMER_LABELS` and so on).

## Try it
```bash
python searchbot.py examples/sample_chat.txt
python -m unittest discover -s tests -t .
```
