import streamlit as st
import pandas as pd
import pdfplumber
import re
import io
from datetime import datetime
from pdf2image import convert_from_bytes
import pytesseract

st.set_page_config(page_title="Smart Statement Reader", layout="wide")
st.title("📄 Smart Statement Reader - M-PESA + Bank")
st.write("Upload your M-PESA PDF, CSV or Excel statement to auto-categorize everything")

uploaded_file = st.file_uploader("Upload your PDF or CSV/XLSX Statement", type=["pdf", "csv", "xlsx"])

def clean_amount(x):
    try:
        return float(str(x).replace(",","").replace("-","-").replace("KES","").strip())
    except:
        return 0.0

def clean_details(d):
    # Remove "Completed-200.00" and other junk from end of details
    d = re.sub(r'completed[-\s]*[\d,]+\.?\d*', '', d, flags=re.IGNORECASE)
    return d.strip()

def categorize(details, amount, txid_group):
    raw_d = str(details) # KEEP RAW
    d = clean_details(raw_d).lower() # CLEANED
    has_fuliza = txid_group.get('has_fuliza', False) or 'fuliza' in d or 'overdraft' in d
    is_od_repayment = 'od loan' in d or 'repayment' in d

    if is_od_repayment: return 'Fuliza Repayment'
    if d == "": return 'Fuliza Repayment'

    # FIX: CATCH BUSINESS PAYMENT FROM BANK VIA API + TYPO
    if 'bank' in raw_d.lower() and re.search(r'business payment fr', raw_d, re.IGNORECASE) and 'api' in raw_d.lower():
        return 'Business Payment Received - Fuliza' if has_fuliza else 'Business Payment Received'

    if 'airtime' in d: return 'Airtime - Fuliza' if has_fuliza else 'Airtime'

    # CHECK TILL FULIZA FIRST
    if has_fuliza and ('merchant payment' in d or 'buy goods' in d or 'customer payment to small' in d):
        return 'Till Payment - Fuliza'
    if 'customer payment to small' in d or 'merchant payment' in d or 'buy goods' in d:
        return 'Till Payment'

    if 'overdraft' in d and 'credit party' in d and amount > 0: return 'Fuliza Borrowed'
    if has_fuliza and ('charge' in d or 'fee' in d):
        if 'withdraw' in d: return 'Withdrawal Charge - Fuliza'
        return 'Fuliza Interest/Charges'

    # Business Payment Sent - ADDED MORE MATCHES
    if ('small business' in d and 'pay merchant' in d) or ('small business' in d and 'pay to' in d) or ('small business payment to' in d):
        return 'Business Payment Sent - Fuliza' if has_fuliza else 'Business Payment Sent'

    # Business Payment Received - Customers paying you
    if 'micro sme business' in d or 'customer send money to micro' in d or 'Business Payment from' in d or ('small business' in d and 'payment to customer' in d):
        return 'Business Payment Received - Fuliza' if has_fuliza else 'Business Payment Received'

    if 'lipa na mpesa business' in d or 'business to business' in d or 'b2b' in d: return 'Business to Business - Fuliza' if has_fuliza else 'Business to Business'
    if 'business withdrawal' in d or 'withdrawal to business' in d: return 'Business Withdrawal - Fuliza' if has_fuliza else 'Business Withdrawal'

    if 'withdraw' in d and 'agent' in d: return 'Agent Withdrawal - Fuliza' if has_fuliza else 'Agent Withdrawal'
    if 'customer withdrawal at agent' in d: return 'Agent Withdrawal - Fuliza' if has_fuliza else 'Agent Withdrawal'
    if 'deposit' in d and 'agent' in d: return 'Agent Deposit - Fuliza' if has_fuliza else 'Agent Deposit'

    if 'payment to customer' in d and 'business' not in d: return 'Sent to Person - Fuliza' if has_fuliza else 'Sent to Person'
    if 'received from' in d or 'funds received from' in d: return 'Received from Person - Fuliza' if has_fuliza else 'Received from Person'
    if 'pay bill' in d: return 'Paybill - Fuliza' if has_fuliza else 'Paybill'

    if has_fuliza: return 'Fuliza Borrowed'
    if 'deposit' in d and 'agent' not in d: return 'Bank Deposit'
    return 'Other'

