import pandas as pd
import re
from datetime import datetime
import streamlit as st
from typing import Tuple, Dict, List
import io
import chardet
import PyPDF2
import pdfplumber

# Base Parser Class
class BankStatementParser:
    def __init__(self):
        self.account_info = {}
        self.transactions = []
    
    def parse_date(self, date_str: str) -> datetime:
        """Parse date string to datetime object"""
        try:
            if not date_str or date_str.strip() == '':
                return None
            # Try different date formats
            for fmt in ['%d/%m/%y', '%d/%m/%Y', '%d-%m-%Y', '%d-%m-%y', '%d.%m.%Y', '%d %b %Y']:
                try:
                    return datetime.strptime(date_str.strip(), fmt)
                except:
                    continue
            return None
        except:
            return None
    
    def clean_amount(self, amount_str: str) -> float:
        """Clean and convert amount string to float"""
        if not amount_str or amount_str.strip() == '':
            return 0.0
        try:
            # Remove commas and any non-numeric characters except decimal point
            cleaned = re.sub(r'[^\d.]', '', str(amount_str).replace(',', ''))
            return float(cleaned) if cleaned else 0.0
        except:
            return 0.0
    
    def get_standardized_dataframe(self) -> pd.DataFrame:
        """Create standardized DataFrame with common columns"""
        if not self.transactions:
            return pd.DataFrame()
        
        df = pd.DataFrame(self.transactions)
        
        # Ensure we have all required columns
        required_cols = ['Date', 'Narration', 'Debit', 'Credit', 'Balance']
        for col in required_cols:
            if col not in df.columns:
                df[col] = None
        
        # Select only the required columns in order
        df = df[required_cols]
        
        # Sort by date
        df = df.sort_values('Date').reset_index(drop=True)
        
        return df

# HDFC Bank Parser
class HDFCStatementParser(BankStatementParser):
    def __init__(self):
        super().__init__()
        self.bank_name = "HDFC Bank"
    
    def parse_statement(self, text_content: str) -> Tuple[pd.DataFrame, Dict]:
        """Parse HDFC bank statement"""
        try:
            self._extract_account_info(text_content)
            self._extract_transactions(text_content)
            df = self.get_standardized_dataframe()
            return df, self.account_info
        except Exception as e:
            raise Exception(f"HDFC Parser Error: {str(e)}")
    
    def _extract_account_info(self, text: str):
        """Extract account information from HDFC statement"""
        # Account holder name
        holder_match = re.search(r'M\.S\.\s+([^\n]+)', text)
        if holder_match:
            self.account_info['Account Holder'] = holder_match.group(1).strip()
        
        # Account number
        acc_match = re.search(r'Account No\s*[:\-]\s*(\d+)', text, re.IGNORECASE)
        if not acc_match:
            acc_match = re.search(r'(\d{12,18})', text)  # Look for typical account number
        if acc_match:
            self.account_info['Account Number'] = acc_match.group(1)
        
        # IFSC Code
        ifsc_match = re.search(r'IFSC\s*[:\-]\s*([A-Z0-9]{11})', text, re.IGNORECASE)
        if ifsc_match:
            self.account_info['IFSC Code'] = ifsc_match.group(1)
        
        # Branch
        branch_match = re.search(r'Account Branch\s*[:\-]\s*([^\n]+)', text)
        if branch_match:
            self.account_info['Branch'] = branch_match.group(1).strip()
        
        # Period
        period_match = re.search(r'From\s*[:\-]\s*(\d{2}/\d{2}/\d{4})\s*To\s*[:\-]\s*(\d{2}/\d{2}/\d{4})', text)
        if period_match:
            self.account_info['From Date'] = period_match.group(1)
            self.account_info['To Date'] = period_match.group(2)
        
        self.account_info['Bank'] = self.bank_name
    
    def _extract_transactions(self, text: str):
        """Extract transactions from HDFC statement"""
        # Split by pages
        pages = re.split(r'=+ Page \d+ =+', text)
        
        for page in pages:
            lines = page.split('\n')
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                
                # Check for transaction line (starts with date)
                if re.match(r'\d{2}/\d{2}/\d{2}', line):
                    transaction = self._parse_transaction_line(line, lines, i)
                    if transaction:
                        self.transactions.append(transaction)
                i += 1
    
    def _parse_transaction_line(self, line: str, all_lines: List[str], current_index: int) -> Dict:
        """Parse individual transaction line for HDFC"""
        try:
            # Split by multiple spaces
            parts = re.split(r'\s{2,}', line.strip())
            
            if len(parts) < 4:
                return None
            
            date = parts[0]
            narration = parts[1] if len(parts) > 1 else ""
            ref_no = parts[2] if len(parts) > 2 else ""
            value_date = parts[3] if len(parts) > 3 else ""
            
            # Extract amounts
            amount_pattern = r'([\d,]+\.\d{2})'
            amounts = re.findall(amount_pattern, line)
            
            debit = 0.0
            credit = 0.0
            balance = 0.0
            
            if len(amounts) >= 3:
                debit = self.clean_amount(amounts[-3])
                credit = self.clean_amount(amounts[-2])
                balance = self.clean_amount(amounts[-1])
            elif len(amounts) == 2:
                # Determine if it's debit or credit based on context
                if 'DR' in line.upper() or 'WITHDRAWAL' in line.upper():
                    debit = self.clean_amount(amounts[0])
                else:
                    credit = self.clean_amount(amounts[0])
                balance = self.clean_amount(amounts[1])
            elif len(amounts) == 1:
                balance = self.clean_amount(amounts[0])
            
            # Get full narration (multi-line)
            full_narration = narration
            next_idx = current_index + 1
            while (next_idx < len(all_lines) and 
                   not re.match(r'\d{2}/\d{2}/\d{2}', all_lines[next_idx].strip()) and
                   all_lines[next_idx].strip() != ''):
                full_narration += " " + all_lines[next_idx].strip()
                next_idx += 1
            
            return {
                'Date': self.parse_date(date),
                'Narration': full_narration.strip(),
                'Debit': debit,
                'Credit': credit,
                'Balance': balance
            }
            
        except Exception as e:
            print(f"HDFC Transaction parsing error: {e}")
            return None

