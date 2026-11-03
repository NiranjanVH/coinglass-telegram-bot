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

TARGET_URL = "https://www.coinglass.com/pro/futures/LiquidationHeatMap?coin=BTC&type=pair"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            storage_state="coinglass_state.json",
            viewport={"width": 1400, "height": 900}
        )

        page = await context.new_page()

        await page.goto(TARGET_URL, wait_until="domcontentloaded")
        await page.wait_for_selector(".echarts-for-react", timeout=45000)

        await select_48h_timeframe(page)
        await asyncio.sleep(4)

        raw_data = await page.evaluate(JS_EXTRACT_SCRIPT)

        if not raw_data or raw_data.get("error"):
            raise Exception(f"Chart extraction failed: {raw_data}")

        with open(JSON_FILENAME, "w") as f:
            json.dump(raw_data, f)

        await browser.close()

    data = process_latest_column_from_json()

    if not data or not data["whale_orders"]:
        raise Exception("No liquidation data extracted")

    lines = [
        "<b>🐋 Bitcoin Liquidation Heatmap</b>",
        f"Timestamp: {data['timestamp']}",
        f"Current Price: ${data['price']:,.2f}",
        "",
        "<b>Top 15 Liquidation Orders</b>",
    ]

    for order in data["whale_orders"]:
        lines.append(
            f"{order['side']} | "
            f"${order['price']:,.2f} | "
            f"{order['diff_pct']:+.2f}% | "
            f"${order['volume']:,.0f}"
        )

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "\n".join(lines),
            "parse_mode": "HTML"
        },
        timeout=20
    )

    response.raise_for_status()
    print("Telegram message sent successfully!")

if __name__ == "__main__":
    asyncio.run(main())
