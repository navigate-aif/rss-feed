import streamlit as st
import pandas as pd
import requests
import xml.etree.ElementTree as ET
import re
import os
import base64
from html.parser import HTMLParser
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
from dotenv import load_dotenv
from pymongo import MongoClient
import datetime

# Load environment variables
load_dotenv()

# --- Page Settings ---
st.set_page_config(
    page_title="Navigate AIF | Precision Regulatory Tracker",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Notifications ---
if 'success_msg' in st.session_state:
    st.toast(st.session_state['success_msg'], icon="✅")
    del st.session_state['success_msg']

# --- MongoDB Setup ---
@st.cache_resource
def get_mongo_client():
    mongo_url = os.environ.get('MONGO_URL')
    db_name = os.environ.get('MONGO_DATABASE_NAME', 'compliance')
    client = MongoClient(mongo_url)
    return client[db_name]

db = get_mongo_client()
rss_collection = db['rss_feeds']

# --- HTML Parser ---
class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
    def handle_data(self, data):
        self._parts.append(data)
    def get_text(self):
        return " ".join(self._parts)

def strip_html(raw_html):
    if not raw_html: return ""
    s = HTMLStripper()
    try: s.feed(raw_html)
    except Exception: pass
    return s.get_text()

# --- Config: RBI ---
RBI_STRONG_TERMS = [
    "alternative investment fund", "alternative investment funds", "sebi-registered alternative investment fund",
    "aif registered with sebi", "category i alternative investment fund", "category ii alternative investment fund",
    "category iii alternative investment fund", "investment in aif", "investment in aifs", "exposure to aif",
    "exposure to aifs", "units of aif", "units of aifs", "units of alternative investment fund",
    "units of alternative investment funds", "capital commitment to aif", "drawdown by aif",
    "overseas investment by aif", "overseas investment by aifs", "downstream investment by aif",
    "investment by banks in aif", "investment by nbfcs in aif", "regulated entities investment in aif",
    "investment in alternative investment fund"
]

RBI_CONTEXT_REQUIRED = [
    "venture capital", "private equity", "category i", "category ii", "category iii", "fvci",
    "foreign venture capital investor", "kyc", "ckyc", "aml", "cft", "assets under management",
    "aum", "net asset value", "nav", "private market", "private markets", "investment vehicle",
    "round tripping", "evergreening", "downstream investment", "structured exposure",
    "portfolio investment", "overseas direct investment", "odi", "non-debt instruments",
    "exposure norms", "risk weight", "capital adequacy", "prudential norms", "master direction",
    "reporting requirements", "liberalised remittance scheme", "lrs", "fpi", "fema",
    "foreign exchange management act"
]

# --- Config: SEBI ---
SEBI_STRONG_TERMS = [
    "alternative investment fund", "alternative investment funds", "aif regulations", "sebi aif regulations",
    "aif framework", "aif guidelines", "aif compliance", "aif policy", "aif amendment", "aif master circular",
    "category i aif", "category ii aif", "category iii aif", "angel fund", "angel funds", "large value fund",
    "large value funds", "lvf", "venture capital fund", "venture capital funds", "social impact fund",
    "private equity fund", "private equity funds", "special situation fund", "special situation funds",
    "distressed asset fund", "distressed asset funds", "structured credit fund", "structured credit funds",
    "sustainable infrastructure fund", "sustainable infrastructure funds", "aif reporting", "aif disclosure",
    "placement memorandum", "ppm", "multiples private equity", "multiples alternate asset"
]

SEBI_CONTEXT_REQUIRED = [
    "venture capital", "private equity", "category i", "category ii", "category iii", "fund", "trust",
    "trusts", "sebi order", "adjudication order", "penalty order", "settlement order", "sebi circular",
    "master circular", "kyc", "ckyc", "aml", "cft", "assets under management", "aum", "net asset value",
    "nav", "private market", "private markets", "investment vehicle", "investment vehicles",
    "regulated entities", "investors"
]

AIF_REGEX = r'\b(aif(?!i)|aifs|alternative investment fund|alternative investment funds|lvf)\b'

COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
}

# --- Core Logic ---
def fetch_rss(url):
    try:
        response = requests.get(url, headers=COMMON_HEADERS, timeout=15)
        response.raise_for_status()
        return response.content
    except Exception:
        return None

def find_match(patterns, text):
    for pattern in patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', text, re.IGNORECASE):
            return pattern
    return None

