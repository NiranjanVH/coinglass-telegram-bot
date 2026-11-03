import asyncio
import json
import os
from playwright.async_api import async_playwright
from rich.console import Console
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

console = Console()
JSON_FILENAME = "coinglass_heatmap_datap1.json"
USER_DATA_DIR = os.path.join(os.getcwd(), "playwright_user_data")
TARGET_URL = "https://www.coinglass.com/pro/futures/LiquidationHeatMap?coin=BTC&type=pair"

JS_EXTRACT_SCRIPT = """() => {
    const chartDom = document.querySelector('.echarts-for-react');
    if (!chartDom) return { error: "Chart element not found in DOM." };

    const instanceId = chartDom.getAttribute('_echarts_instance_');
    let chartInstance = null;

    const echartsObj = window.echarts || window._globalEcharts;
    if (echartsObj) {
        if (echartsObj.getInstanceByDom) chartInstance = echartsObj.getInstanceByDom(chartDom);
        if (!chartInstance && instanceId && echartsObj.getInstanceById) chartInstance = echartsObj.getInstanceById(instanceId);
    }

    if (!chartInstance && instanceId) {
        for (let key in window) {
            try {
                if (window[key] && typeof window[key].getInstanceById === 'function') {
                    const inst = window[key].getInstanceById(instanceId);
                    if (inst) { chartInstance = inst; break; }
                }
            } catch(e) {}
        }
    }

    if (!chartInstance) {
        const reactKey = Object.keys(chartDom).find(
            k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')
        );
        if (reactKey && chartDom[reactKey]) {
            let fiber = chartDom[reactKey];
            while (fiber) {
                if (fiber.stateNode) {
                    if (typeof fiber.stateNode.getEchartsInstance === 'function') {
                        chartInstance = fiber.stateNode.getEchartsInstance();
                        break;
                    }
                    if (fiber.stateNode.echartsInstance) {
                        chartInstance = fiber.stateNode.echartsInstance;
                        break;
                    }
                }
                if (fiber.memoizedProps && fiber.memoizedProps.option) {
                    return {
                        xAxis: fiber.memoizedProps.option.xAxis ? fiber.memoizedProps.option.xAxis[0].data : [],
                        yAxis: fiber.memoizedProps.option.yAxis ? fiber.memoizedProps.option.yAxis[0].data : [],
                        series: fiber.memoizedProps.option.series ? fiber.memoizedProps.option.series.map(s => ({ type: s.type, name: s.name, data: s.data })) : []
                    };
                }
                fiber = fiber.return;
            }
        }
    }

    if (!chartInstance) return { error: "Instance unbound" };

    const options = chartInstance.getOption();
    return {
        xAxis: options.xAxis ? options.xAxis[0].data : [],
        yAxis: options.yAxis ? options.yAxis[0].data : [],
        series: options.series ? options.series.map(s => ({
            type: s.type,
            name: s.name || s.type,
            data: s.data
        })) : []
    };
}"""


def format_volume(num):
    if num >= 1e9:
        return f"${num / 1e9:,.2f}B"
    elif num >= 1e6:
        return f"${num / 1e6:,.2f}M"
    elif num >= 1e3:
        return f"${num / 1e3:,.2f}K"
    return f"${num:,.2f}"


