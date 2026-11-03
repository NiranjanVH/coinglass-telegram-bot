
import asyncio
import json
import os
import requests

from playwright.async_api import async_playwright

from scraper import JS_EXTRACT_SCRIPT

TARGET_URL = (
    "https://www.coinglass.com/pro/futures/"
    "LiquidationHeatMap?coin=BTC&type=pair"
)

STORAGE_STATE = "coinglass_state.json"
VIEWPORT = {"width": 1400, "height": 900}


# --------------------------------------------------
# VOLUME FORMAT
# --------------------------------------------------

def format_volume(value):
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:,.2f}K"
    return f"${value:,.2f}"


# --------------------------------------------------
# TIMEFRAME SELECTORS
# --------------------------------------------------

async def select_timeframe(page, timeframe):

    print(f"[{timeframe}] Selecting timeframe...")

    dropdown = page.locator(
        "button.MuiSelect-button:has-text('hour'), "
        "button.MuiSelect-button:has-text('day'), "
        "button.MuiSelect-button:has-text('24 hour')"
    ).first

    await dropdown.wait_for(state="visible", timeout=15000)
    await dropdown.click()

    await asyncio.sleep(1)

    if timeframe == "48H":
        selectors = [
            "li[role='option']:has-text('48 hour')",
            "div[role='option']:has-text('48 hour')",
            "[role='option']:has-text('48h')"
        ]

    elif timeframe == "12H":
        selectors = [
            "li[role='option']:has-text('12 hour')",
            "div[role='option']:has-text('12 hour')",
            "[role='option']:has-text('12h')"
        ]

    elif timeframe == "24H":
        selectors = [
            "li[role='option']:has-text('24 hour')",
            "div[role='option']:has-text('24 hour')",
            "[role='option']:has-text('24h')"
        ]

    elif timeframe == "3D":
        selectors = [
            "li[role='option']:has-text('3 day')",
            "div[role='option']:has-text('3 day')",
            "[role='option']:has-text('3d')"
        ]

    else:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    selected = False

    for selector in selectors:
        option = page.locator(selector).first

        try:
            await option.wait_for(
                state="visible",
                timeout=5000
            )
            await option.click()
            selected = True
            break

        except Exception:
            continue

    if not selected:
        raise RuntimeError(
            f"[{timeframe}] Could not find timeframe option."
        )

    await asyncio.sleep(3)

    print(f"[{timeframe}] Timeframe selected successfully.")


# --------------------------------------------------
# PROCESS CHART DATA
# --------------------------------------------------

def process_chart_data(raw_data):

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

    if not isinstance(raw_candle, (list, tuple)):
        return None

    if len(raw_candle) < 4:
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

        if not isinstance(point, (list, tuple)):
            continue

        if len(point) < 3:
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
            "side": (
                "SHORT LIQ"
                if diff > 0
                else "LONG LIQ"
            ),
            "diff": diff,
            "diff_pct": (diff / current_price) * 100,
        })

    if not orders:
        return None

    # Keep the top 15 highest-volume orders.
    top_orders = sorted(
        orders,
        key=lambda item: item["volume"],
        reverse=True
    )[:15]

    # Display highest price first.
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


# --------------------------------------------------
# SCRAPE ONE TIMEFRAME
# --------------------------------------------------

