"""
Streamlit app: Bank statement PDF -> Excel
Bank-specific parsers with Bank of Baroda support.
"""
import io
import re
import tempfile
from pathlib import Path
from typing import List, Dict, Optional

import pandas as pd
import tabula
import pdfplumber
import pytesseract
import streamlit as st
from pdf2image import convert_from_bytes
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


st.set_page_config(page_title="Bank PDF → Excel", layout="wide")
st.title("Bank Statement Converter")

# Create three columns at the top
col1, col2, col3 = st.columns(3)


with col1:
    bank_name = st.selectbox(
        "Select Bank",
        [
            "Kotak Mahindra Bank",
            "HDFC Bank",
            "ICICI Bank",
            "State Bank of India",
            "Axis Bank",
            "Bank of Baroda",
            "Other Bank",
        ],
    )

with col2:
    uploaded = st.file_uploader("Upload bank statement PDF", type=["pdf"])

with col3:
    st.markdown("### Download")
    if not uploaded:
        st.info("Upload PDF first")

if not uploaded:
    st.info("Please upload a PDF file to begin conversion.")
    st.stop()

pdf_bytes = uploaded.read()

# Regex patterns for Kotak Bank and generic fallbacks
DATE_LINE_RE = re.compile(r"^\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s[A-Za-z]{3}\s\d{4})\s+")
AMOUNT_TAG_RE = re.compile(r"(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))\s*\(?\s*(Dr|Cr)\s*\)?", re.IGNORECASE)
AMOUNT_PLAIN_RE = re.compile(r"(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))")
CHEQUE_REF_RE = re.compile(r"\b(IMPS|UPI|TBMS|NEFT|RTGS|Chq|Cheque|Ref|Reference)[-/]?[A-Za-z0-9]+\b", re.IGNORECASE)


def _clean_amount(val: Optional[str]) -> str:
    """Normalize amount strings for debit/credit/balance columns."""
    if not val:
        return ""
    cleaned = str(val).replace(",", "").replace(" ", "").replace("₹", "").strip()
    # Handle brackets as negative
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return cleaned


# ---------- extraction helpers ----------
def text_from_pdf_bytes(pdf_bytes: bytes) -> List[str]:
    lines: List[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                for ln in txt.splitlines():
                    ln = ln.rstrip()
                    if ln:
                        lines.append(ln)
    except Exception:
        pass
    return lines


def preprocess_image_for_ocr(
    pil_img: Image.Image, sharpen: bool = True, contrast: float = 1.6, threshold: int = 150
) -> Image.Image:
    im = pil_img.convert("L")
    if im.size[0] < 1000:
        im = im.resize((int(im.size[0] * 1.5), int(im.size[1] * 1.5)), Image.BILINEAR)
    try:
        enhancer = ImageEnhance.Contrast(im)
        im = enhancer.enhance(contrast)
    except Exception:
        pass
    if sharpen:
        im = im.filter(ImageFilter.SHARPEN)
    im = ImageOps.autocontrast(im, cutoff=1)
    im = im.point(lambda x: 0 if x < threshold else 255, mode="1")
    im = im.convert("L")
    return im


def ocr_pdf_bytes(pdf_bytes: bytes, dpi: int = 300, max_pages: Optional[int] = None) -> List[str]:
    lines: List[str] = []
    try:
        images = convert_from_bytes(pdf_bytes, dpi=dpi)
    except Exception:
        return lines
    if max_pages:
        images = images[:max_pages]
    for img in images:
        proc = preprocess_image_for_ocr(img, sharpen=True, contrast=1.6, threshold=150)
        try:
            txt = pytesseract.image_to_string(proc, lang="eng", config="--psm 6")
        except Exception:
            txt = ""
        page_lines = [l.strip() for l in txt.splitlines() if l.strip()]
        lines.extend(page_lines)
    return lines


# ---------- generic line-based parser (Kotak and fallback) ----------
def group_lines_into_records(lines: List[str]) -> List[str]:
    records: List[str] = []
    for ln in lines:
        if DATE_LINE_RE.match(ln):
            records.append(ln.strip())
        else:
            if records:
                records[-1] = records[-1] + " " + ln.strip()
            else:
                records.append(ln.strip())
    return records


def parse_record(rec: str) -> Optional[Dict[str, str]]:
    m = DATE_LINE_RE.match(rec)
    if not m:
        return None
    date = m.group(1).strip()
    rest = rec[m.end() :].strip()

    # Remove cheque/reference values to keep narration readable
    rest_clean = CHEQUE_REF_RE.sub("", rest)

    amount_tags = AMOUNT_TAG_RE.findall(rest_clean)
    if not amount_tags:
        plain_amounts = AMOUNT_PLAIN_RE.findall(rest_clean)
        if len(plain_amounts) >= 2:
            txn_amt = plain_amounts[-2].replace(" ", "").replace(",", "")
            bal_amt = plain_amounts[-1].replace(" ", "").replace(",", "")
            narration = AMOUNT_PLAIN_RE.sub("", rest_clean).strip()
            narration = narration.strip(" ,;-")
            return {
                "Date": date,
                "Narration": narration,
                "Debit": txn_amt,
                "Credit": "",
                "Balance": bal_amt,
            }
        narration = rest_clean
        return {"Date": date, "Narration": narration, "Debit": "", "Credit": "", "Balance": ""}

    normalized = [(a.replace(" ", "").replace(",", ""), t.lower()) for a, t in amount_tags]
    txn_amt, txn_tag = normalized[0]
    if len(normalized) >= 2:
        bal_amt = normalized[-1][0]
    else:
        bal_amt = ""
    debit = txn_amt if txn_tag == "dr" else ""
    credit = txn_amt if txn_tag == "cr" else ""

    narration = AMOUNT_TAG_RE.sub("", rest_clean).strip()
    narration = narration.strip(" ,;-")

    return {"Date": date, "Narration": narration, "Debit": debit, "Credit": credit, "Balance": bal_amt}


def parse_records(records: List[str]) -> pd.DataFrame:
    parsed = []
    for r in records:
        p = parse_record(r)
        if p:
            parsed.append(p)
    df = pd.DataFrame(parsed, columns=["Date", "Narration", "Debit", "Credit", "Balance"])
    df = df.fillna("").astype(str)
    return df

# ---------- Tabula extraction ----------
def extract_tabula_tables(pdf_bytes: bytes) -> List[pd.DataFrame]:
    """Extract tables from PDF using tabula and return list of DataFrames."""
    tables: List[pd.DataFrame] = []
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)
        try:
            tables = tabula.read_pdf(
                str(tmp_path), pages="all", multiple_tables=True, lattice=True
            )
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    except Exception:
        pass
    return tables or []