def get_in_out(details, amount, txid_group):
    raw_d = str(details) # KEEP RAW
    d = clean_details(details).lower() # CLEANED
    abs_amount = abs(amount)

    if 'od loan' in d or 'repayment' in d: return 0, abs_amount
    if d == "":
        match_amt = re.search(r'completed[-\s]*([\d,]+\.?\d*)', raw_d, re.IGNORECASE)
        if match_amt: return 0, clean_amount(match_amt.group(1))
        return 0, abs_amount

    # FIX: CATCH BUSINESS PAYMENT FROM BANK VIA API + TYPO
    if 'bank' in raw_d.lower() and re.search(r'business payment fr', raw_d, re.IGNORECASE) and 'api' in raw_d.lower():
        return abs_amount, 0

    if 'airtime' in d:
        return 0, abs_amount

    has_fuliza = txid_group.get('has_fuliza', False) or 'fuliza' in d or 'overdraft' in d

    if has_fuliza and ('charge' in d or 'fee' in d): return 0, abs_amount
    if 'overdraft' in d and 'credit party' in d and amount > 0: return abs_amount, 0
    if has_fuliza and amount < 0: return 0, abs_amount

    # Business Payment Sent = Money OUT - FORCE IT
    if ('small business' in d and 'pay merchant' in d) or ('small business' in d and 'pay to' in d) or ('small business payment to' in d):
        return 0, abs_amount

    # Business Payment Received = Money IN
    if 'micro sme business' in d or 'customer send money to micro' in d or ('small business' in d and 'payment to customer' in d):
        return abs_amount, 0

    if 'received from' in d or 'funds received from' in d: return abs_amount, 0
    if 'payment to customer' in d and 'business' not in d: return 0, abs_amount
    if 'withdraw' in d and 'agent' in d: return 0, abs_amount
    if 'deposit' in d and 'agent' in d: return abs_amount, 0

    return amount if amount > 0 else 0, abs_amount if amount < 0 else 0

def parse_mpesa_text(full_text):
    data = []
    lines = full_text.split('\n')
    buffer = ""
    raw_transactions = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        buffer += " " + line

        match = re.search(r'([A-Z0-9]{10})\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(.+?)\s+([\-\d,]+\.?\d*)\s+([\d,]+\.?\d*)$', buffer)
        if match:
            txid = match.group(1)
            dt = f"{match.group(2)} {match.group(3)}"
            details = match.group(4).strip() # DON'T CLEAN HERE, WE CLEAN IN FUNCTIONS
            amount = clean_amount(match.group(5))
            key = f"{txid}_{dt}"
            raw_transactions.append({'key': key, 'txid': txid, 'dt': dt, 'details': details, 'amount': amount})
            buffer = ""

    txid_groups = {}
    for t in raw_transactions:
        if t['key'] not in txid_groups:
            txid_groups[t['key']] = []
        txid_groups[t['key']].append(t)

    for key, items in txid_groups.items():
        dt = items[0]['dt']
        txid = items[0]['txid']
        has_fuliza = any('fuliza' in i['details'].lower() or 'overdraft' in i['details'].lower() for i in items)
        group = {'has_fuliza': has_fuliza}

        has_borrow = any('overdraft' in i['details'].lower() and 'credit party' in i['details'].lower() for i in items)
        has_withdraw = any('withdraw' in i['details'].lower() and 'agent' in i['details'].lower() for i in items)
        has_charge = any('charge' in i['details'].lower() for i in items)

        if has_borrow and has_withdraw:
            borrow_amt = max([abs(i['amount']) for i in items if 'overdraft' in i['details'].lower()] or [0])
            withdraw_amt = max([abs(i['amount']) for i in items if 'withdraw' in i['details'].lower() and 'agent' in i['details'].lower()] or [0])
            charge_amt = max([abs(i['amount']) for i in items if 'charge' in i['details'].lower()] or [0])

            if borrow_amt > 0:
                data.append([dt, f"{txid} | Fuliza Borrowed", borrow_amt, 0, 'Fuliza Borrowed', "M-PESA"])
            if withdraw_amt > 0:
                data.append([dt, f"{txid} | Agent Withdrawal with Fuliza", 0, withdraw_amt, 'Agent Withdrawal - Fuliza', "M-PESA"])
            if charge_amt > 0:
                data.append([dt, f"{txid} | Withdrawal Charge", 0, charge_amt, 'Withdrawal Charge - Fuliza', "M-PESA"])
            continue

        combined_details = f"{txid} | {' | '.join([i['details'] for i in items])}"
        amounts = [abs(i['amount']) for i in items]
        total_amount = max(amounts) if amounts else 0.0

        cat = categorize(combined_details, total_amount, group)
        paid_in, withdrawn = get_in_out(combined_details, total_amount, group)
        data.append([dt, combined_details, paid_in, withdrawn, cat, "M-PESA"])

    return data

