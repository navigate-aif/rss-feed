import requests
import xml.etree.ElementTree as ET
import re
import sys
import os
import base64
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
collection = db['sebi_rss_feeds']

# --- Configuration: Precision Filtering ---

# These terms are uniquely AIF-related and trigger 100% confidence.
STRONG_AIF_TERMS = [
    "alternative investment fund",
    "alternative investment funds",
    "aif regulations",
    "sebi aif regulations",
    "aif framework",
    "aif guidelines",
    "aif compliance",
    "aif policy",
    "aif amendment",
    "aif master circular",
    "category i aif",
    "category ii aif",
    "category iii aif",
    "angel fund",
    "angel funds",
    "large value fund",
    "large value funds",
    "lvf",
    "venture capital fund",
    "venture capital funds",
    "social impact fund",
    "private equity fund",
    "private equity funds",
    "special situation fund",
    "special situation funds",
    "distressed asset fund",
    "distressed asset funds",
    "structured credit fund",
    "structured credit funds",
    "sustainable infrastructure fund",
    "sustainable infrastructure funds",
    "aif reporting",
    "aif disclosure",
    "placement memorandum",
    "ppm",
    "multiples private equity",
    "multiples alternate asset",
]

# These terms are relevant ONLY if the update also mentions "AIF" or "Alternative Investment Fund".
CONTEXT_REQUIRED_TERMS = [
    "venture capital",
    "private equity",
    "category i",
    "category ii",
    "category iii",
    "fund",
    "trust",
    "trusts",
    "sebi order",
    "adjudication order",
    "penalty order",
    "settlement order",
    "sebi circular",
    "master circular",
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
    "investment vehicles",
    "regulated entities",
    "investors"
]


COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
}

def fetch_sebi_rss(url):
    """Fetches the SEBI RSS feed with browser-mimicking headers."""
    try:
        print(f"[*] Fetching live RSS feed: {url}")
        response = requests.get(url, headers=COMMON_HEADERS, timeout=15)
        response.raise_for_status()
        return response.content
    except requests.RequestException as e:
        print(f"[!] Network Error: {e}")
        return None

def find_match(patterns, text):
    """Checks if any pattern matches the text using whole-word boundaries."""
    for pattern in patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', text, re.IGNORECASE):
            return pattern
    return None


