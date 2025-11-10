import io
import re
from typing import List, Dict, Optional, Callable

import streamlit as st
import pandas as pd
import pdfplumber

st.set_page_config(page_title="Bank Statement Parser", layout="wide")
st.title("Bank Statement PDF → Excel Parser")

# --- 1. SHARED HELPERS & REGEX -----------------------------------------------
COMMON_DATE_RE = re.compile(r'^\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\s+')
# Regex for amounts specifically with (Dr) or (Cr) tags
AMOUNT_WITH_TAG_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))\s*\(\s*(Dr|Cr)\s*\)', re.IGNORECASE)
# Regex for plain amounts (must have a decimal point to avoid capturing long ref numbers)
PLAIN_AMOUNT_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))')

@st.cache_data(show_spinner=False)
def extract_text_from_pdf(file_bytes: bytes) -> List[str]:
    """Generic: Extract text lines from generic PDF."""
    lines: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            # Increased tolerance to better capture text in table cells
            text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
            for ln in text.splitlines():
                ln = ln.rstrip()
                if ln:
                    lines.append(ln)
    return lines

def to_excel_bytes(dff: pd.DataFrame) -> bytes:
    """Generic: Convert DataFrame to Excel bytes."""
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        dff.to_excel(writer, index=False, sheet_name="Statement")
    return out.getvalue()

# --- 2. BANK SPECIFIC PARSERS ------------------------------------------------

def clean_kotak_narration(narration_dirty: str) -> str:
    """
    Applies regex rules to clean merged Chq/Ref No from narration string.
    """
    narration = narration_dirty
    
    # Remove specific reference number patterns observed in Kotak PDFs
    narration = re.sub(r'UPI/[^/]+/\d{12,}/[^ ]+\s+UPI-\d{12,}', lambda m: m.group(0).split('/')[1] + ' ' + m.group(0).split('/')[3].split()[0], narration)
    narration = re.sub(r'SentIMPS\d{12,}', '', narration)
    narration = re.sub(r'IMPS-\d{12,}', '', narration)
    narration = re.sub(r'TBMS-\d{10,}', '', narration)
    narration = re.sub(r'UPI-\d{12,}', '', narration)
    # Remove standalone long digit strings often found as ref numbers
    narration = re.sub(r'\b\d{10,}\b', '', narration) 

    # General cleanup
    narration = narration.replace('  ', ' ')
    return narration.strip(' ,;-')

def parse_kotak(lines: List[str]) -> pd.DataFrame:
    """
    Specific parsing logic for Kotak style statements.
    """
    records: List[str] = []
    for ln in lines:
        if COMMON_DATE_RE.match(ln):
            records.append(ln.strip())
        else:
            if records:
                records[-1] = records[-1] + " " + ln.strip()

    parsed_data = []
    for rec in records:
        m = COMMON_DATE_RE.match(rec)
        if not m: continue
        date = m.group(1).strip()
        rest = rec[m.end():].strip()

        # 1. Try to find explicitly tagged amounts first (Dr/Cr)
        amount_tags = AMOUNT_WITH_TAG_RE.findall(rest)
        
        txn_amt, txn_tag, bal_amt = "", "", ""
        narration_dirty = rest

        if amount_tags:
            normalized = [(a.replace(' ', '').replace(',', ''), t.lower()) for a, t in amount_tags]
            # Assumption: First tagged amount is transaction, last is balance
            txn_amt, txn_tag = normalized[0]
            if len(normalized) >= 2:
                 bal_amt, _ = normalized[-1]
            
            # Remove all tagged amounts from narration
            narration_dirty = AMOUNT_WITH_TAG_RE.sub('', rest)

        else:
            # 2. Fallback: look for plain amounts if tags are missing
            plain_amounts = PLAIN_AMOUNT_RE.findall(rest)
            # Filter out things that look like ref numbers (e.g. no decimal, too long)
            valid_amounts = [a for a in plain_amounts if '.' in a]
            
            if len(valid_amounts) >= 2:
                txn_amt = valid_amounts[-2].replace(' ', '').replace(',', '')
                bal_amt = valid_amounts[-1].replace(' ', '').replace(',', '')
                # Assume it's a debit if unknown, user can fix in UI
                txn_tag = "dr" 
                
                # Remove these specific amounts from narration
                narration_dirty = rest.replace(valid_amounts[-2], '').replace(valid_amounts[-1], '')

        # Clean up the narration
        narration = clean_kotak_narration(narration_dirty)

        parsed_data.append({
            "Date": date,
            "Narration": narration,
            "Withdrawal (Dr)": txn_amt if txn_tag == 'dr' else "",
            "Deposit (Cr)": txn_amt if txn_tag == 'cr' else "",
            "Balance": bal_amt
        })

    return pd.DataFrame(parsed_data)

