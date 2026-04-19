import requests
import xml.etree.ElementTree as ET
import re
import sys
import os
import base64
from html.parser import HTMLParser
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
from dotenv import load_dotenv
from pymongo import MongoClient

# Load environment variables
load_dotenv()

# MongoDB Setup
mongo_url = os.environ.get('MONGO_URL')
db_name = os.environ.get('MONGO_DATABASE_NAME', 'compliance')
client = MongoClient(mongo_url)
db = client[db_name]
collection = db['rbi_rss_feeds']
config_collection = db['keyword_config']

def get_keywords():
    config = config_collection.find_one({"source": "RBI"})
    if config:
        return config.get("strong_terms", STRONG_AIF_TERMS_DEFAULT), config.get("context_terms", CONTEXT_REQUIRED_TERMS_DEFAULT)
    return STRONG_AIF_TERMS_DEFAULT, CONTEXT_REQUIRED_TERMS_DEFAULT

class HTMLStripper(HTMLParser):
    """Strips HTML tags from description fields before keyword matching."""
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return " ".join(self._parts)

def strip_html(raw_html):
    """Converts raw HTML to plain text for safe keyword matching."""
    s = HTMLStripper()
    try:
        s.feed(raw_html)
    except Exception:
        pass
    return s.get_text()

STRONG_AIF_TERMS_DEFAULT = [
    "alternative investment fund",
    "alternative investment funds",
    "sebi-registered alternative investment fund",
    "aif registered with sebi",
    "category i alternative investment fund",
    "category ii alternative investment fund",
    "category iii alternative investment fund",
    "investment in aif",
    "investment in aifs",
    "exposure to aif",
    "exposure to aifs",
    "units of aif",
    "units of aifs",
    "units of alternative investment fund",
    "units of alternative investment funds",
    "capital commitment to aif",
    "drawdown by aif",
    "overseas investment by aif",
    "overseas investment by aifs",
    "downstream investment by aif",
    "investment by banks in aif",
    "investment by nbfcs in aif",
    "regulated entities investment in aif",
    # RBI-specific AIF directions
    "investment in alternative investment fund",
]

CONTEXT_REQUIRED_TERMS_DEFAULT = [
    "venture capital",
    "private equity",
    "category i",
    "category ii",
    "category iii",
    "fvci",
    "foreign venture capital investor",
    "kyc",
    "ckyc",
    "aml",
    "cft",
    "assets under management",
    "aum",
    "net asset value",
    "nav",
    "private market",
    "private markets",
    "investment vehicle",
    "round tripping",
    "evergreening",
    "downstream investment",
    "structured exposure",
    "portfolio investment",
    "overseas direct investment",
    "odi",
    "non-debt instruments",
    "exposure norms",
    "risk weight",
    "capital adequacy",
    "prudential norms",
    "master direction",
    "reporting requirements",
    "liberalised remittance scheme",
    "lrs",
    "fpi",
    "fema",
    "foreign exchange management act",
]

# Load current keywords (either from DB or defaults)
STRONG_AIF_TERMS, CONTEXT_REQUIRED_TERMS = get_keywords()

AIF_CONTEXT_REGEX = r'\b(aif(?!i)|aifs|alternative investment fund|alternative investment funds)\b'

COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
}

def fetch_rss(url):
    """Fetches the RSS feed with browser-mimicking headers."""
    try:
        print(f"[*] Fetching live RSS feed: {url}")
        response = requests.get(url, headers=COMMON_HEADERS, timeout=15)
        response.raise_for_status()
        return response.content
    except requests.RequestException as e:
        print(f"[!] Network Error for {url}: {e}")
        return None

def find_match(patterns, text):
    """Checks if any pattern matches the text using whole-word boundaries."""
    for pattern in patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', text, re.IGNORECASE):
            return pattern
    return None

def extract_pdf_link(page_url):
    """Navigates to the RBI page and tries to find the direct PDF link."""
    try:
        print(f"    [*] Accessing page for PDF extraction: {page_url}")
        response = requests.get(page_url, headers=COMMON_HEADERS, timeout=10)
        response.raise_for_status()
        html_content = response.text

        # Search for pattern: https://rbidocs.rbi.org.in/rdocs/...pdf (case insensitive)
        pdf_match = re.search(r'(https://rbidocs\.rbi\.org\.in/rdocs/[^"\'\s]+\.pdf)', html_content, re.IGNORECASE)
        if pdf_match:
            pdf_url = pdf_match.group(1)
            print(f"    [+] Found PDF URL in page source: {pdf_url}")
            return pdf_url

        print("    [!] Could not find a PDF link in the expected format on this page.")
        return None
    except Exception as e:
        print(f"    [!] Error extracting PDF link from {page_url}: {e}")
        return None

