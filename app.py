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
st._config.set_option('server.maxMessageSize', 200)

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
st.title("📄 Smart Statement Reader - M-PESA + Bank v3.0.4")

uploaded_file = st.file_uploader("Upload your PDF or CSV/XLSX Statement", type=["pdf", "csv", "xlsx"])

def clean_amount(x):
    try: return float(str(x).replace(",","").replace("KES","").strip())
    except: return 0.0

def clean_details(d):
    d = str(d)
    d = re.sub(r'Completed[-\s]*[\d,]+\.?\d*$', 'Completed', d, flags=re.IGNORECASE)
    d = re.sub(r'\s+', ' ', d).strip()
    return d

def categorize(details):
    d = details.lower()
    if 'fuliza' in d or 'overdraft' in d:
        if 'od loan' in d or 'repayment' in d: return 'Fuliza Repayment'
        if 'overdraft' in d and 'credit party' in d: return 'Fuliza Borrowed'
        if 'charge' in d or 'fee' in d: return 'Fuliza Interest/Charges'
        if 'merchant payment' in d or 'buy goods' in d: return 'Till Payment - Fuliza'
        return 'Fuliza Borrowed'
    
    if 'airtime' in d: return 'Airtime'
    if 'customer payment to small' in d or 'merchant payment' in d or 'buy goods' in d: return 'Till Payment'
    if ('small business' in d and 'pay merchant' in d) or ('small business' in d and 'pay to' in d): return 'Business Payment Sent'
    if 'micro sme business' in d or 'customer send money to micro' in d or ('small business' in d and 'payment to customer' in d): return 'Business Payment Received'
    if 'lipa na mpesa business' in d or 'business to business' in d or 'b2b' in d: return 'Business to Business'
    if 'business withdrawal' in d: return 'Business Withdrawal'
    if 'withdraw' in d and 'agent' in d: return 'Agent Withdrawal'
    if 'deposit' in d and 'agent' in d: return 'Agent Deposit'
    if 'payment to customer' in d and 'business' not in d: return 'Sent to Person'
    if 'received from' in d or 'funds received from' in d: return 'Received from Person'
    if 'pay bill' in d: return 'Paybill'
    if 'deposit' in d and 'agent' not in d: return 'Bank Deposit'
    return 'Other'

def get_in_out(amount):
    return (amount, 0.0) if amount > 0 else (0.0, abs(amount))

def parse_mpesa_text(full_text):
    data = []
    pattern = re.compile(
        r'([A-Z0-9]{10})\s+'                      # TXID
        r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+'  # Date Time
        r'(.*?)\s+'                               # Details - lazy
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
            raw_amount = clean_amount(row.get('Amount', clean_amount(row.get('Paid In', 0)) - clean_amount(row.get('Withdrawn', 0))))
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
            batch_size = 2
            progress_placeholder = st.empty()
            for idx, start in enumerate(range(1, total_pages + 1, batch_size)):
                end = min(start + batch_size - 1, total_pages)
                progress_placeholder.text(f"OCR Pages {start}-{end}/{total_pages}...")
                images = convert_from_bytes(file_bytes, dpi=100, first_page=start, last_page=end, fmt='jpg')
                for image in images:
                    text = pytesseract.image_to_string(image)
                    if text: full_text += "\n" + text
                    image.close()
                del images
                gc.collect()
            progress_placeholder.empty()
        data = parse_mpesa_text(full_text)

    df = pd.DataFrame(data, columns=['Date','Details','Paid In','Withdrawn','Category','Source'])
    df['Date'] = pd.to_datetime(df['Date'], format='%Y-%m-%d %H:%M:%S', errors='coerce')
    mask = df['Date'].isna()
    df.loc[mask, 'Date'] = pd.to_datetime(df.loc[mask, 'Date'], format='%d-%m-%Y %H:%M:%S', errors='coerce')
    df['Paid In'] = pd.to_numeric(df['Paid In'], downcast='float')
    df['Withdrawn'] = pd.to_numeric(df['Withdrawn'], downcast='float')
    df['Category'] = df['Category'].astype('category')
    return df

if uploaded_file:
    file_bytes = uploaded_file.getvalue()
    file_type = uploaded_file.name.split('.')[-1].lower()
    if 'df' not in st.session_state or st.session_state.get('filename')!= uploaded_file.name:
        with st.spinner("Processing file..."):
            df = load_and_process(file_bytes, file_type)
            st.session_state.df = df
            st.session_state.filename = uploaded_file.name
            st.session_state.summary = df.groupby('Category')[['Paid In', 'Withdrawn']].sum().reset_index()
            st.session_state.summary['Net'] = st.session_state.summary['Paid In'] - st.session_state.summary['Withdrawn']
    else:
        df = st.session_state.df
    st.success(f"Found {len(df)} transactions 🎉")

    col1, col2, col3 = st.columns(3)
    col1.metric("💰 Money In", f"KES {df['Paid In'].sum():,.2f}")
    col2.metric("💸 Money Out", f"KES {df['Withdrawn'].sum():,.2f}")
    col3.metric("📊 Net", f"KES {df['Paid In'].sum() - df['Withdrawn'].sum():,.2f}")

    st.divider()
    st.subheader("📊 Spending Breakdown by Category")
    st.dataframe(st.session_state.summary.sort_values('Withdrawn', ascending=False), use_container_width=True, hide_index=True)
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.sort_values('Date', ascending=False).to_excel(writer, sheet_name='All Transactions', index=False)
        st.session_state.summary.to_excel(writer, sheet_name='Category Summary', index=False)
    st.download_button("⬇️ Download Full Excel Report", output.getvalue(), f"statement_report_{datetime.now().strftime('%Y%m%d')}.xlsx")
    
    st.divider()
    st.subheader("📑 All Transactions")
    search = st.text_input("🔍 Search Details")
    df_filtered = df.copy()
    if search: df_filtered = df_filtered[df_filtered['Details'].str.contains(search, case=False, na=False)]
    st.dataframe(df_filtered.sort_values('Date', ascending=False), use_container_width=True, hide_index=True)
else:
    st.info("👆 Upload your M-PESA statement to get started")