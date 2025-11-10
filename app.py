# app.py
import io
import re
from typing import List, Dict, Optional
import tempfile

import streamlit as st
import pandas as pd
import pdfplumber
from pdf2image import convert_from_bytes
from PIL import Image, ImageOps, ImageFilter
import pytesseract
import cv2
import numpy as np

st.set_page_config(page_title="Bank PDF → Excel (with OCR fallback)", layout="wide")
st.title("Bank PDF → Excel — text + OCR parser")
st.write("Upload a bank statement PDF (digital or scanned). The app will try text extraction first, then OCR if needed.")

uploaded = st.file_uploader("Upload bank statement PDF", type=["pdf"])
if not uploaded:
    st.info("Upload a PDF to begin.")
    st.stop()

pdf_bytes = uploaded.read()
st.sidebar.markdown(f"**Filename:** {uploaded.name} — {len(pdf_bytes)//1024} KB")

# ---------- Regexes & helpers ----------
DATE_LINE_RE = re.compile(r'^\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s[A-Za-z]{3}\s\d{4})\s+')
AMOUNT_TAG_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))\s*\(?\s*(Dr|Cr)\s*\)?', re.IGNORECASE)
AMOUNT_PLAIN_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))')

def text_from_pdf_bytes(pdf_bytes: bytes) -> List[str]:
    """Extract text lines using pdfplumber. Returns list of non-empty lines."""
    lines = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for ln in text.splitlines():
                    ln = ln.rstrip()
                    if ln:
                        lines.append(ln)
    except Exception as e:
        st.sidebar.error(f"pdfplumber error: {e}")
    return lines

# ---------- OCR helpers ----------
def preprocess_image_for_ocr(pil_img: Image.Image) -> Image.Image:
    """Basic preprocessing: grayscale, bilateral filter, adaptive thresholding, and optional deskew."""
    # Convert to OpenCV image
    img = np.array(pil_img.convert('RGB'))
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    # Denoise (bilateral preserves edges)
    denoised = cv2.bilateralFilter(gray, d=9, sigmaColor=75, sigmaSpace=75)

    # Adaptive threshold (works well for uneven lighting)
    th = cv2.adaptiveThreshold(denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 31, 15)

    # Optional morphological opening to remove small noise
    kernel = np.ones((1, 1), np.uint8)
    opened = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel)

    # Convert back to PIL
    proc = Image.fromarray(opened)
    return proc

def ocr_pdf_bytes(pdf_bytes: bytes, dpi: int = 300, max_pages: Optional[int]=None) -> List[str]:
    """Convert PDF pages to images and OCR each page, returning lines."""
    lines = []
    try:
        images = convert_from_bytes(pdf_bytes, dpi=dpi)
    except Exception as e:
        st.error(f"pdf2image error: {e}")
        return lines

    if max_pages:
        images = images[:max_pages]

    for i, img in enumerate(images):
        # Simple preview of first page in sidebar
        if i == 0:
            st.sidebar.image(img.resize((300, int(300 * img.height / img.width))), caption="Page 1 preview (for OCR)")

        proc = preprocess_image_for_ocr(img)
        # Use Tesseract to get text
        txt = pytesseract.image_to_string(proc, lang='eng')
        # split into non-empty lines
        page_lines = [l.strip() for l in txt.splitlines() if l.strip()]
        # append with a page delimiter optionally
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

# ---------- Main flow: try text extraction -> OCR fallback ----------
st.info("Attempting text-extraction (fast). If nothing found, will run OCR (slower).")
lines = text_from_pdf_bytes(pdf_bytes)
if len(lines) >= 8:
    st.success(f"pdfplumber extracted {len(lines)} lines; using text extraction.")
    st.subheader("Sample extracted lines")
    st.write(lines[:30])
    used_method = "text"
else:
    st.warning("pdfplumber found little or no text — running OCR fallback (this may take longer).")
    ocr_lines = ocr_pdf_bytes(pdf_bytes, dpi=300, max_pages=None)
    st.subheader("Sample OCR lines (first 80)")
    st.write(ocr_lines[:80])
    lines = ocr_lines
    used_method = "ocr"

records = group_lines_into_records(lines)
st.subheader(f"Grouped into {len(records)} candidate records (first 40 shown)")
st.write(records[:40])

df = parse_records(records)
if df.empty:
    st.error("No transactions parsed. If your PDF is scanned and still fails, try increasing DPI, or upload a sample page for me to tune preprocessing.")
    st.stop()

st.success(f"Parsed {len(df)} transactions using method: {used_method.upper()}.")
st.subheader("Parsed Data (editable)")
edited = st.experimental_data_editor(df, num_rows="dynamic")

# Convert numeric columns if possible (optional: user can toggle)
def try_convert_amounts(dff: pd.DataFrame) -> pd.DataFrame:
    for col in ["Debit", "Credit", "Balance"]:
        if col in dff.columns:
            # strip commas/spaces then try to convert; leave blank as NaN
            dff[col] = dff[col].replace(r'^\s*$', None, regex=True)
            dff[col] = dff[col].str.replace(',', '').str.replace(' ', '')
            dff[col] = pd.to_numeric(dff[col], errors='coerce')
    return dff

if st.checkbox("Convert debit/credit/balance to numeric types (recommended)"):
    edited_conv = try_convert_amounts(edited.copy())
else:
    edited_conv = edited

# Provide Excel download
def to_excel_bytes(dff: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        dff.to_excel(writer, index=False, sheet_name="Statement")
    return out.getvalue()

st.download_button("Download parsed data as Excel", data=to_excel_bytes(edited_conv),
                   file_name=uploaded.name.replace(".pdf", ".xlsx"),
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.markdown("#### Notes / tips")
st.markdown(
    """
- OCR fallback uses preprocessing (grayscale → denoise → adaptive threshold). If OCR errors occur, try different DPI or improve lighting/scan quality.
- If your bank uses a consistent layout, I can tune column-splitting by x-coordinates for higher accuracy.
- For large batches or sensitive data, consider running locally (this app works fully on your machine).
"""
)
