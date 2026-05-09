#!/usr/bin/env python3
"""
JioMart Daily Shipment Report Downloader - v5
Runs daily via GitHub Actions at 10:00 AM IST
Uses xvfb-run + headless=False + stealth patches to bypass bot detection
"""

import asyncio, re, os, json, urllib.parse, urllib.request, tempfile, io
from datetime import datetime, timedelta
from PIL import Image
import pytesseract
from playwright.async_api import async_playwright

# ── Stealth script: hides Playwright/webdriver signatures ─────
STEALTH_SCRIPT = """
// Hide webdriver flag (the #1 bot-detection signal)
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true
});

// Mock realistic plugins list
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
            {name: 'Native Client',     filename: 'internal-nacl-plugin', description: ''},
        ];
        arr.refresh    = () => {};
        arr.item       = (i) => arr[i];
        arr.namedItem  = (n) => arr.find(p => p.name === n) || null;
        return arr;
    }
});

// Languages
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// Hardware signals (headless defaults look suspicious)
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });

// Add minimal chrome object expected by many sites
if (!window.chrome) {
    window.chrome = {
        runtime:    { connect: () => {}, sendMessage: () => {} },
        loadTimes:  function() { return {}; },
        csi:        function() { return {}; },
        app:        {}
    };
}

// Fix permissions query (headless Chromium breaks this)
if (navigator.permissions && navigator.permissions.query) {
    const _origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission, onchange: null })
            : _origQuery(params);
}
"""

# ── Credentials from GitHub Secrets ───────────────────────
JIOMART_USER     = os.environ["JIOMART_USER_ID"]
JIOMART_PASS     = os.environ["JIOMART_PASSWORD"]
MS_CLIENT_ID     = os.environ["MS_CLIENT_ID"]
MS_TENANT_ID     = os.environ["MS_TENANT_ID"]
MS_CLIENT_SECRET = os.environ["MS_CLIENT_SECRET"]
MS_REFRESH_TOKEN = os.environ["MS_REFRESH_TOKEN"]

# ── Date calculations ───────────────────────────────
today     = datetime.today()
yesterday = today - timedelta(days=1)
start     = today.replace(day=1)

start_day   = start.day
end_day     = yesterday.day
month_lower = today.strftime("%b").lower()   # "may"
year_2digit = today.strftime("%y")           # "26"
month_cap   = today.strftime("%b")           # "May"

filename     = f"sr{start_day}-{end_day}{month_lower}{year_2digit}"  # sr1-8may26
month_folder = f"{month_cap}\'{year_2digit}"                           # May'26

ONEDRIVE_PATH = f"Automation Team - Sales & Ads/Jio Mart/Sales/{month_folder}/{filename}.csv"

# ── Debug screenshots ──────────────────────────────
DEBUG_DIR = "debug_screenshots"
os.makedirs(DEBUG_DIR, exist_ok=True)

async def screenshot(page, name: str):
    path = os.path.join(DEBUG_DIR, f"{name}.png")
    await page.screenshot(path=path, full_page=True)
    print(f"Screenshot: {name}.png")


def get_access_token() -> str:
    url  = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "client_id":     MS_CLIENT_ID,
        "client_secret": MS_CLIENT_SECRET,
        "refresh_token": MS_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
        "scope":         "Files.ReadWrite offline_access",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


def upload_to_onedrive(file_path: str, access_token: str):
    encoded_path = urllib.parse.quote(ONEDRIVE_PATH)
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded_path}:/content"
    with open(file_path, "rb") as f:
        content = f.read()
    req = urllib.request.Request(url, data=content, method="PUT")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "text/csv")
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    print(f"Uploaded: {result.get('name')} ({result.get('size')} bytes)")


def solve_captcha_image(img_bytes: bytes) -> str:
    """OCR the captcha image and evaluate the math expression."""
    img = Image.open(io.BytesIO(img_bytes)).convert("L")  # grayscale
    img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    text = pytesseract.image_to_string(img, config="--psm 7 -c tessedit_char_whitelist=0123456789+-= ")
    print(f"   OCR raw text: '{text.strip()}'")
    match = re.search(r"(\d+)\s*([\+\-])\s*(\d+)", text)
    if not match:
        raise ValueError(f"Could not parse captcha OCR output: '{text.strip()}'")
    a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
    return str(a + b if op == "+" else a - b)


