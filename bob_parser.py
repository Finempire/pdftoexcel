"""
Bank of Baroda statement parser utilities.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import PyPDF2

logger = logging.getLogger(__name__)


class BOBStatementParser:
    """Production-ready Bank of Baroda statement parser."""

    # Enhanced regex patterns
    REGEX_PATTERNS = {
        # Metadata patterns
        "account_number": r"(?:Account No:|Current Account)\s*[-:\s]*(\d{3}[X]+\d{3})",
        "account_holder": r"Current Account[^\n]+\n([A-Z][A-Z\s&]+(?:LLP|PVT|LTD|LIMITED)?)",
        "statement_period": r"(?:Statement Period from|period)\s+(\d{2}/\d{2}/\d{4})\s+(?:to|-)\s+(\d{2}/\d{2}/\d{4})",
        "ifsc_code": r"IFSC Code:\s*([A-Z0-9]{11})",
        "branch": r"Branch Name:\s*([A-Z\s]+)",
        "customer_id": r"Customer Id:\s*([A-Z0-9]+)",
        # Transaction line pattern - Bank of Baroda specific format
        # Format: DATE BALANCE CHQ.NO/NARRATION WITHDRAWAL(DR) DEPOSIT(CR)
        "transaction_line": r"^(\d{2}/\d{2}/\d{4})\s+([\d,]+\.\d{2}(?:Cr|Dr)?)?(.+)$",
        # Amount patterns
        "amount": r"([\d,]+\.\d{2})",
        "balance_with_indicator": r"([\d,]+\.\d{2})(Cr|Dr)",
        # Transaction type identifiers
        "neft": r"NEFT-([A-Z0-9]+)",
        "imps": r"IMPS/P2A/(\d+)",
        "rtgs": r"RTGS-([A-Z0-9]+)",
        "upi": r"UPI/(\d+)",
        "ebank": r"EBANK:([^/]+)",
        "charges": r"CHARGES? FOR",
        "loan_recovery": r"Loan Recovery For(\d+)",
    }

    def __init__(self, pdf_bytes: bytes):
        self.pdf_bytes = pdf_bytes
        self.raw_text = ""
        self.metadata: Dict[str, str] = {}
        self.transactions: List[Dict[str, object]] = []

    def extract_text_from_pdf(self) -> str:
        """Extract all text from PDF bytes."""
        try:
            with io.BytesIO(self.pdf_bytes) as file:
                pdf_reader = PyPDF2.PdfReader(file)
                text_parts: List[str] = []
                for page in pdf_reader.pages:
                    page_text = page.extract_text() or ""
                    text_parts.append(page_text)
                return "\n".join(text_parts)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Failed to extract PDF text: %s", exc)
            raise

    @staticmethod
    def parse_indian_amount(amount_str: str) -> float:
        """Parse Indian number format (with commas) to float."""
        if not amount_str or pd.isna(amount_str):
            return 0.0
        try:
            clean = str(amount_str).replace(",", "").strip()
            clean = re.sub(r"(Cr|Dr)$", "", clean).strip()
            return float(clean)
        except (ValueError, AttributeError):
            logger.warning("Cannot parse amount: %s", amount_str)
            return 0.0

    @staticmethod
    def parse_date(date_str: str) -> Optional[datetime]:
        """Parse DD/MM/YYYY format."""
        try:
            return datetime.strptime(date_str.strip(), "%d/%m/%Y")
        except (ValueError, AttributeError):
            return None

    def extract_metadata(self) -> Dict[str, str]:
        """Extract account metadata from raw text."""
        metadata: Dict[str, str] = {}

        match = re.search(self.REGEX_PATTERNS["account_number"], self.raw_text)
        if match:
            metadata["account_number"] = match.group(1)

        match = re.search(self.REGEX_PATTERNS["account_holder"], self.raw_text)
        if match:
            metadata["account_holder"] = match.group(1).strip()

        match = re.search(self.REGEX_PATTERNS["statement_period"], self.raw_text)
        if match:
            metadata["period_start"] = match.group(1)
            metadata["period_end"] = match.group(2)

        match = re.search(self.REGEX_PATTERNS["ifsc_code"], self.raw_text)
        if match:
            metadata["ifsc_code"] = match.group(1)

        match = re.search(self.REGEX_PATTERNS["branch"], self.raw_text)
        if match:
            metadata["branch"] = match.group(1).strip()

        match = re.search(self.REGEX_PATTERNS["customer_id"], self.raw_text)
        if match:
            metadata["customer_id"] = match.group(1)

        return metadata

    @staticmethod
    def identify_transaction_type(description: str) -> str:
        """Identify transaction type from description."""
        desc_upper = description.upper()

        if "NEFT" in desc_upper:
            return "NEFT"
        if "IMPS" in desc_upper:
            return "IMPS"
        if "RTGS" in desc_upper:
            return "RTGS"
        if "UPI" in desc_upper:
            return "UPI"
        if "EBANK" in desc_upper:
            return "EBANK"
        if "CHARGES" in desc_upper:
            return "CHARGES"
        if "LOAN RECOVERY" in desc_upper:
            return "LOAN_RECOVERY"
        if "ATM" in desc_upper:
            return "ATM"
        if "CHQ" in desc_upper or "CHEQUE" in desc_upper:
            return "CHEQUE"
        return "OTHER"

    def parse_transaction_line(self, line: str, next_line: str = "") -> Optional[Tuple[str, str, List[float]]]:
        """Parse a single transaction line."""
        line = line.strip()
        if not line:
            return None

        date_match = re.match(r"^(\d{2}/\d{2}/\d{4})\s+(.+)$", line)
        if not date_match:
            return None

        date_str = date_match.group(1)
        rest = date_match.group(2)

        amounts: List[float] = []
        amount_matches = re.finditer(r"([\d,]+\.\d{2})(Cr|Dr)?", rest)

        for match in amount_matches:
            amount = self.parse_indian_amount(match.group(1))
            amounts.append(amount)

        description = rest
        for match in re.finditer(r"[\d,]+\.\d{2}(?:Cr|Dr)?", rest):
            description = description.replace(match.group(0), "")

        description = " ".join(description.split()).strip()

        if len(description) < 10 and next_line:
            next_clean = next_line.strip()
            if not re.match(r"^\d{2}/\d{2}/\d{4}", next_clean):
                description += " " + next_clean

        return date_str, description.strip(), amounts

    def parse_transactions(self) -> List[Dict[str, object]]:
        """Parse all transactions from text."""
        transactions: List[Dict[str, object]] = []
        lines = self.raw_text.split("\n")

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            if not line or "NARRATION" in line or "Page" in line:
                i += 1
                continue

            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            parsed = self.parse_transaction_line(line, next_line)

            if parsed:
                date_str, description, amounts = parsed

                debit = 0.0
                credit = 0.0
                balance = 0.0

                if len(amounts) == 1:
                    balance = amounts[0]
                elif len(amounts) == 2:
                    balance = amounts[-1]
                    if any(word in description.upper() for word in ["NEFT", "DEPOSIT", "CREDIT", "REFUND"]):
                        credit = amounts[0]
                    else:
                        debit = amounts[0]
                elif len(amounts) >= 3:
                    balance = amounts[-1]
                    debit = amounts[0] if amounts[0] > 0 else 0.0
                    credit = amounts[1] if len(amounts) > 2 and amounts[1] > 0 else 0.0

                transaction_date = self.parse_date(date_str)

                if transaction_date:
                    transaction = {
                        "date": transaction_date,
                        "description": description,
                        "debit": debit,
                        "credit": credit,
                        "balance": balance,
                        "transaction_type": self.identify_transaction_type(description),
                    }
                    transactions.append(transaction)

            i += 1

        return transactions

    def smart_parse_with_balance_validation(self) -> List[Dict[str, object]]:
        """Advanced parser with balance validation."""
        transactions = self.parse_transactions()

        for i in range(1, len(transactions)):
            prev_balance = transactions[i - 1]["balance"]
            curr_balance = transactions[i]["balance"]
            debit = transactions[i]["debit"]
            credit = transactions[i]["credit"]

            balance_change = curr_balance - prev_balance

            if debit > 0 and credit > 0:
                expected_change = credit - debit
                if abs(expected_change - balance_change) > 0.01:
                    if abs((debit - credit) - balance_change) < 0.01:
                        transactions[i]["debit"], transactions[i]["credit"] = credit, debit
            elif debit > 0 and credit == 0:
                if balance_change > 0:
                    transactions[i]["credit"] = debit
                    transactions[i]["debit"] = 0.0
            elif credit > 0 and debit == 0:
                if balance_change < 0:
                    transactions[i]["debit"] = credit
                    transactions[i]["credit"] = 0.0

        return transactions

    def parse(self) -> Dict[str, object]:
        """Main parsing method."""
        logger.info("Starting Bank of Baroda parse")

        self.raw_text = self.extract_text_from_pdf()
        self.metadata = self.extract_metadata()
        logger.info("Extracted metadata: %s", self.metadata)

        self.transactions = self.smart_parse_with_balance_validation()
        logger.info("Parsed %s transactions", len(self.transactions))

        df = pd.DataFrame(self.transactions)

        if not df.empty:
            df = df.sort_values("date")
            df["date_formatted"] = df["date"].dt.strftime("%d/%m/%Y")
            df = df[["date_formatted", "description", "debit", "credit", "balance", "transaction_type"]]
            df.columns = ["Date", "Description", "Debit", "Credit", "Balance", "Type"]

        return {
            "metadata": self.metadata,
            "transactions_df": df,
            "transactions_list": self.transactions,
            "summary": {
                "total_transactions": len(self.transactions),
                "total_debits": df["Debit"].sum() if not df.empty else 0,
                "total_credits": df["Credit"].sum() if not df.empty else 0,
                "opening_balance": df["Balance"].iloc[0] if not df.empty else 0,
                "closing_balance": df["Balance"].iloc[-1] if not df.empty else 0,
            },
        }

    def export_csv(self, output_file: str = "bob_statement.csv") -> Optional[str]:
        """Export to CSV."""
        if self.transactions:
            df = pd.DataFrame(self.transactions)
            df["date"] = df["date"].dt.strftime("%d/%m/%Y")
            df.to_csv(output_file, index=False)
            logger.info("Exported to: %s", output_file)
            return output_file
        return None

    def export_excel(self, output_file: str = "bob_statement.xlsx") -> Optional[str]:
        """Export to Excel with metadata sheet."""
        if self.transactions:
            with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
                pd.DataFrame([self.metadata]).T.to_excel(writer, sheet_name="Account Info", header=["Value"])

                df = pd.DataFrame(self.transactions)
                df["date"] = df["date"].dt.strftime("%d/%m/%Y")
                df.to_excel(writer, sheet_name="Transactions", index=False)

                summary_data = {
                    "Metric": [
                        "Total Transactions",
                        "Total Debits",
                        "Total Credits",
                        "Net Change",
                        "Opening Balance",
                        "Closing Balance",
                    ],
                    "Value": [
                        len(self.transactions),
                        sum(t["debit"] for t in self.transactions),
                        sum(t["credit"] for t in self.transactions),
                        sum(t["credit"] - t["debit"] for t in self.transactions),
                        self.transactions[0]["balance"] if self.transactions else 0,
                        self.transactions[-1]["balance"] if self.transactions else 0,
                    ],
                }
                pd.DataFrame(summary_data).to_excel(writer, sheet_name="Summary", index=False)

            logger.info("Exported to: %s", output_file)
            return output_file
        return None
