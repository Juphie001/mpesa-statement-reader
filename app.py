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
st.title("📄 Smart Statement Reader - M-PESA + Bank v2.9.1d")

uploaded_file = st.file_uploader("Upload your PDF or CSV/XLSX Statement", type=["pdf", "csv", "xlsx"])

def clean_amount(x):
    try: return float(str(x).replace(",","").replace("KES","").strip())
    except: return 0.0

def clean_details(d):
    # Only remove "Completed -123.00" at the end. Keep Paybill names
    return re.sub(r'\s*Completed[-\s]*[\d,]+\.?\d*$', '', str(d), flags=re.IGNORECASE).strip()

def categorize(details, txid_group):
    d = clean_details(details).lower()
    has_fuliza = txid_group.get('has_fuliza', False) or 'fuliza' in d or 'overdraft' in d

    # ===== LOAN KEYWORDS =====
    loan_keywords = ['loan', 'promotion payment', 'disbursement', 'facility', 'credit']
    bank_loan_senders = ['bank of africa', 'boa', 'kcb', 'equity', 'coop bank', 'stanbic', 'ncba', 'family bank', 'dtb', 'absa', 'simplepay', 'eclof', 'tala', 'branch', 'fairmoney', 'okash', 'kopa', 'tower sacco']
    b2c_api = 'via api' in d and 'original conversation id' in d
    # =========================

    # 1. LOAN DISBURSEMENT = Money coming IN
    if any(k in d for k in loan_keywords) and b2c_api:
        return 'Loan Disbursement'
    if any(bank in d for bank in bank_loan_senders) and 'payment from' in d and b2c_api:
        return 'Loan Disbursement'
    if 'payment from' in d and b2c_api and not 'business payment fr' in d:
        return 'Loan Disbursement'

    # 2. LOAN REPAYMENT = Money going OUT
    if any(k in d for k in loan_keywords) and 'pay bill' in d:
        return 'Loan Repayment'
    if any(bank in d for bank in bank_loan_senders) and 'pay bill' in d:
        return 'Loan Repayment'
    if 'od loan' in d or 'repayment' in d or d == "":
        return 'Fuliza Repayment'

    # ===== REST OF EXISTING RULES =====
    if 'bank' in d and 'business payment fr' in d and 'api' in d: return 'Business Payment Received - Fuliza' if has_fuliza else 'Business Payment Received'
    if 'airtime' in d: return 'Airtime - Fuliza' if has_fuliza else 'Airtime'
    if has_fuliza and ('merchant payment' in d or 'buy goods' in d or 'customer payment to small' in d): return 'Till Payment - Fuliza'
    if 'customer payment to small' in d or 'merchant payment' in d or 'buy goods' in d: return 'Till Payment'
    if 'overdraft' in d and 'credit party' in d: return 'Fuliza Borrowed'
    if has_fuliza and ('charge' in d or 'fee' in d):
        if 'withdraw' in d: return 'Withdrawal Charge - Fuliza'
        return 'Fuliza Interest/Charges'
    if ('small business' in d and 'pay merchant' in d) or ('small business' in d and 'pay to' in d): return 'Business Payment Sent - Fuliza' if has_fuliza else 'Business Payment Sent'
    if 'micro sme business' in d or 'customer send money to micro' in d or ('small business' in d and 'payment to customer' in d): return 'Business Payment Received - Fuliza' if has_fuliza else 'Business Payment Received'
    if 'lipa na mpesa business' in d or 'business to business' in d or 'b2b' in d: return 'Business to Business - Fuliza' if has_fuliza else 'Business to Business'
    if 'business withdrawal' in d: return 'Business Withdrawal - Fuliza' if has_fuliza else 'Business Withdrawal'
    if 'withdraw' in d and 'agent' in d: return 'Agent Withdrawal - Fuliza' if has_fuliza else 'Agent Withdrawal'
    if 'deposit' in d and 'agent' in d: return 'Agent Deposit - Fuliza' if has_fuliza else 'Agent Deposit'
    if 'payment to customer' in d and 'business' not in d: return 'Sent to Person - Fuliza' if has_fuliza else 'Sent to Person'
    if 'received from' in d or 'funds received from' in d: return 'Received from Person - Fuliza' if has_fuliza else 'Received from Person'
    if 'pay bill' in d: return 'Paybill - Fuliza' if has_fuliza else 'Paybill'
    if has_fuliza: return 'Fuliza Borrowed'
    if 'deposit' in d and 'agent' not in d: return 'Bank Deposit'
    return 'Other'

