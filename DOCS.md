# Bank Statement Parser

This project includes a modular, multi-bank statement parser with Tabula integration and OCR fallback.

## Usage
1. Launch the Streamlit app: `streamlit run app.py`.
2. Upload a PDF bank statement and choose a bank or select **Auto Detect**.
3. Download parsed transactions in Excel, CSV, or JSON.

## Adding Banks
- Extend `BANK_PATTERNS` in `bank_parser.py` with regex rules for metadata, transaction dates, and bank markers.
- Provide optional identifiers such as IFSC or routing numbers to improve detection.

## Error Handling
- The parser falls back from Tabula to pdfplumber table parsing, then to OCR-based line parsing.
- Confidence scoring blends metadata completeness with transaction volume for quick validation.

## Testing
Run the test suite with `pytest`. Synthetic text-based fixtures validate metadata extraction and date/amount normalization across multiple banks.