def parse_items(xml_content):
    if not xml_content: return []
    try:
        root = ET.fromstring(xml_content)
        items = root.findall('.//item')
        parsed = []
        for item in items:
            title = (item.findtext('title') or "").strip()
            description = (item.findtext('description') or "").strip()
            link = (item.findtext('link') or "").strip()
            pub_date = (item.findtext('pubDate') or "").strip()
            parsed.append({'title': title, 'description': description, 'link': link, 'pubDate': pub_date})
        return parsed
    except Exception:
        return []

def sort_items_by_date(items):
    """Sort items by pubDate in descending order (newest first)."""
    if not items: return []
    def get_sort_key(up):
        try:
            clean_dt = re.sub(r'\s([\+\-]\d{4}|[A-Z]{3,4})$', '', up['pubDate']).strip()
            return pd.to_datetime(clean_dt)
        except:
            return pd.to_datetime("1970-01-01")
    return sorted(items, key=get_sort_key, reverse=True)

def filter_updates(items, strong_terms, context_terms):
    relevant = []
    others = []
    for up in items:
        clean_desc = strip_html(up['description'])
        search_text = f"{up['title']} {clean_desc}".lower()
        is_relevant = False
        match_kw = None
        confidence = ""
        # Tier 1
        match = find_match(strong_terms, search_text)
        if match:
            is_relevant = True
            match_kw = match
            confidence = "High"
        # Tier 2
        if not is_relevant:
            if re.search(AIF_REGEX, search_text):
                match = find_match(context_terms, search_text)
                if match:
                    is_relevant = True
                    match_kw = match
                    confidence = "Medium (Contextual)"
        up['matched'] = match_kw
        up['confidence'] = confidence
        if is_relevant:
            relevant.append(up)
        else:
            others.append(up)
    return relevant, others

def extract_pdf_link(page_url, source):
    try:
        response = requests.get(page_url, headers=COMMON_HEADERS, timeout=10)
        response.raise_for_status()
        html = response.text
        if source == 'RBI':
            match = re.search(r'(https://rbidocs\.rbi\.org\.in/rdocs/[^"\'\s]+\.pdf)', html, re.IGNORECASE)
        else:
            match = re.search(r'file=(https://www\.sebi\.gov\.in/sebi_data/attachdocs/[^&\'"]+\.pdf)', html)
        return match.group(1) if match else None
    except Exception:
        return None

def download_pdf(pdf_url):
    try:
        resp = requests.get(pdf_url, headers=COMMON_HEADERS, timeout=15)
        return resp.content if resp.status_code == 200 else None
    except Exception:
        return None

