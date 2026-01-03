"""Comprehensive bank statement parser with multi-bank regex patterns and Tabula integration."""
from __future__ import annotations

import io
import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from PyPDF2 import PdfReader

try:  # Tabula requires Java; allow graceful degradation during import
    import tabula
except Exception:  # pragma: no cover - optional dependency runtime failure
    tabula = None

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass
class ExtractionResult:
    metadata: Dict[str, object]
    transactions: pd.DataFrame
    confidence: float = 0.0


BANK_PATTERNS: Dict[str, Dict[str, str]] = {
    "chase": {
        "bank_name": r"Chase|JPMorgan Chase",
        "branch": r"Branch\s*:\s*([A-Za-z ]+)",
        "account_holder": r"Account Holder[:\s]+([A-Za-z ,.'-]+)",
        "account_number": r"Account Number[:\s]+(\d{4,16})",
        "statement_period": r"Statement Period[:\s]+(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})",
        "opening_balance": r"Beginning Balance[:\s]+\$?([\d,]+\.\d{2})",
        "closing_balance": r"Ending Balance[:\s]+\$?([\d,]+\.\d{2})",
        "transaction_date": r"\b\d{2}/\d{2}/\d{4}\b",
        "amount": r"\$?\s*[\d,]+\.\d{2}",
        "reference": r"Reference[:\s]+([A-Z0-9-]+)",
    },
    "bank_of_america": {
        "bank_name": r"Bank of America",
        "branch": r"Branch Number[:\s]+(\d+)",
        "account_holder": r"Primary Account Holder[:\s]+([A-Za-z ,.'-]+)",
        "account_number": r"Account Number[:\s]+(\d{4,16})",
        "statement_period": r"Statement Period[:\s]+(\d{2}/\d{2}/\d{4})\s*to\s*(\d{2}/\d{2}/\d{4})",
        "opening_balance": r"Opening Balance[:\s]+\$?([\d,]+\.\d{2})",
        "closing_balance": r"Closing Balance[:\s]+\$?([\d,]+\.\d{2})",
        "transaction_date": r"\b\d{2}/\d{2}/\d{4}\b",
        "amount": r"\$?\s*[\d,]+\.\d{2}",
        "reference": r"Confirmation[:\s]+([A-Z0-9-]+)",
    },
    "wells_fargo": {
        "bank_name": r"Wells Fargo",
        "branch": r"Branch[:\s]+([A-Za-z0-9 -]+)",
        "account_holder": r"Account Name[:\s]+([A-Za-z ,.'-]+)",
        "account_number": r"Account Number[:\s]+(\d{4,16})",
        "statement_period": r"Statement Period[:\s]+(\d{2}/\d{2}/\d{4})\s*through\s*(\d{2}/\d{2}/\d{4})",
        "opening_balance": r"Beginning balance[:\s]+\$?([\d,]+\.\d{2})",
        "closing_balance": r"Ending balance[:\s]+\$?([\d,]+\.\d{2})",
        "transaction_date": r"\b\d{2}/\d{2}/\d{4}\b",
        "amount": r"\$?\s*[\d,]+\.\d{2}",
        "reference": r"Serial Number[:\s]+([A-Z0-9-]+)",
    },
    "citibank": {
        "bank_name": r"Citibank",
        "branch": r"Branch[:\s]+([A-Za-z0-9 -]+)",
        "account_holder": r"Customer Name[:\s]+([A-Za-z ,.'-]+)",
        "account_number": r"Account Number[:\s]+(\d{4,16})",
        "statement_period": r"Statement Period[:\s]+(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})",
        "opening_balance": r"Opening Balance[:\s]+\$?([\d,]+\.\d{2})",
        "closing_balance": r"Closing Balance[:\s]+\$?([\d,]+\.\d{2})",
        "transaction_date": r"\b\d{2}/\d{2}/\d{4}\b",
        "amount": r"\$?\s*[\d,]+\.\d{2}",
        "reference": r"Reference[:\s]+([A-Z0-9-]+)",
    },
    "hdfc": {
        "bank_name": r"HDFC Bank",
        "branch": r"Branch[:\s]+([A-Za-z ]+)",
        "account_holder": r"Customer Name[:\s]+([A-Za-z ,.'-]+)",
        "account_number": r"Account No[.:\s]+(\d{10,16})",
        "statement_period": r"Statement Period[:\s]+(\d{2}-\d{2}-\d{4})\s*to\s*(\d{2}-\d{2}-\d{4})",
        "opening_balance": r"Opening Balance[:\s]+₹?([\d,]+\.\d{2})",
        "closing_balance": r"Closing Balance[:\s]+₹?([\d,]+\.\d{2})",
        "transaction_date": r"\b\d{2}-\d{2}-\d{4}\b",
        "amount": r"₹?\s*[\d,]+\.\d{2}",
        "reference": r"Ref[.:\s]+([A-Z0-9-]+)",
        "ifsc": r"IFSC Code[:\s]+([A-Z]{4}0[A-Z0-9]{6})",
    },
    "icici": {
        "bank_name": r"ICICI Bank",
        "branch": r"Branch[:\s]+([A-Za-z ]+)",
        "account_holder": r"Account Holder[:\s]+([A-Za-z ,.'-]+)",
        "account_number": r"Account No[.:\s]+(\d{10,16})",
        "statement_period": r"Period[:\s]+(\d{2}/\d{2}/\d{4})\s*to\s*(\d{2}/\d{2}/\d{4})",
        "opening_balance": r"Opening Balance[:\s]+₹?([\d,]+\.\d{2})",
        "closing_balance": r"Closing Balance[:\s]+₹?([\d,]+\.\d{2})",
        "transaction_date": r"\b\d{2}/\d{2}/\d{4}\b",
        "amount": r"₹?\s*[\d,]+\.\d{2}",
        "reference": r"UTR[:\s]+([A-Z0-9-]+)",
        "ifsc": r"IFSC[:\s]+([A-Z]{4}0[A-Z0-9]{6})",
    },
    "sbi": {
        "bank_name": r"State Bank of India|SBI",
        "branch": r"Branch[:\s]+([A-Za-z ]+)",
        "account_holder": r"Account Name[:\s]+([A-Za-z ,.'-]+)",
        "account_number": r"Account No[.:\s]+(\d{10,16})",
        "statement_period": r"Statement of Account for period[:\s]+(\d{2}-\d{2}-\d{4})\s*to\s*(\d{2}-\d{2}-\d{4})",
        "opening_balance": r"Opening Balance[:\s]+₹?([\d,]+\.\d{2})",
        "closing_balance": r"Closing Balance[:\s]+₹?([\d,]+\.\d{2})",
        "transaction_date": r"\b\d{2}-\d{2}-\d{4}\b",
        "amount": r"₹?\s*[\d,]+\.\d{2}",
        "reference": r"Ref[.:\s]+([A-Z0-9-]+)",
        "ifsc": r"IFSC[:\s]+([A-Z]{4}0[A-Z0-9]{6})",
    },
    "axis": {
        "bank_name": r"Axis Bank",
        "branch": r"Branch[:\s]+([A-Za-z ]+)",
        "account_holder": r"Customer Name[:\s]+([A-Za-z ,.'-]+)",
        "account_number": r"A/C No[.:\s]+(\d{10,16})",
        "statement_period": r"Statement Period[:\s]+(\d{2}/\d{2}/\d{4})\s*to\s*(\d{2}/\d{2}/\d{4})",
        "opening_balance": r"Opening Balance[:\s]+₹?([\d,]+\.\d{2})",
        "closing_balance": r"Closing Balance[:\s]+₹?([\d,]+\.\d{2})",
        "transaction_date": r"\b\d{2}/\d{2}/\d{4}\b",
        "amount": r"₹?\s*[\d,]+\.\d{2}",
        "reference": r"Ref[.:\s]+([A-Z0-9-]+)",
        "ifsc": r"IFSC[:\s]+([A-Z]{4}0[A-Z0-9]{6})",
    },
    "generic": {
        "bank_name": r"Bank|Statement",
        "branch": r"Branch[:\s]+([A-Za-z ]+)",
        "account_holder": r"Account Holder[:\s]+([A-Za-z ,.'-]+)",
        "account_number": r"Account (?:No|Number)[.:\s]+([A-Za-z0-9]{6,20})",
        "statement_period": r"(\d{2}[/-]\d{2}[/-]\d{2,4})\s*(?:to|-|through)\s*(\d{2}[/-]\d{2}[/-]\d{2,4})",
        "opening_balance": r"Opening Balance[:\s]+([\d,]+\.\d{2})",
        "closing_balance": r"Closing Balance[:\s]+([\d,]+\.\d{2})",
        "transaction_date": r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        "amount": r"₹?\$?\s*[\d,]+\.\d{2}",
        "reference": r"Ref[.:\s]+([A-Z0-9-]+)",
    },
}