def download_pdf(pdf_url):
    """Downloads PDF content."""
    try:
        print(f"    [*] Downloading PDF data from: {pdf_url}")
        response = requests.get(pdf_url, headers=COMMON_HEADERS, timeout=15)
        response.raise_for_status()
        content = response.content
        print(f"    [+] Download complete. Size: {len(content)} bytes.")
        return content
    except Exception as e:
        print(f"    [!] Error downloading PDF from {pdf_url}: {e}")
        return None

def parse_and_filter(xml_content):
    """
    Implements multi-tier contextual filtering for RBI updates.

    Tier 1 — STRONG_AIF_TERMS match in title OR description (High confidence).
              No secondary context check needed.
    Tier 2 — AIF context regex match + CONTEXT_REQUIRED_TERMS in title OR description
              (Medium confidence).

    FIX 1: Both title AND description are now scanned.
    FIX 2: HTML is stripped from descriptions before scanning.
    FIX 3: AIF context regex uses negative lookahead to exclude AIFI.
    FIX 4: Strong terms are standalone High-confidence (no gating).
    FIX 5: Removed over-broad context terms that caused false positives.
    """
    if not xml_content:
        return []

    try:
        root = ET.fromstring(xml_content)
        items = root.findall('.//item')

        relevant_items = []

        for item in items:
            title = (item.findtext('title') or "").strip()
            description_raw = (item.findtext('description') or "").strip()
            link = (item.findtext('link') or "").strip()
            pub_date = (item.findtext('pubDate') or "").strip()

            # FIX 1 + 2: Build a clean combined search field from title + stripped description
            description_text = strip_html(description_raw)
            combined_lower = f"{title} {description_text}".lower()

            is_relevant = False
            matched_kw = None
            confidence = ""

            # --- Tier 1: Strong standalone AIF term anywhere in title or description ---
            strong_match = find_match(STRONG_AIF_TERMS, combined_lower)
            if strong_match:
                is_relevant = True
                matched_kw = strong_match
                confidence = "High"

            # --- Tier 2: AIF context + context-required term ---
            if not is_relevant:
                # FIX 3: Negative lookahead prevents AIFI from matching as AIF
                has_aif_context = re.search(AIF_CONTEXT_REGEX, combined_lower)
                if has_aif_context:
                    context_match = find_match(CONTEXT_REQUIRED_TERMS, combined_lower)
                    if context_match:
                        is_relevant = True
                        matched_kw = context_match
                        confidence = "Medium (Contextual)"

            if is_relevant:
                relevant_items.append({
                    'title': title,
                    'description': description_raw,
                    'link': link,
                    'pubDate': pub_date,
                    'matched': matched_kw,
                    'confidence': confidence
                })

        return relevant_items
    except ET.ParseError as e:
        print(f"[!] XML Parsing Error: {e}")
        return []