def send_email(updates, recipients, source):
    api_key = os.environ.get('SENDGRID_API_KEY')
    if not api_key: return False, "No SG Key"
    sender = "communications@navigateaif.com"
    is_rbi = "RBI" in source
    is_sebi = "SEBI" in source
    if is_rbi:
        header_title = "Navigate AIF – RBI Regulatory Tracker"
        header_subtitle = "RBI Notifications, Circulars & Press Releases"
        header_sub_color = "#dbeafe"
        body_intro = "Find the new <strong>AIF relevant regulatory update(s)</strong> released by RBI"
        link_label = "View Official RBI Link"
        disclaimer = "This alert is automatically generated using Navigate AIF's precision keyword framework for RBI data."
        footer = "© 2026 Navigate AIF | RBI Intelligence System"
        subject = f"RBI RSS Alert: {len(updates)} New Relevant Updates"
    elif is_sebi:
        header_title = "Navigate AIF – SEBI Regulatory Tracker"
        header_subtitle = "Precision-Monitored AIF Regulatory Updates"
        header_sub_color = "#cbd5e1"
        body_intro = "Find the new <strong>AIF relevant regulatory update(s)</strong> released by SEBI"
        link_label = "View Official SEBI Link"
        disclaimer = "This alert is generated using Navigate AIF's precise research. Full PDF documents have been attached where available."
        footer = "© 2026 Navigate AIF | Regulatory Intelligence"
        subject = "SEBI Regulatory Update"
    else:
        header_title = "Navigate AIF – General Industry Tracker"
        header_subtitle = "Industry News & Market Intelligence"
        header_sub_color = "#e2e8f0"
        body_intro = "Find the latest <strong>industry news</strong> related to AIFs and Alternative Investments"
        link_label = "View Official Source"
        disclaimer = "This news alert is curated from industry search feeds for market monitoring purposes."
        footer = "© 2026 Navigate AIF | Market Intelligence"
        subject = f"Industry Alert: {len(updates)} New Market Updates"

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
                max-width: 680px; margin: auto; border: 1px solid #e6e6e6; border-radius: 8px; overflow: hidden;">
        <div style="background-color: #074173; padding: 20px;">
            <h2 style="color: #ffffff; margin: 0;">{header_title}</h2>
            <p style="color: {header_sub_color}; margin: 5px 0 0 0; font-size: 14px;">{header_subtitle}</p>
        </div>
        <div style="padding: 25px;">
            <p style="font-size: 15px; color: #334155;">{body_intro}</p>
            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
    """
    for up in updates:
        raw_date = re.sub(r'\s([\+\-]\d{4}|[A-Z]{3,4})$', '', up['pubDate']).strip()
        try:
            dt = pd.to_datetime(raw_date)
            # 12-hour period format as requested
            display_date = dt.strftime('%a, %d %b %Y %I:%M %p')
        except Exception:
            display_date = raw_date
        html_body += f"""
            <div style="margin-bottom: 28px; padding: 18px; border: 1px solid #e5e7eb; border-radius: 6px;">
                <h3 style="margin: 0 0 8px 0; color: #0f172a; font-size: 16px;">{up['title']}</h3>
                <p style="margin: 0; font-size: 13px; color: #64748b;"><strong>Date of Publication:</strong> {display_date}</p>
                <p style="margin: 10px 0;">
                    <a href="{up['link']}" style="background-color: #2563eb; color: #ffffff; padding: 8px 14px;
                       font-size: 13px; text-decoration: none; border-radius: 4px; display: inline-block;">{link_label}</a>
                </p>
            </div>
        """
    html_body += f"""
            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 25px 0;">
            <p style="font-size: 12px; color: #94a3b8;">{disclaimer}</p>
        </div>
        <div style="background: #f8fafc; padding: 15px; text-align: center; font-size: 12px; color: #64748b;">{footer}</div>
    </div>
    """
    message = Mail(from_email=sender, to_emails=recipients, subject=subject, html_content=html_body)
    for up in updates:
        pdf_url = extract_pdf_link(up['link'], "RBI" if is_rbi else "SEBI")
        if pdf_url:
            data = download_pdf(pdf_url)
            if data:
                enc = base64.b64encode(data).decode()
                safe_title = re.sub(r'[^\w\-_\.]', '_', up['title'])[:50].strip('_')
                filename = f"{safe_title}.pdf"
                message.add_attachment(Attachment(FileContent(enc), FileName(filename), FileType('application/pdf'), Disposition('attachment')))
    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        return response.status_code == 202, "Sent"
    except Exception as e:
        return False, str(e)

# --- Broadcast Dialog ---
@st.dialog("Confirm Dispatch")
def show_confirm_dialog(items, emails):
    st.markdown("**Send newsletter to these recipients?**")
    st.code(", ".join(emails))
    st.markdown(f"**Updates selected:** `{len(items)}`")
    st.markdown("---")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Send Now", use_container_width=True, type="primary"):
            active_scope = st.session_state.get('active_tab', 'RBI Updates')
            if 'RBI' in active_scope: source_label = "RBI"
            elif 'SEBI' in active_scope: source_label = "SEBI"
            else: source_label = "General"
            success, msg = send_email(items, emails, source_label)
            if success:
                st.session_state['success_msg'] = f"Successfully emailed {len(items)} updates to {len(emails)} recipients!"
                active_scope = st.session_state.get('active_tab', 'RBI Updates')
                source_type = 'rbi' if 'RBI' in active_scope else ('sebi' if 'SEBI' in active_scope else 'news')
                for i in items:
                    rss_collection.update_one(
                        {"link": i['link'], "type": source_type},
                        {"$set": {"title": i['title'], "link": i['link'], "date": i['pubDate'], "type": source_type}},
                        upsert=True
                    )
                st.rerun()
            else:
                st.error(f"Failed to send: {msg}")
    with col2:
        if st.button("Cancel", use_container_width=True):
            st.rerun()

# ═══════════════════════════════════════════════════════════════════
# GLOBAL CSS — matches screenshot exactly
# ═══════════════════════════════════════════════════════════════════
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600&display=swap');

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif !important; }
.stApp { background-color: #F5F3EE !important; }

/* Hide Streamlit chrome - while keeping sidebar accessible */
#MainMenu, footer { visibility: hidden !important; }
header { visibility: hidden !important; }
header button { visibility: visible !important; color: #0D1B2A !important; }
.stDeployButton { display: none !important; }

/* ── Sidebar ── */
section[data-testid="stSidebar"] {
    background-color: #FFFFFF !important;
    border-right: 0.5px solid #E2E8F0 !important;
}
section[data-testid="stSidebar"] > div { padding-top: 24px !important; }
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] label { color: #334155 !important; }

/* Sidebar Text Input / Area */
section[data-testid="stSidebar"] .stTextArea textarea {
    background-color: #F9FAFB !important;
    border: 0.5px solid #D1D5DB !important;
    border-radius: 8px !important;
    color: #0F172A !important;
    font-size: 13px !important;
    resize: none !important;
}
section[data-testid="stSidebar"] .stTextArea textarea::placeholder { color: #94A3B8 !important; }
section[data-testid="stSidebar"] .stTextArea textarea:focus {
    border-color: #3B82F6 !important;
    box-shadow: 0 0 0 3px rgba(59,130,246,0.05) !important;
}
section[data-testid="stSidebar"] hr { border-color: #F1F5F9 !important; }

/* Sidebar primary button */
section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background-color: #0D1B2A !important;
    border: none !important;
    color: #FFFFFF !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    border-radius: 8px !important;
    padding: 11px 0 !important;
}
section[data-testid="stSidebar"] .stButton > button[kind="primary"] * {
    color: #FFFFFF !important;
}
section[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover { 
    background-color: #1E2D3D !important;
    box-shadow: 0 4px 12px rgba(0,0,0,0.1) !important;
}

/* Sidebar secondary buttons (Select All / Clear) */
section[data-testid="stSidebar"] .stButton > button:not([kind="primary"]) {
    background-color: #FFFFFF !important;
    border: 0.5px solid #CBD5E1 !important;
    color: #334155 !important;
    border-radius: 8px !important;
    font-size: 13.5px !important;
    font-weight: 500 !important;
}
section[data-testid="stSidebar"] .stButton > button:not([kind="primary"]):hover {
    background-color: #F8FAFC !important;
    border-color: #94A3B8 !important;
    color: #0F172A !important;
}

/* ── Page title ── */
h1 {
    font-family: 'DM Serif Display', serif !important;
    font-size: 2rem !important;
    font-weight: 400 !important;
    color: #0D1B2A !important;
    letter-spacing: -0.02em !important;
    margin-bottom: 2px !important;
}

/* ── Section headers ── */
h3 {
    font-family: 'DM Sans', sans-serif !important;
    font-size: 11px !important;
    font-weight: 700 !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase !important;
    color: #6B7280 !important;
    margin-top: 20px !important;
    margin-bottom: 10px !important;
}

/* ── Caption ── */
.stCaption, [data-testid="stCaptionContainer"] p {
    color: #9CA3AF !important;
    font-size: 12.5px !important;
}

/* ── Radio pill tabs ── */
div[data-testid="stRadio"] > label { display: none !important; }
div[data-testid="stRadio"] > div {
    display: flex !important;
    gap: 8px !important;
    flex-wrap: wrap !important;
    margin-bottom: 20px !important;
}
div[data-testid="stRadio"] label {
    padding: 8px 22px !important;
    border: 0.5px solid #D1D5DB !important;
    border-radius: 24px !important;
    background-color: #FFFFFF !important;
    font-size: 14px !important;
    font-weight: 400 !important;
    color: #6B7280 !important;
    cursor: pointer !important;
    white-space: nowrap !important;
    transition: all 0.12s ease !important;
}
div[data-testid="stRadio"] label:hover {
    background-color: #F9FAFB !important;
    border-color: #9CA3AF !important;
    color: #111827 !important;
}
/* Active tab */
div[data-testid="stRadio"] [data-baseweb="radio"] input:checked ~ div label {
    background-color: #0D1B2A !important;
    border-color: #0D1B2A !important;
    color: #FFFFFF !important;
    font-weight: 500 !important;
}
div[data-testid="stRadio"] [data-baseweb="radio"] input:checked ~ div label * {
    color: #FFFFFF !important;
}
div[data-testid="stRadio"] [data-baseweb="radio"] div[role="radio"] { display: none !important; }

/* ── Top toolbar buttons ── */
.stButton > button {
    background-color: #FFFFFF !important;
    border: 0.5px solid #D1D5DB !important;
    color: #374151 !important;
    border-radius: 24px !important;
    font-size: 13.5px !important;
    font-weight: 500 !important;
    padding: 8px 20px !important;
    transition: all 0.12s ease !important;
}
.stButton > button:hover {
    background-color: #F9FAFB !important;
    border-color: #9CA3AF !important;
    color: #111827 !important;
}

/* ── Search input ── */
.stTextInput label, .stDateInput label {
    font-size: 12px !important;
    font-weight: 600 !important;
    letter-spacing: 0.05em !important;
    text-transform: uppercase !important;
    color: #6B7280 !important;
}

/* ── Cards ── */
div[data-testid="stVerticalBlockBorderWrapper"] {
    background-color: #FFFFFF !important;
    border: 0.5px solid #E5E7EB !important;
    border-radius: 12px !important;
    box-shadow: none !important;
    margin-bottom: 8px !important;
    transition: border-color 0.12s, box-shadow 0.12s !important;
}
div[data-testid="stVerticalBlockBorderWrapper"]:hover {
    border-color: #D1D5DB !important;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05) !important;
}

/* ── Checkbox ── */
.stCheckbox [data-baseweb="checkbox"] [aria-checked="true"] div {
    background-color: #0D1B2A !important;
    border-color: #0D1B2A !important;
}

/* ── Divider ── */
hr[data-testid="stDivider"] {
    border-color: #F3F4F6 !important;
    margin: 24px 0 !important;
}

/* ── Date input ── */
/* -- Date input styling is now merged with TextInput label -- */
.stDateInput input {
    background-color: #FFFFFF !important;
    border: 0.5px solid #D1D5DB !important;
    border-radius: 8px !important;
    font-size: 13.5px !important;
    color: #374151 !important;
}

/* ── Spinner ── */
.stSpinner > div { border-top-color: #1D4ED8 !important; }

/* ── Toast ── */
div[data-testid="stToast"] {
    background-color: #0D1B2A !important;
    color: #F1F5F9 !important;
    border-radius: 8px !important;
    font-size: 13.5px !important;
}

/* ── Alert boxes ── */
div[data-testid="stAlert"] {
    border-radius: 8px !important;
    font-size: 13.5px !important;
}

/* ── Force White helper ── */
.force-white { color: #FFFFFF !important; }
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("""
    <div style="margin-bottom: 20px;">
        <div style="font-family:'DM Serif Display',serif; font-size:1.35rem; color:#0D1B2A; line-height:1.2; margin-bottom:4px;">Navigate AIF</div>
        <div style="font-size:11px; color:#64748B; letter-spacing:0.08em; text-transform:uppercase; font-weight:600;">Regulatory Intelligence</div>
    </div>
    <div style="background:#F8FAFC; border:0.5px solid #E2E8F0; border-radius:8px; padding:12px 14px; margin-bottom:22px; font-size:12.5px; color:#475569; line-height:1.6;">
        Precision-monitors AIF regulatory changes across
        <span style="color:#0D1B2A; font-weight:600;">RBI</span> &amp;
        <span style="color:#0D1B2A; font-weight:600;">SEBI</span>.
    </div>

    <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
        <svg width="15" height="15" viewBox="0 0 20 20" fill="none">
            <rect x="2" y="4" width="16" height="12" rx="2" stroke="#475569" stroke-width="1.4"/>
            <path d="M2 8l8 5 8-5" stroke="#475569" stroke-width="1.4"/>
        </svg>
        <span style="font-size:15px; font-weight:600; color:#1E293B;">Email Dispatch</span>
    </div>
    <div style="font-size:12.5px; color:#64748B; margin-bottom:18px; line-height:1.5;">
        Select items and send branded emails with attachments
    </div>
    """, unsafe_allow_html=True)

    sidebar_bulk_container = st.container()

    st.markdown("""
    <div style="font-size:11px; font-weight:700; letter-spacing:0.07em; text-transform:uppercase;
                color:#475569; margin-bottom:8px; margin-top:4px;">
        Recipients (comma-separated)
    </div>
    """, unsafe_allow_html=True)
    recipients_input = st.text_area(
        "Email Recipients",
        placeholder="email1@example.com, email2@example.com",
        label_visibility="collapsed",
        height=110
    )
    recipients = [e.strip() for e in recipients_input.split(",") if e.strip()]
    st.markdown("""
    <div style="font-size:11.5px; color:#475569; margin-top:6px;">
        Enter email addresses separated by commas
    </div>
    """, unsafe_allow_html=True)
    
    # Placeholder for the Send button to appear below the recipients box
    sidebar_send_container = st.container()

# ═══════════════════════════════════════════════════════════════════
# PAGE HEADER
# ═══════════════════════════════════════════════════════════════════
st.markdown("""
<div style="margin-bottom:2px;">
    <span style="font-family:'DM Serif Display',serif; font-size:2rem; color:#0D1B2A; letter-spacing:-0.02em;">
        Navigate AIF
    </span>
