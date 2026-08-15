import streamlit as st
import pandas as pd
import pdfplumber
import re
import io

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
        st.stop()
    elif not st.session_state["password_correct"]:
        st.title("🔒 Smart Statement Reader")
        st.text_input("Enter Password", type="password", on_change=password_entered, key="password")
        st.error("😕 Password incorrect")
        st.stop()

check_password()
st.title("📄 Smart Statement Reader - M-PESA + Bank v3.0.3.8")

uploaded_file = st.file_uploader("Upload your PDF or CSV/XLSX Statement", type=["pdf", "csv", "xlsx"])

def clean_amount(x):
    try: return float(str(x).replace(",","").replace("KES","").strip())
    except: return 0.0

def clean_details(d):
    return re.sub(r'\s+', ' ', str(d)).strip()

def categorize(details):
    d = details.lower()
    if 'od loan' in d and 'repayment' in d: return 'Fuliza Repayment'
    if 'fuliza' in d: return 'Fuliza'
    if 'airtime' in d: return 'Airtime'
    if 'till' in d or 'buy goods' in d or 'merchant payment' in d: return 'Till Payment' # NEW
    if 'pay bill' in d: return 'Paybill'
    if 'send money' in d: return 'Sent to Person'
    if 'received' in d or 'funds received' in d: return 'Received'
    if 'withdraw' in d: return 'Withdrawal'
    if 'deposit' in d: return 'Deposit'
    return 'Other'

@st.cache_data(show_spinner=False, ttl=3600)
def load_and_process(file_bytes, file_type):
    data = []
    seen_txids = set() # NEW: for TXID dedup

    if file_type in ['csv', 'xlsx']:
        df_raw = pd.read_csv(io.BytesIO(file_bytes)) if file_type == 'csv' else pd.read_excel(io.BytesIO(file_bytes))
        for _, row in df_raw.iterrows():
            txid = str(row.get('Receipt No.', ''))
            if txid in seen_txids: continue # skip duplicate TXID
            seen_txids.add(txid)

            details = str(row.get('Details', ''))
            paid_in = clean_amount(row.get('Paid In', 0))
            withdrawn = clean_amount(row.get('Withdrawn', 0))
            cat = categorize(details)
            data.append([row.get('Completion Time', ''), f"{txid} | {clean_details(details)}", paid_in, withdrawn, cat, "M-PESA"])

    elif file_type == 'pdf':
        all_rows = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables(table_settings={"text_x_tolerance": 3, "text_y_tolerance": 3})
                if tables:
                    for table in tables:
                        for row in table[1:]:
                            if row and row[0]:
                                all_rows.append(row)

        for row in all_rows:
            if len(row) == 7:
                receipt_no, completion_time, details, status, paid_in, withdrawn, balance = row

                if receipt_no in seen_txids: continue # skip duplicate TXID
                seen_txids.add(receipt_no)

                paid_in = clean_amount(paid_in)
                withdrawn = clean_amount(withdrawn)
                cat = categorize(details)
                data.append([completion_time, f"{receipt_no} | {clean_details(details)}", paid_in, withdrawn, cat, "M-PESA"])

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

    if df.empty:
        st.error("No transactions found.")
        st.stop()

    st.success(f"Found {len(df)} unique transactions 🎉")
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