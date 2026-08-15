import streamlit as st
import pandas as pd
import pdfplumber
import re
import io
from collections import defaultdict

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
st.title("📄 Smart Statement Reader - M-PESA + Bank v3.0.4.0")

uploaded_file = st.file_uploader("Upload your PDF or CSV/XLSX Statement", type=["pdf", "csv", "xlsx"])

def clean_amount(x):
    try: return float(str(x).replace(",","").replace("KES","").strip())
    except: return 0.0

def clean_details(d):
    return re.sub(r'\s+', ' ', str(d)).strip()

def categorize(details):
    d = details.lower()
    if 'od loan' in d and 'repayment' in d: return 'Fuliza Repayment'
    if 'agent' in d and ('deposit' in d or 'withdraw' in d): return 'Agent Deposit'
    if 'withdrawal from agent' in d: return 'Agent Withdrawal'
    if 'merchant customer payment' in d: return 'Received - Till'
    if 'fuliza' in d and 'merchant payment' in d: return 'Till Payment - Fuliza'
    if 'merchant payment' in d: return 'Till Payment'
    if 'fuliza' in d and ('till' in d or 'buy goods' in d): return 'Till Payment - Fuliza'
    if 'fuliza' in d: return 'Fuliza'
    if 'till' in d or 'buy goods' in d: return 'Till Payment'
    if 'airtime' in d: return 'Airtime'
    if 'pay bill' in d: return 'Paybill'
    if 'send money' in d: return 'Sent to Person'
    if 'received' in d or 'funds received' in d: return 'Received'
    if 'withdraw' in d: return 'Withdrawal'
    if 'deposit' in d: return 'Deposit'
    if 'charges' in d: return 'Charges' # NEW
    return 'Other'

def merge_tx_group(rows):
    """Merge all rows with same TXID into 1 row"""
    txid = rows[0][0]
    dt = rows[0][1]

    all_details = []
    total_in = 0.0
    total_out = 0.0
    categories = set()

    for r in rows:
        details, paid_in, withdrawn = r[2], r[3], r[4]
        all_details.append(details)
        total_in += paid_in
        total_out += withdrawn
        categories.add(categorize(details))

    # Priority: If any line is Fuliza Repayment, use that. Else if Charges present, add it
    main_cat = 'Other'
    if 'Fuliza Repayment' in categories: main_cat = 'Fuliza Repayment'
    elif 'Till Payment - Fuliza' in categories: main_cat = 'Till Payment - Fuliza'
    elif 'Till Payment' in categories: main_cat = 'Till Payment'
    elif 'Received - Till' in categories: main_cat = 'Received - Till'
    elif 'Fuliza' in categories: main_cat = 'Fuliza'
    elif 'Charges' in categories: main_cat = 'Charges'
    else: main_cat = list(categories)[0]

    merged_details = " | ".join(all_details)
    return [dt, f"{txid} | {clean_details(merged_details)}", total_in, total_out, main_cat, "M-PESA"]

@st.cache_data(show_spinner=False, ttl=3600)
def load_and_process(file_bytes, file_type):
    raw_rows = [] # [txid, dt, details, paid_in, withdrawn]

    if file_type in ['csv', 'xlsx']:
        df_raw = pd.read_csv(io.BytesIO(file_bytes)) if file_type == 'csv' else pd.read_excel(io.BytesIO(file_bytes))
        for _, row in df_raw.iterrows():
            txid = str(row.get('Receipt No.', ''))
            details = str(row.get('Details', ''))
            paid_in = clean_amount(row.get('Paid In', 0))
            withdrawn = clean_amount(row.get('Withdrawn', 0))
            raw_rows.append([txid, row.get('Completion Time', ''), details, paid_in, withdrawn])

    elif file_type == 'pdf':
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables(table_settings={"text_x_tolerance": 3, "text_y_tolerance": 3})
                if tables:
                    for table in tables:
                        for row in table[1:]:
                            if row and row[0] and len(row) == 7:
                                receipt_no, completion_time, details, status, paid_in, withdrawn, balance = row
                                raw_rows.append([receipt_no, completion_time, details, clean_amount(paid_in), clean_amount(withdrawn)])

    # GROUP BY TXID
    grouped = defaultdict(list)
    for r in raw_rows:
        grouped[r[0]].append(r)

    data = []
    for txid, rows in grouped.items():
        data.append(merge_tx_group(rows))

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

    st.success(f"Found {len(df)} grouped transactions 🎉")
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