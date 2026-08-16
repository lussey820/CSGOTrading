"""SteamDT chart screenshot module.

Render the trend page with Playwright (headless Chromium) and capture both
the K-line (candlestick) chart and the Line (area) chart as PNGs. Both
images are then fed to the vision analyst agent for combined LLM analysis.

Two chart modes on SteamDT (verified by clicking the switch button):
  - K-line (candlestick): URL ?type=klinecharts, switch button reads
    "Switch to Line Chart" (button text = the mode you would switch TO).
  - Line (area): URL ?type=charts, switch button reads
    "Switch to Candlestick Chart".

So the current mode is always the OPPOSITE of what the switch button says.
"""

import os
import asyncio
import logging
import sys
from pathlib import Path
from urllib.parse import quote
from typing import Optional, Dict

from dotenv import load_dotenv

load_dotenv()

# --- Local Playwright bootstrap (project-local install to bypass sandbox) ---
# playwright + chromium may be vendored under <project>/libs and
# <project>/playwright_browsers. Set these up before any `import playwright`.
_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_LIBS_DIR = _PROJ_ROOT / "libs"
if _LIBS_DIR.is_dir() and str(_LIBS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIBS_DIR))
_BROWSERS_DIR = _PROJ_ROOT / "playwright_browsers"
if _BROWSERS_DIR.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_BROWSERS_DIR))
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

STEAMDT_WEB_BASE = "https://www.steamdt.com"
DEFAULT_SCREENSHOT_DIR = _PROJ_ROOT / "assets" / "screenshots"

# CSS selectors for the chart container, in order of preference.
# .steam-kline-pro-chart-wrap wraps both the price canvas and the volume subchart.
CHART_SELECTORS = [
    ".steam-kline-pro-chart-wrap",
    ".chart-panel.is-active-chart-panel",
    ".steam-kline-pro",
    ".charts-area.is-kline-chart",
]

# Switch-button text patterns. The button text always describes the mode you
# would switch TO, so its presence means we are currently in the OPPOSITE mode.
_SWITCH_TO_LINE_PATTERNS = [
    "Switch to Line Chart",
    "Switch to Area Chart",
    "切换到走势图",
    "切换到折线图",
]
_SWITCH_TO_CANDLE_PATTERNS = [
    "Switch to Candlestick Chart",
    "Switch to K-Line Chart",
    "切换到K线图",
    "切换到蜡烛图",
]


def _build_url(
    market_hash_name: str, locale: str = "en", chart_type: str = "klinecharts"
) -> str:
    """Build the SteamDT trend page URL with the requested chart type."""
    return (
        f"{STEAMDT_WEB_BASE}/{locale}/cs2/{quote(market_hash_name)}"
        f"?tab=trend&type={chart_type}"
    )


def _safe_filename(name: str) -> str:
    """Convert a market hash name to a filesystem-safe basename."""
    for ch in '|():"/\\':
        name = name.replace(ch, "")
    return name.strip().replace(" ", "_")


def _dismiss_cookie_consent(page):
    """Click the 'Agree' button on the cookie consent banner if present."""
    try:
        page.locator(
            'button:has-text("Agree"), button:has-text("同意")'
        ).first.click(timeout=2000)
        logger.info("Dismissed cookie consent")
    except Exception:
        pass  # Non-fatal


def _wait_for_chart(page, timeout_ms: int = 15000) -> Optional[str]:
    """Wait for the chart canvas to render with non-zero size.

    Returns the first matching CSS selector that is visible with a real size,
    or None if no candidate becomes ready in time.
    """
    try:
        page.wait_for_selector("canvas", state="attached", timeout=timeout_ms)
    except Exception:
        logger.warning("canvas element not attached within timeout")

    for sel in CHART_SELECTORS:
        try:
            page.wait_for_selector(sel, state="visible", timeout=5000)
            box = page.locator(sel).first.bounding_box()
            if box and box["width"] > 100 and box["height"] > 100:
                return sel
        except Exception:
            continue
    return None


