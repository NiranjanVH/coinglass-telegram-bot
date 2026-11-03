
import asyncio
import json
import os
import requests

from playwright.async_api import async_playwright

from scraper import (
    JS_EXTRACT_SCRIPT,
    JSON_FILENAME,
    process_latest_column_from_json,
    select_48h_timeframe,
)

TARGET_URL = (
    "https://www.coinglass.com/pro/futures/"
    "LiquidationHeatMap?coin=BTC&type=pair"
)


async def main():

    async with async_playwright() as p:

        print("Launching Chromium...")

        browser = await p.chromium.launch(
            headless=True
        )

        context = await browser.new_context(
            storage_state="coinglass_state.json",
            viewport={
                "width": 1400,
                "height": 900
            }
        )

        page = await context.new_page()

        print("Opening CoinGlass...")

        response = await page.goto(
            TARGET_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        await asyncio.sleep(10)

        # Diagnostics
        print("\n========== DIAGNOSTICS ==========")

        print("URL:", page.url)
        print("TITLE:", await page.title())

        print(
            "HTTP STATUS:",
            response.status if response else "Unknown"
        )

        body_text = await page.locator("body").inner_text()

        print("\nPAGE CONTENT:")
        print(body_text[:3000])

        print("\n========== END DIAGNOSTICS ==========")

        await page.screenshot(
            path="debug.png",
            full_page=True
        )

        # Check chart
        print("Waiting for chart...")

        try:
            await page.wait_for_selector(
                ".echarts-for-react",
                timeout=45000
            )

        except Exception:
            print("Chart not found.")

            await browser.close()

            raise RuntimeError(
                "CoinGlass chart unavailable. "
                "Check URL, title and page content above."
            )

        print("Chart detected.")

        # Select 48-hour timeframe
        try:
            await select_48h_timeframe(page)
        except Exception as e:
            print("Timeframe selection warning:", e)

        await asyncio.sleep(5)

        # Extract chart data
        print("Extracting chart data...")

        raw_data = await page.evaluate(
            JS_EXTRACT_SCRIPT
        )

        if not raw_data or raw_data.get("error"):
            await browser.close()

            raise RuntimeError(
                f"Chart extraction failed: {raw_data}"
            )

        with open(JSON_FILENAME, "w") as f:
            json.dump(raw_data, f)

        await browser.close()

    # Process extracted data
    print("Processing liquidation data...")

    data = process_latest_column_from_json()

    if not data:
        raise RuntimeError("No liquidation data processed.")

    if not data["whale_orders"]:
        raise RuntimeError("No whale orders found.")

    # Build Telegram message
    lines = [
        "<b>🐋 Bitcoin Liquidation Heatmap</b>",
        "",
        f"<b>Timestamp:</b> {data['timestamp']}",
        f"<b>Current Price:</b> ${data['price']:,.2f}",
        "",
        "<b>Top 15 Liquidation Orders</b>",
        ""
    ]

    for order in data["whale_orders"]:

        lines.append(
            f"{order['side']} | "
            f"${order['price']:,.2f} | "
            f"{order['diff_pct']:+.2f}% | "
            f"${order['volume']:,.0f}"
        )

    message = "\n".join(lines)

    # Telegram delivery
    print("Sending Telegram message...")

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        },
        timeout=30
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram error: {result}"
        )

    print("Telegram message sent successfully!")


if __name__ == "__main__":
    asyncio.run(main())
