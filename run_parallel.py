import asyncio
import json
import os
import hashlib
from playwright.async_api import async_playwright
from scraper import JS_EXTRACT_SCRIPT

PAIR_URL = (
    "https://www.coinglass.com/pro/futures/"
    "LiquidationHeatMap?coin=BTC&type=pair"
)

SYMBOL_URL = (
    "https://www.coinglass.com/pro/futures/"
    "LiquidationHeatMap?coin=BTC&type=symbol"
)

STORAGE_STATE = "coinglass_state.json"

VIEWPORT = {"width": 1400, "height": 900}

TIMEFRAMES = ["48H", "12H", "24H", "3D"]

CHART_SELECTOR = ".echarts-for-react"

MAX_EXTRACTION_ATTEMPTS = 15
CHART_POLL_INTERVAL = 1.5
TIMEFRAME_TIMEOUT = 45


def is_valid_chart_data(data):
    if not isinstance(data, dict) or "error" in data:
        return False

    x_axis = data.get("xAxis", [])
    y_axis = data.get("yAxis", [])
    series = data.get("series", [])

    if not x_axis or not y_axis or not series:
        return False

    candle = next(
        (s for s in series if s.get("type") == "candlestick"),
        None
    )

    heatmap = next(
        (s for s in series if s.get("type") == "heatmap"),
        None
    )

    if not candle or not heatmap:
        return False

    if len(candle.get("data", [])) < len(x_axis):
        return False

    if not heatmap.get("data"):
        return False

    return True


def chart_fingerprint(data):
    """Identifies the currently loaded timeframe's chart dataset."""

    if not is_valid_chart_data(data):
        return None

    candle = next(
        s for s in data["series"]
        if s.get("type") == "candlestick"
    )

    heatmap = next(
        s for s in data["series"]
        if s.get("type") == "heatmap"
    )

    signature = {
        "x_first": data["xAxis"][0],
        "x_last": data["xAxis"][-1],
        "x_length": len(data["xAxis"]),
        "y_length": len(data["yAxis"]),
        "last_candle": candle["data"][-1],
        "heatmap_length": len(heatmap["data"]),
    }

    return hashlib.md5(
        json.dumps(signature, sort_keys=True, default=str).encode()
    ).hexdigest()


async def select_timeframe(page, timeframe):
    """Selects a timeframe from the CoinGlass dropdown."""

    print(f"Selecting timeframe: {timeframe}")

    dropdown = page.locator(
        "button.MuiSelect-button"
    ).first

    await dropdown.wait_for(
        state="visible",
        timeout=10000
    )

    await dropdown.click()

    option_text = {
        "48H": "48 hour",
        "12H": "12 hour",
        "24H": "24 hour",
        "3D": "3 day",
    }[timeframe]

    option = page.locator(
        '[role="option"]'
    ).filter(has_text=option_text).first

    await option.wait_for(
        state="visible",
        timeout=10000
    )

    await option.click()

    print(f"Selected: {timeframe}")


async def extract_updated_chart(page, previous_fingerprint):
    """
    Waits until the chart has genuinely changed and remains stable.
    Prevents saving the previous timeframe's data.
    """

    last_fingerprint = None
    stable_count = 0

    for attempt in range(MAX_EXTRACTION_ATTEMPTS * 2):

        try:
            data = await page.evaluate(JS_EXTRACT_SCRIPT)

            if is_valid_chart_data(data):

                fingerprint = chart_fingerprint(data)

                if (
                    fingerprint != previous_fingerprint
                    and fingerprint == last_fingerprint
                ):
                    stable_count += 1
                elif fingerprint != previous_fingerprint:
                    stable_count = 0

                last_fingerprint = fingerprint

                if stable_count >= 1:
                    return data

        except Exception as e:
            print(f"Extraction attempt error: {e}")

        await asyncio.sleep(CHART_POLL_INTERVAL)

    return None




