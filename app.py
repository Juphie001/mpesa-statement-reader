import streamlit as st
import pandas as pd
import pdfplumber
import re
import io
import gc
from datetime import datetime
from pdf2image import convert_from_bytes, pdfinfo_from_bytes
import pytesseract

st.set_page_config(page_title="Smart Statement Reader", layout="wide")
st._config.set_option('server.maxUploadSize', 200)

def check_password():
    def password_entered():
        if st.session_state["password"] == st.secrets["password"]:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False
    if "password_correct" not in st.session_state:
        st.title("🔒 Smart Statement Reader")
        st.text_input("Enter Password", type="password", on_change=password_entered, key="password")
        st.caption("This app is private. Contact owner for access.")
        st.stop()
    elif not st.session_state["password_correct"]:
        st.title("🔒 Smart Statement Reader")
        st.text_input("Enter Password", type="password", on_change=password_entered, key="password")
        st.error("😕 Password incorrect")
        st.stop()

check_password()
st.title("📄 Smart Statement Reader - M-PESA + Bank v3.0.3.1")

uploaded_file = st.file_uploader("Upload your PDF or CSV/XLSX Statement", type=["pdf", "csv", "xlsx"])

def clean_amount(x):
    try: return float(str(x).replace(",","").replace("KES","").strip())
    except: return 0.0

def clean_details(d):
    return re.sub(r'\s+', ' ', str(d)).strip()

def categorize(details):
    d = details.lower()
    
    # NEW: Catch OD Loan Repayment first
    if 'od loan' in d and 'repayment' in d:
        return 'Fuliza Repayment'
        
    if 'fuliza' in d: return 'Fuliza'
    if 'airtime' in d: return 'Airtime'
    if 'till' in d or 'buy goods' in d: return 'Till Payment'
    if 'pay bill' in d: return 'Paybill'
    if 'send money' in d: return 'Sent to Person'
    if 'received' in d: return 'Received'
    if 'withdraw' in d: return 'Withdrawal'
    if 'deposit' in d: return 'Deposit'
    return 'Other'

def get_in_out(amount):
    return (amount, 0.0) if amount > 0 else (0.0, abs(amount))

def parse_mpesa_text(full_text):
    data = []
    pattern = re.compile(
        r'([A-Z0-9]{10})\s+'                      # TXID
        r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+'  # Date Time
        r'(.*?)\s+'                               # Details
        r'(-?[\d,]+\.?\d+)\s+'                    # Amount
        r'([\d,]+\.?\d+)'                         # Balance
    )
    for match in pattern.finditer(full_text):
        txid = match.group(1)
        dt = f"{match.group(2)} {match.group(3)}"
        details = clean_details(match.group(4))
        amount = clean_amount(match.group(5))
        
        cat = categorize(details)
        paid_in, withdrawn = get_in_out(amount)
        data.append([dt, f"{txid} | {details}", paid_in, withdrawn, cat, "M-PESA"])
    return data

@st.cache_data(show_spinner=False, ttl=3600)
def load_and_process(file_bytes, file_type):
    data = []
    if file_type in ['csv', 'xlsx']:
        df_raw = pd.read_csv(io.BytesIO(file_bytes)) if file_type == 'csv' else pd.read_excel(io.BytesIO(file_bytes))
        for _, row in df_raw.iterrows():
            details = str(row.get('Details', ''))
            raw_amount = clean_amount(row.get('Amount', 0))
            cat = categorize(details)
            paid_in, withdrawn = get_in_out(raw_amount)
            data.append([row.get('Completion Time', ''), f"{row.get('Receipt No.', '')} | {clean_details(details)}", paid_in, withdrawn, cat, "M-PESA"])
    else:
        full_text = ""
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text: full_text += "\n" + text
        except: pass
        if len(full_text.strip()) < 50:
            info = pdfinfo_from_bytes(file_bytes)
            total_pages = info["Pages"]
            for start in range(1, total_pages + 1, 2):
                end = min(start + 1, total_pages)
                images = convert_from_bytes(file_bytes, dpi=100, first_page=start, last_page=end)
                for image in images:
                    text = pytesseract.image_to_string(image)
                    if text: full_text += "\n" + text
                    image.close()
                gc.collect()
        data = parse_mpesa_text(full_text)

    df = pd.DataFrame(data, columns=['Date','Details','Paid In','Withdrawn','Category','Source'])
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    df['Paid In'] = pd.to_numeric(df['Paid In'])
    df['Withdrawn'] = pd.to_numeric(df['Withdrawn'])
    return df

if uploaded_file:
    file_bytes = uploaded_file.getvalue()
    file_type = uploaded_file.name.split('.')[-1].lower()
    with st.spinner("Processing file..."):
        df = load_and_process(file_bytes, file_type)
    
    st.success(f"Found {len(df)} transactions 🎉")
    col1, col2, col3 = st.columns(3)
    col1.metric("💰 Money In", f"KES {df['Paid In'].sum():,.2f}")
    col2.metric("💸 Money Out", f"KES {df['Withdrawn'].sum():,.2f}")
    col3.metric("📊 Net", f"KES {df['Paid In'].sum() - df['Withdrawn'].sum():,.2f}")

    st.subheader("📊 Category Summary")
    summary = df.groupby('Category')[['Paid In', 'Withdrawn']].sum().reset_index()
    st.dataframe(summary, use_container_width=True, hide_index=True)
    
    st.subheader("📑 All Transactions")
    st.dataframe(df.sort_values('Date', ascending=False), use_container_width=True, hide_index=True)
    
    csv = df.to_csv(index=False).encode()
    st.download_button("⬇️ Download CSV", csv, "statement.csv", mime="text/csv")
else:
    st.info("👆 Upload your M-PESA statement to get started")