
import asyncio
import json
import os
import requests

from playwright.async_api import async_playwright

from scraper import (
    JS_EXTRACT_SCRIPT,
    select_48h_timeframe,
)

TARGET_URL = (
    "https://www.coinglass.com/pro/futures/"
    "LiquidationHeatMap?coin=BTC&type=pair"
)

STORAGE_STATE = "coinglass_state.json"

VIEWPORT = {"width": 1400, "height": 900}


def format_volume(value):
    """Use K/M/B for liquidation volume only."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:,.2f}K"
    return f"${value:,.2f}"


async def select_12h_timeframe(page):
    """Select the 12-hour timeframe."""
    dropdown = page.locator(
        "button.MuiSelect-button:has-text('hour'), "
        "button.MuiSelect-button:has-text('24 hour')"
    ).first

    await dropdown.wait_for(state="visible", timeout=15000)
    await dropdown.click()
    await asyncio.sleep(1)

    option = page.locator(
        "li[role='option']:has-text('12 hour'), "
        "div[role='option']:has-text('12 hour'), "
        "[role='option']:has-text('12h')"
    ).first

    await option.wait_for(state="visible", timeout=10000)
    await option.click()
    await asyncio.sleep(3)

    print("12H timeframe selected.")


def process_chart_data(raw_data):
    """Process the latest chart column and return the top 15 walls."""

    if not raw_data or raw_data.get("error"):
        return None

    x_axis = raw_data.get("xAxis", [])
    y_axis = raw_data.get("yAxis", [])
    series_list = raw_data.get("series", [])

    if not x_axis or not series_list:
        return None

    last_idx = len(x_axis) - 1

    candle_series = next(
        (
            s for s in series_list
            if isinstance(s, dict)
            and s.get("type") == "candlestick"
        ),
        None
    )

    heatmap_series = next(
        (
            s for s in series_list
            if isinstance(s, dict)
            and s.get("type") == "heatmap"
        ),
        None
    )

    if not candle_series or not heatmap_series:
        return None

    candle_data = candle_series.get("data", [])

    if len(candle_data) <= last_idx:
        return None

    raw_candle = candle_data[last_idx]

    if not isinstance(raw_candle, (list, tuple)) or len(raw_candle) < 4:
        return None

    candle = {
        "open": float(raw_candle[0]),
        "close": float(raw_candle[1]),
        "low": float(raw_candle[2]),
        "high": float(raw_candle[3]),
    }

    current_price = candle["close"]

    if current_price <= 0:
        return None

    orders = []

    for point in heatmap_series.get("data", []):

        if not isinstance(point, (list, tuple)) or len(point) < 3:
            continue

        x_idx, y_idx, volume = point[0], point[1], point[2]

        if x_idx != last_idx:
            continue

        try:
            y_index = int(y_idx)

            price_level = float(
                y_axis[y_index]
                if 0 <= y_index < len(y_axis)
                else y_idx
            )

            volume = float(volume)

        except (TypeError, ValueError, IndexError):
            continue

        if volume <= 0:
            continue

        diff = price_level - current_price

        orders.append({
            "price": price_level,
            "volume": volume,
            "side": "SHORT LIQ" if diff > 0 else "LONG LIQ",
            "diff": diff,
            "diff_pct": (diff / current_price) * 100,
        })

    if not orders:
        return None

    # Keep the 15 highest-volume liquidation walls.
    top_orders = sorted(
        orders,
        key=lambda item: item["volume"],
        reverse=True
    )[:15]

    # Display higher prices first.
    top_orders.sort(
        key=lambda item: item["price"],
        reverse=True
    )

    return {
        "timestamp": x_axis[last_idx],
        "price": current_price,
        "candle": candle,
        "whale_orders": top_orders,
    }


async def scrape_timeframe(label, timeframe_selector, output_file):

    print(f"[{label}] Starting browser...")

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )

        try:
            context = await browser.new_context(
                storage_state=STORAGE_STATE,
                viewport=VIEWPORT,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )

            page = await context.new_page()

            print(f"[{label}] Opening CoinGlass...")

            response = await page.goto(
                TARGET_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            print(f"[{label}] URL: {page.url}")
            print(f"[{label}] TITLE: {await page.title()}")
            print(
                f"[{label}] HTTP STATUS: "
                f"{response.status if response else 'Unknown'}"
            )

            if response and response.status >= 400:
                await page.screenshot(
                    path=f"debug_{label.lower()}.png",
                    full_page=True
                )
                raise RuntimeError(
                    f"[{label}] CoinGlass returned HTTP "
                    f"{response.status}"
                )

            print(f"[{label}] Waiting for page initialization...")
            await asyncio.sleep(15)

            await page.screenshot(
                path=f"debug_{label.lower()}.png",
                full_page=True
            )

            print(f"[{label}] Waiting for chart...")

            await page.wait_for_selector(
                ".echarts-for-react",
                state="visible",
                timeout=45000
            )

            print(f"[{label}] Chart detected.")

            print(f"[{label}] Selecting timeframe...")

            await timeframe_selector(page)

            print(f"[{label}] Waiting for chart hydration...")
            await asyncio.sleep(60)

            raw_data = None

            for attempt in range(1, 7):

                print(f"[{label}] Extraction attempt {attempt}/6")

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
                        print(f"[{label}] Valid chart data detected.")
                        break

                    print(f"[{label}] Chart data incomplete.")

                else:
                    print(f"[{label}] Extraction error: {candidate}")

                await asyncio.sleep(15)

            if raw_data is None:
                raise RuntimeError(
                    f"[{label}] Chart data did not become ready."
                )

            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(raw_data, f)

            print(f"[{label}] Data saved to {output_file}")

            processed = process_chart_data(raw_data)

            if not processed:
                raise RuntimeError(
                    f"[{label}] Could not process chart data."
                )

            if not processed["whale_orders"]:
                raise RuntimeError(
                    f"[{label}] No liquidation walls found."
                )

            print(
                f"[{label}] Found "
                f"{len(processed['whale_orders'])} liquidation walls."
            )

            return processed

        finally:
            await browser.close()
            print(f"[{label}] Browser closed.")


def build_telegram_message(data, label):

    orders = data["whale_orders"]

    lines = [
        f"<b>⚡ COINGLASS BTC | {label}</b>",
        "",
        f"🕒 {data['timestamp']}",
        f"💰 Price: ${data['price']:,.2f}",
        f"🔺 High: ${data['candle']['high']:,.2f}",
        f"🔻 Low: ${data['candle']['low']:,.2f}",
        "",
        "<b>🐋 LIQUIDATION WALLS</b>",
        "",
    ]

    # Find the highest and second-highest volumes.
    sorted_volumes = sorted(
        [order["volume"] for order in orders],
        reverse=True
    )

    highest = sorted_volumes[0] if sorted_volumes else 0
    second = sorted_volumes[1] if len(sorted_volumes) > 1 else 0

    separator_inserted = False

    for order in orders:

        # Active price separator.
        if (
            not separator_inserted
            and order["price"] < data["price"]
        ):
            lines.extend([
                f"<b>━━ ACTIVE: ${data['price']:,.2f} ━━</b>",
                ""
            ])
            separator_inserted = True

        is_long = order["side"] == "LONG LIQ"

        emoji = "🟢" if is_long else "🔴"
        side = "L" if is_long else "S"

        rank = ""

        if order["volume"] == highest:
            rank = " ⭐"

        elif order["volume"] == second and second > 0:
            rank = " 🥈"

        # One compact line per liquidation record.
        lines.append(
            f"{emoji} <b>{side}</b> "
            f"${order['price']:,.0f} | "
            f"{format_volume(order['volume'])}"
            f"{rank}"
        )

    if not separator_inserted:
        lines.extend([
            "",
            f"<b>━━ ACTIVE: ${data['price']:,.2f} ━━</b>"
        ])

    return "\n".join(lines)


def send_telegram_message(message, label):

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
            f"[{label}] Telegram error: {result}"
        )

    print(f"[{label}] Telegram message sent successfully!")


async def main():

    print("=" * 50)
    print("STARTING PARALLEL COINGLASS SCRAPERS")
    print("=" * 50)

    # Both browser sessions start concurrently.
    results = await asyncio.gather(
        scrape_timeframe(
            "48H",
            select_48h_timeframe,
            "coinglass_heatmap_48h.json"
        ),
        scrape_timeframe(
            "12H",
            select_12h_timeframe,
            "coinglass_heatmap_12h.json"
        ),
        return_exceptions=True
    )

    labels = ["48H", "12H"]
    successful = 0
    errors = []

    for label, result in zip(labels, results):

        if isinstance(result, Exception):
            print(f"[{label}] FAILED: {result}")
            errors.append(f"{label}: {result}")
            continue

        message = build_telegram_message(result, label)

        print(f"[{label}] Sending Telegram message...")

        try:
            await asyncio.to_thread(
                send_telegram_message,
                message,
                label
            )
            successful += 1

        except Exception as error:
            print(f"[{label}] Telegram failed: {error}")
            errors.append(f"{label} Telegram: {error}")

    print("=" * 50)
    print(f"SUCCESSFUL MESSAGES: {successful}/2")

    if errors:
        print("ERRORS:")
        for error in errors:
            print(error)

        raise RuntimeError(
            "One or more parallel scraper jobs failed."
        )

    print("BOTH TIMEFRAMES COMPLETED SUCCESSFULLY!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