def get_in_out(amount):
    return (amount, 0.0) if amount > 0 else (0.0, abs(amount))

def parse_mpesa_text(full_text):
    data = []
    lines = full_text.split('\n')
    buffer = ""
    raw_transactions = []
    for line in lines:
        line = line.strip()
        if not line: continue
        buffer += " " + line
        # FIX: (.+) instead of (.+?) to grab full details including "908251 - ECLOF-KENYA Acc."
        match = re.search(r'([A-Z0-9]{10})\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(.+)\s+([\-\d,]+\.?\d*)\s+([\d,]+\.?\d*)$', buffer)
        if match:
            txid, dt = match.group(1), f"{match.group(2)} {match.group(3)}"
            details, amount = match.group(4).strip(), clean_amount(match.group(5))
            raw_transactions.append({'key': f"{txid}_{dt}", 'txid': txid, 'dt': dt, 'details': details, 'amount': amount})
            buffer = ""
    txid_groups = {}
    for t in raw_transactions: txid_groups.setdefault(t['key'], []).append(t)
    for key, items in txid_groups.items():
        dt, txid = items[0]['dt'], items[0]['txid']
        has_fuliza = any('fuliza' in i['details'].lower() or 'overdraft' in i['details'].lower() for i in items)
        group = {'has_fuliza': has_fuliza}
        has_borrow = any('overdraft' in i['details'].lower() and 'credit party' in i['details'].lower() for i in items)
        has_withdraw = any('withdraw' in i['details'].lower() and 'agent' in i['details'].lower() for i in items)
        if has_borrow and has_withdraw:
            for i in items:
                paid_in, withdrawn = get_in_out(i['amount'])
                cat = categorize(i['details'], group)
                data.append([dt, f"{txid} | {clean_details(i['details'])}", paid_in, withdrawn, cat, "M-PESA"])
            continue
        combined_details = f"{txid} | {' | '.join([clean_details(i['details']) for i in items])}"
        main_amount = max([i['amount'] for i in items], key=abs) if items else 0.0
        cat = categorize(combined_details, group)
        paid_in, withdrawn = get_in_out(main_amount)
        data.append([dt, combined_details, paid_in, withdrawn, cat, "M-PESA"])
    return data

@st.cache_data(show_spinner=False, ttl=3600)
def load_and_process(file_bytes, file_type):
    data = []
    if file_type in ['csv', 'xlsx']:
        df_raw = pd.read_csv(io.BytesIO(file_bytes)) if file_type == 'csv' else pd.read_excel(io.BytesIO(file_bytes))
        for _, row in df_raw.iterrows():
            details = str(row.get('Details', ''))
            raw_amount = clean_amount(row.get('Amount', clean_amount(row.get('Paid In', 0)) - clean_amount(row.get('Withdrawn', 0))))
            group = {'has_fuliza': 'fuliza' in details.lower() or 'overdraft' in details.lower()}
            cat = categorize(details, group)
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

    # DATE FIX: Handle 3 formats
    df['Date'] = pd.to_datetime(df['Date'], format='%Y-%m-%d %H:%M:%S', errors='coerce')
    mask = df['Date'].isna()
    df.loc[mask, 'Date'] = pd.to_datetime(df.loc[mask, 'Date'], format='%d-%m-%Y %H:%M:%S', errors='coerce')
    mask = df['Date'].isna()
    df.loc[mask, 'Date'] = pd.to_datetime(df.loc[mask, 'Date'], dayfirst=True, errors='coerce')

    df['Paid In'] = pd.to_numeric(df['Paid In'], downcast='float')
    df['Withdrawn'] = pd.to_numeric(df['Withdrawn'], downcast='float')
    df['Category'] = df['Category'].astype('category')
    return df