async def scrape_market_timeframes(browser, market, url, timeframes):
    """Uses one browser context and one page for all timeframes."""

    results = {}
    failed = []
    context = None

    try:
        context_args = {
            "viewport": VIEWPORT,
        }

        if os.path.exists(STORAGE_STATE):
            context_args["storage_state"] = STORAGE_STATE

        context = await browser.new_context(**context_args)
        page = await context.new_page()

        print(f"\nOpening {market} market...")

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        # Preserve the original visible-chart requirement.
        await page.locator(CHART_SELECTOR).first.wait_for(
            state="visible",
            timeout=60000
        )

        # Wait for valid initial chart data.
        baseline = None

        for attempt in range(40):
            try:
                candidate = await page.evaluate(JS_EXTRACT_SCRIPT)

                if is_valid_chart_data(candidate):
                    baseline = candidate
                    break

            except Exception as e:
                print(f"{market}: Initial extraction attempt {attempt + 1}: {e}")

            await asyncio.sleep(1)

        if baseline is None:
            print(f"{market}: Initial chart data unavailable.")
            return results, list(timeframes)

        previous_fingerprint = chart_fingerprint(baseline)
        current_timeframe = "48H"

        for timeframe in timeframes:

            print(f"\n{market} - Processing {timeframe}")

            try:
                if timeframe == current_timeframe:
                    data = baseline

                else:
                    await select_timeframe(page, timeframe)

                    data = await extract_updated_chart(
                        page,
                        previous_fingerprint
                    )

                if data is None:
                    print(f"{market} {timeframe}: Extraction failed.")
                    failed.append(timeframe)
                    continue

                fingerprint = chart_fingerprint(data)

                if not fingerprint:
                    print(f"{market} {timeframe}: Invalid chart data.")
                    failed.append(timeframe)
                    continue

                previous_fingerprint = fingerprint
                current_timeframe = timeframe
                baseline = data

                results[timeframe] = data

                filename = f"{market.lower()}_{timeframe.lower()}.json"

                with open(filename, "w") as f:
                    json.dump(data, f)

                print(
                    f"{market} {timeframe}: SUCCESS "
                    f"({len(data['xAxis'])} candles)"
                )

            except Exception as e:
                print(f"{market} {timeframe}: {e}")
                failed.append(timeframe)

    except Exception as e:
        print(f"{market}: Market loading failed: {e}")

        for timeframe in timeframes:
            if timeframe not in results and timeframe not in failed:
                failed.append(timeframe)

    finally:
        if context:
            await context.close()

    return results, failed


async def run_all_markets():
    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        markets = {
            "PAIR": PAIR_URL,
            "SYMBOL": SYMBOL_URL,
        }

        all_results = {}
        all_failures = {}

        try:
            # Initial parallel scraping.
            tasks = [
                scrape_market_timeframes(
                    browser,
                    market,
                    url,
                    TIMEFRAMES
                )
                for market, url in markets.items()
            ]

            outputs = await asyncio.gather(
                *tasks,
                return_exceptions=True
            )

            for market, output in zip(markets.keys(), outputs):

                if isinstance(output, Exception):
                    print(f"{market} failed: {output}")

                    all_results[market] = {}
                    all_failures[market] = TIMEFRAMES.copy()
                    continue

                results, failed = output

                all_results[market] = results
                all_failures[market] = failed

            # Retry failed timeframes once.
            for market in markets:

                failed = all_failures.get(market, [])

                if not failed:
                    continue

                print(f"\nRetrying {market}: {failed}")

                try:
                    retry_results, still_failed = (
                        await scrape_market_timeframes(
                            browser,
                            market,
                            markets[market],
                            failed
                        )
                    )

                    all_results[market].update(retry_results)
                    all_failures[market] = still_failed

                except Exception as e:
                    print(f"{market} retry failed: {e}")

            return all_results, all_failures

        finally:
            await browser.close()


async def main():
    results, failures = await run_all_markets()

    print("\n========== SCRAPING SUMMARY ==========")

    for market in ["PAIR", "SYMBOL"]:

        print(f"\n{market}")

        for timeframe in TIMEFRAMES:

            if timeframe in results.get(market, {}):
                print(f"  {timeframe}: SUCCESS")
            else:
                print(f"  {timeframe}: FAILED")

    # Results are available for your existing processing
    # and Telegram functions.
    return results


if __name__ == "__main__":
    asyncio.run(main())
