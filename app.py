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
st.title("📄 Smart Statement Reader - M-PESA + Bank v3.0.3.2")

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

    # FIX: Catch Fuliza Repayments first
    if 'od loan' in d and 'repayment' in d:
        return 'Fuliza Repayment'
    if 'fuliza' in d or 'overdraft' in d:
        if 'repayment' in d: return 'Fuliza Repayment'
        if 'borrowed' in d: return 'Fuliza Borrowed'
        if 'interest' in d or 'charge' in d: return 'Fuliza Interest/Charges'
        return 'Fuliza'

    if 'airtime' in d: return 'Airtime'
    if 'till' in d or 'buy goods' in d or 'customer payment to small' in d: return 'Till Payment'
    if 'pay bill' in d: return 'Paybill'
    if 'send money' in d or 'payment to customer' in d: return 'Sent to Person'
    if 'received' in d or 'funds received' in d: return 'Received'
    if 'withdraw' in d: return 'Withdrawal'
    if 'deposit' in d: return 'Deposit'
    return 'Other'

def get_in_out(amount):
    return (amount, 0.0) if amount > 0 else (0.0, abs(amount))

# NEW: TXID BLOCK PARSER
def process_block(block_lines):
    data = []
    full_block = " ".join(block_lines)
    header_match = re.match(r'([A-Z0-9]{10})\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})', full_block)
    if not header_match: return []

    txid = header_match.group(1)
    dt = f"{header_match.group(2)} {header_match.group(3)}"

    # Get amount + balance from end of block. Handles trailing junk
    amt_bal_match = re.search(r'(-?[\d,]+\.?\d+)\s+([\d,]+\.?\d+)(?:\s+\d+\s+\d+)?\s*$', full_block)
    if not amt_bal_match: return []

    amount = clean_amount(amt_bal_match.group(1))
    details_part = full_block[header_match.end():amt_bal_match.start()].strip()
    details = clean_details(details_part)

    cat = categorize(details)
    paid_in, withdrawn = get_in_out(amount)
    data.append([dt, f"{txid} | {details}", paid_in, withdrawn, cat, "M-PESA"])
    return data

def parse_mpesa_text(full_text):
    data = []
    lines = [l.strip() for l in full_text.split('\n') if l.strip()]
    txid_pattern = re.compile(r'^([A-Z0-9]{10})\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})')

    current_block = []
    for line in lines:
        if txid_pattern.match(line):
            if current_block: data.extend(process_block(current_block))
            current_block = [line]
        else:
            current_block.append(line)
    if current_block: data.extend(process_block(current_block))
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
        if len(full_text.strip()) < 50: # Fallback to OCR
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
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
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
    st.subheader("📊 Category Summary")
    st.dataframe(st.session_state.summary.sort_values('Withdrawn', ascending=False), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("📑 All Transactions")

    # NEW: PAGINATION + SEARCH
    col1, col2 = st.columns(2)
    with col1: search = st.text_input("🔍 Search Details")
    with col2:
        cats = sorted(df['Category'].unique())
        cat_filter = st.multiselect("Filter by Category", options=cats, default=[])

    df_filtered = df.copy()
    if search: df_filtered = df_filtered[df_filtered['Details'].str.contains(search, case=False, na=False)]
    if cat_filter: df_filtered = df_filtered[df_filtered['Category'].isin(cat_filter)]

    colA, colB = st.columns([1,3])
    with colA:
        page_size = st.selectbox("Rows per page", [100, 200, 500], index=0)
    total_pages = max(1, (len(df_filtered) - 1) // page_size + 1)
    with colB:
        page_number = st.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1)

    start_idx = (page_number - 1) * page_size
    end_idx = start_idx + page_size
    st.info(f"Showing rows {start_idx+1} to {min(end_idx, len(df_filtered))} of {len(df_filtered)}")

    df_display = df_filtered.sort_values('Date', ascending=False).iloc[start_idx:end_idx]
    st.dataframe(df_display, use_container_width=True, hide_index=True)

    csv = df_filtered.to_csv(index=False).encode()
    st.download_button("⬇️ Download Filtered CSV", csv, f"filtered_statement_{datetime.now().strftime('%Y%m%d')}.csv", mime="text/csv")
else:
    st.info("👆 Upload your M-PESA statement to get started")