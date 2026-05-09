#!/usr/bin/env python3
"""
JioMart Daily Shipment Report Downloader - v3
Runs daily via GitHub Actions at 10:00 AM IST
"""

import asyncio, re, os, json, urllib.parse, urllib.request, tempfile, io
from datetime import datetime, timedelta
from PIL import Image
import pytesseract
from playwright.async_api import async_playwright

JIOMART_USER     = os.environ["JIOMART_USER_ID"]
JIOMART_PASS     = os.environ["JIOMART_PASSWORD"]
MS_CLIENT_ID     = os.environ["MS_CLIENT_ID"]
MS_TENANT_ID     = os.environ["MS_TENANT_ID"]
MS_CLIENT_SECRET = os.environ["MS_CLIENT_SECRET"]
MS_REFRESH_TOKEN = os.environ["MS_REFRESH_TOKEN"]

today     = datetime.today()
yesterday = today - timedelta(days=1)
start     = today.replace(day=1)
start_day   = start.day
end_day     = yesterday.day
month_lower = today.strftime("%b").lower()
year_2digit = today.strftime("%y")
month_cap   = today.strftime("%b")
filename     = f"sr{start_day}-{end_day}{month_lower}{year_2digit}"
month_folder = f"{month_cap}'{year_2digit}"
ONEDRIVE_PATH = f"Automation Team - Sales & Ads/Jio Mart/Sales/{month_folder}/{filename}.csv"

DEBUG_DIR = "debug_screenshots"
os.makedirs(DEBUG_DIR, exist_ok=True)

async def screenshot(page, name):
    await page.screenshot(path=os.path.join(DEBUG_DIR, f"{name}.png"), full_page=True)
    print(f"Screenshot: {name}.png")

def get_access_token():
    url  = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "client_id": MS_CLIENT_ID, "client_secret": MS_CLIENT_SECRET,
        "refresh_token": MS_REFRESH_TOKEN, "grant_type": "refresh_token",
        "scope": "Files.ReadWrite offline_access",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]

def upload_to_onedrive(file_path, access_token):
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{urllib.parse.quote(ONEDRIVE_PATH)}:/content"
    with open(file_path, "rb") as f:
        content = f.read()
    req = urllib.request.Request(url, data=content, method="PUT")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "text/csv")
    with urllib.request.urlopen(req) as resp:
        r = json.loads(resp.read())
    print(f"Uploaded: {r.get('name')} ({r.get('size')} bytes)")

def solve_captcha_image(img_bytes):
    img = Image.open(io.BytesIO(img_bytes)).convert("L")
    img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    text = pytesseract.image_to_string(img, config="--psm 7 -c tessedit_char_whitelist=0123456789+-= ")
    print(f"OCR: '{text.strip()}'")
    m = re.search(r"(\d+)\s*([\+\-])\s*(\d+)", text)
    if not m:
        raise ValueError(f"Captcha parse failed: '{text.strip()}'")
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    return str(a + b if op == "+" else a - b)

async def run():
    download_dir = tempfile.mkdtemp()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage",
                  "--disable-gpu","--disable-blink-features=AutomationControlled","--window-size=1920,1080"]
        )
        context = await browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        # Step 1: Login
        print("Logging into JioMart...")
        await page.goto("https://identity.seller.jiomart.com/sso/login", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector("#user_user_id", timeout=60000)
        await screenshot(page, "01_login_page")
        await page.fill("#user_user_id", JIOMART_USER)
        await page.fill("#user_password", JIOMART_PASS)
        captcha_el = page.locator('img[alt="captcha"]')
        await captcha_el.wait_for(state="visible", timeout=15000)
        answer = solve_captcha_image(await captcha_el.screenshot())
        print(f"Captcha: {answer}")
        await page.fill("#captcha", answer)
        await screenshot(page, "02_before_submit")
        await page.click("#login-submit-btn")
        try:
            await page.wait_for_url("**/seller.jiomart.com/**", timeout=30000)
            print("Login successful!")
        except Exception:
            await screenshot(page, "02b_login_failed")
            raise RuntimeError("Login failed")
        await screenshot(page, "03_post_login")

        # Step 2: Navigate via left panel Reports -> Shipment Report
        print("Opening Reports > Shipment Report from left panel...")
        await page.wait_for_load_state("networkidle", timeout=30000)
        try:
            await page.wait_for_selector("a.dropdown-toggle.reports", timeout=15000)
            await page.click("a.dropdown-toggle.reports")
        except Exception:
            await page.locator("nav a, .sidebar a, .left-menu a").filter(
                has_text=re.compile(r"report", re.I)).first.click()
        await screenshot(page, "04_reports_dropdown")
        await page.wait_for_selector('a[href*="ShipmentReport"]', state="visible", timeout=15000)
        await page.click('a[href*="ShipmentReport"]')
        await page.wait_for_load_state("networkidle", timeout=30000)
        await screenshot(page, "05_shipment_report_page")
        print("On Shipment Report page")

        # Step 3-5: Dates and filename
        await page.click('input[placeholder="Start Date"]')
        await page.wait_for_selector("td, .day", state="visible", timeout=10000)
        await page.locator("td").filter(has_text=re.compile(r"^01$")).first.click()
        await screenshot(page, "06_start_date")
        await page.click('input[placeholder="End Date"]')
        await page.wait_for_selector("td, .day", state="visible", timeout=10000)
        await page.locator("td").filter(has_text=re.compile(rf"^{end_day:02d}$")).first.click()
        await screenshot(page, "07_end_date")
        await page.fill('input[placeholder*="File Name"]', filename)

        # Step 6: Generate
        await page.click('button:has-text("Generate Report")')
        print("Generation requested...")
        await screenshot(page, "08_generate_clicked")
        await page.wait_for_timeout(5000)

        # Step 7: Poll for download
        save_path = None
        for attempt in range(20):
            await page.reload()
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.wait_for_timeout(2000)
            await screenshot(page, f"09_poll_{attempt+1:02d}")
            rows = page.locator("tr")
            for i in range(await rows.count()):
                row_text = await rows.nth(i).inner_text()
                if filename in row_text and "Generated" in row_text:
                    print(f"Report ready (attempt {attempt+1})")
                    dl_btn = rows.nth(i).locator('a, button').filter(has_text=re.compile(r"download", re.I))
                    async with page.expect_download() as dl_info:
                        await dl_btn.click()
                    d = await dl_info.value
                    save_path = os.path.join(download_dir, f"{filename}.csv")
                    await d.save_as(save_path)
                    print(f"Downloaded: {save_path}")
                    break
            if save_path:
                break
            print(f"Still processing... ({attempt+1}/20)")

        await browser.close()
        if not save_path:
            raise RuntimeError("Report not ready after 20 attempts.")

        # Step 8: Upload to OneDrive
        print("Uploading to OneDrive...")
        upload_to_onedrive(save_path, get_access_token())
        print(f"Done! OneDrive path: {ONEDRIVE_PATH}")

if __name__ == "__main__":
    asyncio.run(run())