if uploaded_file:
    file_bytes = uploaded_file.getvalue()
    file_type = uploaded_file.name.split('.')[-1].lower()

    if 'df' not in st.session_state or st.session_state.get('filename')!= uploaded_file.name:
        with st.spinner("Processing file... This may take 2-3min for large PDFs"):
            df = load_and_process(file_bytes, file_type)
            st.session_state.df = df
            st.session_state.filename = uploaded_file.name
            st.session_state.summary = df.groupby('Category')[['Paid In', 'Withdrawn']].sum().reset_index()
            st.session_state.summary['Net'] = st.session_state.summary['Paid In'] - st.session_state.summary['Withdrawn']
    else:
        df = st.session_state.df

    st.success(f"Found {len(df)} transactions 🎉")

    # ===== LOAN METRICS =====
    loan_in = df[df['Category'] == 'Loan Disbursement']['Paid In'].sum()
    loan_out = df[df['Category'] == 'Loan Repayment']['Withdrawn'].sum() + df[df['Category'] == 'Fuliza Repayment']['Withdrawn'].sum()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("💰 Money In", f"KES {df['Paid In'].sum():,.2f}")
    col2.metric("💸 Money Out", f"KES {df['Withdrawn'].sum():,.2f}")
    col3.metric("📊 Net", f"KES {df['Paid In'].sum() - df['Withdrawn'].sum():,.2f}")
    col4.metric("🏦 Loans Taken", f"KES {loan_in:,.2f}")
    col5.metric("↩️ Loans Repaid", f"KES {loan_out:,.2f}")

    st.divider()
    st.subheader("📊 Spending Breakdown by Category")
    st.dataframe(st.session_state.summary.sort_values('Withdrawn', ascending=False), use_container_width=True, hide_index=True)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.sort_values('Date', ascending=False).to_excel(writer, sheet_name='All Transactions', index=False)
        st.session_state.summary.to_excel(writer, sheet_name='Category Summary', index=False)
    st.download_button("⬇️ Download Full Excel Report", output.getvalue(), f"statement_report_{datetime.now().strftime('%Y%m%d')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.divider()
    st.subheader("📑 All Transactions")
    col1, col2 = st.columns(2)
    with col1: search = st.text_input("🔍 Search Details")
    with col2:
        cats = sorted(df['Category'].unique())
        cat_filter = st.multiselect("Filter by Category", options=cats, default=[])

    df_filtered = df.copy()
    if search: df_filtered = df_filtered[df_filtered['Details'].str.contains(search, case=False, na=False)]
    if cat_filter: df_filtered = df_filtered[df_filtered['Category'].isin(cat_filter)]

    # ===== PAGINATION ONLY =====
    colA, colB = st.columns([1,3])
    with colA:
        page_size = st.selectbox("Rows per page", [100, 200, 500], index=1)
    total_pages = max(1, (len(df_filtered) - 1) // page_size + 1)
    with colB:
        page_number = st.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1)

    start_idx = (page_number - 1) * page_size
    end_idx = start_idx + page_size

    st.info(f"Showing rows {start_idx+1} to {min(end_idx, len(df_filtered))} of {len(df_filtered)}")
    df_display = df_filtered.sort_values('Date', ascending=False).iloc[start_idx:end_idx]
    st.dataframe(df_display, use_container_width=True, hide_index=True)
    # ===========================

    csv = df_filtered.to_csv(index=False).encode()
    st.download_button("⬇️ Download Filtered CSV", csv, f"filtered_statement_{datetime.now().strftime('%Y%m%d')}.csv", mime="text/csv")
else:
    st.info("👆 Upload your M-PESA statement to get started")