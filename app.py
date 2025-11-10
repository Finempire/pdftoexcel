# app.py
"""
Streamlit app: Bank statement PDF -> Excel
Clean version - Shows bank selection, file upload and download button
"""
import io
import re
from typing import List, Dict, Optional

import streamlit as st
import pandas as pd
import pdfplumber
from pdf2image import convert_from_bytes
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import pytesseract

st.set_page_config(page_title="Bank PDF → Excel", layout="wide")
st.title("Bank Statement Converter")

# Create three columns at the top
col1, col2, col3 = st.columns(3)

with col1:
    bank_name = st.selectbox(
        "Select Bank",
        ["Kotak Mahindra Bank", "HDFC Bank", "ICICI Bank", "State Bank of India", "Axis Bank", "Other Bank"]
    )

with col2:
    uploaded = st.file_uploader("Upload bank statement PDF", type=["pdf"])

with col3:
    if uploaded:
        st.markdown("### Download")
    else:
        st.markdown("### Download")
        st.info("Upload PDF first")

if not uploaded:
    st.info("Please upload a PDF file to begin conversion.")
    st.stop()

pdf_bytes = uploaded.read()

# ---------- Common extraction helpers ----------
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

def preprocess_image_for_ocr(pil_img: Image.Image, sharpen: bool = True, contrast: float = 1.6, threshold: int = 150) -> Image.Image:
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
    im = im.point(lambda x: 0 if x < threshold else 255, mode='1')
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
            txt = pytesseract.image_to_string(proc, lang='eng', config="--psm 6")
        except Exception:
            txt = ""
        page_lines = [l.strip() for l in txt.splitlines() if l.strip()]
        lines.extend(page_lines)
    return lines

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

# ---------- Kotak Bank Parser ----------
if bank_name == "Kotak Mahindra Bank":
    # Regex patterns for Kotak Bank
    DATE_LINE_RE = re.compile(r'^\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s[A-Za-z]{3}\s\d{4})\s+')
    AMOUNT_TAG_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))\s*\(?\s*(Dr|Cr)\s*\)?', re.IGNORECASE)
    AMOUNT_PLAIN_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))')
    CHEQUE_REF_RE = re.compile(r'\b(IMPS|UPI|TBMS|NEFT|RTGS|Chq|Cheque|Ref|Reference)[-/]?[A-Za-z0-9]+\b', re.IGNORECASE)

    def parse_record(rec: str) -> Optional[Dict[str, str]]:
        m = DATE_LINE_RE.match(rec)
        if not m:
            return None
        date = m.group(1).strip()
        rest = rec[m.end():].strip()
        
        # चेक रेफरेंस को पूरी तरह से हटाएं
        rest_clean = CHEQUE_REF_RE.sub('', rest)
        
        amount_tags = AMOUNT_TAG_RE.findall(rest_clean)
        if not amount_tags:
            plain_amounts = AMOUNT_PLAIN_RE.findall(rest_clean)
            if len(plain_amounts) >= 2:
                txn_amt = plain_amounts[-2].replace(' ', '').replace(',', '')
                bal_amt = plain_amounts[-1].replace(' ', '').replace(',', '')
                narration = AMOUNT_PLAIN_RE.sub('', rest_clean).strip()
                narration = narration.strip(' ,;-')
                return {"Date": date, "Narration": narration, "Debit": txn_amt, "Credit": "", "Balance": bal_amt}
            else:
                narration = rest_clean
                return {"Date": date, "Narration": narration, "Debit": "", "Credit": "", "Balance": ""}
        
        normalized = [(a.replace(' ', '').replace(',', ''), t.lower()) for a, t in amount_tags]
        txn_amt, txn_tag = normalized[0]
        if len(normalized) >= 2:
            bal_amt, bal_tag = normalized[-1]
        else:
            bal_amt = ""
        debit = txn_amt if txn_tag == 'dr' else ""
        credit = txn_amt if txn_tag == 'cr' else ""
        
        narration = AMOUNT_TAG_RE.sub('', rest_clean).strip()
        narration = narration.strip(' ,;-')
        
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