TABULA_CONFIGS = {
    "default": {"lattice": True, "stream": False, "multiple_tables": True, "pages": "all"},
    "fallback": {"lattice": False, "stream": True, "guess": True, "pages": "all"},
}

TRANSACTION_CATEGORY_RULES: List[Tuple[str, List[str]]] = [
    ("ATM", ["atm", "cash withdrawal"]),
    ("Online", ["upi", "imps", "neft", "rtgs", "netbanking", "online"]),
    ("Card", ["pos", "card", "debit card", "credit card"]),
    ("Transfer", ["transfer", "trx", "trf", "fund"]),
    ("Charge", ["fee", "charge", "gst", "tax"]),
    ("Cheque", ["chq", "cheque"]),
]


class BankStatementParser:
    def __init__(self, bank_name: Optional[str] = None):
        self.bank_name = bank_name.lower() if bank_name else None

    # ---------- text helpers ----------
    def _text_from_pdf(self, pdf_bytes: bytes) -> str:
        buffer = io.BytesIO(pdf_bytes)
        try:
            reader = PdfReader(buffer)
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    return "\n".join(page.extract_text() or "" for page in pdf.pages)
            except Exception:
                return ""

    def _text_from_ocr(self, pdf_bytes: bytes, dpi: int = 300) -> str:
        try:
            images = convert_from_bytes(pdf_bytes, dpi=dpi)
        except Exception:
            return ""
        lines: List[str] = []
        for img in images:
            proc = self._preprocess_image(img)
            try:
                txt = pytesseract.image_to_string(proc, lang="eng", config="--psm 6")
            except Exception:
                txt = ""
            lines.extend([ln.strip() for ln in txt.splitlines() if ln.strip()])
        return "\n".join(lines)

    @staticmethod
    def _preprocess_image(img: Image.Image) -> Image.Image:
        im = img.convert("L")
        if im.size[0] < 1000:
            im = im.resize((int(im.size[0] * 1.5), int(im.size[1] * 1.5)), Image.BILINEAR)
        im = ImageEnhance.Contrast(im).enhance(1.8)
        im = im.filter(ImageFilter.SHARPEN)
        im = ImageOps.autocontrast(im, cutoff=1)
        im = im.point(lambda x: 0 if x < 150 else 255, mode="1")
        im = im.convert("L")
        return im

    # ---------- bank detection ----------
    def detect_bank(self, pdf_bytes: bytes) -> str:
        text = self._text_from_pdf(pdf_bytes)
        if not text:
            text = self._text_from_ocr(pdf_bytes, dpi=350)
        lowered = text.lower()
        for name, patterns in BANK_PATTERNS.items():
            bank_re = patterns.get("bank_name")
            if bank_re and re.search(bank_re, text, re.IGNORECASE):
                self.bank_name = name
                return name
        signature_markers = {
            "ifsc": r"IFSC\s+Code|IFSC" ,
            "routing": r"Routing\s+Number",
            "customer_id": r"Customer ID",
        }
        if any(re.search(pat, text, re.IGNORECASE) for pat in signature_markers.values()):
            self.bank_name = "generic"
            return "generic"
        header_sample = "\n".join(text.splitlines()[:5]).lower()
        if "bank" in header_sample:
            self.bank_name = "generic"
            return "generic"
        self.bank_name = "generic"
        return "generic"

    # ---------- metadata extraction ----------
    def _extract_metadata_from_text(self, bank_key: str, text: str) -> Dict[str, object]:
        patterns = BANK_PATTERNS.get(bank_key, BANK_PATTERNS["generic"])
        meta: Dict[str, object] = {
            "bank_name": bank_key,
            "account_holder": None,
            "account_number": None,
            "statement_period": {"from": None, "to": None},
            "opening_balance": None,
            "closing_balance": None,
            "branch": None,
        }
        for field_name, regex in patterns.items():
            if field_name in {"transaction_date", "amount", "bank_name"}:
                continue
            m = re.search(regex, text, re.IGNORECASE)
            if not m:
                continue
            if field_name == "statement_period":
                meta["statement_period"] = {
                    "from": self._standardize_date(m.group(1)),
                    "to": self._standardize_date(m.group(2)),
                }
            elif field_name in {"opening_balance", "closing_balance"}:
                meta[field_name] = self._parse_amount(m.group(1))
            else:
                meta[field_name] = m.group(1).strip()
        return meta

    def extract_metadata(self, pdf_bytes: bytes) -> Dict[str, object]:
        bank_key = self.bank_name or self.detect_bank(pdf_bytes)
        text = self._text_from_pdf(pdf_bytes)
        if not text:
            text = self._text_from_ocr(pdf_bytes, dpi=350)
        return self._extract_metadata_from_text(bank_key, text)

    # ---------- transaction extraction ----------
    def extract_transactions(self, pdf_bytes: bytes) -> pd.DataFrame:
        bank_key = self.bank_name or self.detect_bank(pdf_bytes)
        tables = self._extract_tables_with_tabula(pdf_bytes)
        if not tables:
            tables = self._extract_tables_with_pdfplumber(pdf_bytes)
        combined = self._normalize_tables(tables)
        if combined.empty:
            combined = self._fallback_line_parser(pdf_bytes, bank_key)
        combined = self._post_process_transactions(combined)
        return combined

    def _extract_tables_with_tabula(self, pdf_bytes: bytes) -> List[pd.DataFrame]:
        if tabula is None:
            return []
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = Path(tmp.name)
            tables: List[pd.DataFrame] = []
            try:
                tables = tabula.read_pdf(str(tmp_path), **TABULA_CONFIGS["default"])
                if not tables:
                    tables = tabula.read_pdf(str(tmp_path), **TABULA_CONFIGS["fallback"])
            finally:
                tmp_path.unlink(missing_ok=True)
            return tables or []
        except Exception as exc:
            logger.debug("Tabula extraction failed: %s", exc)
            return []

    @staticmethod
    def _extract_tables_with_pdfplumber(pdf_bytes: bytes) -> List[pd.DataFrame]:
        tables: List[pd.DataFrame] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    for table in page.extract_tables():
                        if not table or len(table) < 2:
                            continue
                        df = pd.DataFrame(table[1:], columns=table[0])
                        tables.append(df)
        except Exception:
            return []
        return tables

    def _normalize_tables(self, tables: List[pd.DataFrame]) -> pd.DataFrame:
        normalized_frames: List[pd.DataFrame] = []
        for tbl in tables:
            df = tbl.copy().fillna("")
            lower_cols = [str(c).strip().lower() for c in df.columns]
            df.columns = lower_cols
            candidate_cols = {
                "date": [c for c in df.columns if "date" in c],
                "description": [c for c in df.columns if any(k in c for k in ["narration", "description", "particulars"])],
                "debit": [c for c in df.columns if "debit" in c or "withdraw" in c],
                "credit": [c for c in df.columns if "credit" in c or "deposit" in c],
                "balance": [c for c in df.columns if "balance" in c or "bal" == c],
                "reference": [c for c in df.columns if any(k in c for k in ["chq", "ref", "utr", "cheque"])]
            }
            if not candidate_cols["date"] or not candidate_cols["description"]:
                continue
            norm = pd.DataFrame({
                "date": df[candidate_cols["date"][0]].astype(str),
                "description": df[candidate_cols["description"][0]].astype(str),
                "debit": df[candidate_cols["debit"][0]].astype(str) if candidate_cols["debit"] else "",
                "credit": df[candidate_cols["credit"][0]].astype(str) if candidate_cols["credit"] else "",
                "balance": df[candidate_cols["balance"][0]].astype(str) if candidate_cols["balance"] else "",
                "reference": df[candidate_cols["reference"][0]].astype(str) if candidate_cols["reference"] else "",
            })
            normalized_frames.append(norm)
        if not normalized_frames:
            return pd.DataFrame()
        combined = pd.concat(normalized_frames, ignore_index=True)
        combined = combined.replace("nan", "").replace("NaN", "")
        return combined

    def _fallback_line_parser(self, pdf_bytes: bytes, bank_key: str) -> pd.DataFrame:
        text = self._text_from_pdf(pdf_bytes)
        if not text:
            text = self._text_from_ocr(pdf_bytes, dpi=350)
        pattern = BANK_PATTERNS.get(bank_key, BANK_PATTERNS["generic"]).get("transaction_date", BANK_PATTERNS["generic"]["transaction_date"])
        lines = [ln.strip() for ln in text.splitlines() if re.search(pattern, ln)]
        rows = []
        for ln in lines:
            date_match = re.search(pattern, ln)
            if not date_match:
                continue
            date = self._standardize_date(date_match.group(0))
            remainder = ln[date_match.end():].strip()
            amounts = re.findall(BANK_PATTERNS[bank_key].get("amount", BANK_PATTERNS["generic"]["amount"]), remainder)
            debit = credit = balance = ""
            if amounts:
                debit = self._parse_amount(amounts[0])
                if len(amounts) > 1:
                    balance = self._parse_amount(amounts[-1])
            rows.append({
                "date": date,
                "description": re.sub(BANK_PATTERNS[bank_key].get("amount", BANK_PATTERNS["generic"]["amount"]), "", remainder).strip(),
                "debit": debit,
                "credit": credit,
                "balance": balance,
                "reference": "",
            })
        return pd.DataFrame(rows)

    # ---------- post processing ----------
    def _post_process_transactions(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.fillna("")
        df["date"] = df["date"].apply(self._standardize_date)
        for col in ["debit", "credit", "balance"]:
            if col in df.columns:
                df[col] = df[col].apply(self._parse_amount)
        df["category"] = df["description"].apply(self._categorize_description)
        return df

    # ---------- validation and scoring ----------
    def validate_data(self, data: ExtractionResult) -> bool:
        meta_ok = bool(data.metadata.get("account_number"))
        txn_ok = not data.transactions.empty
        return meta_ok and txn_ok

    def compute_confidence(self, metadata: Dict[str, object], transactions: pd.DataFrame) -> float:
        fields = [metadata.get("account_holder"), metadata.get("account_number"), metadata.get("opening_balance"), metadata.get("closing_balance")]
        filled = sum(1 for f in fields if f)
        meta_score = filled / len(fields) if fields else 0
        txn_score = min(1.0, len(transactions) / 10.0) if not transactions.empty else 0
        return round((meta_score * 0.6) + (txn_score * 0.4), 2)

    # ---------- utilities ----------
    @staticmethod
    def _standardize_date(date_str: Optional[str]) -> Optional[str]:
        if not date_str:
            return None
        for fmt in (
            "%d-%m-%Y",
            "%d/%m/%Y",
            "%m/%d/%Y",
            "%d %b %Y",
            "%Y-%m-%d",
            "%d-%m-%y",
            "%d/%m/%y",
        ):
            try:
                return datetime.strptime(date_str.strip(), fmt).date().isoformat()
            except ValueError:
                continue
        return date_str

    @staticmethod
    def _parse_amount(value: Optional[str]) -> Optional[float]:
        if value is None:
            return None
        cleaned = str(value).replace(",", "").replace("₹", "").replace("$", "").strip()
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = f"-{cleaned[1:-1]}"
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _categorize_description(self, desc: str) -> str:
        lower = desc.lower()
        for category, keywords in TRANSACTION_CATEGORY_RULES:
            if any(k in lower for k in keywords):
                return category
        return "General"

    # ---------- export helpers ----------
    @staticmethod
    def export_csv(df: pd.DataFrame, path: Path) -> Path:
        df.to_csv(path, index=False)
        return path

    @staticmethod
    def export_json(df: pd.DataFrame, path: Path) -> Path:
        df.to_json(path, orient="records", force_ascii=False)
        return path

    @staticmethod
    def export_excel(df: pd.DataFrame, path: Path) -> Path:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Statement")
        return path

    # ---------- high level ----------
    def parse(self, pdf_bytes: bytes) -> ExtractionResult:
        bank_key = self.bank_name or self.detect_bank(pdf_bytes)
        metadata = self.extract_metadata(pdf_bytes)
        transactions = self.extract_transactions(pdf_bytes)
        confidence = self.compute_confidence(metadata, transactions)
        result = ExtractionResult(metadata=metadata, transactions=transactions, confidence=confidence)
        return result


__all__ = ["BankStatementParser", "BANK_PATTERNS", "TABULA_CONFIGS", "ExtractionResult"]
