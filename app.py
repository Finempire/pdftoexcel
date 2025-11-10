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
AMOUNT_WITH_TAG_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))\s*\(\s*(Dr|Cr)\s*\)', re.IGNORECASE)
PLAIN_AMOUNT_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))')

@st.cache_data(show_spinner=False)
def extract_text_from_pdf(file_bytes: bytes) -> List[str]:
    """Generic: Extract text lines from generic PDF."""
    lines: List[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=2) or "" # Added tolerance
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
    This is based on patterns observed in the sample PDF.
    """
    narration = narration_dirty
    
    # Rule for UPI: "UPI/Raghvendra Kuma/4107.../LAPT UPI-4107..." -> "Raghvendra Kuma LAPT"
    narration = re.sub(r'UPI/([^/]+)/\d{12,}/([^ ]+)\s+UPI-\d{12,}', r'\1 \2', narration)
    
    # Rule for SentIMPS: "SentIMPS...LAPT ASSES ... IMPS-..." -> "LAPT ASSES ..."
    narration = re.sub(r'SentIMPS\d{12,}(\S*\s*[\s\S]*?)IMPS-\d{12,}', r'\1', narration, flags=re.DOTALL)
    
    # Rule for Charges: "Chrg: ... TBMS-1415..." -> "Chrg: ..."
    narration = re.sub(r'^(Chrg:[\s\S]*?)TBMS-\d{10,}', r'\1', narration)
    
    # Rule for CLG INST: "BY CLG INST ... //ICICI/NOIDA" -> "ICICI/NOIDA"
    narration = re.sub(r'BY CLG INST \S+\s*//(.*)', r'\1', narration)

    # Fallback cleanup for any remaining stray codes at the end of a line
    narration = re.sub(r'IMPS-\d{10,}$', '', narration)
    narration = re.sub(r'TBMS-\d{10,}$', '', narration)
    narration = re.sub(r'UPI-\d{10,}$', '', narration)
    narration = re.sub(r'SentIMPS\d{10,}$', '', narration)
    
    # Final whitespace cleanup
    return ' '.join(narration.split()).strip(' ,;-')


def parse_kotak(lines: List[str]) -> pd.DataFrame:
    """
    Specific parsing logic for Kotak style statements.
    """
    # A. Group lines based on Date at start
    records: List[str] = []
    for ln in lines:
        if COMMON_DATE_RE.match(ln):
            records.append(ln.strip())
        else:
            if records:
                records[-1] = records[-1] + " " + ln.strip()

    # B. Parse individual grouped records
    parsed_data = []
    for rec in records:
        m = COMMON_DATE_RE.match(rec)
        if not m: continue
        date = m.group(1).strip()
        rest = rec[m.end():].strip()

        amount_tags = AMOUNT_WITH_TAG_RE.findall(rest)

        if amount_tags:
            normalized = [(a.replace(' ', '').replace(',', ''), t.lower()) for a, t in amount_tags]
            txn_amt, txn_tag = normalized[0]
            bal_amt, bal_tag = normalized[-1] if len(normalized) >= 2 else ("", "")
            
            debit = txn_amt if txn_tag == 'dr' else ""
            credit = txn_amt if txn_tag == 'cr' else ""
            
            # --- MODIFICATION ---
            # Get dirty narration by removing amounts
            narration_dirty = AMOUNT_WITH_TAG_RE.sub('', rest)
            # Clean the dirty narration
            narration = clean_kotak_narration(narration_dirty)
            # --- END MODIFICATION ---

            parsed_data.append({"Date": date, "Narration": narration, "Debit": debit, "Credit": credit, "Balance": bal_amt})
        else:
             plain_amounts = PLAIN_AMOUNT_RE.findall(rest)
             if len(plain_amounts) >= 2:
                 txn_amt = plain_amounts[-2].replace(' ', '').replace(',', '')
                 bal_amt = plain_amounts[-1].replace(' ', '').replace(',', '')
                 
                 # --- MODIFICATION ---
                 narration_dirty = PLAIN_AMOUNT_RE.sub('', rest).strip()
                 narration = clean_kotak_narration(narration_dirty)
                 # --- END MODIFICATION ---
                 
                 parsed_data.append({"Date": date, "Narration": narration, "Debit": txn_amt, "Credit": "", "Balance": bal_amt})
             else:
                 # Fallback for lines with no discernible amounts
                 narration_dirty = rest
                 narration = clean_kotak_narration(narration_dirty)
                 parsed_data.append({"Date": date, "Narration": narration, "Debit": "", "Credit": "", "Balance": ""})

    return pd.DataFrame(parsed_data, columns=["Date", "Narration", "Debit", "Credit", "Balance"])

def parse_hdfc_dummy(lines: List[str]) -> pd.DataFrame:
    """Placeholder for HDFC logic. Currently uses Kotak logic as fallback."""
    st.warning("HDFC parser is not yet implemented. Using default parser.")
    return parse_kotak(lines)

def parse_sbi_dummy(lines: List[str]) -> pd.DataFrame:
    """Placeholder for SBI logic. Currently uses Kotak logic as fallback."""
    st.warning("SBI parser is not yet implemented. Using default parser.")
    return parse_kotak(lines)

# Registry of available parsers
BANK_PARSERS: Dict[str, Callable[[List[str]], pd.DataFrame]] = {
    "Kotak Bank": parse_kotak,
    "HDFC Bank (Beta)": parse_hdfc_dummy,
    "SBI (Beta)": parse_sbi_dummy,
}

# --- 3. STREAMLIT UI ---------------------------------------------------------

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

# --- 4. MAIN PROCESSING LOOP -------------------------------------------------
st.subheader(f"Preview: {selected_file.name} ({selected_bank_name})")

try:
    with st.spinner("Extracting text..."):
        pdf_bytes = selected_file.getvalue()
        raw_text_lines = extract_text_from_pdf(pdf_bytes)

    with st.spinner(f"Parsing using {selected_bank_name} rules..."):
        parser_function = BANK_PARSERS[selected_bank_name]
        df = parser_function(raw_text_lines)

    if df.empty:
        st.error(f"No transactions found using the {selected_bank_name} parser. The file might be a different format.")
        with st.expander("See raw text for debugging"):
            st.write(raw_text_lines[:20])
    else:
        df_display = df.fillna("").astype(str)
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
            st.markdown("---")
            with st.expander("See raw text for debugging"):
                st.write(raw_text_lines[:20])

except Exception as e:
    st.error(f"An error occurred while processing the file: {e}")
    st.exception(e) # Show full traceback for debugging
