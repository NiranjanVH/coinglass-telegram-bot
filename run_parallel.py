import asyncio
import json
import os
import time
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

TIMEFRAMES = ["48H", "12H", "24H", "3D", "1W", "1M"]
SCRAPE_CONCURRENCY = 2

CHART_SELECTOR = ".echarts-for-react"

MAX_EXTRACTION_ATTEMPTS = 60
CHART_WAIT_TIMEOUT = 30000
RETRY_DELAY = 1


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
# DIAGNOSTIC DUMP
# ==================================================

async def save_debug_artifacts(page, market, timeframe, prefix="debug"):

    base = f"{prefix}_{market.lower()}_{timeframe.lower()}"
    try:
        print(f"[{market} {timeframe}] Debug URL: {page.url}")
        await page.screenshot(path=f"{base}.png", full_page=True)
    except Exception as error:
        print(f"[{market} {timeframe}] Screenshot failed: {error}")

    try:
        html = await page.content()
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as error:
        print(f"[{market} {timeframe}] HTML dump failed: {error}")


# ==================================================
# LEGACY VERSION LINK ACTION
# ==================================================

async def click_legacy_version_link(page, market, timeframe):

    print(
        f"[{market} {timeframe}] "
        "Looking for legacy version link (a.TopBar_old__BITL7)..."
    )

    if "/legacy/" in page.url:
        print(
            f"[{market} {timeframe}] "
            f"Already on legacy URL ({page.url})."
        )
        return

    link = page.locator(
        "a.TopBar_old__BITL7, a[href*='/legacy/liquidation-heatmap']"
    ).first

    clicked = False

    try:
        await link.wait_for(state="visible", timeout=15000)
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                await link.click()
        except Exception:
            await link.click()
        clicked = True
    except Exception:
        clicked = await page.evaluate(
            """
            () => {
                const el = document.querySelector('a.TopBar_old__BITL7')
                    || document.querySelector('a[href*="/legacy/liquidation-heatmap"]');
                if (!el) return false;
                el.click();
                return true;
            }
            """
        )

    if clicked:
        print(
            f"[{market} {timeframe}] "
            "Clicked legacy version link; waiting for legacy view..."
        )
        try:
            await page.wait_for_url("**/legacy/**", timeout=15000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(3)
        print(
            f"[{market} {timeframe}] "
            f"Post-click URL: {page.url}"
        )
    else:
        print(
            f"[{market} {timeframe}] "
            f"Legacy version link not found on {page.url}; continuing."
        )


# ==================================================
# TIMEFRAME SELECTORS
# ==================================================

async def select_timeframe(page, timeframe):

    print(f"[{timeframe}] Selecting timeframe...")

    dropdown = page.locator(
        "button.MuiSelect-button:has-text('hour'), "
        "button.MuiSelect-button:has-text('day'), "
        "button.MuiSelect-button:has-text('week'), "
        "button.MuiSelect-button:has-text('month'), "
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
        ],

        "1W": [
            "li[role='option']:has-text('1 week')",
            "div[role='option']:has-text('1 week')",
            "[role='option']:has-text('1w')"
        ],

        "1M": [
            "li[role='option']:has-text('1 month')",
            "div[role='option']:has-text('1 month')",
            "[role='option']:has-text('1m')"
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

    populated_x_by_y = {}
    for point in heatmap_series.get("data", []):
        if not isinstance(point, (list, tuple)) or len(point) < 3:
            continue

        try:
            x_index = int(point[0])
            y_index = int(point[1])
            volume = float(point[2])
        except (TypeError, ValueError):
            continue

        if (
            0 <= x_index < len(x_axis)
            and 0 <= y_index < len(y_axis)
            and volume > 0
        ):
            populated_x_by_y.setdefault(y_index, set()).add(x_index)

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

        populated_columns = populated_x_by_y.get(y_index, set())
        first_x_index = last_idx
        while first_x_index - 1 in populated_columns:
            first_x_index -= 1

        consecutive_columns = last_idx - first_x_index + 1
        first_touch_x_index = None

        for history_x_index in range(first_x_index, last_idx + 1):
            if history_x_index >= len(candle_data):
                continue

            historical_candle = candle_data[history_x_index]
            if (
                not isinstance(historical_candle, (list, tuple))
                or len(historical_candle) < 4
            ):
                continue

            try:
                candle_low = float(historical_candle[2])
                candle_high = float(historical_candle[3])
            except (TypeError, ValueError):
                continue

            if candle_low <= price_level <= candle_high:
                first_touch_x_index = history_x_index
                break

        if first_touch_x_index is not None:
            elapsed_columns = first_touch_x_index - first_x_index + 1
            remaining_columns = max(0, consecutive_columns - elapsed_columns)
        else:
            remaining_columns = consecutive_columns

        orders.append({

            "price": price_level,

            "volume": volume,

            "bar_columns": remaining_columns,

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

    orders.sort(
        key=lambda item: item["price"],
        reverse=True
    )

    return {

        "timestamp": x_axis[last_idx],

        "price": current_price,

        "candle": candle,

        "whale_orders": orders

    }


# ==================================================
# SCRAPE ONE MARKET / TIMEFRAME
# ==================================================

async def scrape_market_timeframe(
    browser,
    market,
    timeframe,
    target_url,
    semaphore
):
    async with semaphore:
        return await _scrape_market_timeframe(
            browser,
            market,
            timeframe,
            target_url
        )


async def _scrape_market_timeframe(
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

            await save_debug_artifacts(page, market, timeframe, prefix="debug")

            raise RuntimeError(
                f"CoinGlass returned HTTP {status}"
            )

        print(
            f"[{market} {timeframe}] "
            "Switching to legacy version before waiting for chart..."
        )

        await click_legacy_version_link(
            page,
            market,
            timeframe
        )

        chart_ready = await wait_for_chart(
            page,
            market,
            timeframe,
            timeout=60000
        )

        if not chart_ready:

            await save_debug_artifacts(page, market, timeframe, prefix="debug")

            raise RuntimeError(
                f"[{market} {timeframe}] "
                "Chart did not appear after 60 seconds."
            )

        await select_timeframe(
            page,
            timeframe
        )

        chart_ready = await wait_for_chart(
            page,
            market,
            timeframe,
            timeout=CHART_WAIT_TIMEOUT
        )

        if not chart_ready:
            await save_debug_artifacts(page, market, timeframe, prefix="debug_failed")

            raise RuntimeError(
                f"[{market} {timeframe}] "
                "Chart did not reappear after selecting timeframe."
            )

        print(
            f"[{market} {timeframe}] "
            "Waiting for chart data..."
        )

        raw_data = None
        failure_reason = "Chart data not ready after all retries."

        for attempt in range(
            1,
            MAX_EXTRACTION_ATTEMPTS + 1
        ):

            print(
                f"[{market} {timeframe}] "
                f"Extraction attempt "
                f"{attempt}/{MAX_EXTRACTION_ATTEMPTS}"
            )

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

            if (
                isinstance(candidate, dict)
                and candidate.get("error") == "Chart element not found in DOM."
            ):
                chart_ready = await wait_for_chart(
                    page,
                    market,
                    timeframe,
                    timeout=5000
                )

                if not chart_ready:
                    failure_reason = "Chart element did not return during extraction."
                    break

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

            await save_debug_artifacts(page, market, timeframe, prefix="debug_failed")

            raise RuntimeError(
                f"[{market} {timeframe}] {failure_reason}"
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
# SCRAPE ONE MARKET - PARALLEL TIMEFRAMES
# ==================================================

async def scrape_market(
    market,
    target_url,
    timeframes,
    semaphore
):

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
                        target_url,
                        semaphore
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

    all_orders = data["whale_orders"]
    nearest_orders = sorted(
        all_orders,
        key=lambda order: abs(order["price"] - data["price"])
    )[:5]

    highest_volume_orders = sorted(
        all_orders,
        key=lambda order: order["volume"],
        reverse=True
    )

    if highest_volume_orders and not any(
        highest_volume_orders[0] is order
        for order in nearest_orders
    ):
        nearest_order_ids = {id(order) for order in nearest_orders}
        nearest_orders.extend(
            order
            for order in highest_volume_orders
            if id(order) not in nearest_order_ids
        )
        nearest_orders = nearest_orders[:7]

    orders = sorted(
        nearest_orders,
        key=lambda order: order["price"],
        reverse=True
    )

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

    ranked_orders = sorted(
        orders,
        key=lambda order: order["volume"],
        reverse=True
    )
    rank_by_id = {
        id(order): rank
        for rank, order in enumerate(ranked_orders[:2], start=1)
    }

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

        rank = {
            1: "⭐",
            2: "🥈",
        }.get(rank_by_id.get(id(order)), "")

        lines.append(
            f"{emoji}{order['price']:,.0f}|"
            f"{order['bar_columns']}c|"
            f"{format_volume(order['volume']).replace('$', '')}"
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

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }

    for attempt in range(2):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=(10, 60)
            )
            break
        except requests.exceptions.ConnectTimeout:
            if attempt == 1:
                raise
            print(
                f"[{market} {timeframe}] Telegram connection timed out; "
                "retrying once."
            )
            time.sleep(2)
        except requests.exceptions.ReadTimeout as error:
            raise RuntimeError(
                f"[{market} {timeframe}] Telegram response timed out; "
                "delivery status is unknown, so it was not retried to avoid "
                "sending a duplicate."
            ) from error

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
# COMPACT TWO-COLUMN MOBILE FORMAT
# ==================================================

def build_ratio_bar_line(market_data, timeframe_order, total_blocks=16):

    long_vol = 0.0
    short_vol = 0.0

    for tf in timeframe_order:
        res = market_data.get(tf)
        if res is None or isinstance(res, BaseException):
            continue
        for order in res.get("whale_orders", []):
            vol = float(order.get("volume", 0.0))
            if vol <= 0:
                continue
            if order.get("side") == "LONG LIQ":
                long_vol += vol
            else:
                short_vol += vol

    total_vol = long_vol + short_vol
    if total_vol <= 0:
        return ""

    long_pct = (long_vol / total_vol) * 100.0
    short_pct = 100.0 - long_pct
    long_blocks = max(1, min(total_blocks - 1, round((long_vol / total_vol) * total_blocks)))
    short_blocks = total_blocks - long_blocks

    bar = ("🟩" * long_blocks) + ("🟥" * short_blocks)
    return f"L {long_pct:.0f}% {bar} {short_pct:.0f}% S"


def build_consolidated_message(all_results):

    timeframe_order = ["12H", "24H", "48H", "3D", "1W", "1M"]
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
                f"{order['bar_columns']}c|"
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

        for first, second in [
            ("12H", "24H"),
            ("48H", "3D"),
            ("1W", "1M")
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

        ratio_line = build_ratio_bar_line(market_data, timeframe_order)
        if ratio_line:
            lines.append(ratio_line)

        message = (
            f"<b>{lines[0]}</b>\n"
            "<pre>" +
            "\n".join(lines[1:]) +
            "</pre>"
        )

        messages.append(message)

    return messages


# ==================================================
# MAIN - PARALLEL SCRAPERS + RETRY + CONSOLIDATED MESSAGE
# ==================================================

async def main():

    print("=" * 65)
    print("STARTING PARALLEL COINGLASS SCRAPERS")
    print("=" * 65)

    markets = {
        "PAIR": PAIR_URL,
        "SYMBOL": SYMBOL_URL
    }

    all_results = {"PAIR": {}, "SYMBOL": {}}
    scrape_semaphore = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    # --------------------------------------------------
    # STEP 1: RUN INITIAL PARALLEL SCRAPERS
    # --------------------------------------------------

    market_results = await asyncio.gather(
        *[
            scrape_market(
                market,
                url,
                TIMEFRAMES,
                scrape_semaphore
            )
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
    # STEP 2: RETRY FAILED TIMEFRAMES IN PARALLEL BY MARKET
    # --------------------------------------------------

    failed_jobs = []

    for market in markets:
        for timeframe in TIMEFRAMES:
            res = all_results[market].get(timeframe)
            if res is None or isinstance(res, BaseException):
                failed_jobs.append((market, timeframe))

    if failed_jobs:
        print("\n" + "=" * 65)
        print(f"RETRYING {len(failed_jobs)} FAILED TIMEFRAME(S) IN BATCHES...")
        print("=" * 65)

        failed_timeframes_by_market = {}
        for market, timeframe in failed_jobs:
            failed_timeframes_by_market.setdefault(market, []).append(timeframe)

        retry_markets = list(failed_timeframes_by_market)
        retry_results = await asyncio.gather(
            *[
                scrape_market(
                    market,
                    markets[market],
                    failed_timeframes_by_market[market],
                    scrape_semaphore
                )
                for market in retry_markets
            ],
            return_exceptions=True
        )

        for market, retry_result in zip(retry_markets, retry_results):
            if isinstance(retry_result, BaseException):
                print(f"[RETRY ERROR] {market}: {retry_result}")
                continue

            for timeframe, result in retry_result.items():
                if isinstance(result, BaseException):
                    print(f"[RETRY FAILED] {market} {timeframe} failed again: {result}")
                else:
                    all_results[market][timeframe] = result
                    print(f"[RETRY SUCCESS] {market} {timeframe} recovered successfully!")

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

            for index, message in enumerate(consolidated_message, 1):
                print(
                    f"[CONSOLIDATED] Sending message "
                    f"{index}/{len(consolidated_message)}..."
                )

                await asyncio.to_thread(
                    send_telegram_message,
                    message,
                    "CONSOLIDATED",
                    f"{index}/{len(consolidated_message)}"
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

    expected_messages = len(markets) * len(TIMEFRAMES) + 2
    print(
        f"SUCCESSFUL TELEGRAM MESSAGES: {successful}/{expected_messages}"
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
