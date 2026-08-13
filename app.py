import streamlit as st
import pandas as pd
import pdfplumber
import re
import io
import gc
from datetime import datetime
from pdf2image import convert_from_bytes
import pytesseract

# ========== CONFIG ==========
st.set_page_config(page_title="Smart Statement Reader", layout="wide")
st._config.set_option('server.maxUploadSize', 200) # Allow up to 200MB uploads
st._config.set_option('server.maxMessageSize', 200)
# ========== END CONFIG ==========

# ========== PASSWORD GATE ==========
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
# ========== END PASSWORD GATE ==========

check_password()

st.title("📄 Smart Statement Reader - M-PESA + Bank")
st.write("Upload your M-PESA PDF, CSV or Excel statement to auto-categorize everything")
st.caption("Tip: For large PDFs >60 pages, use CSV or split PDF. Filtering uses cache to save RAM.")

uploaded_file = st.file_uploader("Upload your PDF or CSV/XLSX Statement", type=["pdf", "csv", "xlsx"])

def clean_amount(x):
    try:
        return float(str(x).replace(",","").replace("KES","").strip())
    except:
        return 0.0

def clean_details(d):
    d = re.sub(r'completed[-\s]*[\d,]+\.?\d*', '', str(d), flags=re.IGNORECASE)
    return d.strip()

def categorize(details, txid_group):
    d = clean_details(details).lower()
    has_fuliza = txid_group.get('has_fuliza', False) or 'fuliza' in d or 'overdraft' in d

    if 'od loan' in d or 'repayment' in d or d == "": return 'Fuliza Repayment'
    if 'bank' in d and re.search(r'business payment fr', details, re.IGNORECASE) and 'api' in d:
        return 'Business Payment Received - Fuliza' if has_fuliza else 'Business Payment Received'
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
    if amount < 0:
        return 0.0, abs(amount) # Withdrawn
    elif amount > 0:
        return amount, 0.0 # Paid In
    else:
        return 0.0, 0.0

def parse_mpesa_text(full_text):
    data = []
    lines = full_text.split('\n')
    buffer = ""
    raw_transactions = []

    for line in lines:
        line = line.strip()
        if not line: continue
        buffer += " " + line
        match = re.search(r'([A-Z0-9]{10})\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(.+?)\s+([\-\d,]+\.?\d*)\s+([\d,]+\.?\d*)$', buffer)
        if match:
            txid = match.group(1)
            dt = f"{match.group(2)} {match.group(3)}"
            details = match.group(4).strip()
            amount = clean_amount(match.group(5))
            key = f"{txid}_{dt}"
            raw_transactions.append({'key': key, 'txid': txid, 'dt': dt, 'details': details, 'amount': amount})
            buffer = ""

    txid_groups = {}
    for t in raw_transactions:
        txid_groups.setdefault(t['key'], []).append(t)

    for key, items in txid_groups.items():
        dt = items[0]['dt']
        txid = items[0]['txid']
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

@st.cache_data(show_spinner=False) # CRITICAL: Cache so filtering doesn't re-run everything
def load_data(data):
    df = pd.DataFrame(data, columns=['Date','Details','Paid In','Withdrawn','Category','Source'])
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce', dayfirst=True)
    return df

