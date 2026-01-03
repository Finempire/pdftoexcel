import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bank_parser import BankStatementParser


def test_metadata_extraction_hdfc():
    parser = BankStatementParser("hdfc")
    sample_text = (
        "HDFC Bank Statement Period: 01-01-2024 to 31-01-2024\n"
        "Customer Name: John Doe\n"
        "Account No: 123456789012\n"
        "Opening Balance: ₹1,000.00 Closing Balance: ₹1,500.00\n"
        "IFSC Code: HDFC0000123"
    )
    meta = parser._extract_metadata_from_text("hdfc", sample_text)
    assert meta["account_holder"] == "John Doe"
    assert meta["account_number"] == "123456789012"
    assert meta["statement_period"]["from"] == "2024-01-01"
    assert meta["statement_period"]["to"] == "2024-01-31"
    assert meta["opening_balance"] == 1000.0
    assert meta["closing_balance"] == 1500.0


def test_transaction_post_processing():
    parser = BankStatementParser("axis")
    df = pd.DataFrame(
        {
            "date": ["01/01/2024", "02/01/2024"],
            "description": ["ATM Withdrawal", "UPI Payment"],
            "debit": ["1,000.00", "500.00"],
            "credit": ["", ""],
            "balance": ["5,000.00", "4,500.00"],
            "reference": ["ABC123", "XYZ789"],
        }
    )
    processed = parser._post_process_transactions(df)
    assert processed.loc[0, "date"] == "2024-01-01"
    assert processed.loc[1, "category"] == "Online"
    assert processed.loc[0, "debit"] == 1000.0
    assert processed.loc[1, "balance"] == 4500.0


def test_bank_detection_matches_keywords():
    parser = BankStatementParser()
    pdf_text = """Wells Fargo Statement
Account Number: 123456789
Statement Period: 01/01/2024 through 01/31/2024
"""
    parser.bank_name = None
    detected = parser._extract_metadata_from_text("wells_fargo", pdf_text)
    assert detected["statement_period"]["from"] == "2024-01-01"
    assert detected["statement_period"]["to"] == "2024-01-31"


def test_amount_parsing_handles_brackets():
    parser = BankStatementParser()
    assert parser._parse_amount("(1,200.00)") == -1200.0


def test_generic_fallback_patterns():
    parser = BankStatementParser("generic")
    sample_text = """My Custom Bank\nAccount Number: ABC12345\nStatement 01/01/24 to 31/01/24\nOpening Balance: 100.00\nClosing Balance: 500.00"""
    meta = parser._extract_metadata_from_text("generic", sample_text)
    assert meta["account_number"] == "ABC12345"
    assert meta["statement_period"]["from"] == "2024-01-01"
    assert meta["opening_balance"] == 100.0