if uploaded_file:
    data = []
    file_type = uploaded_file.name.split('.')[-1].lower()

    with st.spinner("Reading file... this may take 30s for scanned PDFs"):
        if file_type in ['csv', 'xlsx']:
            df_raw = pd.read_csv(uploaded_file) if file_type == 'csv' else pd.read_excel(uploaded_file)
            st.info("Detected: M-PESA Excel/CSV Statement")
            for _, row in df_raw.iterrows():
                details = str(row.get('Details', '')) # DON'T CLEAN HERE
                raw_amount = clean_amount(row.get('Amount', clean_amount(row.get('Paid In', 0)) - clean_amount(row.get('Withdrawn', 0))))
                group = {'has_fuliza': 'fuliza' in details.lower() or 'overdraft' in details.lower()}
                cat = categorize(details, raw_amount, group)
                paid_in, withdrawn = get_in_out(details, raw_amount, group)
                data.append([row.get('Completion Time', ''), f"{row.get('Receipt No.', '')} | {clean_details(details)}", paid_in, withdrawn, cat, "M-PESA"])
        else:
            file_bytes = uploaded_file.getvalue()
            st.info("Detected: M-PESA PDF Statement")
            full_text = ""
            pdf_read_ok = False

            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    for i, page in enumerate(pdf.pages):
                        text = page.extract_text()
                        if text:
                            full_text += "\n" + text
                    pdf_read_ok = True
            except Exception as e:
                st.warning(f"pdfplumber failed: {type(e).__name__}. Switching to OCR...")

            if not pdf_read_ok or len(full_text.strip()) < 50:
                st.info("Running full OCR on PDF... this takes ~30s")
                images = convert_from_bytes(file_bytes, dpi=300)
                for i, image in enumerate(images):
                    st.write(f"OCR Page {i+1}/{len(images)}...")
                    text = pytesseract.image_to_string(image)
                    if text:
                        full_text += "\n" + text

            data = parse_mpesa_text(full_text)

    if not data:
        st.error("No transactions found. File might be corrupted, password protected, or fully scanned.")
        st.stop()

    df = pd.DataFrame(data, columns=['Date','Details','Paid In','Withdrawn','Category','Source'])
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
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
    business_received = df[df['Category']=='Business Payment Received']['Paid In'].sum()
    business_sent = df[df['Category']=='Business Payment Sent']['Withdrawn'].sum()
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

    st.divider()
    st.subheader("📑 All Transactions")
    col1, col2, col3 = st.columns(3)
    with col1:
        search = st.text_input("🔍 Search Details", placeholder="TXID, Name, Phone, Fuliza...")
    with col2:
        cats = sorted(df['Category'].unique(), key=lambda x: (not 'Fuliza' in x, x))
        cat_filter = st.multiselect("Filter by Category", options=cats, default=[])
    with col3:
        if pd.notna(df['Date'].min()):
            min_date = df['Date'].min().date()
            max_date = df['Date'].max().date()
            date_range = st.date_input("Date Range", [min_date, max_date])
        else:
            date_range = []

    df_filtered = df.copy()
    if search:
        df_filtered = df_filtered[df_filtered['Details'].str.contains(search, case=False, na=False)]
    if cat_filter:
        df_filtered = df_filtered[df_filtered['Category'].isin(cat_filter)]
    if len(date_range) == 2:
        df_filtered = df_filtered[(df_filtered['Date'].dt.date >= date_range[0]) & (df_filtered['Date'].dt.date <= date_range[1])]

    st.write(f"Showing **{len(df_filtered)}** of **{len(df)}** transactions")
    st.dataframe(df_filtered.sort_values('Date', ascending=False), use_container_width=True, hide_index=True)

    csv = df_filtered.to_csv(index=False).encode()
    st.download_button("⬇️ Download Filtered CSV", csv, f"filtered_statement_{datetime.now().strftime('%Y%m%d')}.csv")
else:
    st.info("👆 Upload your M-PESA statement to get started")