# ---------- HDFC Bank Parser ----------
elif bank_name == "HDFC Bank":
    # Regex patterns for HDFC Bank
    DATE_LINE_RE = re.compile(r'^\s*(\d{1,2}/\d{1,2}/\d{2,4})\s+')
    AMOUNT_RE = re.compile(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2}))')
    VALUE_DT_RE = re.compile(r'\s(\d{1,2}/\d{1,2}/\d{2,4})\s*$')
    # HDFC के लिए withdrawal और deposit amounts को अलग-अलग पहचानने के लिए
    WITHDRAWAL_RE = re.compile(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2}))\s+(?:\d{1,3}(?:,\d{3})*(?:\.\d{2}))\s+(\d{1,3}(?:,\d{3})*(?:\.\d{2}))')

    def parse_record(rec: str) -> Optional[Dict[str, str]]:
        m = DATE_LINE_RE.match(rec)
        if not m:
            return None
        date = m.group(1).strip()
        rest = rec[m.end():].strip()

        # Remove trailing 'value date' if present
        rest = VALUE_DT_RE.sub('', rest).strip()

        # Find all amounts
        amounts = AMOUNT_RE.findall(rest)
        amounts_norm = [a.replace(',', '') for a in amounts]

        # Remove amounts from narration
        narration = AMOUNT_RE.sub('', rest).strip()

        # Remove trailing numeric ref tokens
        narration = re.sub(r'\b\d{6,}\b', '', narration).strip()
        narration = narration.strip(' ,;-')

        debit = ""
        credit = ""
        balance = ""

        # HDFC statement format: Date | Narration | Withdrawal | Deposit | Balance
        if len(amounts_norm) >= 3:
            # Typically: Withdrawal, Deposit, Balance
            debit = amounts_norm[0] if amounts_norm[0] != '' else ""
            credit = amounts_norm[1] if amounts_norm[1] != '' else ""
            balance = amounts_norm[2] if len(amounts_norm) > 2 else ""
        elif len(amounts_norm) == 2:
            # Check if it's withdrawal + balance or deposit + balance
            # For HDFC, usually first is withdrawal, second is balance (if credit is empty)
            # Or first is deposit, second is balance (if debit is empty)
            # We'll check the context
            if 'withdrawal' in rest.lower() or 'w/d' in rest.lower():
                debit = amounts_norm[0]
                balance = amounts_norm[1]
            elif 'deposit' in rest.lower() or 'dep' in rest.lower():
                credit = amounts_norm[0]
                balance = amounts_norm[1]
            else:
                # Assume first is transaction amount, second is balance
                # Leave both debit and credit blank for manual correction
                balance = amounts_norm[1]
        elif len(amounts_norm) == 1:
            balance = amounts_norm[0]

        return {
            "Date": date,
            "Narration": narration,
            "Debit": debit,
            "Credit": credit,
            "Balance": balance
        }

    def parse_records(records: List[str]) -> pd.DataFrame:
        parsed = []
        for r in records:
            p = parse_record(r)
            if p:
                parsed.append(p)
        df = pd.DataFrame(parsed, columns=["Date", "Narration", "Debit", "Credit", "Balance"])
        df = df.fillna("").astype(str)
        return df

# ---------- Other Banks (Generic Parser) ----------
else:
    # Generic regex patterns for other banks
    DATE_LINE_RE = re.compile(r'^\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s[A-Za-z]{3}\s\d{4})\s+')
    AMOUNT_TAG_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))\s*\(?\s*(Dr|Cr)\s*\)?', re.IGNORECASE)
    AMOUNT_PLAIN_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))')

    def parse_record(rec: str) -> Optional[Dict[str, str]]:
        m = DATE_LINE_RE.match(rec)
        if not m:
            return None
        date = m.group(1).strip()
        rest = rec[m.end():].strip()
        
        amount_tags = AMOUNT_TAG_RE.findall(rest)
        if not amount_tags:
            plain_amounts = AMOUNT_PLAIN_RE.findall(rest)
            if len(plain_amounts) >= 2:
                txn_amt = plain_amounts[-2].replace(' ', '').replace(',', '')
                bal_amt = plain_amounts[-1].replace(' ', '').replace(',', '')
                narration = AMOUNT_PLAIN_RE.sub('', rest).strip()
                narration = narration.strip(' ,;-')
                return {"Date": date, "Narration": narration, "Debit": txn_amt, "Credit": "", "Balance": bal_amt}
            else:
                narration = rest
                return {"Date": date, "Narration": narration, "Debit": "", "Credit": "", "Balance": ""}
        
        normalized = [(a.replace(' ', '').replace(',', ''), t.lower()) for a, t in amount_tags]
        txn_amt, txn_tag = normalized[0]
        if len(normalized) >= 2:
            bal_amt, bal_tag = normalized[-1]
        else:
            bal_amt = ""
        debit = txn_amt if txn_tag == 'dr' else ""
        credit = txn_amt if txn_tag == 'cr' else ""
        
        narration = AMOUNT_TAG_RE.sub('', rest).strip()
        narration = narration.strip(' ,;-')
        
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

# ---------- Main flow ----------
# Attempt text extraction
lines = text_from_pdf_bytes(pdf_bytes)

if len(lines) < 8:
    lines = ocr_pdf_bytes(pdf_bytes, dpi=350, max_pages=None)

records = group_lines_into_records(lines)
df = parse_records(records)

if df.empty:
    st.error("No transactions parsed. Please try with a different PDF.")
    st.stop()

# Automatically convert amounts to numeric for download
df_download = df.copy()
for col in ["Debit", "Credit", "Balance"]:
    if col in df_download.columns:
        df_download[col] = df_download[col].replace(r'^\s*$', None, regex=True)
        df_download[col] = df_download[col].str.replace(',', '', regex=False).str.replace(' ', '', regex=False)
        df_download[col] = pd.to_numeric(df_download[col], errors='coerce')

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
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# Show parsed dataframe preview below the three columns
st.markdown("---")
st.subheader("Excel Preview")
st.dataframe(df)
