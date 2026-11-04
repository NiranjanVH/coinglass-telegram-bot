import asyncio
import json
import os
import requests

from playwright.async_api import async_playwright
from scraper import JS_EXTRACT_SCRIPT


# ==================================================
# CONFIGURATION
# ==================================================

PAIR_URL = (
    "https://www.coinglass.com/pro/futures/"
    "LiquidationHeatMap?coin=BTC&type=pair"
)

SYMBOL_URL = (
    "https://www.coinglass.com/pro/futures/"
    "LiquidationHeatMap?coin=BTC&type=symbol"
)

STORAGE_STATE = "coinglass_state.json"

VIEWPORT = {
    "width": 1400,
    "height": 900
}

TIMEFRAMES = ["48H", "12H", "24H", "3D"]

CHART_SELECTOR = ".echarts-for-react"

MAX_EXTRACTION_ATTEMPTS = 8
CHART_WAIT_TIMEOUT = 20000
RETRY_DELAY = 10


# ==================================================
# VOLUME FORMAT
# ==================================================

def format_volume(value):

    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f}B"

    if value >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"

    if value >= 1_000:
        return f"${value / 1_000:,.2f}K"

    return f"${value:,.2f}"


# ==================================================
# TIMEFRAME SELECTORS
# ==================================================

async def select_timeframe(page, timeframe):

    print(f"[{timeframe}] Selecting timeframe...")

    dropdown = page.locator(
        "button.MuiSelect-button:has-text('hour'), "
        "button.MuiSelect-button:has-text('day'), "
        "button.MuiSelect-button:has-text('24 hour')"
    ).first

    await dropdown.wait_for(
        state="visible",
        timeout=20000
    )

    await dropdown.click()

    await asyncio.sleep(1)

    selectors = {

        "48H": [
            "li[role='option']:has-text('48 hour')",
            "div[role='option']:has-text('48 hour')",
            "[role='option']:has-text('48h')"
        ],

        "12H": [
            "li[role='option']:has-text('12 hour')",
            "div[role='option']:has-text('12 hour')",
            "[role='option']:has-text('12h')"
        ],

        "24H": [
            "li[role='option']:has-text('24 hour')",
            "div[role='option']:has-text('24 hour')",
            "[role='option']:has-text('24h')"
        ],

        "3D": [
            "li[role='option']:has-text('3 day')",
            "div[role='option']:has-text('3 day')",
            "[role='option']:has-text('3d')"
        ]

    }

    if timeframe not in selectors:
        raise ValueError(
            f"Unsupported timeframe: {timeframe}"
        )

    selected = False

    for selector in selectors[timeframe]:

        option = page.locator(selector).first

        try:

            await option.wait_for(
                state="visible",
                timeout=7000
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

    print(
        f"[{timeframe}] Timeframe selected successfully."
    )


# ==================================================
# WAIT FOR CHART
# ==================================================

async def wait_for_chart(
    page,
    market,
    timeframe,
    timeout=CHART_WAIT_TIMEOUT
):

    print(
        f"[{market} {timeframe}] "
        "Waiting for chart to appear..."
    )

    try:

        await page.locator(
            CHART_SELECTOR
        ).first.wait_for(
            state="visible",
            timeout=timeout
        )

        print(
            f"[{market} {timeframe}] "
            "Chart detected successfully."
        )

        return True

    except Exception:

        print(
            f"[{market} {timeframe}] "
            "Chart not visible yet."
        )

        return False


# ==================================================
# VALIDATE RAW CHART DATA
# ==================================================

def is_valid_chart_data(candidate):

    if not candidate or candidate.get("error"):
        return False

    x_axis = candidate.get("xAxis", [])
    series = candidate.get("series", [])

    if not x_axis or not series:
        return False

    candle = next(
        (
            s for s in series
            if isinstance(s, dict)
            and s.get("type") == "candlestick"
        ),
        None
    )

    heatmap = next(
        (
            s for s in series
            if isinstance(s, dict)
            and s.get("type") == "heatmap"
        ),
        None
    )

    if not candle or not heatmap:
        return False

    candle_data = candle.get("data", [])
    heatmap_data = heatmap.get("data", [])

    if len(candle_data) < len(x_axis):
        return False

    if not heatmap_data:
        return False

    return True


# ==================================================
# PROCESS CHART DATA
# ==================================================

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

    try:

        candle = {
            "open": float(raw_candle[0]),
            "close": float(raw_candle[1]),
            "low": float(raw_candle[2]),
            "high": float(raw_candle[3]),
        }

    except (TypeError, ValueError):
        return None

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

        try:

            if int(x_idx) != last_idx:
                continue

            y_index = int(y_idx)

            if 0 <= y_index < len(y_axis):
                price_level = float(y_axis[y_index])
            else:
                price_level = float(y_idx)

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

            "diff_pct": (
                diff / current_price
            ) * 100

        })

    if not orders:
        return None

    top_orders = sorted(
        orders,
        key=lambda item: item["volume"],
        reverse=True
    )[:15]

    top_orders.sort(
        key=lambda item: item["price"],
        reverse=True
    )

    return {

        "timestamp": x_axis[last_idx],

        "price": current_price,

        "candle": candle,

        "whale_orders": top_orders

    }


