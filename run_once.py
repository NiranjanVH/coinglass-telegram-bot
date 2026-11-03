
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

STORAGE_STATE = "coinglass_state.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def format_volume(value):
    """Use K, M and B for liquidation volume only."""

    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"

    elif value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"

    elif value >= 1_000:
        return f"${value / 1_000:.2f}K"

    return f"${value:.2f}"


def format_price(value):
    """Always show actual BTC price, never K/M/B."""

    return f"${value:,.2f}"


async def main():

    async with async_playwright() as p:

        print("Launching Chromium...")

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )

        context_options = {
            "viewport": {
                "width": 1400,
                "height": 900,
            },
            "user_agent": USER_AGENT,
            "locale": "en-US",
            "timezone_id": "UTC",
            "ignore_https_errors": False,
        }

        # Reuse saved CoinGlass login session if available.
        if os.path.exists(STORAGE_STATE):
            context_options["storage_state"] = STORAGE_STATE
            print("Loading saved CoinGlass session...")
        else:
            print(
                "WARNING: No saved CoinGlass session found."
            )

        context = await browser.new_context(
            **context_options
        )

        page = await context.new_page()

        # Apply the initialization script BEFORE loading CoinGlass.
        await page.add_init_script("""
            window.__echarts_instances =
                window.__echarts_instances || [];

            let _echarts = undefined;

            Object.defineProperty(window, 'echarts', {
                get: function() {
                    return _echarts;
                },
                set: function(val) {
                    _echarts = val;
                    window._globalEcharts = val;
                },
                configurable: true
            });
        """)

        try:

            print("Opening CoinGlass...")

            response = await page.goto(
                TARGET_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            status = (
                response.status
                if response
                else None
            )

            print("URL:", page.url)
            print("TITLE:", await page.title())
            print("HTTP STATUS:", status)

            print("Waiting for page initialization...")

            await asyncio.sleep(10)

            # Save diagnostics before attempting extraction.
            await page.screenshot(
                path="debug.png",
                full_page=True
            )

            page_content = (
                await page.locator("body").inner_text()
            )

            print("\nPAGE CONTENT:")
            print(page_content[:3000])

            # Stop immediately if CoinGlass returns an HTTP error.
            if status and status >= 400:

                raise RuntimeError(
                    f"CoinGlass returned HTTP {status}. "
                    "The chart cannot be extracted. "
                    "Check debug.png and browser session."
                )

            # Wait for the actual chart.
            print("Waiting for chart...")

            await page.wait_for_selector(
                ".echarts-for-react",
                state="visible",
                timeout=60000
            )

            print("Chart detected!")

            # Select 48-hour timeframe.
            print("Selecting 48-hour timeframe...")

            await select_48h_timeframe(page)

            print("Waiting for chart hydration...")

            await asyncio.sleep(10)

            # Extract chart data with retries.
            raw_data = None

            for attempt in range(1, 7):

                print(
                    f"Extraction attempt {attempt}/6"
                )

                candidate = await page.evaluate(
                    JS_EXTRACT_SCRIPT
                )

                if (
                    candidate
                    and not candidate.get("error")
                ):

                    x_axis = candidate.get(
                        "xAxis", []
                    )

                    series = candidate.get(
                        "series", []
                    )

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
                        and len(candle.get("data", []))
                        >= len(x_axis)
                        and len(heatmap.get("data", [])) > 0
                    ):

                        raw_data = candidate

                        print(
                            "Valid chart data detected!"
                        )

                        break

                    else:

                        print(
                            "Chart data incomplete."
                        )

                else:

                    print(
                        "Extraction error:",
                        candidate
                    )

                await asyncio.sleep(15)

            if raw_data is None:

                await page.screenshot(
                    path="extraction_error.png",
                    full_page=True
                )

                raise RuntimeError(
                    "Chart data did not become ready."
                )

            # Save chart data.
            with open(
                JSON_FILENAME,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    raw_data,
                    f
                )

            print("Chart data saved successfully.")

            # Refresh saved login state if possible.
            try:
                await context.storage_state(
                    path=STORAGE_STATE
                )
                print("Login session saved.")
            except Exception as e:
                print("Could not save session:", e)

        finally:

            await browser.close()

    # ==========================================
    # PROCESS LIQUIDATION DATA
    # ==========================================

    print("Processing liquidation data...")

    data = process_latest_column_from_json()

    if not data:

        raise RuntimeError(
            "No liquidation data processed."
        )

    if not data.get("whale_orders"):

        raise RuntimeError(
            "No liquidation orders found."
        )

    orders = data["whale_orders"]

    print(
        f"Liquidation orders found: {len(orders)}"
    )

    # ==========================================
    # PREPARE TELEGRAM MESSAGE
    # ==========================================

    lines = [

        "<b>⚡ COINGLASS BTC | 48H</b>",

        "",

        f"🕒 {data['timestamp']}",

        f"💰 Price: {format_price(data['price'])}",

        f"🔺 High: {format_price(data['candle']['high'])}",

        f"🔻 Low: {format_price(data['candle']['low'])}",

        "",

        "<b>🐋 LIQUIDATION WALLS</b>",

        "",
    ]

    # Find highest and second-highest volumes.
    sorted_volumes = sorted(
        (
            order["volume"]
            for order in orders
        ),
        reverse=True
    )

    highest_vol = (
        sorted_volumes[0]
        if sorted_volumes
        else 0
    )

    second_highest_vol = (
        sorted_volumes[1]
        if len(sorted_volumes) > 1
        else 0
    )

    separator_inserted = False

    for order in orders:

        # Insert active BTC price between
        # short and long liquidation levels.
        if (
            not separator_inserted
            and order["price"] < data["price"]
        ):

            lines.extend([
                "",
                (
                    f"<b>━━ ACTIVE: "
                    f"{format_price(data['price'])} ━━</b>"
                ),
                "",
            ])

            separator_inserted = True

        # Compact liquidation side.
        if order["side"] == "LONG LIQ":

            side = "🟢 L"

        else:

            side = "🔴 S"

        # Highest-volume indicators.
        rank = ""

        if (
            highest_vol > 0
            and order["volume"] == highest_vol
        ):

            rank = " ⭐ 1ST"

        elif (
            second_highest_vol > 0
            and order["volume"] == second_highest_vol
        ):

            rank = " 🥈 2ND"

        # ONE LINE PER RECORD.
        lines.append(
            f"{side} "
            f"{format_price(order['price'])} "
            f"| {format_volume(order['volume'])}"
            f"{rank}"
        )

    # Add active price if no lower-priced order exists.
    if not separator_inserted:

        lines.extend([
            "",
            (
                f"<b>━━ ACTIVE: "
                f"{format_price(data['price'])} ━━</b>"
            ),
        ])

    message = "\n".join(lines)

    # ==========================================
    # SEND TELEGRAM MESSAGE
    # ==========================================

    print("\nSending Telegram message...")

    token = os.environ["TELEGRAM_BOT_TOKEN"]

    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    telegram_response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",

        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },

        timeout=30
    )

    telegram_response.raise_for_status()

    result = telegram_response.json()

    if not result.get("ok"):

        raise RuntimeError(
            f"Telegram error: {result}"
        )

    print(
        "Telegram message sent successfully!"
    )

    print(
        f"Total liquidation records sent: {len(orders)}"
    )


if __name__ == "__main__":

    asyncio.run(main())
