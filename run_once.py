
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


def format_volume(value):
    """Use K, M, B only for liquidation volume."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    elif value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    elif value >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"


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

            print("Selecting 48-hour timeframe...")
            await select_48h_timeframe(page)

            print("Waiting 60 seconds for chart hydration...")
            await asyncio.sleep(60)

            raw_data = None

            for attempt in range(1, 7):

                print(f"Extraction attempt {attempt}/6")

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
                    print("Extraction error:", candidate)

                await asyncio.sleep(15)

            if raw_data is None:
                raise RuntimeError(
                    "Chart data did not become ready."
                )

            with open(JSON_FILENAME, "w") as f:
                json.dump(raw_data, f)

            print("Chart data saved.")

        finally:
            await browser.close()

    print("Processing liquidation data...")

    data = process_latest_column_from_json()

    if not data:
        raise RuntimeError("No liquidation data processed.")

    if not data["whale_orders"]:
        raise RuntimeError("No whale orders found.")

    orders = data["whale_orders"]

    print(f"Liquidation orders found: {len(orders)}")

    # Telegram message
    lines = [
        "<b>⚡ COINGLASS BTC | 48H</b>",
        "",
        f"🕒 {data['timestamp']}",
        f"💰 Price: ${data['price']:,.2f}",
        f"🔺 High: ${data['candle']['high']:,.2f}",
        f"🔻 Low: ${data['candle']['low']:,.2f}",
        "",
        "<b>🐋 LIQUIDATION WALLS</b>",
        ""
    ]

    sorted_volumes = sorted(
        [order["volume"] for order in orders],
        reverse=True
    )

    highest_vol = sorted_volumes[0]

    second_highest_vol = (
        sorted_volumes[1]
        if len(sorted_volumes) > 1
        else 0
    )

    separator_inserted = False

    for order in orders:

        # Active price separator
        if (
            not separator_inserted
            and order["price"] < data["price"]
        ):

            lines.extend([
                f"<b>━━ ACTIVE: ${data['price']:,.2f} ━━</b>",
                ""
            ])

            separator_inserted = True

        # Volume ranking
        rank = ""

        if order["volume"] == highest_vol:
            rank = " ⭐ 1ST"

        elif (
            order["volume"] == second_highest_vol
            and second_highest_vol > 0
        ):
            rank = " 🥈 2ND"

        # No intensity bars
        lines.append(
            f"<b>{order['side']}</b> | "
            f"${order['price']:,.2f}\n"
            f"Δ {order['diff']:+,.2f} "
            f"({order['diff_pct']:+.2f}%) | "
            f"{format_volume(order['volume'])}{rank}"
        )

    if not separator_inserted:
        lines.extend([
            "",
            f"<b>━━ ACTIVE: ${data['price']:,.2f} ━━</b>"
        ])

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

    print(
        f"Telegram message sent successfully! "
        f"{len(orders)} orders included."
    )


if __name__ == "__main__":
    asyncio.run(main())
