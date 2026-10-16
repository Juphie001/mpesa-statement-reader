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
        if s.startswith('(') and s.endswith(')'): s = '-' + s[1:-1]
        return float(s)
    except: return 0.0

def clean_details(d):
    return re.sub(r'\s+', ' ', str(d)).strip()

def categorize(details, amount_in):
    d = str(details).lower()
    if 'via api' in d and amount_in >= 3000: return 'Loan Taken'
    if 'od loan' in d and 'repayment' in d: return 'Fuliza Repayment'
    if 'small business withdrawal' in d and 'to mpesa account' in d: return 'Self Transfer'
    if 'business account to mpesa' in d: return 'Self Transfer'
    if 'withdrawal' in d and 'agent' in d: return 'Agent Withdrawal'
    if 'bundle' in d or 'airtime' in d: return 'Airtime'
    if 'small business' in d and 'received' in d: return 'Received - Business'
    if 'business' in d and 'received from' in d: return 'Received - Business'
    if 'small business payment' in d and 'to customer' in d: return 'Sent - Business'
    if 'customer transfer to' in d: return 'Sent to Person'
    if 'pay bill' in d: return 'Paybill'
    if 'merchant' in d: return 'Till Payment'
    if 'till' in d or 'buy goods' in d: return 'Till Payment'
    if 'received from' in d: return 'Received'
    if 'charges' in d or 'transaction cost' in d: return 'Charges'
    return 'Other'

def categorize_bank(narrative):
    n = str(narrative).lower()
    if 'mpesa' in n and 'payment to' in n: return 'Sent - M-Pesa'
    if 'mmp' in n or 'mpesa ref' in n: return 'Received - M-Pesa'
    if 'charge' in n or 'commission' in n or 'fee' in n: return 'Charges'
    if 'salary' in n: return 'Salary'
    return 'Other Bank'

def merge_tx_group(rows):
    txid = rows[0][0]; dt = rows[0][1]; all_details=[]; total_in=0.0; total_out=0.0; categories=set()
    for r in rows:
        all_details.append(r[2]); total_in+=r[3]; total_out+=r[4]
        cat=categorize(r[2], r[3]);
        if cat!='Charges': categories.add(cat)
        else:
            if not categories: categories.add(cat)
    main_cat='Other'
    priority=['Loan Taken','Fuliza Repayment','Self Transfer','Received - Business','Sent - Business','Till Payment','Paybill','Airtime','Sent to Person','Received','Charges']
    for p in priority:
        if p in categories: main_cat=p; break
    if main_cat=='Other' and categories: main_cat=list(categories)[0]
    return [dt, f"{txid} | {clean_details(' | '.join(all_details))}", total_in, total_out, main_cat, "M-PESA"]

def load_and_process_mpesa(file_bytes, file_type):
    raw_rows=[]
    if file_type in ['csv','xlsx']:
        df_raw = pd.read_csv(io.BytesIO(file_bytes)) if file_type=='csv' else pd.read_excel(io.BytesIO(file_bytes))
        for _, row in df_raw.iterrows():
            raw_rows.append([str(row.get('Receipt No.','')), row.get('Completion Time',''), str(row.get('Details','')), clean_amount(row.get('Paid In',0)), clean_amount(row.get('Withdrawn',0))])
    else:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables(table_settings={"text_x_tolerance": 3, "text_y_tolerance": 3})
                if tables:
                    for table in tables:
                        for row in table[1:]:
                            if row and len(row)==7 and row[0]:
                                raw_rows.append([row[0], row[1], row[2], clean_amount(row[4]), clean_amount(row[5])])
    grouped=defaultdict(list)
    for r in raw_rows: grouped[r[0]].append(r)
    data=[merge_tx_group(rows) for rows in grouped.values()]
    df=pd.DataFrame(data, columns=['Date','Details','Paid In','Withdrawn','Category','Source'])
    df['Date']=pd.to_datetime(df['Date'], errors='coerce')
    return df