if uploaded_file:
    data = []
    file_type = uploaded_file.name.split('.')[-1].lower()

    with st.spinner("Reading file... this may take 1-2min for scanned PDFs"):
        if file_type in ['csv', 'xlsx']:
            df_raw = pd.read_csv(uploaded_file) if file_type == 'csv' else pd.read_excel(uploaded_file)
            st.info("Detected: M-PESA Excel/CSV Statement")
            for _, row in df_raw.iterrows():
                details = str(row.get('Details', ''))
                raw_amount = clean_amount(row.get('Amount', clean_amount(row.get('Paid In', 0)) - clean_amount(row.get('Withdrawn', 0))))
                group = {'has_fuliza': 'fuliza' in details.lower() or 'overdraft' in details.lower()}
                cat = categorize(details, group)
                paid_in, withdrawn = get_in_out(raw_amount)
                data.append([row.get('Completion Time', ''), f"{row.get('Receipt No.', '')} | {clean_details(details)}", paid_in, withdrawn, cat, "M-PESA"])
        else:
            file_bytes = uploaded_file.getvalue()
            file_size_mb = len(file_bytes) / 1024 / 1024
            st.info(f"Detected: M-PESA PDF Statement - {file_size_mb:.2f} MB")

            full_text = ""
            pdf_read_ok = False

            # STEP 1: Try pdfplumber first - uses almost no RAM
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    st.write(f"Found {len(pdf.pages)} pages. Trying text extraction...")
                    for i, page in enumerate(pdf.pages):
                        text = page.extract_text()
                        if text: full_text += "\n" + text
                    pdf_read_ok = True
            except Exception as e:
                st.warning(f"pdfplumber failed: {type(e).__name__}. Switching to OCR...")

            # STEP 2: OCR in BATCHES - OPTIMIZED FOR 1GB CLOUD
            if not pdf_read_ok or len(full_text.strip()) < 50:
                if file_size_mb > 3:
                    st.warning("⚠️ Large PDF detected. Using low-memory OCR mode for Streamlit Cloud...")

                try:
                    from pdf2image import pdfinfo_from_bytes
                    info = pdfinfo_from_bytes(file_bytes)
                    total_pages = info["Pages"]

                    batch_size = 2 # CRITICAL: Only 2 pages at a time for 1GB RAM
                    progress_bar = st.progress(0)

                    for idx, start in enumerate(range(1, total_pages + 1, batch_size)):
                        end = min(start + batch_size - 1, total_pages)
                        st.write(f"OCR Pages {start}-{end}/{total_pages}...")

                        images = convert_from_bytes(file_bytes, dpi=100, first_page=start, last_page=end, fmt='jpg') # dpi 100 = 50% less RAM
                        for image in images:
                            text = pytesseract.image_to_string(image)
                            if text: full_text += "\n" + text
                            image.close()
                            del image
                        del images
                        gc.collect()

                        progress_bar.progress((idx + 1) / ((total_pages // batch_size) + 1))

                except Exception as e:
                    st.error(f"OCR failed: {e}. For large PDFs >60 pages try CSV export from M-PESA instead.")
                    st.stop()

            data = parse_mpesa_text(full_text)

    if not data:
        st.error("No transactions found. File might be corrupted, password protected, or fully scanned.")
        st.stop()

    df = load_data(data) # USE CACHED VERSION
    st.success(f"Found {len(df)} transactions 🎉")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("💰 Money In", f"KES {df['Paid In'].sum():,.2f}")
    col2.metric("💸 Money Out", f"KES {df['Withdrawn'].sum():,.2f}")
    col3.metric("📊 Net", f"KES {df['Paid In'].sum() - df['Withdrawn'].sum():,.2f}")
    col4.metric("📑 Transactions", len(df))

    fuliza_borrowed = df[df['Category']=='Fuliza Borrowed']['Paid In'].sum()
    till_fuliza = df[df['Category']=='Till Payment - Fuliza']['Withdrawn'].sum()
    fuliza_repaid = df[df['Category']=='Fuliza Repayment']['Withdrawn'].sum()
    agent_fuliza = df[df['Category']=='Agent Withdrawal - Fuliza']['Withdrawn'].sum()
    withdrawal_charges = df[df['Category']=='Withdrawal Charge - Fuliza']['Withdrawn'].sum()
    business_received = df[df['Category'].str.contains('Business Payment Received')]['Paid In'].sum()
    business_sent = df[df['Category'].str.contains('Business Payment Sent')]['Withdrawn'].sum()
    airtime_spent = df[df['Category'].str.contains('Airtime')]['Withdrawn'].sum()

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("📱 Fuliza Borrowed", f"KES {fuliza_borrowed:,.2f}")
    col6.metric("🏪 Till - Fuliza", f"KES {till_fuliza:,.2f}")
    col7.metric("↩️ Fuliza Repaid", f"KES {fuliza_repaid:,.2f}")
    col8.metric("🏧 Agent Fuliza", f"KES {agent_fuliza:,.2f}")

    st.write("")
    col9, col10, col11, col12 = st.columns(4)
    col9.metric("🏢 Business Received", f"KES {business_received:,.2f}")
    col10.metric("🏪 Business Sent", f"KES {business_sent:,.2f}")
    col11.metric("📞 Airtime Spent", f"KES {airtime_spent:,.2f}")
    col12.metric("💳 Withdrawal Charges", f"KES {withdrawal_charges:,.2f}")

    st.divider()
    st.subheader("📊 Spending Breakdown by Category")
    summary = df.groupby('Category')[['Paid In', 'Withdrawn']].sum().reset_index()
    summary['Net'] = summary['Paid In'] - summary['Withdrawn']
    summary = summary.sort_values('Withdrawn', ascending=False)
    st.dataframe(summary, use_container_width=True, hide_index=True)

    # EXCEL EXPORT
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.sort_values('Date', ascending=False).to_excel(writer, sheet_name='All Transactions', index=False)
        summary.to_excel(writer, sheet_name='Category Summary', index=False)
    st.download_button("⬇️ Download Full Excel Report", output.getvalue(), f"statement_report_{datetime.now().strftime('%Y%m%d')}.xlsx")

    st.divider()
    st.subheader("📑 All Transactions")
    col1, col2 = st.columns(2)
    with col1: search = st.text_input("🔍 Search Details")
    with col2:
        cats = sorted(df['Category'].unique(), key=lambda x: (not 'Fuliza' in x, x))
        cat_filter = st.multiselect("Filter by Category", options=cats, default=[])

    df_filtered = df.copy()
    if search: df_filtered = df_filtered[df_filtered['Details'].str.contains(search, case=False, na=False)]
    if cat_filter: df_filtered = df_filtered[df_filtered['Category'].isin(cat_filter)]

    st.write(f"Showing **{len(df_filtered)}** of **{len(df)}** transactions")

    # PAGINATION - CRITICAL FOR 1GB RAM
    page_size = 100
    total_pages = max(1, (len(df_filtered) - 1) // page_size + 1)
    page_number = st.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1)

    start_idx = (page_number - 1) * page_size
    end_idx = start_idx + page_size

    st.dataframe(df_filtered.sort_values('Date', ascending=False).iloc[start_idx:end_idx], use_container_width=True, hide_index=True)

    csv = df_filtered.to_csv(index=False).encode()
    st.download_button("⬇️ Download Filtered CSV", csv, f"filtered_statement_{datetime.now().strftime('%Y%m%d')}.csv")
else:
    st.info("👆 Upload your M-PESA statement to get started")