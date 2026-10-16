import streamlit as st
import pandas as pd
import pdfplumber
import re
import io
from collections import defaultdict

st.set_page_config(page_title="Smart Statement Reader", layout="wide")

def check_password():
    def password_entered():
        if st.session_state["password"] == st.secrets.get("password", "1234"):
            st.session_state["password_correct"] = True
            if "password" in st.session_state:
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
st.title("📄 Smart Statement Reader")
statement_mode = st.radio("Select Statement Type", ["M-Pesa", "Bank Statement"], horizontal=True)
uploaded_file = st.file_uploader("Upload your PDF or CSV/XLSX Statement", type=["pdf", "csv", "xlsx"])

def clean_amount(x):
    try:
        if pd.isna(x) or x == '': return 0.0
        s = str(x).replace(",", "").replace("KES", "").replace("Cr", "").replace("Dr", "").strip()
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        return float(s)
    except: return 0.0

def clean_details(d):
    return re.sub(r'\s+', ' ', str(d)).strip()

def categorize(details, amount_in):
    d = str(details).lower()
    d = re.sub(r'\s+', ' ', d).strip()
    if 'via api' in d and amount_in >= 3000: return 'Loan Taken'
    if 'od loan' in d and 'repayment' in d: return 'Fuliza Repayment'
    if 'small business withdrawal' in d and 'to mpesa account' in d: return 'Self Transfer'
    if 'business account to mpesa' in d: return 'Self Transfer'
    if 'withdrawal' in d and 'agent' in d: return 'Agent Withdrawal'
    if 'withdrawal from agent' in d: return 'Agent Withdrawal'
    if 'deposit' in d and 'agent' in d: return 'Agent Deposit'
    if 'bundle' in d: return 'Airtime'
    if 'airtime' in d: return 'Airtime'
    if 'micro' in d and 'sme' in d and 'received from' in d: return 'Received - Business'
    if 'small business' in d and 'received' in d: return 'Received - Business'
    if 'customer payment' in d and 'small business' in d: return 'Received - Business'
    if 'business' in d and 'received from' in d: return 'Received - Business'
    if 'sme business' in d and 'received' in d: return 'Received - Business'
    if 'small business payment' in d and 'to customer' in d: return 'Sent - Business'
    if 'small business transfer to' in d: return 'Sent - Business'
    if 'small business transfer from' in d: return 'Received - Business'
    if 'customer transfer to' in d: return 'Sent to Person'
    if 'transfer to' in d and '254' in d and 'received' not in d: return 'Sent to Person'
    if 'customer transfer from' in d: return 'Received'
    if 'customer transfer' in d and 'fuliza' in d: return 'Sent to Person'
    if 'fuliza' in d and 'merchant payment' in d: return 'Till Payment - Fuliza'
    if 'fuliza' in d and ('till' in d or 'buy goods' in d): return 'Till Payment - Fuliza'
    if 'pay bill' in d: return 'Paybill'
    if 'pay merchant' in d: return 'Till Payment'
    if 'merchant customer payment' in d: return 'Received - Till'
    if 'merchant payment' in d: return 'Till Payment'
    if 'till' in d or 'buy goods' in d: return 'Till Payment'
    if 'received from' in d: return 'Received'
    if 'funds received' in d: return 'Received'
    if 'received' in d and 'business' in d: return 'Received - Business'
    if 'send money' in d: return 'Sent to Person'
    if 'withdraw' in d: return 'Withdrawal'
    if 'deposit' in d: return 'Deposit'
    if 'charges' in d or 'charge' in d or 'transaction cost' in d: return 'Charges'
    return 'Other'

def categorize_bank(narrative):
    n = str(narrative).lower()
    if 'mpesa' in n and 'payment to' in n: return 'Sent - M-Pesa'
    if 'mmp' in n or 'mpesa ref' in n: return 'Received - M-Pesa'
    if 'salary' in n or 'payroll' in n: return 'Salary'
    if 'charge' in n or 'commission' in n or 'fee' in n: return 'Charges'
    if 'transfer' in n: return 'Bank Transfer'
    if 'atm' in n or 'withdrawal' in n: return 'Withdrawal'
    if 'deposit' in n: return 'Deposit'
    return 'Other Bank'

def merge_tx_group(rows):
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
        cat = categorize(details, paid_in)
        if cat!= 'Charges': categories.add(cat)
        else:
            if not categories: categories.add(cat)
    main_cat = 'Other'
    priority = ['Loan Taken', 'Fuliza Repayment', 'Self Transfer', 'Received - Business', 'Sent - Business',
                'Till Payment - Fuliza', 'Till Payment', 'Paybill', 'Airtime', 'Sent to Person',
                'Received - Till', 'Received', 'Agent Withdrawal', 'Agent Deposit', 'Withdrawal', 'Deposit', 'Charges']
    for p in priority:
        if p in categories:
            main_cat = p
            break
    if main_cat == 'Other' and categories:
        main_cat = list(categories)[0]
    merged_details = " | ".join(all_details)
    return [dt, f"{txid} | {clean_details(merged_details)}", total_in, total_out, main_cat, "M-PESA"]

def load_and_process_mpesa(file_bytes, file_type):
    raw_rows = []
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
    grouped = defaultdict(list)
    for r in raw_rows: grouped[r[0]].append(r)
    data = []
    for txid, rows in grouped.items(): data.append(merge_tx_group(rows))
    df = pd.DataFrame(data, columns=['Date','Details','Paid In','Withdrawn','Category','Source'])
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    return df