def send_email_notification(updates, recipients):
    """Sends an email notification using SendGrid."""
    api_key = os.environ.get('SENDGRID_API_KEY')
    if not api_key:
        print("[!] SendGrid API Key not found in environment.")
        return

    sender = "communications@navigateaif.com"

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; 
                max-width: 680px; 
                margin: auto; 
                border: 1px solid #e6e6e6; 
                border-radius: 8px; 
                overflow: hidden;">

        <!-- Header -->
        <div style="background-color: #074173; padding: 20px;">
            <h2 style="color: #ffffff; margin: 0;">
                Navigate AIF – RBI Regulatory Tracker
            </h2>
            <p style="color: #dbeafe; margin: 5px 0 0 0; font-size: 14px;">
                RBI Notifications, Circulars & Press Releases
            </p>
        </div>

        <!-- Body -->
        <div style="padding: 25px;">
            <p style="font-size: 15px; color: #334155;">
                Find the new <strong>AIF relevant regulatory update(s)</strong> released by RBI
            </p>

            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
    """

    for up in updates:
        html_body += f"""
            <div style="margin-bottom: 28px; padding: 18px; border: 1px solid #e5e7eb; border-radius: 6px;">
                <h3 style="margin: 0 0 8px 0; color: #0f172a; font-size: 16px;">
                    {up['title']}
                </h3>
                <p style="margin: 0; font-size: 13px; color: #64748b;">
                    <strong>Date of Publication:</strong> {up['pubDate']}
                </p>
                <p style="margin: 10px 0;">
                    <a href="{up['link']}" 
                    style="
                        background-color: #2563eb;
                        color: #ffffff;
                        padding: 8px 14px;
                        font-size: 13px;
                        text-decoration: none;
                        border-radius: 4px;
                        display: inline-block;">
                        View Official RBI Link
                    </a>
                </p>
            </div>
        """

    html_body += """
            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 25px 0;">
            <p style="font-size: 12px; color: #94a3b8;">
                This alert is automatically generated using Navigate AIF's precision keyword framework for RBI data.
            </p>
        </div>

        <!-- Footer -->
        <div style="background: #f8fafc; padding: 15px; text-align: center; font-size: 12px; color: #64748b;">
            © 2026 Navigate AIF | RBI Intelligence System
        </div>
    </div>
    """

    message = Mail(
        from_email=sender,
        to_emails=recipients,
        subject=f"RBI RSS Alert: {len(updates)} New Relevant Updates",
        html_content=html_body
    )

    # Process and add attachments
    for up in updates:
        print(f"[*] Attempting to fetch PDF for: {up['title']}")
        pdf_link = extract_pdf_link(up['link'])
        if pdf_link:
            pdf_data = download_pdf(pdf_link)
            if pdf_data:
                encoded_file = base64.b64encode(pdf_data).decode()
                safe_title = re.sub(r'[^\w\-_\.]', '_', up['title'])[:50].strip('_')
                filename = f"{safe_title}.pdf"

                attachment = Attachment(
                    FileContent(encoded_file),
                    FileName(filename),
                    FileType('application/pdf'),
                    Disposition('attachment')
                )
                message.add_attachment(attachment)
                print(f"    [+] Attached: {filename}")
        else:
            print("    [!] No direct PDF link found on the page.")

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(f"[*] Email sent successfully! Status Code: {response.status_code}")
    except Exception as e:
        print(f"[!] Error sending email: {e}")
        if hasattr(e, 'body'):
            print(f"    [!] Details: {e.body}")

def main():
    print("\n" + "=" * 80)
    print("      RBI RSS TRACKER: AIF PRECISION MONITOR")
    print("=" * 80)

    urls = [
        "https://rbi.org.in/pressreleases_rss.xml",
        "https://rbi.org.in/notifications_rss.xml",
        "https://rbi.org.in/Publication_rss.xml",
        "https://rbi.org.in/speeches_rss.xml",
        "https://rbi.org.in/tenders_rss.xml"
    ]

    all_relevant_updates = []
    seen_links = set()

    for url in urls:
        xml_content = fetch_rss(url)
        if xml_content:
            updates = parse_and_filter(xml_content)
            for up in updates:
                if up['link'] not in seen_links:
                    all_relevant_updates.append(up)
                    seen_links.add(up['link'])

    if not all_relevant_updates:
        print("\n[i] No relevant AIF updates found across RBI feeds.")
    else:
        # Filter out already sent updates using MongoDB
        new_updates = []
        for up in all_relevant_updates:
            if collection.find_one({"link": up['link']}):
                print(f"[i] Email already sent for: {up['title']}")
            else:
                new_updates.append(up)

        if not new_updates:
            print("\n[i] All relevant updates have already been notified.")
            return

        count = len(new_updates)
        print(f"\n[+] Found {count} new relevant updates across all feeds:")
        print("=" * 80)

        for i, up in enumerate(new_updates, 1):
            print(f"[{i}] {up['title']}")
            print(f"    Date of Publication : {up['pubDate']}")
            print(f"    Link               : {up['link']}")
            print(f"    Matched Keyword    : {up['matched']}  [{up['confidence']}]")
            if i < count:
                print("-" * 80)
        print("=" * 80)

        # User confirmation FIRST
        confirm = input(f"\n[?] {count} new relevant updates found. Do you want to send email notifications? (yes/no): ").strip().lower()

        if confirm == 'yes':
            recipients = []
            try:
                num_recipients = int(input("[?] How many recipients would you like to notify? ").strip())
                for i in range(num_recipients):
                    email = input(f"    Enter email address {i+1}: ").strip()
                    if email:
                        recipients.append(email)
            except ValueError:
                print("[!] Invalid number.")

            if not recipients:
                print("[!] No recipients specified. Email notification cancelled.")
                return

            print(f"[*] Starting notification process for: {', '.join(recipients)}")
            send_email_notification(new_updates, recipients)

            # Store in MongoDB after successful notification
            for up in new_updates:
                collection.insert_one({
                    "title": up['title'],
                    "description": up['description'],
                    "date": up['pubDate'],
                    "link": up['link']
                })
            print(f"[*] {count} updates stored in MongoDB.")
        else:
            print("[i] Email notification cancelled.")

        print("=" * 80 + "\n")

if __name__ == "__main__":
    main()