</div>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════
@st.cache_data(ttl=900)
def load_all_feeds():
    sync_time = datetime.datetime.now().strftime('%-d/%m/%Y, %-I:%M %p')
    rbi_urls = [
        "https://rbi.org.in/pressreleases_rss.xml",
        "https://rbi.org.in/notifications_rss.xml",
        "https://rbi.org.in/Publication_rss.xml",
        "https://rbi.org.in/speeches_rss.xml",
        "https://rbi.org.in/tenders_rss.xml"
    ]
    sebi_url = "https://www.sebi.gov.in/sebirss.xml"
    all_rbi_items = []
    for url in rbi_urls:
        all_rbi_items.extend(parse_items(fetch_rss(url)))
    all_rbi_items = sort_items_by_date(all_rbi_items)
    r_rel, r_oth = filter_updates(all_rbi_items, RBI_STRONG_TERMS, RBI_CONTEXT_REQUIRED)
    all_sebi_items = parse_items(fetch_rss(sebi_url))
    all_sebi_items = sort_items_by_date(all_sebi_items)
    s_rel, s_oth = filter_updates(all_sebi_items, SEBI_STRONG_TERMS, SEBI_CONTEXT_REQUIRED)
    return r_rel, r_oth, s_rel, s_oth, sync_time

with st.spinner("Fetching regulatory feeds…"):
    rbi_relevant, rbi_others, sebi_relevant, sebi_others, last_sync = load_all_feeds()