# ==================================================
# SCRAPE ONE MARKET / TIMEFRAME
# ==================================================

async def scrape_market_timeframe(
    browser,
    market,
    timeframe,
    target_url
):

    output_file = (
        f"coinglass_{market.lower()}_"
        f"{timeframe.lower()}.json"
    )

    print(
        f"\n[{market} {timeframe}] "
        "Starting browser context..."
    )

    context_args = {

        "viewport": VIEWPORT,

        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

    }

    if os.path.exists(STORAGE_STATE):
        context_args["storage_state"] = STORAGE_STATE

    context = await browser.new_context(
        **context_args
    )

    try:

        page = await context.new_page()

        print(
            f"[{market} {timeframe}] "
            f"Opening {target_url}"
        )

        response = await page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        print(
            f"[{market} {timeframe}] URL: {page.url}"
        )

        print(
            f"[{market} {timeframe}] TITLE: "
            f"{await page.title()}"
        )

        status = response.status if response else None

        print(
            f"[{market} {timeframe}] "
            f"HTTP STATUS: {status}"
        )

        if status is not None and status >= 400:

            await page.screenshot(
                path=(
                    f"debug_{market.lower()}_"
                    f"{timeframe.lower()}.png"
                ),
                full_page=True
            )

            raise RuntimeError(
                f"CoinGlass returned HTTP {status}"
            )

        print(
            f"[{market} {timeframe}] "
            "Initializing page..."
        )

        chart_ready = await wait_for_chart(
            page,
            market,
            timeframe,
            timeout=60000
        )

        if not chart_ready:

            await page.screenshot(
                path=(
                    f"debug_{market.lower()}_"
                    f"{timeframe.lower()}.png"
                ),
                full_page=True
            )

            raise RuntimeError(
                f"[{market} {timeframe}] "
                "Chart did not appear after 60 seconds."
            )

        await page.screenshot(
            path=(
                f"debug_{market.lower()}_"
                f"{timeframe.lower()}.png"
            ),
            full_page=True
        )

        await select_timeframe(
            page,
            timeframe
        )

        print(
            f"[{market} {timeframe}] "
            "Hydrating chart..."
        )

        await asyncio.sleep(45)

        raw_data = None

        for attempt in range(
            1,
            MAX_EXTRACTION_ATTEMPTS + 1
        ):

            print(
                f"[{market} {timeframe}] "
                f"Extraction attempt "
                f"{attempt}/{MAX_EXTRACTION_ATTEMPTS}"
            )

            chart_ready = await wait_for_chart(
                page,
                market,
                timeframe,
                timeout=CHART_WAIT_TIMEOUT
            )

            if not chart_ready:

                print(
                    f"[{market} {timeframe}] "
                    "Chart temporarily unavailable. "
                    "Retrying..."
                )

                await asyncio.sleep(RETRY_DELAY)
                continue

            try:

                candidate = await page.evaluate(
                    JS_EXTRACT_SCRIPT
                )

            except Exception as error:

                print(
                    f"[{market} {timeframe}] "
                    f"JavaScript extraction failed: {error}"
                )

                await asyncio.sleep(RETRY_DELAY)
                continue

            if is_valid_chart_data(candidate):

                raw_data = candidate

                print(
                    f"[{market} {timeframe}] "
                    "Valid chart data detected."
                )

                break

            print(
                f"[{market} {timeframe}] "
                f"Chart data incomplete: {candidate}"
            )

            await asyncio.sleep(RETRY_DELAY)

        if raw_data is None:

            await page.screenshot(
                path=(
                    f"debug_failed_{market.lower()}_"
                    f"{timeframe.lower()}.png"
                ),
                full_page=True
            )

            raise RuntimeError(
                f"[{market} {timeframe}] "
                "Chart data not ready after all retries."
            )

        with open(
            output_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(raw_data, f)

        print(
            f"[{market} {timeframe}] "
            f"Saved {output_file}"
        )

        processed = process_chart_data(raw_data)

        if not processed or not processed["whale_orders"]:

            raise RuntimeError(
                f"[{market} {timeframe}] "
                "No liquidation data processed."
            )

        print(
            f"[{market} {timeframe}] "
            f"{len(processed['whale_orders'])} "
            "orders extracted."
        )

        return processed

    finally:

        await context.close()

        print(
            f"[{market} {timeframe}] "
            "Browser context closed."
        )


# ==================================================
# SCRAPE ONE MARKET - FOUR PARALLEL TIMEFRAMES
# ==================================================

async def scrape_market(market, target_url, timeframes=TIMEFRAMES):

    print("\n" + "=" * 60)
    print(f"STARTING {market} SCRAPERS ({', '.join(timeframes)})")
    print("=" * 60)

    async with async_playwright() as p:

        browser = await p.chromium.launch(

            headless=True,

            args=[

                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"

            ]

        )

        try:

            results = await asyncio.gather(

                *[

                    scrape_market_timeframe(
                        browser,
                        market,
                        timeframe,
                        target_url
                    )

                    for timeframe in timeframes

                ],

                return_exceptions=True

            )

            return dict(
                zip(timeframes, results)
            )

        finally:

            await browser.close()

            print(
                f"[{market}] Browser closed."
            )


# ==================================================
# BUILD TELEGRAM MESSAGE
# ==================================================

def build_telegram_message(data, timeframe, market):

    orders = data["whale_orders"]

    lines = [
        f"<b>⚡ COINGLASS BTC | {market} | {timeframe}</b>",
        "",
        f"🕒 {data['timestamp']}",
        f"💰 Price: ${data['price']:,.2f}",
        f"🔺 High: ${data['candle']['high']:,.2f}",
        f"🔻 Low: ${data['candle']['low']:,.2f}",
        "",
        "<b>🐋 LIQUIDATION WALLS</b>",
        ""
    ]

    volumes = sorted(
        [order["volume"] for order in orders],
        reverse=True
    )

    highest = volumes[0] if volumes else 0
    second = volumes[1] if len(volumes) > 1 else 0

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


# ==================================================
# SEND TELEGRAM MESSAGE
# ==================================================

def send_telegram_message(message, market, timeframe):

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
            f"[{market} {timeframe}] Telegram error: {result}"
        )

    print(
        f"[{market} {timeframe}] "
        "Telegram message sent successfully!"
    )


