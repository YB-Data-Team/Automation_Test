#!/usr/bin/env python3
"""
JioMart Daily Shipment Report Downloader
Runs daily via GitHub Actions at 10:00 AM IST
"""

import asyncio, re, os, json, urllib.parse, urllib.request, tempfile
from datetime import datetime, timedelta
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

start_day   = start.day                         # e.g. 1
end_day     = yesterday.day                     # e.g. 7
month_lower = today.strftime("%b").lower()      # e.g. "may"
year_2digit = today.strftime("%y")              # e.g. "26"
month_cap   = today.strftime("%b")              # e.g. "May"

filename     = f"sr{start_day}-{end_day}{month_lower}{year_2digit}"   # sr1-7may26
month_folder = f"{month_cap}'{year_2digit}"                            # May'26

# ── OneDrive destination path ──────────────────────────────────
ONEDRIVE_PATH = f"Automation Team - Sales & Ads/Jio Mart/Sales/{month_folder}/{filename}.csv"


def get_access_token() -> str:
    """Exchange refresh token for a new access token."""
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
    """Upload file to OneDrive via Microsoft Graph API."""
    encoded_path = urllib.parse.quote(ONEDRIVE_PATH)
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded_path}:/content"
    with open(file_path, "rb") as f:
        content = f.read()
    req = urllib.request.Request(url, data=content, method="PUT")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "text/csv")
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    print(f"✅ Uploaded to OneDrive: {result.get('name')} ({result.get('size')} bytes)")


def solve_captcha_text(text: str) -> str:
    """Parse and solve a math captcha expression like '80 + 5 ='."""
    match = re.search(r"(\d+)\s*([\+\-])\s*(\d+)", text)
    if not match:
        raise ValueError(f"Could not parse captcha text: '{text}'")
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

        await page.fill('input[placeholder*="User id"], input[name*="user"], #username', JIOMART_USER)
        await page.fill('input[type="password"]', JIOMART_PASS)

        # Solve captcha — read raw text from captcha box then evaluate
        captcha_el   = page.locator("text=/\\d+ [\\+\\-] \\d+/").first
        captcha_text = await captcha_el.inner_text()
        answer       = solve_captcha_text(captcha_text)
        print(f"🧮 Captcha: {captcha_text.strip()} → {answer}")
        await page.fill('input[placeholder*="sum"], input[placeholder*="Enter"]', answer)

        await page.click('button:has-text("Submit")')
        await page.wait_for_url("**/seller.jiomart.com/**", timeout=20000)
        print("✅ Login successful!")

        # ── 2. Navigate to Shipment Report ────────────────────
        await page.goto("https://seller.jiomart.com/oms/reports/ShipmentReport?page=1&per_page=20")
        await page.wait_for_load_state("networkidle")
        print("📋 On Shipment Report page")

        # ── 3. Start Date — click day 01 ──────────────────────
        await page.click('input[placeholder="Start Date"]')
        await page.wait_for_selector("td, .day", state="visible")
        # Click the cell whose full text is exactly "01"
        await page.locator("td, .day").filter(has_text=re.compile(r"^01$")).first.click()
        print(f"📅 Start date: {start.strftime('%d %b %y')}")

        # ── 4. End Date — click yesterday's day ───────────────
        await page.click('input[placeholder="End Date"]')
        await page.wait_for_selector("td, .day", state="visible")
        await page.locator("td, .day").filter(
            has_text=re.compile(rf"^{end_day:02d}$")
        ).first.click()
        print(f"📅 End date:   {yesterday.strftime('%d %b %y')}")

        # ── 5. File name ───────────────────────────────────────
        await page.fill('input[placeholder*="File Name"]', filename)
        print(f"📁 Filename:   {filename}")

        # ── 6. Generate ────────────────────────────────────────
        await page.click('button:has-text("Generate Report")')
        print("⚙️  Generation requested, waiting...")
        await page.wait_for_timeout(5000)

        # ── 7. Poll until "Generated" + download button ────────
        save_path = None
        for attempt in range(15):           # max ~2.5 minutes
            await page.reload()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)

            rows = page.locator("tr")
            for i in range(await rows.count()):
                row_text = await rows.nth(i).inner_text()
                if filename in row_text and "Generated" in row_text:
                    print(f"✅ Report ready (attempt {attempt+1})")
                    dl_btn = rows.nth(i).locator('a, button').filter(
                        has_text=re.compile(r"download", re.I)
                    )
                    async with page.expect_download() as dl_info:
                        await dl_btn.click()
                    download  = await dl_info.value
                    save_path = os.path.join(download_dir, f"{filename}.csv")
                    await download.save_as(save_path)
                    print(f"⬇️  Downloaded to: {save_path}")
                    break
            if save_path:
                break
            print(f"   Still processing... ({attempt+1}/15)")

        await browser.close()

        if not save_path:
            raise RuntimeError("Report was not ready after 15 attempts. Check JioMart manually.")

        # ── 8. Upload to OneDrive ──────────────────────────────
        print("☁️  Uploading to OneDrive...")
        token = get_access_token()
        upload_to_onedrive(save_path, token)
        print(f"\n🎉 Done! File saved to OneDrive: {ONEDRIVE_PATH}")


if __name__ == "__main__":
    asyncio.run(run())
