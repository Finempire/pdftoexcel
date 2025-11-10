# app.py
"""
Streamlit app: Bank statement PDF -> Excel
Text-extraction first (pdfplumber). If not enough extracted text, uses OCR fallback.
This version uses Pillow-only preprocessing (no OpenCV) so it works on Python versions
without opencv-python wheels.
System deps required:
 - Poppler (for pdf2image)
 - Tesseract OCR (for pytesseract)
Python packages:
 pip install streamlit pdfplumber pdf2image pytesseract pillow pandas openpyxl
"""
import io
import re
from typing import List, Dict, Optional
import tempfile

import streamlit as st
import pandas as pd
import pdfplumber
from pdf2image import convert_from_bytes
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import pytesseract

st.set_page_config(page_title="Bank PDF → Excel (Pillow OCR)", layout="wide")
st.title("Bank PDF → Excel — text + OCR parser (Pillow only)")
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
    lines: List[str] = []
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

# ---------- Pillow-only OCR preprocessing ----------
def preprocess_image_for_ocr(pil_img: Image.Image, sharpen: bool = True, contrast: float = 1.5, threshold: int = 160) -> Image.Image:
    """
    Simple Pillow-based preprocessing pipeline:
      - convert to grayscale
      - enhance contrast
      - optional sharpen
      - autocontrast
      - threshold -> binary image which often improves Tesseract on noisy scans
    Returns grayscale PIL Image.
    """
    # Convert to grayscale
    im = pil_img.convert("L")

    # Slightly upscale small images to improve OCR (if small)
    if im.size[0] < 1000:
        im = im.resize((int(im.size[0] * 1.5), int(im.size[1] * 1.5)), Image.BILINEAR)

    # Enhance contrast
    try:
        enhancer = ImageEnhance.Contrast(im)
        im = enhancer.enhance(contrast)
    except Exception:
        pass

    # Optional sharpen
    if sharpen:
        im = im.filter(ImageFilter.SHARPEN)

    # Autocontrast (stretch)
    im = ImageOps.autocontrast(im, cutoff=1)

    # Apply simple thresholding to get binary-like image (helps OCR in many cases)
    im = im.point(lambda x: 0 if x < threshold else 255, mode='1')

    # Convert back to L for tesseract (it accepts both)
    im = im.convert("L")
    return im

def ocr_pdf_bytes(pdf_bytes: bytes, dpi: int = 300, max_pages: Optional[int] = None) -> List[str]:
    """Convert PDF pages to images and OCR each page, returning non-empty lines."""
    lines: List[str] = []
    try:
        images = convert_from_bytes(pdf_bytes, dpi=dpi)
    except Exception as e:
        st.error(f"pdf2image error (poppler missing or unreadable PDF): {e}")
        return lines

    if max_pages:
        images = images[:max_pages]

    for i, img in enumerate(images):
        if i == 0:
            # preview small thumbnail
            try:
                st.sidebar.image(img.resize((300, int(300 * img.height / img.width))), caption="Page 1 preview (OCR)")
            except Exception:
                pass

        proc = preprocess_image_for_ocr(img, sharpen=True, contrast=1.5, threshold=150)
        # Use Tesseract to get text
        try:
            txt = pytesseract.image_to_string(proc, lang='eng')
        except Exception as e:
            st.sidebar.error(f"Tesseract OCR error: {e}")
            txt = ""
        page_lines = [l.strip() for l in txt.splitlines() if l.strip()]
        lines.extend(page_lines)
    return lines

def group_lines_into_records(lines: List[str]) -> List[str]:
    """Combine wrapped narration lines into single transaction records based on date-start heuristic."""
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
    """Parse a single combined record string into Date, Narration, Debit, Credit, Balance."""
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

# ---------- Main flow ----------
st.info("Attempting text-extraction (fast). If not enough text is found, OCR will be used (slower).")
lines = text_from_pdf_bytes(pdf_bytes)

# heuristics to decide if pdfplumber text is usable
if len(lines) >= 8:
    st.success(f"pdfplumber extracted {len(lines)} lines; using text extraction.")
    st.subheader("Sample extracted lines (first 30):")
    st.write(lines[:30])
    used_method = "text"
else:
    st.warning("pdfplumber found little or no text — running OCR fallback (this may take longer).")
    ocr_lines = ocr_pdf_bytes(pdf_bytes, dpi=300, max_pages=None)
    st.subheader("Sample OCR lines (first 80):")
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

# Optionally convert amounts to numeric
def try_convert_amounts(dff: pd.DataFrame) -> pd.DataFrame:
    for col in ["Debit", "Credit", "Balance"]:
        if col in dff.columns:
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
- This version avoids OpenCV and uses Pillow-only preprocessing so it runs on systems where opencv-python wheels are not available.
- OCR quality depends on scan resolution and clarity. If OCR errors appear, try increasing DPI or improving the scan.
- If statements come from a consistent bank/layout, I can add a bank-specific parser (higher accuracy).
"""
)
