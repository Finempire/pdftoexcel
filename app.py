"""
Streamlit app: Advanced Bank Statement Parser and Excel exporter.
"""
import io
import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from bob_parser import BOBStatementParser


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

            if date_idx is None or narration_idx is None:
                continue

            # If the header came from the first row, skip that row when reading data
            start_index = 1 if header is first_row else 0
            for _, row in data_rows.iloc[start_index:].iterrows():
                cells = row.tolist()
                if max(date_idx, narration_idx) >= len(cells):
                    continue
                date = (cells[date_idx] or "").strip()
                narration = (cells[narration_idx] or "").strip()
                debit = _clean_amount(cells[debit_idx]) if debit_idx is not None and debit_idx < len(cells) else ""
                credit = _clean_amount(cells[credit_idx]) if credit_idx is not None and credit_idx < len(cells) else ""
                balance = _clean_amount(cells[balance_idx]) if balance_idx is not None and balance_idx < len(cells) else ""
                ref_val = (cells[ref_idx] or "").strip() if ref_idx is not None and ref_idx < len(cells) else ""

                if date.lower() == "date":
                    continue

                full_narration = narration
                if ref_val:
                    full_narration = (
                        f"{narration} (Ref: {ref_val})" if narration else f"Ref: {ref_val}"
                    )

                if any([date, full_narration, debit, credit, balance]):
                    rows.append(
                        {
                            "Date": date,
                            "Narration": full_narration,
                            "Debit": debit,
                            "Credit": credit,
                            "Balance": balance,
                        }
                    )

            # break candidate loop if we successfully mapped columns
            if rows:
                break

    if rows:
        df_rows = pd.DataFrame(rows, columns=["Date", "Narration", "Debit", "Credit", "Balance"])
        return df_rows.fillna("").astype(str)

    return pd.DataFrame(columns=["Date", "Narration", "Debit", "Credit", "Balance"])


# ---------- Bank of Baroda table parser ----------
def parse_bank_of_baroda_advanced(pdf_bytes: bytes) -> pd.DataFrame:
    """Parse Bank of Baroda statements using the dedicated parser."""
    try:
        parser = BOBStatementParser(pdf_bytes)
        result = parser.parse()
    except Exception:
        return pd.DataFrame(columns=["Date", "Narration", "Debit", "Credit", "Balance"])

    df = result.get("transactions_df", pd.DataFrame())
    if df is None or df.empty:
        return pd.DataFrame(columns=["Date", "Narration", "Debit", "Credit", "Balance"])

    df = df.rename(columns={"Description": "Narration"})
    df = df[["Date", "Narration", "Debit", "Credit", "Balance"]]
    df = df.fillna("").astype(str)
    return df


def parse_bank_of_baroda(pdf_bytes: bytes) -> pd.DataFrame:
    rows: List[Dict[str, str]] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    header = [(cell or "").strip().lower() for cell in table[0]]

                    def find_idx(options: List[str]) -> Optional[int]:
                        for i, col in enumerate(header):
                            for opt in options:
                                if opt in col:
                                    return i
                        return None

                    date_idx = find_idx(["date"])
                    narration_idx = find_idx(["narration", "particulars", "description"])
                    chq_idx = find_idx(["chq", "cheque", "ch no", "chq no", "chq no."])
                    debit_idx = find_idx(["debit", "withdrawal"])
                    credit_idx = find_idx(["credit", "deposit"])
                    balance_idx = find_idx(["balance", "bal"])

                    if date_idx is None or narration_idx is None:
                        continue

                    for row in table[1:]:
                        if not row or max(date_idx, narration_idx) >= len(row):
                            continue
                        date = (row[date_idx] or "").strip()
                        narration = (row[narration_idx] or "").strip()
                        chq_val = (row[chq_idx] or "").strip() if chq_idx is not None and chq_idx < len(row) else ""
                        debit = _clean_amount(row[debit_idx]) if debit_idx is not None and debit_idx < len(row) else ""
                        credit = _clean_amount(row[credit_idx]) if credit_idx is not None and credit_idx < len(row) else ""
                        balance = _clean_amount(row[balance_idx]) if balance_idx is not None and balance_idx < len(row) else ""

                        # Skip header-like repeats
                        if date.lower() == "date":
                            continue

                        full_narration = narration
                        if chq_val:
                            full_narration = f"{narration} (Chq/Ref: {chq_val})" if narration else f"Chq/Ref: {chq_val}"

                        if any([date, full_narration, debit, credit, balance]):
                            rows.append(
                                {
                                    "Date": date,
                                    "Narration": full_narration,
                                    "Debit": debit,
                                    "Credit": credit,
                                    "Balance": balance,
                                }
                            )
    except Exception:
        rows = []

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

    if bank == "Bank of Baroda":
        advanced_bob_df = parse_bank_of_baroda_advanced(pdf_bytes)
        if not advanced_bob_df.empty:
            return advanced_bob_df

        bob_df = parse_bank_of_baroda(pdf_bytes)
        if not bob_df.empty:
            return bob_df

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