# ==================================================
# CONSOLIDATED LIQUIDATION MESSAGE
# ==================================================


# ==================================================
# CONSOLIDATED LIQUIDATION MESSAGE
# COMPACT TWO-COLUMN MOBILE FORMAT
# ==================================================

def build_consolidated_message(all_results):

    timeframe_order = ["12H", "24H", "48H", "3D"]
    market_order = ["PAIR", "SYMBOL"]

    def build_panel(result, timeframe):

        if result is None or isinstance(result, BaseException):
            return {
                "header": timeframe,
                "price": "N/A",
                "rows": []
            }

        current_price = result["price"]

        orders = sorted(
            result.get("whale_orders", []),
            key=lambda x: x["volume"],
            reverse=True
        )[:5]

        rows = []

        for index, order in enumerate(orders):

            emoji = (
                "🔴" if order["side"] == "SHORT LIQ"
                else "🟢"
            )

            star = "⭐" if index == 0 else ""

            rows.append(
                f"{emoji}{order['price']:,.0f}|"
                f"{format_volume(order['volume']).replace('$', '')}{star}"
            )

        return {
            "header": timeframe,
            "price": f"${current_price:,.0f}",
            "rows": rows
        }

    messages = []

    for market in market_order:

        market_data = all_results.get(market, {})

        panels = {
            tf: build_panel(
                market_data.get(tf),
                tf
            )
            for tf in timeframe_order
        }

        lines = [
            f"🐋 BTC LIQ {market}",
            ""
        ]

        # First pair: 12H and 24H
        # Second pair: 48H and 3D

        for first, second in [
            ("12H", "24H"),
            ("48H", "3D")
        ]:

            left = panels[first]
            right = panels[second]

            lines.append(
                f"{left['header']:<16}{right['header']}"
            )

            lines.append(
                f"{left['price']:<16}{right['price']}"
            )

            for i in range(5):

                left_row = (
                    left["rows"][i]
                    if i < len(left["rows"])
                    else ""
                )

                right_row = (
                    right["rows"][i]
                    if i < len(right["rows"])
                    else ""
                )

                lines.append(
                    f"{left_row:<16}{right_row}"
                )

            lines.append("")

        message = (
            f"<b>{lines[0]}</b>\n"
            "<pre>" +
            "\n".join(lines[1:]) +
            "</pre>"
        )

        messages.append(message)

    return messages