async def run():
    download_dir = tempfile.mkdtemp()

    async with async_playwright() as p:
        # ── Launch: non-headless via Xvfb virtual display ────────
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1920,1080",
                "--start-maximized",
            ]
        )
        context = await browser.new_context(
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )

        # ── Inject stealth patches BEFORE any page loads ─────────
        await context.add_init_script(STEALTH_SCRIPT)

        page = await context.new_page()

        # ── 1. Login ─────────────────────────────────────
        print("Logging into JioMart...")
        await page.goto(
            "https://identity.seller.jiomart.com/sso/login",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        print(f"   URL: {page.url}")

        # Allow JS to render the form
        await page.wait_for_timeout(5000)
        await screenshot(page, "01_after_goto")

        # Confirm stealth patch is working
        wd_value = await page.evaluate("navigator.webdriver")
        print(f"   navigator.webdriver = {wd_value}")  # should be None

        # Wait for the login form
        print("   Waiting for login form (#user_user_id)...")
        await page.wait_for_selector("#user_user_id", timeout=60000)
        await screenshot(page, "02_login_form_visible")
        print("Login form found!")

        await page.fill("#user_user_id", JIOMART_USER)
        await page.fill("#user_password", JIOMART_PASS)

        # OCR the captcha image
        captcha_img_el = page.locator('img[alt="captcha"]')
        await captcha_img_el.wait_for(state="visible", timeout=15000)
        img_bytes = await captcha_img_el.screenshot()
        answer    = solve_captcha_image(img_bytes)
        print(f"Captcha answer: {answer}")

        await page.fill("#captcha", answer)
        await screenshot(page, "03_before_submit")
        await page.click("#login-submit-btn")

        try:
            await page.wait_for_url("**/seller.jiomart.com/**", timeout=30000)
            print("Login successful!")
        except Exception:
            await screenshot(page, "03b_login_failed")
            raise RuntimeError("Login failed - check captcha or credentials. See screenshot.")

        await screenshot(page, "04_post_login")

        # ── 2. Navigate via left panel: Reports -> Shipment Report ──
        print("Opening Reports menu from left panel...")
        await page.wait_for_load_state("networkidle", timeout=30000)

        # Click the Reports dropdown in the left navigation
        try:
            await page.wait_for_selector("a.dropdown-toggle.reports", timeout=15000)
            await page.click("a.dropdown-toggle.reports")
            print("   Reports dropdown clicked")
        except Exception:
            # Fallback: look for any nav link containing "Report"
            await page.locator("nav a, .sidebar a, .left-menu a").filter(
                has_text=re.compile(r"report", re.I)
            ).first.click()
            print("   Reports dropdown clicked (fallback)")

        await screenshot(page, "05_reports_dropdown")

        # Click Shipment Report link from dropdown
        await page.wait_for_selector('a[href*="ShipmentReport"]', state="visible", timeout=15000)
        await page.click('a[href*="ShipmentReport"]')
        await page.wait_for_load_state("networkidle", timeout=30000)
        await screenshot(page, "06_shipment_report_page")
        print("On Shipment Report page")

        # ── 3. Start Date ──────────────────────────────
        await page.click('input[placeholder="Start Date"]')
        await page.wait_for_selector("td, .day", state="visible", timeout=10000)
        await page.locator("td").filter(
            has_text=re.compile(r"^01$")
        ).first.click()
        print(f"Start date: {start.strftime('%d %b %y')}")
        await screenshot(page, "07_start_date_set")

        # ── 4. End Date ────────────────────────────────
        await page.click('input[placeholder="End Date"]')
        await page.wait_for_selector("td, .day", state="visible", timeout=10000)
        await page.locator("td").filter(
            has_text=re.compile(rf"^{end_day:02d}$")
        ).first.click()
        print(f"End date: {yesterday.strftime('%d %b %y')}")
        await screenshot(page, "08_end_date_set")

        # ── 5. File name ───────────────────────────────
        await page.fill('input[placeholder*="File Name"]', filename)
        print(f"Filename: {filename}")

        # ── 6. Generate ────────────────────────────────
        await page.click('button:has-text("Generate Report")')
        print("Generation requested...")
        await screenshot(page, "09_generate_clicked")
        await page.wait_for_timeout(5000)

        # ── 7. Poll until Generated + download ready ───────────
        save_path = None
        for attempt in range(20):
            await page.reload()
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.wait_for_timeout(2000)
            await screenshot(page, f"10_poll_attempt_{attempt+1:02d}")

            rows = page.locator("tr")
            count = await rows.count()
            for i in range(count):
                row_text = await rows.nth(i).inner_text()
                if filename in row_text and "Generated" in row_text:
                    print(f"Report ready! (attempt {attempt+1})")
                    dl_btn = rows.nth(i).locator('a, button').filter(
                        has_text=re.compile(r"download", re.I)
                    )
                    async with page.expect_download() as dl_info:
                        await dl_btn.click()
                    download  = await dl_info.value
                    save_path = os.path.join(download_dir, f"{filename}.csv")
                    await download.save_as(save_path)
                    print(f"Downloaded: {save_path}")
                    break
            if save_path:
                break
            print(f"   Still processing... ({attempt+1}/20)")

        await browser.close()

        if not save_path:
            raise RuntimeError("Report not ready after 20 attempts. Check JioMart manually.")

        # ── 8. Upload to OneDrive ──────────────────────────
        print("Uploading to OneDrive...")
        token = get_access_token()
        upload_to_onedrive(save_path, token)
        print(f"Done! Saved to OneDrive: {ONEDRIVE_PATH}")


if __name__ == "__main__":
    asyncio.run(run())
