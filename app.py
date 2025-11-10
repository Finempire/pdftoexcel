# app.py
"""
Streamlit bank-statement parser (text + OCR fallback)
- Uses pdfplumber for text PDFs
- Falls back to OCR (pytesseract) with preprocessing
- If opencv-python (cv2) is installed it uses a stronger cv2-based preprocessing (deskew + denoise)
- If cv2 not installed, falls back to Pillow-only preprocessing so it works on Python versions without OpenCV wheels.

System deps:
 - Poppler (for pdf2image)
 - Tesseract OCR (for pytesseract)

Python packages:
 pip install streamlit pdfplumber pdf2image pytesseract pillow pandas openpyxl
 Optional: pip install opencv-python
"""

import io
import re
from typing import List, Dict, Optional
import tempfile
import math

import streamlit as st
import pandas as pd
import pdfplumber
from pdf2image import convert_from_bytes
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import pytesseract

# Try to import OpenCV. If available, use it for better preprocessing.
try:
    import cv2
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False

st.set_page_config(page_title="Bank PDF → Excel (auto OpenCV/Pillow)", layout="wide")
st.title("Bank PDF → Excel — text + OCR parser (auto OpenCV / Pillow)")
st.write("Uploads: text-based PDFs are parsed fast. Scanned PDFs use OCR fallback. If OpenCV is installed, a stronger preprocessing pipeline is used.")

uploaded = st.file_uploader("Upload bank statement PDF", type=["pdf"])
if not uploaded:
    st.info("Upload a PDF to begin.")
    st.stop()

pdf_bytes = uploaded.read()
st.sidebar.markdown(f"**Filename:** {uploaded.name} — {len(pdf_bytes)//1024} KB")
st.sidebar.markdown(f"**OpenCV available:** {'Yes' if HAS_CV2 else 'No'}")

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