def tabula_tables_to_lines(tables: List[pd.DataFrame]) -> List[str]:
    lines: List[str] = []
    for tbl in tables:
        try:
            df = tbl.fillna("")
            for _, row in df.iterrows():
                row_values = [str(v).strip() for v in row.tolist() if str(v).strip()]
                if row_values:
                    lines.append(" ".join(row_values))
        except Exception:
            continue
    return lines


def parse_tabula_structured(tables: List[pd.DataFrame]) -> pd.DataFrame:
    """Attempt to build a structured dataframe directly from tabula tables."""

    def find_idx(header: List[str], options: List[str]) -> Optional[int]:
        for i, col in enumerate(header):
            for opt in options:
                if opt in col:
                    return i
        return None

    rows: List[Dict[str, str]] = []

    for tbl in tables:
        try:
            df = tbl.fillna("")
        except Exception:
            continue
        if df.empty:
            continue

        # Try both column names and first row as header hints
        header_candidates: List[List[str]] = [
            [str(c or "").strip().lower() for c in df.columns]
        ]
        first_row = [str(v).strip().lower() for v in df.iloc[0].tolist()]
        header_candidates.append(first_row)

        data_rows = df
        for header in header_candidates:
            date_idx = find_idx(header, ["date"])
            narration_idx = find_idx(header, ["narration", "particulars", "description"])
            debit_idx = find_idx(header, ["debit", "withdrawal"])
            credit_idx = find_idx(header, ["credit", "deposit"])
            balance_idx = find_idx(header, ["balance", "bal", "running bal", "closing bal"])
            ref_idx = find_idx(header, ["ref", "chq", "cheque", "utr", "utr no", "ch no", "chq no"])

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

    if rows:
        df = pd.DataFrame(rows, columns=["Date", "Narration", "Debit", "Credit", "Balance"])
        df = df.fillna("").astype(str)
        return df

    # Fallback to generic parsing if table extraction fails
    lines = text_from_pdf_bytes(pdf_bytes)
    if len(lines) < 8:
        lines = ocr_pdf_bytes(pdf_bytes, dpi=350, max_pages=None)
    records = group_lines_into_records(lines)
    return parse_records(records)


# ---------- Parser dispatcher ----------
def parse_pdf_by_bank(bank: str, pdf_bytes: bytes) -> pd.DataFrame:
    tables = extract_tabula_tables(pdf_bytes)

    # Prefer structured tabula output whenever columns are detected
    structured_df = parse_tabula_structured(tables)
    if not structured_df.empty:
        return structured_df

    if bank == "Bank of Baroda":
        bob_df = parse_bank_of_baroda(pdf_bytes)
        if not bob_df.empty:
            return bob_df

    # Generic fallback: use tabula lines, then text, then OCR
    lines = tabula_tables_to_lines(tables)
    if len(lines) < 5:
        lines = text_from_pdf_bytes(pdf_bytes)
    if len(lines) < 8:
        lines = ocr_pdf_bytes(pdf_bytes, dpi=350, max_pages=None)
    records = group_lines_into_records(lines)
    return parse_records(records)


# ---------- Main flow ----------
df = parse_pdf_by_bank(bank_name, pdf_bytes)

if df.empty:
    st.error("No transactions parsed. Please try with a different PDF.")
    st.stop()

# Automatically convert amounts to numeric for download
df_download = df.copy()
for col in ["Debit", "Credit", "Balance"]:
    if col in df_download.columns:
        df_download[col] = df_download[col].replace(r"^\s*$", None, regex=True)
        df_download[col] = df_download[col].str.replace(",", "", regex=False).str.replace(" ", "", regex=False)
        df_download[col] = pd.to_numeric(df_download[col], errors="coerce")


# Download function for Excel
def to_excel_bytes(dff: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        dff.to_excel(writer, index=False, sheet_name="Statement")
    return out.getvalue()


# Download button in the third column
with col3:
    st.download_button(
        "Download Excel",
        data=to_excel_bytes(df_download),
        file_name=f"{bank_name.replace(' ', '_')}_{uploaded.name.replace('.pdf', '.xlsx')}",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# Show parsed dataframe preview below the three columns
st.markdown("---")
st.subheader("Excel Preview")
st.dataframe(df)
