#!/usr/bin/env python3
"""
JioMart Daily Shipment Report Downloader
Runs daily via GitHub Actions at 10:00 AM IST
"""

import asyncio, re, os, json, urllib.parse, urllib.request, tempfile, io
from datetime import datetime, timedelta
from PIL import Image
import pytesseract
from playwright.async_api import async_playwright

# ── Credentials from GitHub Secrets ───────────────────────────
JIOMART_USER     = os.environ["JIOMART_USER_ID"]
JIOMART_PASS     = os.environ["JIOMART_PASSWORD"]
MS_CLIENT_ID     = os.environ["MS_CLIENT_ID"]
MS_TENANT_ID     = os.environ["MS_TENANT_ID"]
MS_CLIENT_SECRET = os.environ["MS_CLIENT_SECRET"]
MS_REFRESH_TOKEN = os.environ["MS_REFRESH_TOKEN"]

# ── Date calculations ──────────────────────────────────────────
today     = datetime.today()
yesterday = today - timedelta(days=1)
start     = today.replace(day=1)

start_day   = start.day
end_day     = yesterday.day
month_lower = today.strftime("%b").lower()   # "may"
year_2digit = today.strftime("%y")           # "26"
month_cap   = today.strftime("%b")           # "May"

filename     = f"sr{start_day}-{end_day}{month_lower}{year_2digit}"  # sr1-7may26
month_folder = f"{month_cap}'{year_2digit}"                           # May'26

ONEDRIVE_PATH = f"Automation Team - Sales & Ads/Jio Mart/Sales/{month_folder}/{filename}.csv"


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
    print(f"✅ Uploaded: {result.get('name')} ({result.get('size')} bytes)")


def solve_captcha_image(img_bytes: bytes) -> str:
    """OCR the captcha image and evaluate the math expression."""
    img = Image.open(io.BytesIO(img_bytes)).convert("L")  # grayscale
    # Enlarge for better OCR accuracy
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
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        # ── 1. Login ───────────────────────────────────────────
        print("🔐 Logging into JioMart...")
        await page.goto("https://identity.seller.jiomart.com/sso/login")
        await page.wait_for_load_state("networkidle")

        # Correct field IDs confirmed from DOM inspection
        await page.fill("#user_user_id", JIOMART_USER)
        await page.fill("#user_password", JIOMART_PASS)

        # Screenshot the captcha image element → OCR → solve
        captcha_img_el = page.locator('img[alt="captcha"]')
        await captcha_img_el.wait_for(state="visible")
        img_bytes = await captcha_img_el.screenshot()
        answer    = solve_captcha_image(img_bytes)
        print(f"🧮 Captcha answer: {answer}")

        await page.fill("#captcha", answer)
        await page.click("#login-submit-btn")
        await page.wait_for_url("**/seller.jiomart.com/**", timeout=20000)
        print("✅ Login successful!")

        # ── 2. Navigate to Shipment Report ────────────────────
        await page.goto("https://seller.jiomart.com/oms/reports/ShipmentReport?page=1&per_page=20")
        await page.wait_for_load_state("networkidle")
        print("📋 On Shipment Report page")

        # ── 3. Start Date ──────────────────────────────────────
        await page.click('input[placeholder="Start Date"]')
        await page.wait_for_selector("td, .day", state="visible")
        await page.locator("td").filter(
            has_text=re.compile(r"^01$")
        ).first.click()
        print(f"📅 Start date: {start.strftime('%d %b %y')}")

        # ── 4. End Date ────────────────────────────────────────
        await page.click('input[placeholder="End Date"]')
        await page.wait_for_selector("td, .day", state="visible")
        await page.locator("td").filter(
            has_text=re.compile(rf"^{end_day:02d}$")
        ).first.click()
        print(f"📅 End date:   {yesterday.strftime('%d %b %y')}")

        # ── 5. File name ───────────────────────────────────────
        await page.fill('input[placeholder*="File Name"]', filename)
        print(f"📁 Filename:   {filename}")

        # ── 6. Generate ────────────────────────────────────────
        await page.click('button:has-text("Generate Report")')
        print("⚙️  Generation requested...")
        await page.wait_for_timeout(5000)

        # ── 7. Poll until Generated + download ready ───────────
        save_path = None
        for attempt in range(15):
            await page.reload()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)

            rows = page.locator("tr")
            for i in range(await rows.count()):
                row_text = await rows.nth(i).inner_text()
                if filename in row_text and "Generated" in row_text:
                    print(f"✅ Report ready! (attempt {attempt+1})")
                    dl_btn = rows.nth(i).locator('a, button').filter(
                        has_text=re.compile(r"download", re.I)
                    )
                    async with page.expect_download() as dl_info:
                        await dl_btn.click()
                    download  = await dl_info.value
                    save_path = os.path.join(download_dir, f"{filename}.csv")
                    await download.save_as(save_path)
                    print(f"⬇️  Downloaded: {save_path}")
                    break
            if save_path:
                break
            print(f"   Still processing... ({attempt+1}/15)")

        await browser.close()

        if not save_path:
            raise RuntimeError("Report not ready after 15 attempts. Check JioMart manually.")

        # ── 8. Upload to OneDrive ──────────────────────────────
        print("☁️  Uploading to OneDrive...")
        token = get_access_token()
        upload_to_onedrive(save_path, token)
        print(f"\n🎉 Done! Saved to OneDrive: {ONEDRIVE_PATH}")


if __name__ == "__main__":
    asyncio.run(run())