st.caption(f"Regulatory & News Intelligence Dashboard  ·  Last synced: {last_sync}")

# ═══════════════════════════════════════════════════════════════════
# TOP TOOLBAR (reserve slot — rendered after tabs)
# ═══════════════════════════════════════════════════════════════════
top_button_container = st.container()

# ═══════════════════════════════════════════════════════════════════
# TABS
# ═══════════════════════════════════════════════════════════════════
active_tab = st.radio(
    "Feeds",
    ["RBI Updates", "SEBI Updates", "General AIF News"],
    horizontal=True,
    label_visibility="collapsed",
    key="active_tab"
)

all_selected_items = []

# ── Callbacks ──────────────────────────────────────────────────────
def cb_select_all():
    active_scope = st.session_state.get('active_tab', 'RBI Updates')
    if 'RBI' in active_scope: tab_prefix = 'RBI'
    elif 'SEBI' in active_scope: tab_prefix = 'SEBI'
    else: tab_prefix = 'General'
    st.session_state[f'force_select_{tab_prefix}'] = True
    st.session_state['force_select_all'] = False
    target_prefix = f'sel_{tab_prefix}'
    for k in list(st.session_state.keys()):
        if k.startswith(target_prefix):
            st.session_state[k] = True

def cb_clear_all():
    active_scope = st.session_state.get('active_tab', 'RBI Updates')
    if 'RBI' in active_scope: tab_prefix = 'RBI'
    elif 'SEBI' in active_scope: tab_prefix = 'SEBI'
    else: tab_prefix = 'General'
    st.session_state[f'force_select_{tab_prefix}'] = False
    st.session_state['force_select_all'] = False
    target_prefix = f'sel_{tab_prefix}'
    for k in list(st.session_state.keys()):
        if k.startswith(target_prefix):
            st.session_state[k] = False