async def scrape_timeframe(timeframe):

    output_file = f"coinglass_heatmap_{timeframe.lower()}.json"

    print(f"\n[{timeframe}] Starting browser...")

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled"
            ]
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

            print(f"[{timeframe}] Opening CoinGlass...")

            response = await page.goto(
                TARGET_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            print(f"[{timeframe}] URL: {page.url}")
            print(f"[{timeframe}] TITLE: {await page.title()}")

            status = response.status if response else None

            print(f"[{timeframe}] HTTP STATUS: {status}")

            if status is not None and status >= 400:

                await page.screenshot(
                    path=f"debug_{timeframe.lower()}.png",
                    full_page=True
                )

                raise RuntimeError(
                    f"CoinGlass returned HTTP {status}"
                )

            print(f"[{timeframe}] Initializing page...")

            await asyncio.sleep(15)

            await page.screenshot(
                path=f"debug_{timeframe.lower()}.png",
                full_page=True
            )

            await page.wait_for_selector(
                ".echarts-for-react",
                state="visible",
                timeout=45000
            )

            print(f"[{timeframe}] Chart detected.")

            await select_timeframe(page, timeframe)

            print(f"[{timeframe}] Hydrating chart...")

            await asyncio.sleep(60)

            raw_data = None

            for attempt in range(1, 7):

                print(
                    f"[{timeframe}] "
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

                        print(
                            f"[{timeframe}] "
                            "Valid chart data detected."
                        )

                        break

                    print(f"[{timeframe}] Chart data incomplete.")

                else:
                    print(
                        f"[{timeframe}] Extraction error: {candidate}"
                    )

                await asyncio.sleep(15)

            if raw_data is None:
                raise RuntimeError(
                    f"[{timeframe}] Chart data not ready."
                )

            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(raw_data, f)

            print(f"[{timeframe}] Saved {output_file}")

            processed = process_chart_data(raw_data)

            if not processed or not processed["whale_orders"]:
                raise RuntimeError(
                    f"[{timeframe}] No liquidation data processed."
                )

            print(
                f"[{timeframe}] "
                f"{len(processed['whale_orders'])} orders extracted."
            )

            return processed

        finally:

            await browser.close()

            print(f"[{timeframe}] Browser closed.")


# --------------------------------------------------
# BUILD TELEGRAM MESSAGE
# --------------------------------------------------

def build_telegram_message(data, timeframe):

    orders = data["whale_orders"]

    lines = [
        f"<b>⚡ COINGLASS BTC | {timeframe}</b>",
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

    highest = sorted_volumes[0] if sorted_volumes else 0

    second = (
        sorted_volumes[1]
        if len(sorted_volumes) > 1
        else 0
    )

    separator_inserted = False

    for order in orders:

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


# --------------------------------------------------
# SEND TELEGRAM MESSAGE
# --------------------------------------------------

def send_telegram_message(message, timeframe):

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
            f"[{timeframe}] Telegram error: {result}"
        )

    print(
        f"[{timeframe}] Telegram message sent successfully!"
    )


# --------------------------------------------------
# MAIN - FOUR PARALLEL SCRAPERS
# --------------------------------------------------

async def main():

    print("=" * 60)
    print("STARTING FOUR PARALLEL COINGLASS SCRAPERS")
    print("=" * 60)

    timeframes = ["48H", "12H", "24H", "3D"]

    # Start all four browser sessions concurrently.
    results = await asyncio.gather(
        *[
            scrape_timeframe(timeframe)
            for timeframe in timeframes
        ],
        return_exceptions=True
    )

    successful = 0
    errors = []

    # Send one independent Telegram message per timeframe.
    for timeframe, result in zip(timeframes, results):

        if isinstance(result, BaseException):

            print(f"[{timeframe}] FAILED: {result}")
            errors.append(f"{timeframe}: {result}")
            continue

        try:

            message = build_telegram_message(
                result,
                timeframe
            )

            print(f"[{timeframe}] Sending Telegram message...")

            await asyncio.to_thread(
                send_telegram_message,
                message,
                timeframe
            )

            successful += 1

        except Exception as error:

            print(f"[{timeframe}] ERROR: {error}")
            errors.append(f"{timeframe}: {error}")

    print("\n" + "=" * 60)
    print(f"SUCCESSFUL TELEGRAM MESSAGES: {successful}/4")

    if errors:

        print("\nERRORS:")

        for error in errors:
            print(error)

        raise RuntimeError(
            "One or more timeframe jobs failed."
        )

    print("ALL FOUR TIMEFRAMES COMPLETED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
