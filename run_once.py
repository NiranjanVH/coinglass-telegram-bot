
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

        try:
            print("Opening CoinGlass...")

            response = await page.goto(
                TARGET_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            print("URL:", page.url)
            print("TITLE:", await page.title())
            print(
                "HTTP STATUS:",
                response.status if response else "Unknown"
            )

            print("Response status:", response.status if response else None)
            print("Final URL:", page.url)
            print("Page title:", await page.title())
            print("Page content:", (await page.locator("body").inner_text())[:5000])
            print("Waiting for page initialization...")
            await asyncio.sleep(15)

            await page.screenshot(
                path="debug.png",
                full_page=True
            )

            print("Waiting for chart...")

            await page.wait_for_selector(
                ".echarts-for-react",
                timeout=45000
            )

            print("Chart detected.")

            # Select 48-hour timeframe
            print("Selecting 48-hour timeframe...")

            await select_48h_timeframe(page)

            # Allow chart to update after timeframe selection
            print("Waiting 60 seconds for chart hydration...")

            await asyncio.sleep(60)

            # Extract chart data with retries
            raw_data = None

            for attempt in range(1, 7):

                print(
                    f"Extraction attempt {attempt}/6"
                )

                candidate = await page.evaluate(
                    JS_EXTRACT_SCRIPT
                )

                if candidate and not candidate.get("error"):

                    x_axis = candidate.get("xAxis", [])
                    series = candidate.get("series", [])

                    candle = next(
                        (
                            s for s in series
                            if s.get("type") == "candlestick"
                        ),
                        None
                    )

                    heatmap = next(
                        (
                            s for s in series
                            if s.get("type") == "heatmap"
                        ),
                        None
                    )

                    if (
                        x_axis
                        and candle
                        and heatmap
                        and len(candle.get("data", [])) >= len(x_axis)
                        and len(heatmap.get("data", [])) > 0
                    ):

                        raw_data = candidate

                        print("Valid chart data detected.")

                        break

                    else:
                        print("Chart data incomplete.")

                else:
                    print(
                        "Extraction error:",
                        candidate
                    )

                await asyncio.sleep(15)

            if raw_data is None:

                raise RuntimeError(
                    "Chart data did not become ready."
                )

            # Save extracted chart data
            with open(JSON_FILENAME, "w") as f:

                json.dump(raw_data, f)

            print("Chart data saved.")

        finally:
            await browser.close()

    # Process data using original scraper function
    print("Processing liquidation data...")

    data = process_latest_column_from_json()

    if not data:
        raise RuntimeError(
            "No liquidation data processed."
        )

    if not data["whale_orders"]:
        raise RuntimeError(
            "No whale orders found."
        )

    # Prepare Telegram message
    lines = [
        "<b>⚡ LIVE COINGLASS FEED PAIR (48 HOUR TIMEFRAME)</b>",
        "",
        f"<b>Timestamp:</b> {data['timestamp']}",
        f"<b>Current Price:</b> ${data['price']:,.2f}",
        f"<b>High:</b> ${data['candle']['high']:,.2f}",
        f"<b>Low:</b> ${data['candle']['low']:,.2f}",
        "",
        "<b>🐋 Liquidation Walls Above & Below Current Price</b>",
        ""
    ]

    highest_vol = max(
        order["volume"]
        for order in data["whale_orders"]
    )

    second_highest = sorted(
        data["whale_orders"],
        key=lambda x: x["volume"],
        reverse=True
    )

    second_highest_vol = (
        second_highest[1]["volume"]
        if len(second_highest) > 1
        else 0
    )

    separator_inserted = False

    for order in data["whale_orders"]:

        if (
            not separator_inserted
            and order["price"] < data["price"]
        ):

            lines.extend([
                f"<b>ACTIVE PRICE: ${data['price']:,.2f}</b>",
                ""
            ])

            separator_inserted = True

        bar_len = max(
            1,
            int((order["volume"] / highest_vol) * 15)
        )

        rank = ""

        if order["volume"] == highest_vol:
            rank = " ⭐ 1ST HIGHEST"

        elif order["volume"] == second_highest_vol:
            rank = " 🥈 2ND HIGHEST"

        lines.append(
            f"<b>{order['side']}</b> | "
            f"${order['price']:,.2f} | "
            f"{order['diff']:+,.2f} "
            f"({order['diff_pct']:+.2f}%) | "
            f"${order['volume']:,.0f}\n"
            f"{'█' * bar_len}{rank}"
        )

    message = "\n".join(lines)

    # Send Telegram message
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