def process_latest_column_from_json():
    if not os.path.exists(JSON_FILENAME):
        return None

    try:
        with open(JSON_FILENAME, "r") as f:
            raw_data = json.load(f)
    except Exception:
        return None

    if not raw_data or "error" in raw_data:
        return None

    x_axis = raw_data.get("xAxis", [])
    y_axis = raw_data.get("yAxis", [])
    series_list = raw_data.get("series", [])

    if not x_axis or not series_list:
        return None

    last_x_idx = len(x_axis) - 1
    latest_timestamp = x_axis[last_x_idx]

    candlestick_series = next(
        (s for s in series_list if isinstance(s, dict) and s.get("type") == "candlestick"),
        None,
    )
    heatmap_series = next(
        (s for s in series_list if isinstance(s, dict) and s.get("type") == "heatmap"),
        None,
    )

    latest_candle = {"open": 0, "close": 0, "low": 0, "high": 0}
    if candlestick_series and "data" in candlestick_series:
        c_data = candlestick_series["data"]
        if len(c_data) > last_x_idx:
            raw_c = c_data[last_x_idx]
            if isinstance(raw_c, (list, tuple)) and len(raw_c) >= 4:
                latest_candle = {
                    "open": float(raw_c[0]),
                    "close": float(raw_c[1]),
                    "low": float(raw_c[2]),
                    "high": float(raw_c[3]),
                }

    current_price = latest_candle["close"]
    whale_orders = []
    if heatmap_series and "data" in heatmap_series:
        for point in heatmap_series["data"]:
            if isinstance(point, (list, tuple)) and len(point) >= 3:
                x_idx, y_idx, volume = point[0], point[1], point[2]
                if x_idx == last_x_idx:
                    price_level = float(
                        y_axis[y_idx] if y_idx < len(y_axis) else y_idx
                    )
                    diff = price_level - current_price
                    diff_pct = (
                        (diff / current_price) * 100 if current_price else 0
                    )

                    whale_orders.append(
                        {
                            "price": price_level,
                            "volume": float(volume),
                            "side": (
                                "SHORT LIQ"
                                if price_level > current_price
                                else "LONG LIQ"
                            ),
                            "diff": diff,
                            "diff_pct": diff_pct,
                        }
                    )

    top_orders = sorted(
        whale_orders, key=lambda item: item["volume"], reverse=True
    )[:15]

    highest_vol = top_orders[0]["volume"] if len(top_orders) > 0 else 0
    second_highest_vol = top_orders[1]["volume"] if len(top_orders) > 1 else 0

    for order in top_orders:
        if order["volume"] == highest_vol and highest_vol > 0:
            order["rank"] = 1
        elif order["volume"] == second_highest_vol and second_highest_vol > 0:
            order["rank"] = 2
        else:
            order["rank"] = 0

    whale_orders_by_price = sorted(
        top_orders, key=lambda item: item["price"], reverse=True
    )

    return {
        "timestamp": latest_timestamp,
        "price": current_price,
        "candle": latest_candle,
        "whale_orders": whale_orders_by_price,
        "max_vol": highest_vol if highest_vol > 0 else 1.0,
    }


def render_dashboard(data):
    if not data or not data.get("whale_orders"):
        return Group(
            Panel(
                "[bold yellow]Syncing with JSON dataset...[/]", 
                title="Status",
                expand=False
            )
        )

    curr_price = data["price"]
    summary_text = (
        f"**Timestamp:** {data['timestamp']}  |  "
        f"**Current Price:** [bold underline cyan]${curr_price:,.2f}[/]  |  "
        f"**High:** ${data['candle']['high']:,.2f}  |  "
        f"**Low:** ${data['candle']['low']:,.2f}"
    )

    table = Table(
        title="🐋 Liquidation Walls Above & Below Current Price", expand=False
    )
    table.add_column("Price Level ($)", justify="right")
    table.add_column("Distance / Delta", justify="right")
    table.add_column("Type", justify="center")
    table.add_column("Volume ($ USD)", justify="right")
    table.add_column("Intensity Bar & Rank", justify="left")

    max_vol = data["max_vol"]
    separator_inserted = False

    for order in data["whale_orders"]:
        if not separator_inserted and order["price"] < curr_price:
            table.add_row(
                f"[bold underline cyan]${curr_price:,.2f}[/]",
                "[dim cyan]$0.00 (0.00%)[/]",
                "[bold underline cyan]► ACTIVE ◄[/]",
                "───",
                "────────────────",
                style="dim cyan",
            )
            separator_inserted = True

        bar_len = max(1, int((order["volume"] / max_vol) * 15))
        is_short = order["side"] == "SHORT LIQ"
        diff_prefix = "+" if order["diff"] > 0 else ""

        if order["rank"] == 1:
            base_color = "bright_red" if is_short else "bright_green"
            price_str = f"[bold {base_color}]${order['price']:,.2f}[/]"
            diff_str = f"[bold {base_color}]{diff_prefix}${order['diff']:,.2f} ({diff_prefix}{order['diff_pct']:.2f}%)[/]"
            side_str = f"[bold {base_color}]{order['side']}[/]"
            vol_str = f"[bold {base_color}]{format_volume(order['volume'])}[/]"
            bar_str = f"[bold {base_color}]{'█' * bar_len}[/] [bold {base_color}]⭐ 1ST HIGHEST[/]"

        elif order["rank"] == 2:
            base_color = "red" if is_short else "green"
            price_str = f"[{base_color}]${order['price']:,.2f}[/]"
            diff_str = f"[{base_color}]{diff_prefix}${order['diff']:,.2f} ({diff_prefix}{order['diff_pct']:.2f}%)[/]"
            side_str = f"[{base_color}]{order['side']}[/]"
            vol_str = f"[{base_color}]{format_volume(order['volume'])}[/]"
            bar_str = f"[{base_color}]{'█' * bar_len}[/] [{base_color}]🥈 2ND HIGHEST[/]"

        else:
            base_color = "dark_red" if is_short else "dark_green"
            price_str = f"[white]${order['price']:,.2f}[/]"
            diff_str = f"[{base_color}]{diff_prefix}${order['diff']:,.2f} ({diff_prefix}{order['diff_pct']:.2f}%)[/]"
            side_str = f"[{base_color}]{order['side']}[/]"
            vol_str = f"[yellow]{format_volume(order['volume'])}[/]"
            bar_str = f"[{base_color}]{'█' * bar_len}[/]"

        table.add_row(price_str, diff_str, side_str, vol_str, bar_str)

    if not separator_inserted:
        table.add_row(
            f"[bold underline cyan]${curr_price:,.2f}[/]",
            "[dim cyan]$0.00 (0.00%)[/]",
            "[bold underline cyan]► ACTIVE ◄[/]",
            "───",
            "────────────────",
            style="dim cyan",
        )

    return Group(
        Panel(
            summary_text,
            title="⚡ LIVE COINGLASS FEED PAIR (48 HOUR TIMEFRAME)",
            border_style="cyan",
            expand=False
        ),
        Panel(
            table, 
            border_style="blue", 
            expand=False
        )
    )