def _get_current_mode(page) -> str:
    """Determine the current chart mode from the URL parameter.

    SteamDT's switch button click triggers a full navigation that changes
    the URL ?type= param, so URL is the most reliable signal. After a switch
    both button labels can coexist in the DOM, so button text alone is not
    sufficient to determine the current mode.
    """
    url = page.url
    if "type=klinecharts" in url:
        return "kline"
    if "type=charts" in url or "type=chart" in url:
        return "line"
    # Fallback: if URL unclear, presence of "Switch to Candlestick Chart"
    # button means we are in line mode (button offers switch TO candle).
    if page.locator(
        'button:has-text("Switch to Candlestick Chart"), '
        'button:has-text("Switch to K-Line Chart"), '
        'button:has-text("切换到K线图")'
    ).count() > 0:
        return "line"
    return "kline"


def _find_switch_button(page, target_mode: str):
    """Locate the switch button that toggles TO the target mode.

    target_mode: 'kline' or 'line'.
    Returns the button locator (may have count() == 0 if not found).
    """
    if target_mode == "line":
        return page.locator(
            'button:has-text("Switch to Line Chart"), '
            'button:has-text("Switch to Area Chart"), '
            'button:has-text("切换到走势图"), '
            'button:has-text("切换到折线图")'
        ).first
    return page.locator(
        'button:has-text("Switch to Candlestick Chart"), '
        'button:has-text("Switch to K-Line Chart"), '
        'button:has-text("切换到K线图"), '
        'button:has-text("切换到蜡烛图")'
    ).first


