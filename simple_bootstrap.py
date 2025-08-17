import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await (await browser.new_context()).new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        print("Opened.")
        await page.wait_for_timeout(3000)
        await browser.close()

asyncio.run(run())