# ── Section label helper ───────────────────────────────────────────
def section_label(text, count=None):
    count_html = ""
    if count is not None:
        count_html = f'<span style="background:#F3F4F6; color:#6B7280; border:0.5px solid #E5E7EB; padding:1px 10px; border-radius:20px; font-size:11px; font-weight:600; margin-left:10px;">{count}</span>'
    st.markdown(f"""
    <div style="display:flex; align-items:center; margin:22px 0 12px 0;">
        <span style="font-size:11px; font-weight:700; color:#6B7280; letter-spacing:0.08em; text-transform:uppercase;">{text}</span>
        {count_html}
    </div>
    """, unsafe_allow_html=True)

# ── Display Items ──────────────────────────────────────────────────
def display_items(items, source_name, is_relevant):
    if not items:
        st.markdown(f"""
        <div style="text-align:center; padding:48px 0; color:#9CA3AF; font-size:14px;">
            No {'relevant ' if is_relevant else ''}updates found.
        </div>
        """, unsafe_allow_html=True)
        return

    if 'RBI' in source_name: s_type = "rbi"
    elif 'SEBI' in source_name: s_type = "sebi"
    else: s_type = "news"

    item_list = []
    links = [up['link'] for up in items]
    found_docs = list(rss_collection.find({"link": {"$in": links}, "type": s_type}, {"link": 1}))
    sent_links = {doc['link'] for doc in found_docs}

    for up in items:
        sent = up['link'] in sent_links
        item_list.append({**up, "Notified": "Yes" if sent else "No"})

    search_query = st.session_state.get("search_query", "").lower()
    if search_query:
        item_list = [
            up for up in item_list
            if search_query in up['title'].lower()
            or search_query in up['description'].lower()
            or search_query in up['pubDate'].lower()
        ]
        if not item_list:
            st.markdown(f"""
            <div style="text-align:center; padding:40px 0; color:#9CA3AF; font-size:14px;">
                No results match "<strong style="color:#6B7280;">{search_query}</strong>"
            </div>
            """, unsafe_allow_html=True)
            return

    for i, up in enumerate(item_list):
        with st.container(border=True):
            col_chk, col_content = st.columns([0.45, 9.55], vertical_alignment="center")

            with col_chk:
                chk_key = f"sel_{source_name}_{i}_{up['title'][:10]}"
                if chk_key not in st.session_state:
                    if 'RBI' in source_name: tab_prefix = 'RBI'
                    elif 'SEBI' in source_name: tab_prefix = 'SEBI'
                    else: tab_prefix = 'General'
                    if tab_prefix == 'General':
                        default_val = False
                    else:
                        default_val = (is_relevant and up['Notified'] == 'No')
                    if st.session_state.get(f'force_select_{tab_prefix}', False):
                        default_val = True
                    st.session_state[chk_key] = default_val

                is_selected = st.checkbox("Select", label_visibility="collapsed", key=chk_key)
                if is_selected:
                    all_selected_items.append(up)

            with col_content:
                # Badge: AIF Relevant / Miscellaneous
                if is_relevant:
                    main_badge = '<span style="background:#EFF6FF; color:#1D4ED8; border:0.5px solid #BFDBFE; padding:3px 9px; border-radius:5px; font-size:10px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase;">AIF Relevant</span>'
                else:
                    main_badge = '<span style="background:#F9FAFB; color:#6B7280; border:0.5px solid #E5E7EB; padding:3px 9px; border-radius:5px; font-size:10px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase;">Miscellaneous</span>'

                # Confidence badge
                confidence_badge = ""
                if up.get('confidence') == "High":
                    confidence_badge = '<span style="background:#F0FDF4; color:#15803D; border:0.5px solid #BBF7D0; padding:3px 9px; border-radius:5px; font-size:10px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; margin-left:6px;">High</span>'
                elif up.get('confidence') == "Medium (Contextual)":
                    confidence_badge = '<span style="background:#FFFBEB; color:#B45309; border:0.5px solid #FDE68A; padding:3px 9px; border-radius:5px; font-size:10px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; margin-left:6px;">Contextual</span>'

                # Sent badge
                sent_badge = ""
                if up['Notified'] == 'Yes':
                    sent_badge = '<span style="background:#F9FAFB; color:#9CA3AF; border:0.5px solid #E5E7EB; padding:3px 9px; border-radius:5px; font-size:10px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; margin-left:6px;">Sent</span>'

                # Date formatting
                raw_date = re.sub(r'\s([\+\-]\d{4}|[A-Z]{3,4})$', '', up['pubDate']).strip()
                try:
                    parsed_dt = pd.to_datetime(raw_date)
                    date_display = parsed_dt.strftime('%-d %b %Y')
                except Exception:
                    date_display = raw_date[:16] if len(raw_date) > 16 else raw_date

                link_label = "View Source" if "General" in source_name else "View Document"

                st.markdown(f"""
                <div style="padding:14px 6px 12px 2px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:6px;">
                        <div style="display:flex; align-items:center; flex-wrap:wrap; gap:0;">
                            {main_badge}{confidence_badge}{sent_badge}
                        </div>
                        <span style="color:#9CA3AF; font-size:13px; font-weight:400; white-space:nowrap;">{date_display}</span>
                    </div>
                    <div style="font-size:15.5px; font-weight:500; color:#111827; margin-bottom:10px; line-height:1.5;">{up['title']}</div>
                    <a href="{up['link']}" target="_blank"
                       style="color:#2563EB; font-size:13px; font-weight:500; text-decoration:none; display:inline-flex; align-items:center; gap:4px;">
                        {link_label}
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                            <path d="M2 10L10 2M10 2H5M10 2V7" stroke="#2563EB" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                    </a>
                </div>
                """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# TAB CONTENT
# ═══════════════════════════════════════════════════════════════════
if active_tab == "RBI Updates":
    section_label("AIF Relevant", len(rbi_relevant))
    display_items(rbi_relevant, "RBI", True)
    st.divider()
    section_label("Miscellaneous RBI Updates", len(rbi_others))
    display_items(rbi_others, "RBI_Other", False)

elif active_tab == "SEBI Updates":
    section_label("AIF Relevant", len(sebi_relevant))
    display_items(sebi_relevant, "SEBI", True)
    st.divider()
    section_label("Miscellaneous SEBI Updates", len(sebi_others))
    display_items(sebi_others, "SEBI_Other", False)

elif active_tab == "General AIF News":
    col_q, col_d = st.columns([6, 4], vertical_alignment="center")
    with col_q:
        news_query = st.text_input(
            "Search Query",
            value="AIF alternative investment fund India",
            help="Search string used for Google News RSS"
        )
    with col_d:
        today = datetime.date.today()
        month_ago = today - datetime.timedelta(days=30)
        date_range = st.date_input("Filter by Date", value=(month_ago, today), format="DD/MM/YYYY")

    if news_query:
        with st.spinner("Fetching industry news…"):
            encoded_q = requests.utils.quote(news_query)
            gn_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-IN&gl=IN&ceid=IN:en"
            gn_items = sort_items_by_date(parse_items(fetch_rss(gn_url)))
            final_items = []
            if len(date_range) == 2:
                start_dt, end_dt = date_range
                for item in gn_items:
                    try:
                        clean_dt_str = re.sub(r'\s([\+\-]\d{4}|[A-Z]{3,4})$', '', item['pubDate']).strip()
                        item_dt = pd.to_datetime(clean_dt_str).date()
                        if start_dt <= item_dt <= end_dt:
                            final_items.append(item)
                    except:
                        final_items.append(item)
            else:
                final_items = gn_items

            if final_items:
                section_label("General Industry Intelligence", len(final_items))
            else:
                st.markdown("""
                <div style="text-align:center; padding:60px 0; color:#9CA3AF; font-size:14px;">
                    General news results will appear here based on your search query
                </div>
                """, unsafe_allow_html=True)
            display_items(final_items, "General", True)
    else:
        st.markdown("""
        <div style="text-align:center; padding:60px 0; color:#9CA3AF; font-size:14px;">
            General news results will appear here based on your search query
        </div>
        """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# TOP TOOLBAR RENDER
# ═══════════════════════════════════════════════════════════════════
with top_button_container:
    col1, col2, col_div, col4 = st.columns([1.3, 1.0, 0.2, 7.5], vertical_alignment="center")
    with col1:
        st.button("Select All", key="top_selectall_btn", use_container_width=True, on_click=cb_select_all)
    with col2:
        st.button("Clear", key="top_clear_btn", use_container_width=True, on_click=cb_clear_all)
    with col_div:
        st.markdown("<div style='border-left:0.5px solid #E5E7EB; height:36px; margin:auto;'></div>", unsafe_allow_html=True)
    with col4:
        st.text_input(
            "Search",
            placeholder="Search by title, date, or keywords...",
            label_visibility="collapsed",
            key="search_query"
        )

# ═══════════════════════════════════════════════════════════════════
# SIDEBAR ACTION BUTTONS
# ═══════════════════════════════════════════════════════════════════
with sidebar_bulk_container:
    selected_count = len(all_selected_items)
    st.markdown(f"""
    <div style="background:#F1F5F9; border:0.5px solid #E2E8F0; border-radius:8px;
                padding:13px 16px; display:flex; justify-content:space-between;
                align-items:center; margin-bottom:10px;">
        <span style="font-size:13px; color:#475569;">Items selected</span>
        <div style="background:#0D1B2A; color:#FFFFFF !important; font-size:13px; font-weight:700;
                     width:28px; height:28px; border-radius:50%; display:inline-flex;
                     align-items:center; justify-content:center;">
             <b class="force-white">{selected_count}</b>
        </div>
    </div>
    """, unsafe_allow_html=True)

    sb_col1, sb_col2 = st.columns(2)
    with sb_col1:
        st.button("Select All", use_container_width=True, on_click=cb_select_all, key="sb_select_all")
    with sb_col2:
        st.button("Clear", use_container_width=True, on_click=cb_clear_all, key="sb_clear")

with sidebar_send_container:
    st.markdown("<div style='margin-top:20px;'></div>", unsafe_allow_html=True)
    if st.button("Send Selected Emails", type="primary", use_container_width=True):
        if not recipients:
            st.error("Please add at least one recipient email address.")
        elif not all_selected_items:
            st.warning("No items selected. Please select updates to send.")
        else:
            show_confirm_dialog(all_selected_items, recipients)