def load_and_process_bank(file_bytes, file_type):
    data=[]
    if file_type in ['csv','xlsx']:
        df_raw = pd.read_csv(io.BytesIO(file_bytes)) if file_type=='csv' else pd.read_excel(io.BytesIO(file_bytes))
        df_raw.columns=[c.strip() for c in df_raw.columns]
        for _, row in df_raw.iterrows():
            w=clean_amount(row.get('Withdrawals',0)); d=clean_amount(row.get('Deposits',0))
            if w==0 and d==0: continue
            data.append([str(row.get('Tran Date','')), clean_details(row.get('Transaction Narrative','')), d, w, categorize_bank(str(row.get('Transaction Narrative',''))), "BANK"])
    else:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            full_text=""
            for page in pdf.pages:
                full_text += "\n" + (page.extract_text() or "")
            lines=[l.strip() for l in full_text.split('\n') if l.strip()]
            i=0
            while i < len(lines):
                line=lines[i]
                if 'Tran Date' in line or 'Account Statement' in line or 'PERIS' in line or 'Page' in line and 'Of' in line:
                    i+=1; continue
                # Match multiline format
                m = re.search(r'(\d{2}-\d{2}-\d{2,4})\s+(\d{2}-\d{2}-\d{2,4})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})Cr', line)
                m2 = re.search(r'(\d{2}-\d{2}-\d{2,4})\s+(\d{2}-\d{2}-\d{2,4})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})Cr', line)
                if m or m2:
                    amounts = re.findall(r'[\d,]+\.\d{2}', line)
                    if len(amounts) < 2: i+=1; continue
                    tran_date = re.search(r'\d{2}-\d{2}-\d{2,4}', line).group()
                    remaining = amounts[:-1]
                    narrative_parts=[]
                    after_cr = re.split(r'[\d,]+\.\d{2}Cr', line)[-1].strip()
                    if after_cr: narrative_parts.append(after_cr)
                    j=i+1
                    while j < len(lines) and j < i+4:
                        nl=lines[j]
                        if re.match(r'^\d{2}-\d{2}-\d{2,4}\s+\d{2}-\d{2}-\d{2,4}', nl): break
                        if 'Page' in nl or 'Tran Date' in nl or 'Account' in nl: break
                        if re.match(r'^\d{2}-\d{2}-\d{2,4}\s+[\d,]+\.\d{2}Cr', nl): break
                        narrative_parts.append(nl)
                        j+=1
                    # Also check previous line for MPESA Payment header
                    if i>0 and not re.match(r'^\d{2}-\d{2}-\d{2,4}', lines[i-1]):
                        if 'MPESA' in lines[i-1] or 'MMP' in lines[i-1] or '/' in lines[i-1]:
                            narrative_parts.insert(0, lines[i-1])
                    narrative=" ".join(narrative_parts).strip()
                    w=d=0.0
                    low=(narrative+ " + line).lower()
                    if len(remaining)==1:
                        amt=clean_amount(remaining[0])
                        if 'payment to' in low or ' to 254' in low or ' to 2' in low: w=amt
                        elif 'mmp' in low or 'mpesa ref' in low: d=amt
                        else: w=amt
                    else:
                        w=clean_amount(remaining[0]); d=clean_amount(remaining[1])
                    if w!=0 or d!=0:
                        data.append([tran_date, clean_details(narrative) if narrative else clean_details(line), d, w, categorize_bank(narrative), "BANK"])
                        i=j-1
                i+=1

    df=pd.DataFrame(data, columns=['Date','Details','Paid In','Withdrawn','Category','Source'])
    df['Date']=pd.to_datetime(df['Date'], dayfirst=True, errors='coerce')
    df=df.drop_duplicates()
    return df

if uploaded_file:
    file_bytes=uploaded_file.getvalue()
    file_type=uploaded_file.name.split('.')[-1].lower()
    with st.spinner(f"Processing {statement_mode}..."):
        df = load_and_process_mpesa(file_bytes, file_type) if statement_mode=="M-Pesa" else load_and_process_bank(file_bytes, file_type)
    if df.empty:
        st.error("No transactions found.")
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                txt=""
                for p in pdf.pages[:2]: txt+= (p.extract_text() or "") + "\n---PAGE---\n"
                st.code(txt[:4000])
        except: pass
        st.stop()
    df=df.sort_values('Date', ascending=False).reset_index(drop=True)
    df['Paid In']=pd.to_numeric(df['Paid In'], errors='coerce').fillna(0)
    df['Withdrawn']=pd.to_numeric(df['Withdrawn'], errors='coerce').fillna(0)
    st.success(f"Found {len(df)} transactions 🎉")
    col1,col2,col3=st.columns(3)
    col1.metric("💰 Money In", f"KES {df['Paid In'].sum():,.2f}")
    col2.metric("💸 Money Out", f"KES {df['Withdrawn'].sum():,.2f}")
    col3.metric("📊 Total", f"KES {df['Paid In'].sum()+df['Withdrawn'].sum():,.2f}")
    st.subheader("📊 Category Summary")
    st.dataframe(df.groupby('Category')[['Paid In','Withdrawn']].sum().reset_index(), use_container_width=True, hide_index=True)
    st.subheader("📑 All Transactions")
    c1,c2=st.columns([2,3])
    with c1:
        cats=['All']+sorted(df['Category'].unique().tolist())
        sel=st.selectbox("Filter by Category", cats)
    with c2: search=st.text_input("Search Details", "")
    df_f=df.copy()
    if sel!='All': df_f=df_f[df_f['Category']==sel]
    if search: df_f=df_f[df_f['Details'].str.contains(search, case=False, na=False)]
    rows=st.selectbox("Rows per page", [50,100,200,500], index=1)
    pages=max(1, (len(df_f)-1)//rows+1)
    p=st.number_input("Page", 1, pages, 1)
    st.dataframe(df_f.iloc[(p-1)*rows:p*rows], use_container_width=True, hide_index=True)
    st.download_button("⬇️ Download CSV", df_f.to_csv(index=False).encode(), "filtered.csv", "text/csv")
else:
    st.info(f"👆 Upload your {statement_mode} statement")