# Kotak Bank Parser
class KotakStatementParser(BankStatementParser):
    def __init__(self):
        super().__init__()
        self.bank_name = "Kotak Bank"
    
    def parse_statement(self, text_content: str) -> Tuple[pd.DataFrame, Dict]:
        """Parse Kotak bank statement"""
        try:
            self._extract_account_info(text_content)
            self._extract_transactions(text_content)
            df = self.get_standardized_dataframe()
            return df, self.account_info
        except Exception as e:
            raise Exception(f"Kotak Parser Error: {str(e)}")
    
    def _extract_account_info(self, text: str):
        """Extract account information from Kotak statement"""
        # Account holder name
        holder_match = re.search(r'Account Name\s*[:\-]\s*([^\n]+)', text, re.IGNORECASE)
        if holder_match:
            self.account_info['Account Holder'] = holder_match.group(1).strip()
        
        # Account number
        acc_match = re.search(r'Account No\s*[:\-]\s*(\d+)', text, re.IGNORECASE)
        if not acc_match:
            acc_match = re.search(r'Account Number\s*[:\-]\s*(\d+)', text, re.IGNORECASE)
        if acc_match:
            self.account_info['Account Number'] = acc_match.group(1)
        
        # IFSC Code
        ifsc_match = re.search(r'IFSC\s*[:\-]\s*([A-Z0-9]{11})', text, re.IGNORECASE)
        if ifsc_match:
            self.account_info['IFSC Code'] = ifsc_match.group(1)
        
        # Branch
        branch_match = re.search(r'Branch\s*[:\-]\s*([^\n]+)', text, re.IGNORECASE)
        if branch_match:
            self.account_info['Branch'] = branch_match.group(1).strip()
        
        # Period
        period_match = re.search(r'Period\s*[:\-]\s*(\d{2}/\d{2}/\d{4})\s*to\s*(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
        if period_match:
            self.account_info['From Date'] = period_match.group(1)
            self.account_info['To Date'] = period_match.group(2)
        
        self.account_info['Bank'] = self.bank_name
    
    def _extract_transactions(self, text: str):
        """Extract transactions from Kotak statement"""
        # Kotak statement typically has a table format
        lines = text.split('\n')
        
        # Find the transaction table header
        start_index = -1
        for i, line in enumerate(lines):
            if re.search(r'Date\s+Description\s+Debit\s+Credit\s+Balance', line, re.IGNORECASE):
                start_index = i + 1
                break
        
        if start_index == -1:
            # Alternative header pattern
            for i, line in enumerate(lines):
                if re.search(r'Date\s+Narration\s+Withdrawal\s+Deposit\s+Balance', line, re.IGNORECASE):
                    start_index = i + 1
                    break
        
        if start_index == -1:
            # Look for transaction lines directly
            start_index = 0
        
        # Process transactions
        i = start_index
        while i < len(lines):
            line = lines[i].strip()
            
            # Check for transaction line (starts with date)
            if re.match(r'\d{2}/\d{2}/\d{4}', line) or re.match(r'\d{2}/\d{2}/\d{2}', line):
                transaction = self._parse_transaction_line(line, lines, i)
                if transaction:
                    self.transactions.append(transaction)
            
            i += 1
    
    def _parse_transaction_line(self, line: str, all_lines: List[str], current_index: int) -> Dict:
        """Parse individual transaction line for Kotak"""
        try:
            # Kotak format: Date, Description/Narration, Debit, Credit, Balance
            parts = re.split(r'\s{2,}', line.strip())
            
            if len(parts) < 3:
                return None
            
            date = parts[0]
            narration = parts[1] if len(parts) > 1 else ""
            
            # Find amounts - they could be in different positions
            amount_pattern = r'([\d,]+\.\d{2})'
            amounts = re.findall(amount_pattern, line)
            
            debit = 0.0
            credit = 0.0
            balance = 0.0
            
            if len(amounts) >= 3:
                # Typically: Debit, Credit, Balance
                debit = self.clean_amount(amounts[-3])
                credit = self.clean_amount(amounts[-2])
                balance = self.clean_amount(amounts[-1])
            elif len(amounts) == 2:
                # Could be Debit/Credit and Balance
                if 'DR' in line.upper() or 'WITHDRAWAL' in line.upper():
                    debit = self.clean_amount(amounts[0])
                else:
                    credit = self.clean_amount(amounts[0])
                balance = self.clean_amount(amounts[1])
            elif len(amounts) == 1:
                balance = self.clean_amount(amounts[0])
            
            # Get full narration
            full_narration = narration
            next_idx = current_index + 1
            while (next_idx < len(all_lines) and 
                   not re.match(r'\d{2}/\d{2}/\d{2,4}', all_lines[next_idx].strip()) and
                   all_lines[next_idx].strip() != '' and
                   not re.search(r'Closing Balance|Page', all_lines[next_idx].strip(), re.IGNORECASE)):
                full_narration += " " + all_lines[next_idx].strip()
                next_idx += 1
            
            return {
                'Date': self.parse_date(date),
                'Narration': full_narration.strip(),
                'Debit': debit,
                'Credit': credit,
                'Balance': balance
            }
            
        except Exception as e:
            print(f"Kotak Transaction parsing error: {e}")
            return None

# File Processing Utilities
class FileProcessor:
    @staticmethod
    def detect_encoding(content: bytes) -> str:
        """Detect file encoding"""
        try:
            result = chardet.detect(content)
            encoding = result.get('encoding', 'utf-8')
            confidence = result.get('confidence', 0)
            
            # Fallback to common encodings if confidence is low
            if confidence < 0.7:
                encodings_to_try = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
                for enc in encodings_to_try:
                    try:
                        content.decode(enc)
                        return enc
                    except UnicodeDecodeError:
                        continue
            return encoding or 'utf-8'
        except:
            return 'utf-8'
    
    @staticmethod
    def extract_text_from_pdf(pdf_content: bytes) -> str:
        """Extract text from PDF using multiple methods"""
        text = ""
        
        # Method 1: Try pdfplumber (better for text extraction)
        try:
            with pdfplumber.open(io.BytesIO(pdf_content)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            if text.strip():
                return text
        except Exception as e:
            st.warning(f"pdfplumber failed: {e}")
        
        # Method 2: Try PyPDF2 as fallback
        try:
            pdf_file = io.BytesIO(pdf_content)
            pdf_reader = PyPDF2.PdfReader(pdf_file)
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        except Exception as e:
            st.warning(f"PyPDF2 failed: {e}")
        
        return text
    
    @staticmethod
    def process_uploaded_file(uploaded_file) -> str:
        """Process uploaded file and return text content"""
        try:
            file_content = uploaded_file.getvalue()
            
            # Check if it's PDF
            if uploaded_file.type == 'application/pdf' or uploaded_file.name.lower().endswith('.pdf'):
                st.info("📄 PDF file detected. Extracting text...")
                text_content = FileProcessor.extract_text_from_pdf(file_content)
                if not text_content.strip():
                    raise Exception("No text could be extracted from PDF")
                return text_content
            
            # For text files, detect encoding and decode
            else:
                encoding = FileProcessor.detect_encoding(file_content)
                st.info(f"📝 Text file detected. Using encoding: {encoding}")
                return file_content.decode(encoding)
                
        except UnicodeDecodeError as e:
            st.error(f"Encoding error: {e}")
            # Try fallback encodings
            fallback_encodings = ['latin-1', 'cp1252', 'iso-8859-1', 'utf-16']
            file_content = uploaded_file.getvalue()
            
            for encoding in fallback_encodings:
                try:
                    st.info(f"Trying fallback encoding: {encoding}")
                    return file_content.decode(encoding)
                except UnicodeDecodeError:
                    continue
            
            raise Exception(f"Could not decode file with any encoding. Tried: {fallback_encodings}")
        
        except Exception as e:
            raise Exception(f"File processing error: {str(e)}")

# Main Parser Factory
class BankStatementProcessor:
    def __init__(self):
        self.parsers = {
            'HDFC Bank': HDFCStatementParser,
            'Kotak Bank': KotakStatementParser
        }
    
    def get_supported_banks(self) -> List[str]:
        """Get list of supported banks"""
        return list(self.parsers.keys())
    
    def parse_statement(self, bank_name: str, file_content: str) -> Tuple[pd.DataFrame, Dict]:
        """Parse statement based on bank type"""
        if bank_name not in self.parsers:
            raise ValueError(f"Unsupported bank: {bank_name}. Supported banks: {list(self.parsers.keys())}")
        
        parser_class = self.parsers[bank_name]
        parser = parser_class()
        return parser.parse_statement(file_content)

# Streamlit UI Application
def main():
    st.set_page_config(
        page_title="Multi-Bank Statement Parser",
        page_icon="🏦",
        layout="wide"
    )
    
    st.title("🏦 Multi-Bank Statement Parser")
    st.markdown("Upload your bank statements to parse and analyze transactions")
    
    # Initialize processor
    processor = BankStatementProcessor()
    file_processor = FileProcessor()
    
    # Sidebar for bank selection
    st.sidebar.header("Bank Selection")
    selected_bank = st.sidebar.selectbox(
        "Choose Your Bank",
        options=processor.get_supported_banks(),
        help="Select your bank to use the appropriate parser"
    )
    
    # File upload
    uploaded_file = st.file_uploader(
        f"Upload {selected_bank} Statement",
        type=['pdf', 'txt', 'csv'],
        help=f"Upload {selected_bank} statement in PDF, TXT, or CSV format"
    )
    
    if uploaded_file:
        try:
            # Process file and get text content
            with st.spinner("Processing file..."):
                text_content = file_processor.process_uploaded_file(uploaded_file)
            
            # Show file info
            st.success(f"✅ File processed successfully! Size: {len(text_content)} characters")
            
            # Optional: Show raw text preview
            with st.expander("📋 View Raw Text Preview"):
                st.text_area("Raw Text (first 2000 chars)", text_content[:2000], height=200)
            
            # Parse statement
            with st.spinner(f"Parsing {selected_bank} statement..."):
                df, account_info = processor.parse_statement(selected_bank, text_content)
            
            if df is not None and not df.empty:
                st.success(f"✅ Successfully parsed {len(df)} transactions from {selected_bank}!")
                
                # Display account information
                st.subheader("📋 Account Information")
                if account_info:
                    info_data = []
                    for key, value in account_info.items():
                        info_data.append({"Field": key, "Value": value})
                    info_df = pd.DataFrame(info_data)
                    st.table(info_df)
                else:
                    st.info("No account information found in the statement")
                
                # Display transaction summary
                st.subheader("💰 Transaction Summary")
                col1, col2, col3, col4 = st.columns(4)
                
                total_debit = df['Debit'].sum()
                total_credit = df['Credit'].sum()
                final_balance = df['Balance'].iloc[-1] if not df.empty else 0
                
                with col1:
                    st.metric("Total Transactions", len(df))
                with col2:
                    st.metric("Total Debit", f"₹{total_debit:,.2f}")
                with col3:
                    st.metric("Total Credit", f"₹{total_credit:,.2f}")
                with col4:
                    st.metric("Final Balance", f"₹{final_balance:,.2f}")
                
                # Display transactions
                st.subheader("📊 Transactions")
                
                # Add filters
                col1, col2 = st.columns(2)
                with col1:
                    if not df.empty and df['Date'].notna().any():
                        min_date = df['Date'].min()
                        max_date = df['Date'].max()
                        date_range = st.date_input(
                            "Filter by Date Range",
                            value=(min_date, max_date),
                            min_value=min_date,
                            max_value=max_date
                        )
                
                with col2:
                    search_term = st.text_input("Search in Narration")
                
                # Apply filters
                filtered_df = df.copy()
                if not df.empty and df['Date'].notna().any():
                    if len(date_range) == 2:
                        filtered_df = filtered_df[
                            (filtered_df['Date'] >= pd.to_datetime(date_range[0])) & 
                            (filtered_df['Date'] <= pd.to_datetime(date_range[1]))
                        ]
                
                if search_term:
                    filtered_df = filtered_df[
                        filtered_df['Narration'].str.contains(search_term, case=False, na=False)
                    ]
                
                st.dataframe(filtered_df, use_container_width=True)
                
                # Additional analysis
                st.subheader("📈 Financial Analysis")
                
                tab1, tab2, tab3 = st.tabs(["Monthly Summary", "Transaction Types", "Export Data"])
                
                with tab1:
                    # Monthly summary
                    if not df.empty and df['Date'].notna().any():
                        monthly_df = df.copy()
                        monthly_df['Month'] = monthly_df['Date'].dt.to_period('M')
                        monthly_summary = monthly_df.groupby('Month').agg({
                            'Debit': 'sum',
                            'Credit': 'sum',
                            'Date': 'count'
                        }).rename(columns={'Date': 'Transaction Count'})
                        
                        st.write("Monthly Transaction Summary:")
                        st.dataframe(monthly_summary)
                        
                        # Monthly chart
                        monthly_chart_data = monthly_summary[['Debit', 'Credit']].reset_index()
                        monthly_chart_data['Month'] = monthly_chart_data['Month'].astype(str)
                        st.bar_chart(monthly_chart_data.set_index('Month'))
                    else:
                        st.info("No date data available for monthly analysis")
                
                with tab2:
                    # Transaction type analysis
                    if not df.empty:
                        df['Transaction Type'] = df.apply(
                            lambda x: 'Debit' if x['Debit'] > 0 else 'Credit', 
                            axis=1
                        )
                        type_counts = df['Transaction Type'].value_counts()
                        
                        col1, col2 = st.columns(2)
                        with col1:
                            st.write("Transaction Type Distribution:")
                            st.dataframe(type_counts)
                        with col2:
                            st.bar_chart(type_counts)
                
                with tab3:
                    # Export options
                    st.write("Download Parsed Data:")
                    
                    # CSV Download
                    csv = filtered_df.to_csv(index=False)
                    st.download_button(
                        label="📥 Download as CSV",
                        data=csv,
                        file_name=f"{selected_bank.replace(' ', '_')}_statement.csv",
                        mime="text/csv"
                    )
                    
                    # Excel Download
                    excel_buffer = io.BytesIO()
                    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                        filtered_df.to_excel(writer, sheet_name='Transactions', index=False)
                        if account_info:
                            info_df = pd.DataFrame([account_info])
                            info_df.to_excel(writer, sheet_name='Account Info', index=False)
                    
                    st.download_button(
                        label="📥 Download as Excel",
                        data=excel_buffer.getvalue(),
                        file_name=f"{selected_bank.replace(' ', '_')}_statement.xlsx",
                        mime="application/vnd.ms-excel"
                    )
                
            else:
                st.error("❌ No transactions found in the statement")
                
        except Exception as e:
            st.error(f"❌ Error processing statement: {str(e)}")
            st.info("""
            💡 **Troubleshooting Tips:**
            - Make sure you've selected the correct bank
            - Ensure the file is not password protected
            - Try uploading a different file format (PDF/TXT)
            - Check if the statement contains transaction data
            """)
    
    else:
        # Show instructions when no file is uploaded
        st.info("👆 Please upload a bank statement to get started")
        
        with st.expander("ℹ️ Instructions & Supported Formats"):
            st.markdown(f"""
            ### Supported Banks:
            {', '.join(processor.get_supported_banks())}
            
            ### Expected Data Format:
            All parsers extract the following standardized columns:
            - **Date**: Transaction date
            - **Narration**: Transaction description
            - **Debit**: Amount withdrawn
            - **Credit**: Amount deposited  
            - **Balance**: Closing balance after transaction
            
            ### Supported File Formats:
            - **PDF**: Bank statements in PDF format (text-based, not scanned)
            - **TXT**: Plain text files
            - **CSV**: Comma-separated values
            
            ### File Requirements:
            - Files should contain extractable text
            - PDFs should not be scanned images (OCR not supported yet)
            - Minimum file size: 1KB, Maximum: 200MB
            
            ### How to Use:
            1. Select your bank from the dropdown
            2. Upload your statement file
            3. View parsed data and analysis
            4. Download results in CSV/Excel format
            """)

# Required installations
def show_installation_instructions():
    st.sidebar.markdown("---")
    st.sidebar.subheader("Installation Requirements")
    st.sidebar.code("""
pip install pandas streamlit chardet PyPDF2 pdfplumber openpyxl
""")

if __name__ == "__main__":
    show_installation_instructions()
    main()