# ---------- Image preprocessing (cv2 if available, else Pillow) ----------
def preprocess_with_cv2(pil_img: Image.Image, target_width=2000) -> Image.Image:
    """
    OpenCV preprocessing pipeline:
     - convert to grayscale
     - optionally resize to target_width (maintain aspect)
     - bilateral filter (denoise)
     - adaptive threshold
     - deskew using largest contour / angle estimate
    Returns a PIL Image (grayscale).
    """
    img = pil_img.convert("RGB")
    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)  # cv2 uses BGR
    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)

    # Resize if small / scale to target_width
    h, w = gray.shape[:2]
    if w < target_width:
        scale = target_width / float(w)
        new_w = int(w * scale)
        new_h = int(h * scale)
        gray = cv2.resize(gray, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    # Denoise (bilateral preserves edges)
    den = cv2.bilateralFilter(gray, d=9, sigmaColor=75, sigmaSpace=75)

    # Morphological opening to remove small noise
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1,1))
    opened = cv2.morphologyEx(den, cv2.MORPH_OPEN, kernel)

    # Adaptive threshold
    th = cv2.adaptiveThreshold(opened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 35, 15)

    # Deskew: compute angle from largest non-background contour using Hough or minAreaRect on edges
    try:
        coords = np.column_stack(np.where(th < 255))
        angle = 0.0
        if coords.shape[0] > 0:
            rect = cv2.minAreaRect(coords)
            angle = rect[-1]
            if angle < -45:
                angle = -(90 + angle)
            else:
                angle = -angle
            # rotate image to correct angle
            (h2, w2) = th.shape[:2]
            center = (w2 // 2, h2 // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            rotated = cv2.warpAffine(th, M, (w2, h2), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
            th = rotated
    except Exception:
        pass

    # Convert back to PIL
    pil_out = Image.fromarray(th).convert("L")
    return pil_out

def preprocess_with_pillow(pil_img: Image.Image, sharpen: bool=True, contrast: float=1.6, threshold: int=150) -> Image.Image:
    """
    Pillow-only preprocessing pipeline:
      - grayscale
      - upscale modestly if small
      - contrast enhancement
      - sharpen
      - autocontrast
      - threshold -> binary image
    """
    im = pil_img.convert("L")
    # upscale small images
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

# Lazy import of numpy only if cv2 available (to avoid unnecessary dependency)
if HAS_CV2:
    import numpy as np
else:
    # but some helper functions below may still want a local np; create a limited numpy-like shim for shapes if needed
    import numpy as np  # pillow fallback still uses numpy for convert_from_bytes internal; numpy is normally available

def preprocess_image_for_ocr(pil_img: Image.Image) -> Image.Image:
    # If cv2 available, use that pipeline
    if HAS_CV2:
        try:
            return preprocess_with_cv2(pil_img, target_width=2000)
        except Exception:
            # fallback to pillow
            return preprocess_with_pillow(pil_img, sharpen=True, contrast=1.6, threshold=150)
    else:
        return preprocess_with_pillow(pil_img, sharpen=True, contrast=1.6, threshold=150)

# ---------- OCR flow ----------
def ocr_pdf_bytes(pdf_bytes: bytes, dpi: int = 350, max_pages: Optional[int] = None) -> List[str]:
    """Convert PDF -> images and run OCR with preprocessing. Returns list of non-empty lines."""
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

        proc = preprocess_image_for_ocr(img)
        # Tesseract config can be tuned; using page segmentation mode 6 (assume a single uniform block of text)
        tesseract_config = "--psm 6"
        try:
            txt = pytesseract.image_to_string(proc, lang='eng', config=tesseract_config)
        except Exception as e:
            st.sidebar.error(f"Tesseract OCR error: {e}")
            txt = ""
        page_lines = [l.strip() for l in txt.splitlines() if l.strip()]
        lines.extend(page_lines)
    return lines

# ---------- Grouping & parsing ----------
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
st.info("Attempting text-extraction first (fast). If insufficient, OCR fallback will run (slower).")
lines = text_from_pdf_bytes(pdf_bytes)

# Heuristic: if few lines found, do OCR. Use len >= 8 as earlier heuristic.
if len(lines) >= 8:
    st.success(f"pdfplumber extracted {len(lines)} lines; using text extraction.")
    st.subheader("Sample extracted lines (first 30):")
    st.write(lines[:30])
    used_method = "text"
else:
    st.warning("pdfplumber found little or no text — running OCR fallback (this may take longer).")
    # Using a slightly higher DPI tuned for bank statements
    ocr_lines = ocr_pdf_bytes(pdf_bytes, dpi=400, max_pages=None)
    st.subheader("Sample OCR lines (first 80):")
    st.write(ocr_lines[:80])
    lines = ocr_lines
    used_method = "ocr"

records = group_lines_into_records(lines)
st.subheader(f"Grouped into {len(records)} candidate records (first 40 shown)")
st.write(records[:40])

df = parse_records(records)
if df.empty:
    st.error("No transactions parsed. If your PDF is scanned and still fails, try increasing DPI or upload a sample page for me to tune preprocessing.")
    st.stop()

st.success(f"Parsed {len(df)} transactions using method: {used_method.upper()}.")
st.subheader("Parsed Data (editable)")
edited = st.experimental_data_editor(df, num_rows="dynamic")

# Optionally convert amounts to numeric
def try_convert_amounts(dff: pd.DataFrame) -> pd.DataFrame:
    for col in ["Debit", "Credit", "Balance"]:
        if col in dff.columns:
            dff[col] = dff[col].replace(r'^\s*$', None, regex=True)
            dff[col] = dff[col].str.replace(',', '', regex=False).str.replace(' ', '', regex=False)
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

st.markdown("#### Notes & tips")
st.markdown(
    """
- If OpenCV (cv2) is installed, the app uses a stronger preprocessing pipeline (deskew + denoise + adaptive threshold). This usually improves OCR accuracy significantly.
- If you run into errors installing opencv-python on Python 3.13, create a venv using Python 3.11 or 3.12 and `pip install opencv-python`.
- For your Kotak PDF (text-based), the text-extraction path is used (fast & accurate). OCR fallback is only used for scanned images.
- If you want I can further tune the thresholds (contrast/threshold) specifically for your uploaded Kotak sample — tell me if you see OCR mistakes and paste sample lines that are wrong.
"""
)