async def background_playwright_worker(page):
    """Periodically clicks the UI refresh button to update chart data without reloading."""
    refresh_button_selector = "button.MuiButton-root:has(svg path[d*='M497.408'])"
    
    while True:
        try:
            refresh_btn = page.locator(refresh_button_selector).first
            if await refresh_btn.is_visible():
                await refresh_btn.click()
            else:
                await page.locator("button.MuiButton-root:has(svg)").first.click()
            
            await asyncio.sleep(2)

            raw_data = await page.evaluate(JS_EXTRACT_SCRIPT)
            if raw_data and not raw_data.get("error"):
                with open(JSON_FILENAME, "w") as f:
                    json.dump(raw_data, f)
        except Exception:
            pass
        
        await asyncio.sleep(5)


async def select_48h_timeframe(page):
    """Selects '48 hour' option from the MUI dropdown."""
    try:
        console.print("[bold yellow]Attempting to set timeframe to 48 hour...[/]")
        
        dropdown_button = page.locator("button.MuiSelect-button:has-text('hour'), button.MuiSelect-button:has-text('24 hour')").first
        await dropdown_button.wait_for(state="visible", timeout=15000)
        await dropdown_button.click()
        await asyncio.sleep(1)

        option_12h = page.locator("li[role='option']:has-text('48 hour'), div[role='option']:has-text('48 hour'), [role='option']:has-text('48h')").first
        await option_12h.wait_for(state="visible", timeout=10000)
        await option_12h.click()
        
        console.print("[bold green]Successfully selected 12 hour timeframe![/]")
        await asyncio.sleep(3)
    except Exception as e:
        console.print(f"[bold red]Could not automatically select 48h dropdown: {e}[/]")
        console.print("[bold cyan]If you are not logged in yet, please log in and manually select '48 hour' in the browser.[/]")


async def main():
    async with async_playwright() as p:
        console.print("[bold green]Launching persistent browser session...[/]")
        
        context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=True,
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        await page.add_init_script("""
            window.__echarts_instances = window.__echarts_instances || [];
            let _echarts = undefined;
            Object.defineProperty(window, 'echarts', {
                get: function() { return _echarts; },
                set: function(val) { _echarts = val; window._globalEcharts; },
                configurable: true
            });
        """)

        console.print(f"Navigating to {TARGET_URL}...")
        await page.goto(
            TARGET_URL,
            wait_until="domcontentloaded",
        )
        
        await page.wait_for_selector(".echarts-for-react", timeout=30000)
        
        await select_48h_timeframe(page)
        
        console.print("[bold yellow]Hydrating initial chart dataset...[/]")
        await asyncio.sleep(4)

        initial_data = await page.evaluate(JS_EXTRACT_SCRIPT)
        if initial_data and not initial_data.get("error"):
            with open(JSON_FILENAME, "w") as f:
                json.dump(initial_data, f)
            console.print(
                f"[bold green]Saved baseline to {JSON_FILENAME}[/]"
            )

        asyncio.create_task(background_playwright_worker(page))

        with Live(
            render_dashboard(process_latest_column_from_json()),
            refresh_per_second=2,
            console=console,
        ) as live:
            while True:
                processed = process_latest_column_from_json()
                live.update(render_dashboard(processed))
                await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
