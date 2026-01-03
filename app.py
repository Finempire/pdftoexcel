"""
Streamlit app: Advanced Bank Statement Parser and Excel exporter.
"""
import io
import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from bank_parser import BankStatementParser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Bank PDF → Excel", layout="wide")
st.title("Bank Statement Converter")

# Sidebar and configuration
st.sidebar.header("Parser Settings")
bank_choice = st.sidebar.selectbox(
    "Select Bank (or Auto Detect)",
    [
        "Auto Detect",
        "Chase",
        "Bank of America",
        "Wells Fargo",
        "Citibank",
        "HDFC Bank",
        "ICICI Bank",
        "State Bank of India",
        "Axis Bank",
        "Other",
    ],
)

with st.sidebar.expander("Export Options"):
    export_format = st.selectbox("Format", ["Excel", "CSV", "JSON"])

with st.sidebar.expander("OCR"):
    use_ocr = st.checkbox("Force OCR fallback", value=False)

uploaded = st.file_uploader("Upload bank statement PDF", type=["pdf"])
if not uploaded:
    st.info("Please upload a PDF file to begin conversion.")
    st.stop()

pdf_bytes = uploaded.read()

bank_key: Optional[str] = None
if bank_choice != "Auto Detect":
    bank_key = bank_choice.lower().replace(" ", "_") if bank_choice != "Other" else None

parser = BankStatementParser(bank_key)

if use_ocr:
    parser.bank_name = parser.bank_name or parser.detect_bank(pdf_bytes)

with st.spinner("Parsing statement..."):
    result = parser.parse(pdf_bytes)

if not parser.validate_data(result):
    st.error("Could not validate parsed data. Please try a different PDF or bank selection.")
    st.stop()

st.success(f"Parsed {len(result.transactions)} transactions for bank: {result.metadata.get('bank_name', 'unknown')}")
st.caption(f"Extraction confidence: {result.confidence * 100:.0f}%")

# Metadata display
meta_col1, meta_col2, meta_col3 = st.columns(3)
meta_col1.metric("Account Holder", result.metadata.get("account_holder") or "N/A")
meta_col1.metric("Account Number", result.metadata.get("account_number") or "N/A")
meta_col2.metric("Statement From", result.metadata.get("statement_period", {}).get("from") or "N/A")
meta_col2.metric("Statement To", result.metadata.get("statement_period", {}).get("to") or "N/A")
meta_col3.metric("Opening Balance", result.metadata.get("opening_balance") or "N/A")
meta_col3.metric("Closing Balance", result.metadata.get("closing_balance") or "N/A")

# Data cleaning for download
transactions = result.transactions.copy()
for col in ["debit", "credit", "balance"]:
    if col in transactions.columns:
        transactions[col] = pd.to_numeric(transactions[col], errors="coerce")


def to_excel_bytes(dff: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        dff.to_excel(writer, index=False, sheet_name="Statement")
    return out.getvalue()


def to_json_bytes(dff: pd.DataFrame) -> bytes:
    return dff.to_json(orient="records", force_ascii=False).encode("utf-8")


def to_csv_bytes(dff: pd.DataFrame) -> bytes:
    return dff.to_csv(index=False).encode("utf-8")


if export_format == "Excel":
    data_bytes = to_excel_bytes(transactions)
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    filename = uploaded.name.replace(".pdf", ".xlsx")
elif export_format == "CSV":
    data_bytes = to_csv_bytes(transactions)
    mime = "text/csv"
    filename = uploaded.name.replace(".pdf", ".csv")
else:
    data_bytes = to_json_bytes(transactions)
    mime = "application/json"
    filename = uploaded.name.replace(".pdf", ".json")

st.download_button(
    f"Download {export_format}",
    data=data_bytes,
    file_name=filename,
    mime=mime,
)

st.markdown("---")
st.subheader("Parsed Transactions")
st.dataframe(transactions)

st.markdown("---")
st.subheader("Raw Metadata")
st.json(result.metadata)