# ==================================================
# SEND CONSOLIDATED TELEGRAM MESSAGES
# ==================================================

def send_consolidated_telegram_message(messages):

    if not messages:
        print("[CONSOLIDATED] No valid data available.")
        return

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    for index, message in enumerate(messages, 1):

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
                f"Consolidated Telegram error: {result}"
            )

        print(
            f"[CONSOLIDATED] Message {index}/{len(messages)} sent successfully!"
        )



# ==================================================
# MAIN - EIGHT SCRAPERS + RETRY + CONSOLIDATED MESSAGE
# ==================================================

async def main():

    print("=" * 65)
    print("STARTING EIGHT PARALLEL COINGLASS SCRAPERS")
    print("=" * 65)

    markets = {
        "PAIR": PAIR_URL,
        "SYMBOL": SYMBOL_URL
    }

    all_results = {"PAIR": {}, "SYMBOL": {}}

    # --------------------------------------------------
    # STEP 1: RUN INITIAL PARALLEL SCRAPERS
    # --------------------------------------------------

    market_results = await asyncio.gather(
        *[
            scrape_market(market, url, TIMEFRAMES)
            for market, url in markets.items()
        ],
        return_exceptions=True
    )

    for market, result in zip(markets.keys(), market_results):

        if isinstance(result, BaseException):

            print(f"[{market}] WORKER FAILED: {result}")

            for tf in TIMEFRAMES:
                all_results[market][tf] = result

        else:

            all_results[market] = result

    # --------------------------------------------------
    # STEP 2: IDENTIFY FAILURES & RETRY THEM ONE-BY-ONE
    # --------------------------------------------------

    failed_jobs = []

    for market in markets:
        for timeframe in TIMEFRAMES:
            res = all_results[market].get(timeframe)
            if res is None or isinstance(res, BaseException):
                failed_jobs.append((market, timeframe))

    if failed_jobs:
        print("\n" + "=" * 65)
        print(f"RETRYING {len(failed_jobs)} FAILED TIMEFRAME(S)...")
        print("=" * 65)

        for market, timeframe in failed_jobs:
            print(f"\n[RETRY] Starting background scrape for {market} {timeframe}...")
            try:
                # Scrape single timeframe using its target url
                retry_res = await scrape_market(market, markets[market], [timeframe])
                if timeframe in retry_res and not isinstance(retry_res[timeframe], BaseException):
                    all_results[market][timeframe] = retry_res[timeframe]
                    print(f"[RETRY SUCCESS] {market} {timeframe} recovered successfully!")
                else:
                    print(f"[RETRY FAILED] {market} {timeframe} failed again.")
            except Exception as e:
                print(f"[RETRY ERROR] {market} {timeframe}: {e}")

    successful = 0
    errors = []

    # --------------------------------------------------
    # STEP 3: SEND TELEGRAM MESSAGES FOR ALL SUCCESSFUL RESULTS
    # --------------------------------------------------

    print("\n" + "=" * 65)
    print("STARTING TELEGRAM DELIVERY")
    print("=" * 65)

    for market in markets:

        market_data = all_results.get(market, {})

        for timeframe in TIMEFRAMES:

            result = market_data.get(timeframe)

            if result is None or isinstance(result, BaseException):

                errors.append(
                    f"{market} {timeframe}: Failed after retry"
                )

                continue

            try:

                message = build_telegram_message(
                    result,
                    timeframe,
                    market
                )

                print(
                    f"[{market} {timeframe}] "
                    "Sending Telegram message..."
                )

                await asyncio.to_thread(
                    send_telegram_message,
                    message,
                    market,
                    timeframe
                )

                successful += 1

            except Exception as error:

                print(
                    f"[{market} {timeframe}] ERROR: {error}"
                )

                errors.append(
                    f"{market} {timeframe}: {error}"
                )

    # --------------------------------------------------
    # STEP 4: BUILD & SEND CONSOLIDATED MESSAGE
    # --------------------------------------------------

    print("\n" + "=" * 65)
    print("BUILDING CONSOLIDATED LIQUIDATION REPORT")
    print("=" * 65)

    try:

        consolidated_message = build_consolidated_message(
            all_results
        )

        if consolidated_message:

            print(
                "[CONSOLIDATED] Sending ninth Telegram message..."
            )

            await asyncio.to_thread(
                send_consolidated_telegram_message,
                consolidated_message
            )

            successful += 1

        else:

            print(
                "[CONSOLIDATED] No valid liquidation data available."
            )

            errors.append(
                "CONSOLIDATED: No valid liquidation data"
            )

    except Exception as error:

        print(
            f"[CONSOLIDATED] ERROR: {error}"
        )

        errors.append(
            f"CONSOLIDATED: {error}"
        )

    # --------------------------------------------------
    # STEP 5: FINAL DELIVERY SUMMARY
    # --------------------------------------------------

    print("\n" + "=" * 65)

    print(
        f"SUCCESSFUL TELEGRAM MESSAGES: {successful}/9"
    )

    if errors:

        print("\nERRORS:")

        for error in errors:

            print(error)

        raise RuntimeError(
            "One or more scraping or Telegram jobs failed even after retry."
        )

    print(
        "ALL SCRAPERS, RETRIES, AND CONSOLIDATED MESSAGE COMPLETED!"
    )

    print("=" * 65)


# ==================================================
# ENTRY POINT
# ==================================================

if __name__ == "__main__":

    asyncio.run(main())
