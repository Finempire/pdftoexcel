# app.py
"""
Streamlit app: Bank statement PDF -> Excel/CSV
Works without streamlit.experimental_data_editor (compatible with older Streamlit versions).
- Text extraction via pdfplumber (fast) if possible.
- OCR fallback via pytesseract + Pillow preprocessing.
- Preview of page 1 image (rasterized) shown after conversion.
- Download parsed table as Excel or CSV.
- If you want to edit rows: download CSV, edit locally, then re-upload to replace parsed table.
System deps required:
 - Poppler (for pdf2image)
 - Tesseract OCR (for pytesseract)
Python packages:
 pip install streamlit pdfplumber pdf2image pytesseract pillow pandas openpyxl
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

st.set_page_config(page_title="Bank PDF → Excel (stable)", layout="wide")
st.title("Bank PDF → Excel — preview & download (no experimental editor)")

uploaded = st.file_uploader("Upload bank statement PDF", type=["pdf"])
if not uploaded:
    st.info("Upload a PDF to begin.")
    st.stop()

pdf_bytes = uploaded.read()
st.sidebar.markdown(f"**Filename:** {uploaded.name} — {len(pdf_bytes)//1024} KB")

# Regex patterns
DATE_LINE_RE = re.compile(r'^\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s[A-Za-z]{3}\s\d{4})\s+')
AMOUNT_TAG_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))\s*\(?\s*(Dr|Cr)\s*\)?', re.IGNORECASE)
AMOUNT_PLAIN_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))')
# चेक रेफरेंस को पहचानने के लिए improved regex
CHEQUE_REF_RE = re.compile(r'\b(IMPS|UPI|TBMS|NEFT|RTGS|Chq|Cheque|Ref|Reference)[-/]?[A-Za-z0-9]+\b', re.IGNORECASE)

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
    except Exception as e:
        st.sidebar.error(f"pdfplumber error: {e}")
    return lines

def rasterize_first_page(pdf_bytes: bytes, dpi: int = 150) -> Optional[Image.Image]:
    """Return a PIL Image of page 1 for preview (or None on error)."""
    try:
        imgs = convert_from_bytes(pdf_bytes, dpi=dpi, first_page=1, last_page=1)
        if imgs:
            return imgs[0]
    except Exception as e:
        st.sidebar.warning(f"Could not rasterize page for preview: {e}")
    return None

# Pillow-only preprocessing (keeps dependency small)
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
    except Exception as e:
        st.error(f"pdf2image error (poppler missing or unreadable PDF): {e}")
        return lines
    if max_pages:
        images = images[:max_pages]
    for i, img in enumerate(images):
        if i == 0:
            try:
                st.sidebar.image(img.resize((300, int(300 * img.height / img.width))), caption="Page 1 preview (OCR)")
            except Exception:
                pass
        proc = preprocess_image_for_ocr(img, sharpen=True, contrast=1.6, threshold=150)
        try:
            txt = pytesseract.image_to_string(proc, lang='eng', config="--psm 6")
        except Exception as e:
            st.sidebar.error(f"Tesseract OCR error: {e}")
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

def parse_record(rec: str) -> Optional[Dict[str, str]]:
    m = DATE_LINE_RE.match(rec)
    if not m:
        return None
    date = m.group(1).strip()
    rest = rec[m.end():].strip()
    
    # चेक रेफरेंस को पूरी तरह से हटाएं (कोई अलग कॉलम नहीं)
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
    # Cheque_Ref column को हटा दिया गया है
    df = pd.DataFrame(parsed, columns=["Date", "Narration", "Debit", "Credit", "Balance"])
    df = df.fillna("").astype(str)
    return df

# ---------- Main flow ----------
st.info("Trying fast text extraction (pdfplumber). If insufficient results, OCR will be used.")

# Attempt text extraction
lines = text_from_pdf_bytes(pdf_bytes)

# Provide preview image of page 1 always (helps user verify)
first_page_img = rasterize_first_page(pdf_bytes, dpi=150)
if first_page_img:
    st.subheader("Preview — Page 1")
    st.image(first_page_img, use_column_width=True)

if len(lines) >= 8:
    st.success(f"pdfplumber extracted {len(lines)} text lines — using text extraction.")
    st.subheader("Sample extracted text lines (first 30):")
    st.write(lines[:30])
    used_method = "text"
else:
    st.warning("pdfplumber found little/no text — running OCR fallback (may take longer).")
    lines = ocr_pdf_bytes(pdf_bytes, dpi=350, max_pages=None)
    st.subheader("Sample OCR lines (first 80):")
    st.write(lines[:80])
    used_method = "ocr"

records = group_lines_into_records(lines)
st.subheader(f"Grouped into {len(records)} candidate records (first 40 shown)")
st.write(records[:40])

df = parse_records(records)
if df.empty:
    st.error("No transactions parsed. If this PDF is scanned and still fails, try increasing DPI or upload a sample page for me to tune preprocessing.")
    st.stop()

# Show parsed dataframe preview
st.subheader("Parsed table preview")
st.dataframe(df.head(200))  # show up to 200 rows in preview

# Convert amounts option
convert_flag = st.checkbox("Convert Debit/Credit/Balance to numeric (strip commas and convert)", value=True)
if convert_flag:
    df_conv = df.copy()
    for col in ["Debit", "Credit", "Balance"]:
        if col in df_conv.columns:
            df_conv[col] = df_conv[col].replace(r'^\s*$', None, regex=True)
            df_conv[col] = df_conv[col].str.replace(',', '', regex=False).str.replace(' ', '', regex=False)
            df_conv[col] = pd.to_numeric(df_conv[col], errors='coerce')
    st.subheader("Totals (after numeric conversion)")
    totals = {}
    if "Debit" in df_conv.columns:
        totals["Debit sum"] = df_conv["Debit"].sum(skipna=True)
    if "Credit" in df_conv.columns:
        totals["Credit sum"] = df_conv["Credit"].sum(skipna=True)
    if "Balance" in df_conv.columns:
        # show last non-null balance if numeric
        try:
            last_balance = df_conv["Balance"].dropna().iloc[-1]
        except Exception:
            last_balance = None
        totals["Last balance (numeric)"] = last_balance
    st.write(totals)
else:
    df_conv = df.copy()

# Download buttons: CSV and Excel
def to_excel_bytes(dff: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        dff.to_excel(writer, index=False, sheet_name="Statement")
    return out.getvalue()

def to_csv_bytes(dff: pd.DataFrame) -> bytes:
    return dff.to_csv(index=False).encode('utf-8')

st.markdown("### Download parsed data")
col1, col2 = st.columns(2)
with col1:
    st.download_button("Download Excel (.xlsx)", data=to_excel_bytes(df_conv),
                       file_name=uploaded.name.replace(".pdf", ".xlsx"),
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
with col2:
    st.download_button("Download CSV", data=to_csv_bytes(df_conv),
                       file_name=uploaded.name.replace(".pdf", ".csv"),
                       mime="text/csv")

st.markdown("---")
st.markdown("### Edit workflow (if you need to correct rows)")
st.markdown(
    """