def extract_pdf_link(page_url):
    """Navigates to the SEBI page and tries to find the direct PDF link."""
    try:
        print(f"    [*] Accessing page for PDF extraction: {page_url}")
        response = requests.get(page_url, headers=COMMON_HEADERS, timeout=10)
        response.raise_for_status()
        html_content = response.text
        
        # Search for pattern: file=https://www.sebi.gov.in/sebi_data/attachdocs/...pdf
        pdf_match = re.search(r'file=(https://www\.sebi\.gov\.in/sebi_data/attachdocs/[^&\'"]+\.pdf)', html_content)
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
    Implements multi-tier contextual filtering to eliminate noise.

    Tier 1 — STRONG_AIF_TERMS match in title/description (High confidence).
    Tier 2 — CONTEXT_REQUIRED_TERMS match + AIF context regex in title/description (Medium confidence).
    """
    if not xml_content:
        return []

    try:
        root = ET.fromstring(xml_content)
        items = root.findall('.//item')
        
        relevant_items = []
        aif_context_regex = r'\b(aif|alternative investment fund|alternative investment funds|lvf)\b'

        for item in items:
            title = (item.findtext('title') or "").strip()
            description = (item.findtext('description') or "").strip()
            link = (item.findtext('link') or "").strip()
            pub_date = (item.findtext('pubDate') or "").strip()
            
            # Clean the date: "09 Mar, 2026 +0530" -> "09 Mar, 2026"
            if " +" in pub_date:
                pub_date = pub_date.split(" +")[0].strip()
            
            content_lower = f"{title} {description}".lower()
            
            is_relevant = False
            matched_kw = None
            confidence = ""

            # --- Tier 1: Strong term in title/description ---
            strong_match = find_match(STRONG_AIF_TERMS, content_lower)
            if strong_match:
                is_relevant = True
                matched_kw = strong_match
                confidence = "High"

            # --- Tier 2: Context-required term + AIF context in title/description ---
            if not is_relevant:
                has_aif_context = re.search(aif_context_regex, content_lower)
                if has_aif_context:
                    context_match = find_match(CONTEXT_REQUIRED_TERMS, content_lower)
                    if context_match:
                        is_relevant = True
                        matched_kw = context_match
                        confidence = "Medium (Contextual)"


            if is_relevant:
                relevant_items.append({
                    'title': title,
                    'description': description,
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
    """Sends an email notification with PDF attachments using SendGrid."""
    api_key = os.environ.get('SENDGRID_API_KEY')
    if not api_key:
        print("[!] SendGrid API Key not found in environment.")
        return

    sender = "communications@navigateaif.com"

    # 1. Build the HTML content body first
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
                Navigate AIF – SEBI Regulatory Tracker
            </h2>
            <p style="color: #cbd5e1; margin: 5px 0 0 0; font-size: 14px;">
                Precision-Monitored AIF Regulatory Updates
            </p>
        </div>

        <!-- Body -->
        <div style="padding: 25px;">
            <p style="font-size: 15px; color: #334155;">
                Find the new <strong>AIF relevant regulatory update(s)</strong> released by SEBI
            </p>

            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
    """

    for up in updates:
        confidence_color = "#074173" if "High" in up['confidence'] else "#f59e0b"
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
                        View Official SEBI Link
                    </a>
                </p>
            </div>
        """

    html_body += """
            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 25px 0;">
            <p style="font-size: 12px; color: #94a3b8;">
                This alert is generated using Navigate AIF's precise research. Full PDF documents have been attached where available.
            </p>
        </div>

        <!-- Footer -->
        <div style="background: #f8fafc; padding: 15px; text-align: center; font-size: 12px; color: #64748b;">
            © 2026 Navigate AIF | Regulatory Intelligence
        </div>
    </div>
    """

    # 2. Create the Mail object properly
    message = Mail(
        from_email=sender,
        to_emails=recipients,
        subject="SEBI Regulatory Update",
        html_content=html_body
    )

    # 3. Process and add attachments
    for up in updates:
        print(f"[*] Attempting to fetch PDF for: {up['title']}")
        pdf_link = extract_pdf_link(up['link'])
        if pdf_link:
            print(f"    [+] PDF found: {pdf_link}")
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

    # 4. Final attempt to send
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
    print("      SEBI RSS TRACKER: AIF PRECISION MONITOR")
    print("=" * 80)

    url = "https://www.sebi.gov.in/sebirss.xml"
    xml_content = fetch_sebi_rss(url)
    
    if not xml_content:
        sys.exit(1)

    relevant_updates = parse_and_filter(xml_content)
    
    if not relevant_updates:
        print("\n[i] No relevant AIF updates found. (Noise filtered out)")
    else:
        # Filter out already sent updates using MongoDB
        new_updates = []
        for up in relevant_updates:
            if collection.find_one({"link": up['link']}):
                print(f"[i] Email already sent for: {up['title']}")
            else:
                new_updates.append(up)
        
        if not new_updates:
            print("\n[i] All relevant updates have already been notified.")
            return

        count = len(new_updates)
        print(f"\n[+] Found {count} new relevant updates (Precision Filtered):")
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
            # Get recipient emails dynamically
            recipients = []
            try:
                num_recipients = int(input("[?] How many recipients would you like to notify? ").strip())
                for i in range(num_recipients):
                    email = input(f"    Enter email address {i+1}: ").strip()
                    if email:
                        recipients.append(email)
            except ValueError:
                print("[!] Invalid number. Defaulting to manual entry.")
                
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