def _ensure_chart_type(page, want_kline: bool, render_delay_ms: int = 4000) -> bool:
    """Switch the chart to K-line (want_kline=True) or Line (want_kline=False).

    Clicks the switch button only if the current mode differs from the target.
    SteamDT's switch triggers a full page navigation (URL ?type= changes),
    so we wait for the new page to settle before verifying via URL.
    Returns True if the page is in the requested mode after the call.
    """
    want = "kline" if want_kline else "line"
    current = _get_current_mode(page)
    if current == want:
        logger.info(f"Already in {want} mode (url={page.url})")
        return True

    logger.info(f"Switching chart from {current} to {want}")
    btn = _find_switch_button(page, want)
    if btn.count() == 0:
        logger.warning(f"No switch button found to switch to {want} mode")
        return False

    try:
        # The click triggers navigation (URL type= param changes). Playwright's
        # auto-wait handles load, then we add an extra render delay.
        btn.click(timeout=5000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(render_delay_ms)
    except Exception as e:
        logger.warning(f"Click switch button failed: {e}")
        return False

    current2 = _get_current_mode(page)
    if current2 == want:
        logger.info(f"Switched to {want} mode OK (url={page.url})")
        return True
    logger.warning(f"Switch verification: expected {want}, got {current2}")
    return current2 == want


def _screenshot_chart(page, output_path: str, used_selector: Optional[str]) -> bool:
    """Screenshot the chart container. Returns True on success."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    if used_selector:
        try:
            page.locator(used_selector).first.screenshot(path=output_path)
            logger.info(
                f"Saved chart screenshot (selector={used_selector}): {output_path}"
            )
            return True
        except Exception as e:
            logger.warning(f"Element screenshot via '{used_selector}' failed: {e}")
    logger.warning("Falling back to full-page screenshot")
    page.screenshot(path=output_path, full_page=False)
    logger.info(f"Saved full-page screenshot: {output_path}")
    return True


def capture_chart_screenshots(
    market_hash_name: str,
    output_dir: Optional[str] = None,
    locale: str = "en",
    headless: bool = True,
    timeout_ms: int = 30000,
    viewport_width: int = 1280,
    viewport_height: int = 900,
    extra_render_delay_ms: int = 2500,
) -> Dict[str, Optional[str]]:
    """Capture both K-line (candlestick) and Line (area) chart screenshots.

    Args:
        market_hash_name: Steam item market hash name
        output_dir: Directory for PNGs. If None, defaults to
            assets/screenshots/. Files are named <safe>_kline.png and
            <safe>_line.png.
        locale: Page locale ('en' or 'zh')
        headless: Use headless browser
        timeout_ms: Navigation timeout in ms
        viewport_width/height: Browser viewport
        extra_render_delay_ms: Extra wait for klinecharts to finish drawing

    Returns:
        Dict {"kline": abs_path_or_None, "line": abs_path_or_None}.
        Entries are None if that chart type could not be captured.
    """
    from playwright.sync_api import sync_playwright

    if output_dir is None:
        output_dir = str(DEFAULT_SCREENSHOT_DIR)
    output_dir = os.path.abspath(output_dir)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    safe = _safe_filename(market_hash_name)
    kline_path = os.path.join(output_dir, f"{safe}_kline.png")
    line_path = os.path.join(output_dir, f"{safe}_line.png")

    url = _build_url(market_hash_name, locale, chart_type="klinecharts")
    logger.info(f"Capturing charts for {market_hash_name}: {url}")

    result: Dict[str, Optional[str]] = {"kline": None, "line": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # domcontentloaded is faster and sufficient: we then wait for the
        # klinecharts canvas to actually render. networkidle often times out
        # on SteamDT due to long-lived connections / periodic refresh.
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:
            logger.warning(f"domcontentloaded wait failed: {e}")
            page.goto(url, wait_until="commit", timeout=timeout_ms)

        _dismiss_cookie_consent(page)
        used_selector = _wait_for_chart(page, timeout_ms=min(timeout_ms, 15000))
        if extra_render_delay_ms > 0:
            page.wait_for_timeout(extra_render_delay_ms)

        # 1. Ensure K-line (candlestick) mode, then screenshot. After a
        # potential switch the canvas is rebuilt, so re-detect the selector.
        if _ensure_chart_type(page, want_kline=True):
            page.wait_for_timeout(1500)
            sel = _wait_for_chart(page, timeout_ms=10000) or used_selector
            if _screenshot_chart(page, kline_path, sel):
                result["kline"] = kline_path

        # 2. Switch to Line (area) mode, then screenshot. Re-detect selector
        # again because the switch reloads the chart canvas.
        if _ensure_chart_type(page, want_kline=False):
            page.wait_for_timeout(1500)
            sel = _wait_for_chart(page, timeout_ms=10000) or used_selector
            if _screenshot_chart(page, line_path, sel):
                result["line"] = line_path

        browser.close()

    logger.info(
        f"Captured charts: kline={'yes' if result['kline'] else 'no'}, "
        f"line={'yes' if result['line'] else 'no'}"
    )
    return result


# Backward-compatible single-screenshot API. Captures both charts but
# returns only the requested one. Used by ad-hoc callers and the CLI.
def capture_chart_screenshot(
    market_hash_name: str,
    output_path: Optional[str] = None,
    locale: str = "en",
    headless: bool = True,
    timeout_ms: int = 30000,
    chart_type: str = "kline",
    **_kwargs,
) -> Optional[str]:
    """Capture a single chart type. chart_type: 'kline' or 'line'.

    Note: this still captures both charts internally (SteamDT requires a page
    load + switch), then returns the path for the requested type.
    """
    out_dir = os.path.dirname(output_path) if output_path else str(DEFAULT_SCREENSHOT_DIR)
    res = capture_chart_screenshots(
        market_hash_name,
        output_dir=out_dir,
        locale=locale,
        headless=headless,
        timeout_ms=timeout_ms,
    )
    path = res.get(chart_type)
    if path and output_path and os.path.abspath(path) != os.path.abspath(output_path):
        # Caller asked for a specific path; rename the captured file
        os.replace(path, output_path)
        return output_path
    return path


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    name = sys.argv[1] if len(sys.argv) > 1 else "AK-47 | Redline (Field-Tested)"
    res = capture_chart_screenshots(name)
    print(f"K-line: {res['kline']}")
    print(f"Line:   {res['line']}")


# ---------------------------------------------------------------------------
# Parallel batch capture (used by warmup to speed up multi-item screenshots)
# ---------------------------------------------------------------------------

def _build_paths(market_hash_name: str, output_dir: str):
    safe = _safe_filename(market_hash_name)
    kline_path = os.path.join(output_dir, f"{safe}_kline.png")
    line_path = os.path.join(output_dir, f"{safe}_line.png")
    return safe, kline_path, line_path


async def _capture_one_async(
    context,
    market_hash_name: str,
    output_dir: str,
    locale: str,
    timeout_ms: int,
    viewport_width: int,
    viewport_height: int,
    render_delay_ms: int,
    sem,
) -> Dict[str, Optional[str]]:
    """Capture both charts for one item using a fresh page in `context`.

    Reuses the shared browser context. Concurrency is bounded by `sem`.
    """
    safe, kline_path, line_path = _build_paths(market_hash_name, output_dir)
    result: Dict[str, Optional[str]] = {"kline": None, "line": None}

    async with sem:
        page = await context.new_page()
        try:
            for chart_type, out_path in (("klinecharts", kline_path), ("charts", line_path)):
                url = _build_url(market_hash_name, locale, chart_type=chart_type)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as e:
                    logger.warning(f"[{safe}] goto {chart_type} failed: {e}")
                    try:
                        await page.goto(url, wait_until="commit", timeout=timeout_ms)
                    except Exception:
                        continue

                # dismiss cookie consent if visible
                try:
                    btn = page.locator(
                        'button:has-text("Agree"), button:has-text("同意")'
                    ).first
                    if await btn.count() > 0:
                        await btn.click(timeout=1500)
                except Exception:
                    pass

                # wait for canvas
                try:
                    await page.wait_for_selector("canvas", timeout=min(timeout_ms, 12000))
                except Exception:
                    pass
                # pick a visible chart container
                used_sel = None
                for sel in CHART_SELECTORS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=2500)
                        box = await page.locator(sel).first.bounding_box()
                        if box and box["width"] > 100 and box["height"] > 100:
                            used_sel = sel
                            break
                    except Exception:
                        continue
                if render_delay_ms > 0:
                    await page.wait_for_timeout(render_delay_ms)

                try:
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    if used_sel:
                        await page.locator(used_sel).first.screenshot(path=out_path)
                    else:
                        await page.screenshot(path=out_path, full_page=False)
                    result["kline" if chart_type == "klinecharts" else "line"] = out_path
                except Exception as e:
                    logger.warning(f"[{safe}] screenshot {chart_type} failed: {e}")
        finally:
            await page.close()

    logger.info(
        f"[{safe}] captured kline={'yes' if result['kline'] else 'no'}, "
        f"line={'yes' if result['line'] else 'no'}"
    )
    return result


async def _capture_many_async(
    items: list,
    output_dir: str,
    locale: str,
    headless: bool,
    timeout_ms: int,
    viewport_width: int,
    viewport_height: int,
    render_delay_ms: int,
    concurrency: int,
) -> Dict[str, Dict[str, Optional[str]]]:
    """Capture charts for multiple items concurrently with one shared browser."""
    from playwright.async_api import async_playwright

    results: Dict[str, Dict[str, Optional[str]]] = {}
    if not items:
        return results

    sem = asyncio.Semaphore(max(1, concurrency))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        try:
            tasks = [
                _capture_one_async(
                    context,
                    name,
                    output_dir,
                    locale,
                    timeout_ms,
                    viewport_width,
                    viewport_height,
                    render_delay_ms,
                    sem,
                )
                for name in items
            ]
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for name, res in zip(items, gathered):
                if isinstance(res, Exception):
                    logger.error(f"capture failed for {name}: {res}")
                    results[name] = {"kline": None, "line": None}
                else:
                    results[name] = res
        finally:
            await context.close()
            await browser.close()
    return results


def capture_chart_screenshots_batch(
    items: list,
    output_dir: Optional[str] = None,
    locale: str = "en",
    headless: bool = True,
    timeout_ms: int = 30000,
    viewport_width: int = 1280,
    viewport_height: int = 900,
    render_delay_ms: int = 2000,
    concurrency: int = 3,
) -> Dict[str, Dict[str, Optional[str]]]:
    """Parallel batch capture. Returns {item_name: {kline, line}}."""
    import asyncio

    if output_dir is None:
        output_dir = str(DEFAULT_SCREENSHOT_DIR)
    output_dir = os.path.abspath(output_dir)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if not items:
        return {}

    logger.info(
        f"Batch capturing {len(items)} items with concurrency={concurrency} "
        f"(output_dir={output_dir})"
    )
    return asyncio.run(
        _capture_many_async(
            list(items),
            output_dir,
            locale,
            headless,
            timeout_ms,
            viewport_width,
            viewport_height,
            render_delay_ms,
            concurrency,
        )
    )