1. Click **Download CSV**, open the file in Excel or a text editor, make corrections, save as CSV.  
2. Use the uploader below to re-upload the corrected CSV — it will replace the parsed table for download/export.
"""
)
corrected = st.file_uploader("Upload corrected CSV (optional)", type=["csv"])
if corrected:
    try:
        corrected_df = pd.read_csv(corrected)
        # basic validation: ensure required columns exist
        expected = {"Date", "Narration", "Debit", "Credit", "Balance"}
        if not expected.issubset(set(corrected_df.columns)):
            st.warning(f"Uploaded CSV columns: {list(corrected_df.columns)}. Expected at least: {sorted(expected)}. App will attempt to continue, but column names may differ.")
        # show preview of corrected and replace df_conv for download
        st.subheader("Corrected table preview")
        st.dataframe(corrected_df.head(200))
        # override df_conv for download buttons (show new download)
        st.markdown("Download corrected data:")
        st.download_button("Download corrected Excel (.xlsx)", data=to_excel_bytes(corrected_df),
                           file_name=uploaded.name.replace(".pdf", ".corrected.xlsx"),
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.download_button("Download corrected CSV", data=to_csv_bytes(corrected_df),
                           file_name=uploaded.name.replace(".pdf", ".corrected.csv"),
                           mime="text/csv")
    except Exception as e:
        st.error(f"Could not read uploaded CSV: {e}")

st.markdown("---")
st.markdown("If results look wrong for some rows, paste a few sample lines from the 'Sample extracted lines' above and I'll tune the regex/heuristics for your bank format.")