def load_and_process_bank(file_bytes, file_type):
    data = []
    if file_type in ['csv', 'xlsx']:
        df_raw = pd.read_csv(io.BytesIO(file_bytes)) if file_type == 'csv' else pd.read_excel(io.BytesIO(file_bytes))
        df_raw.columns = [c.strip() for c in df_raw.columns]
        for _, row in df_raw.iterrows():
            tran_date = str(row.get('Tran Date', row.get('Date','')))
            withdrawals = clean_amount(row.get('Withdrawals',0))
            deposits = clean_amount(row.get('Deposits',0))
            narrative = str(row.get('Transaction Narrative', row.get('Narrative','')))
            ref = str(row.get('Ref No',''))
            if withdrawals==0 and deposits==0: continue
            details = f"{clean_details(ref)} | {clean_details(narrative)}".strip(" |")
            data.append([tran_date, details, deposits, withdrawals, categorize_bank(narrative), "BANK"])
    else:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables(table_settings={"text_x_tolerance": 2, "text_y_tolerance": 3, "intersection_y_tolerance": 8})
                if not tables: continue
                for table in tables:
                    if not table or len(table) < 2: continue
                    for row in table[1:]:
                        if not row: continue
                        row = [str(r or "").strip() for r in row]
                        full_line = " ".join(row)
                        low = full_line.lower()
                        if 'tran date' in low or 'balance b/f' in low or low.strip() == '':
                            continue
                        if not re.search(r'\d{2}-\d{2}-\d{2,4}', full_line):
                            continue

                        narrative = row[-1] if row[-1] else (row[-2] if len(row)>=2 else "")
                        amounts = re.findall(r'[\d,]+\.\d{2}', full_line)
                        if not amounts: continue

                        # Last amount is Balance
                        remaining = amounts[:-1]
                        if not remaining: continue

                        w, d = 0.0, 0.0
                        n_low = narrative.lower()

                        if len(remaining) == 1:
                            amt = clean_amount(remaining[0])
                            # SMART CHECK: Payment to = OUT, Mpesa Ref = IN
                            if 'payment to' in n_low or ('transfer to' in n_low and '254' in n_low):
                                w = amt
                            elif 'mmp' in n_low or 'mpesa ref' in n_low or 'ref' in n_low and 'payment' not in n_low:
                                # For your 7k example - it's a deposit
                                d = amt
                            else:
                                w = amt
                        else:
                            w = clean_amount(remaining[0])
                            d = clean_amount(remaining[1])

                        if w==0 and d==0: continue
                        tran_date = re.search(r'\d{2}-\d{2}-\d{2,4}', full_line).group()
                        data.append([tran_date, clean_details(narrative), d, w, categorize_bank(narrative), "BANK"])

    df = pd.DataFrame(data, columns=['Date','Details','Paid In','Withdrawn','Category','Source'])
    df['Date'] = pd.to_datetime(df['Date'], dayfirst=True, errors='coerce')
    return df

if uploaded_file:
    file_bytes = uploaded_file.getvalue()
    file_type = uploaded_file.name.split('.')[-1].lower()
    with st.spinner(f"Processing {statement_mode} file..."):
        df = load_and_process_mpesa(file_bytes, file_type) if statement_mode == "M-Pesa" else load_and_process_bank(file_bytes, file_type)

    if df.empty:
        st.error("No transactions found.")
        st.stop()

    df = df.sort_values('Date', ascending=False).reset_index(drop=True)
    df['Paid In'] = pd.to_numeric(df['Paid In'], errors='coerce').fillna(0)
    df['Withdrawn'] = pd.to_numeric(df['Withdrawn'], errors='coerce').fillna(0)
    st.success(f"Found {len(df)} transactions from {statement_mode} 🎉")
    col1, col2, col3 = st.columns(3)
    total_in = df['Paid In'].sum()
    total_out = df['Withdrawn'].sum()
    col1.metric("💰 Money In", f"KES {total_in:,.2f}")
    col2.metric("💸 Money Out", f"KES {total_out:,.2f}")
    col3.metric("📊 Total Volume", f"KES {total_in + total_out:,.2f}")

    st.subheader("📊 Category Summary")
    summary = df.groupby('Category')[['Paid In', 'Withdrawn']].sum().reset_index()
    st.dataframe(summary, use_container_width=True, hide_index=True)

    st.subheader("📑 All Transactions")
    col_f1, col_f2 = st.columns([2,3])
    with col_f1:
        categories = ['All'] + sorted(df['Category'].unique().tolist())
        selected_cat = st.selectbox("Filter by Category", categories)
    with col_f2:
        search_term = st.text_input("Search TXID, Name, or Details", "")

    df_filtered = df.copy()
    if selected_cat!= 'All': df_filtered = df_filtered[df_filtered['Category'] == selected_cat]
    if search_term: df_filtered = df_filtered[df_filtered['Details'].str.contains(search_term, case=False, na=False)]

    col_a, col_b, col_c = st.columns([2,2,1])
    with col_a: rows_per_page = st.selectbox("Rows per page", [50, 100, 200, 500], index=2)
    with col_b:
        total_pages = max(1, (len(df_filtered) - 1) // rows_per_page + 1)
        page_number = st.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1)
    with col_c: st.metric("Filtered Total", len(df_filtered))

    start_idx = (page_number - 1) * rows_per_page
    end_idx = start_idx + rows_per_page
    df_page = df_filtered.iloc[start_idx:end_idx]
    st.caption(f"Showing {start_idx + 1} - {min(end_idx, len(df_filtered))} of {len(df_filtered)} transactions")
    st.dataframe(df_page, use_container_width=True, hide_index=True)
    csv = df_filtered.to_csv(index=False).encode()
    st.download_button("⬇️ Download Filtered CSV", csv, "filtered_statement.csv", mime="text/csv")
else:
    st.info(f"👆 Upload your {statement_mode} statement to get started")