# ... (HDFC and SBI dummy parsers remain the same for now)
def parse_hdfc_dummy(lines: List[str]) -> pd.DataFrame:
    return parse_kotak(lines)

def parse_sbi_dummy(lines: List[str]) -> pd.DataFrame:
    return parse_kotak(lines)

BANK_PARSERS = {
    "Kotak Bank": parse_kotak,
    "HDFC Bank (Beta)": parse_hdfc_dummy,
    "SBI (Beta)": parse_sbi_dummy,
}

# --- 3. STREAMLIT UI ---------------------------------------------------------
# ... (UI code remains largely the same, just updated column names in display if needed)

with st.sidebar:
    st.header("⚙️ Settings")
    selected_bank_name = st.selectbox("Select Bank Format", list(BANK_PARSERS.keys()))
    st.divider()
    st.markdown("**Uploaded Files**")

uploaded_files = st.file_uploader(
    f"Upload {selected_bank_name} PDF(s)", 
    type=["pdf"], 
    accept_multiple_files=True
)

if not uploaded_files:
    st.info(f"Please upload one or more {selected_bank_name} statements to begin.")
    st.stop()

if len(uploaded_files) > 1:
    selected_file = st.selectbox("Select file to preview:", uploaded_files, format_func=lambda x: x.name)
else:
    selected_file = uploaded_files[0]

st.sidebar.info(f"Processing: **{selected_file.name}**\n\nFormat: {selected_bank_name}")

st.subheader(f"Preview: {selected_file.name} ({selected_bank_name})")

try:
    with st.spinner("Extracting text..."):
        pdf_bytes = selected_file.getvalue()
        raw_text_lines = extract_text_from_pdf(pdf_bytes)

    with st.spinner(f"Parsing using {selected_bank_name} rules..."):
        parser_function = BANK_PARSERS[selected_bank_name]
        df = parser_function(raw_text_lines)

    if df.empty:
        st.error("No transactions found. Try a different bank format or check the PDF.")
    else:
        df_display = df.fillna("").astype(str)
        
        # Reorder columns to match your request: Date, Narration, Withdrawal, Deposit, Balance
        cols_order = ["Date", "Narration", "Withdrawal (Dr)", "Deposit (Cr)", "Balance"]
        # Ensure all columns exist even if empty
        for col in cols_order:
            if col not in df_display.columns:
                df_display[col] = ""
        df_display = df_display[cols_order]

        col_preview, col_actions = st.columns([3, 1])
        with col_preview:
            edited_df = st.data_editor(
                df_display,
                use_container_width=True,
                num_rows="dynamic",
                height=600,
                key=f"editor_{selected_file.name}"
            )
            st.caption(f"Showing {len(df)} transactions.")

        with col_actions:
            st.markdown("### Download")
            excel_data = to_excel_bytes(edited_df)
            st.download_button(
                label="📥 Download Excel",
                data=excel_data,
                file_name=f"{selected_bank_name}_{selected_file.name.replace('.pdf', '.xlsx')}",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary"
            )

except Exception as e:
    st.error(f"An error occurred: {e}")
