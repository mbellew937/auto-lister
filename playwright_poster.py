import asyncio
import os
import re
from typing import Awaitable, Callable, List, Optional
from playwright.async_api import async_playwright

StatusCallback = Optional[Callable[[str, int, str], Awaitable[None] | None]]
FACEBOOK_FORM_LOAD_TIMEOUT_MS = 120_000
CDP_CONNECT_TIMEOUT_MS = 30_000
CDP_CONNECT_RETRY_SECONDS = 0.5
FACEBOOK_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.6367.201 Safari/537.36"
)


async def _connect_over_cdp_with_retry(playwright, cdp_port: int):
    deadline = asyncio.get_running_loop().time() + (CDP_CONNECT_TIMEOUT_MS / 1000)
    last_error = None
    while True:
        try:
            return await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        except Exception as e:
            last_error = e
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    f"Browser CDP on port {cdp_port} was not ready after {CDP_CONNECT_TIMEOUT_MS // 1000} seconds: {e}"
                ) from e
            await asyncio.sleep(CDP_CONNECT_RETRY_SECONDS)


async def _facebook_login_required(page) -> bool:
    url = (page.url or "").lower()
    if "facebook.com/login" in url or "m.facebook.com/login" in url:
        return True
    try:
        body = (await page.locator("body").inner_text(timeout=1200)).lower()
    except Exception:
        return False
    return (
        "mobile number or email" in body
        and "password" in body
        and ("log in" in body or "login" in body)
    )


async def _force_desktop_facebook_page(context, page):
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send(
            "Network.setUserAgentOverride",
            {"userAgent": FACEBOOK_DESKTOP_UA, "platform": "Windows"},
        )
        await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": False})
    except Exception:
        pass


async def _facebook_app_create_required(page) -> bool:
    try:
        body = (await page.locator("body").inner_text(timeout=1200)).lower()
    except Exception:
        return False
    return "create listings on facebook app" in body and "open facebook" in body


async def _wait_for_facebook_create_form(page):
    deadline = asyncio.get_running_loop().time() + (FACEBOOK_FORM_LOAD_TIMEOUT_MS / 1000)
    last_url = page.url
    while asyncio.get_running_loop().time() < deadline:
        if await _facebook_login_required(page):
            raise RuntimeError("Facebook is asking you to log in. Open the Facebook Browser, log into Facebook there, then tap Post again.")
        if await _facebook_app_create_required(page):
            raise RuntimeError("Facebook mobile web is blocking listing creation and asking for the Facebook app. The poster now opens the create page in desktop mode; close the Facebook Browser, reopen it, then tap Post again.")
        try:
            title_field = page.get_by_label("Title", exact=True)
            await title_field.wait_for(state="visible", timeout=900)
            return title_field
        except Exception:
            last_url = page.url
            await page.wait_for_timeout(600)
    raise RuntimeError(f"Facebook create form did not load within {FACEBOOK_FORM_LOAD_TIMEOUT_MS // 1000} seconds. Current page: {last_url}")

CONDITION_ALIASES = {
    "new": ["New"],
    "like new": ["Like New", "Used - Like New"],
    "good": ["Good", "Used - Good"],
    "fair": ["Fair", "Used - Fair"],
    "poor": ["Poor", "Used - Fair", "Used - Poor"],
}

CATEGORY_ALIASES = {
    "misc": ["Miscellaneous", "General", "Other"],
    "miscellaneous": ["Miscellaneous", "General", "Other"],
    "other": ["Miscellaneous", "Other"],
    "tools": ["Tools", "Home improvement supplies", "Home improvement", "Miscellaneous"],
    "tool": ["Tools", "Home improvement supplies", "Home improvement", "Miscellaneous"],
    "home improvement": ["Home improvement supplies", "Tools", "Home goods", "Miscellaneous"],
    "electronics": ["Electronics", "Miscellaneous"],
    "furniture": ["Furniture", "Home goods", "Miscellaneous"],
    "home goods": ["Home goods", "Furniture", "Miscellaneous"],
    "appliance": ["Appliances", "Home goods", "Miscellaneous"],
    "appliances": ["Appliances", "Home goods", "Miscellaneous"],
    "clothing": ["Clothing", "Miscellaneous"],
    "apparel": ["Clothing", "Miscellaneous"],
    "sporting": ["Sporting goods", "Sports & outdoors", "Miscellaneous"],
    "sports": ["Sporting goods", "Sports & outdoors", "Miscellaneous"],
    "outdoor": ["Garden & outdoor", "Patio & garden", "Miscellaneous"],
    "garden": ["Garden & outdoor", "Patio & garden", "Miscellaneous"],
}
CATEGORY_FALLBACKS = ["Miscellaneous", "Other"]

async def _report(callback: StatusCallback, step: str, progress: int, message: str):
    if not callback:
        return
    result = callback(step, progress, message)
    if asyncio.iscoroutine(result):
        await result

def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()

def _option_variants(label: str, value: str) -> List[str]:
    normalized = _normalize_text(value)
    variants = [value, value.title(), value.upper(), value.capitalize()]

    if label.lower() == "condition":
        variants = CONDITION_ALIASES.get(normalized, []) + variants
    elif label.lower() == "category":
        aliases = []
        for key, values in CATEGORY_ALIASES.items():
            if key == normalized or key in normalized or normalized in key:
                aliases.extend(values)
        variants = aliases + variants + CATEGORY_FALLBACKS

    deduped = []
    seen = set()
    for item in variants:
        if not item:
            continue
        key = _normalize_text(item)
        if not key or key in seen:
            continue
        deduped.append(item)
        seen.add(key)
    return deduped

async def _click_first_visible(locators) -> bool:
    for locator in locators:
        try:
            target = locator.first
            await target.wait_for(state="visible", timeout=1200)
            await target.click(timeout=2500)
            return True
        except Exception:
            continue
    return False

async def _open_marketplace_picker(page, label: str) -> bool:
    pattern = re.compile(rf"^{re.escape(label)}$", re.I)
    locators = [
        page.get_by_label(label, exact=True),
        page.get_by_role("combobox", name=pattern),
        page.get_by_role("button", name=pattern),
        page.locator(f'[aria-label="{label}"]'),
        page.locator(f'text="{label}"'),
    ]
    if await _click_first_visible(locators):
        return True

    try:
        handle = await page.evaluate_handle(
            """(label) => {
                const wanted = (label || '').trim().toLowerCase();
                const roots = Array.from(document.querySelectorAll('label, span, div'));
                for (const node of roots) {
                    if ((node.textContent || '').trim().toLowerCase() !== wanted) continue;
                    let cur = node;
                    for (let depth = 0; depth < 4 && cur; depth += 1, cur = cur.parentElement) {
                        const picker = cur.querySelector('[role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="dialog"], input[type="text"], [role="button"]');
                        if (picker) return picker;
                    }
                }
                return null;
            }""",
            label,
        )
        element = handle.as_element()
        if element:
            await element.click(timeout=2500)
            return True
    except Exception:
        pass
    return False

async def _is_listing_form_textbox(locator) -> bool:
    try:
        return await locator.evaluate(
            """(el) => {
                const parts = [
                    el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'),
                    el.getAttribute('name'),
                    el.getAttribute('id'),
                ];
                const labelledBy = el.getAttribute('aria-labelledby');
                if (labelledBy) {
                    for (const id of labelledBy.split(/\\s+/)) {
                        const node = document.getElementById(id);
                        if (node) parts.push(node.textContent || '');
                    }
                }
                if (el.labels) {
                    for (const label of Array.from(el.labels)) {
                        parts.push(label.textContent || '');
                    }
                }
                const text = parts.filter(Boolean).join(' ').toLowerCase();
                return /\\b(title|price|description)\\b/.test(text);
            }"""
        )
    except Exception:
        return False


async def _find_search_input(page):
    candidates = [
        page.locator('[role="dialog"] [role="searchbox"]'),
        page.locator('[role="listbox"] [role="searchbox"]'),
        page.locator('[role="menu"] [role="searchbox"]'),
        page.locator('[aria-modal="true"] [role="searchbox"]'),
        page.locator('[role="dialog"] [role="textbox"]'),
        page.locator('[role="listbox"] [role="textbox"]'),
        page.locator('[role="menu"] [role="textbox"]'),
        page.locator('[aria-modal="true"] [role="textbox"]'),
        page.locator('[role="dialog"] input[type="text"]'),
        page.locator('[role="listbox"] input[type="text"]'),
        page.locator('[role="menu"] input[type="text"]'),
        page.locator('[aria-modal="true"] input[type="text"]'),
        page.locator('[role="dialog"] [aria-label*="Search" i]'),
        page.locator('[role="listbox"] [aria-label*="Search" i]'),
        page.locator('[role="menu"] [aria-label*="Search" i]'),
        page.locator('[aria-modal="true"] [aria-label*="Search" i]'),
        page.locator('[role="dialog"] input[placeholder*="Search" i]'),
        page.locator('[role="listbox"] input[placeholder*="Search" i]'),
        page.locator('[role="menu"] input[placeholder*="Search" i]'),
        page.locator('[aria-modal="true"] input[placeholder*="Search" i]'),
    ]
    for candidate in candidates:
        try:
            target = candidate.first
            if await target.is_visible(timeout=700):
                if await _is_listing_form_textbox(target):
                    continue
                return target
        except Exception:
            continue
    return None

async def _click_option_text(page, text: str) -> bool:
    pattern = re.compile(rf"^{re.escape(text)}$", re.I)
    roots = [
        page.locator('[aria-modal="true"]').last,
        page.locator('[role="dialog"]').last,
        page.locator('[role="listbox"]').last,
        page.locator('[role="menu"]').last,
    ]
    locators = [
        *(root.get_by_role("option", name=pattern) for root in roots),
        *(root.get_by_role("button", name=pattern) for root in roots),
        *(root.get_by_text(text, exact=True) for root in roots),
    ]
    if await _click_first_visible(locators):
        return True

    try:
        handle = await page.evaluate_handle(
            """(text) => {
                const norm = (value) => (value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
                const wanted = norm(text);
                const isVisible = (node) => {
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const rootCandidates = Array.from(document.querySelectorAll('[aria-modal="true"], [role="dialog"], [role="listbox"], [role="menu"]'))
                    .filter(isVisible);
                const roots = rootCandidates.length ? rootCandidates.reverse() : [document.body];
                const selectors = '[role="option"], [role="button"], div[tabindex], li[tabindex], span[tabindex]';
                const matches = (node) => {
                    const label = norm(node.innerText || node.textContent || '');
                    if (!label || !wanted) return false;
                    return label === wanted || label.startsWith(wanted + ' ') || label.includes(' ' + wanted + ' ');
                };
                for (const root of roots) {
                    const nodes = Array.from(root.querySelectorAll(selectors)).filter(isVisible);
                    for (const node of nodes) {
                        if (matches(node)) return node;
                    }
                }
                return null;
            }""",
            text,
        )
        element = handle.as_element()
        if element:
            await element.click(timeout=2500)
            return True
    except Exception:
        pass
    return False

async def _select_marketplace_option(page, label: str, value: str) -> bool:
    if not await _open_marketplace_picker(page, label):
        return False

    await page.wait_for_timeout(450)

    for option_text in _option_variants(label, value):
        search_input = await _find_search_input(page)
        if search_input:
            try:
                await search_input.fill("")
                await search_input.fill(option_text)
                await page.wait_for_timeout(650)
            except Exception:
                pass

        if await _click_option_text(page, option_text):
            await page.wait_for_timeout(250)
            return True

        if search_input:
            try:
                await search_input.press("ArrowDown")
                await search_input.press("Enter")
                await page.wait_for_timeout(250)
                return True
            except Exception:
                continue
    return False


async def _ensure_field_value(locator, expected: str):
    try:
        current = await locator.input_value(timeout=1200)
        if current != expected:
            await locator.fill(expected)
    except Exception:
        pass

async def reveal_publish_button(cdp_port: int):
    async with async_playwright() as p:
        browser = await _connect_over_cdp_with_retry(p, cdp_port)
        context = browser.contexts[0]
        page = None

        for candidate in context.pages:
            if "facebook.com" in (candidate.url or ""):
                page = candidate
        if page is None:
            page = context.pages[0] if context.pages else await context.new_page()

        await page.bring_to_front()

        try:
            publish_button = page.get_by_role("button", name="Publish")
            await publish_button.wait_for(timeout=6000)
            await publish_button.scroll_into_view_if_needed(timeout=6000)
            await page.wait_for_timeout(250)
            return
        except Exception:
            pass

        await page.evaluate(
            """() => {
                const isScrollable = (el) => {
                    const style = window.getComputedStyle(el);
                    return /(auto|scroll)/.test(style.overflowY || '') && el.scrollHeight > el.clientHeight + 20;
                };

                const textMatches = (el) => (el.innerText || '').trim().toLowerCase() === 'publish';
                const buttonish = (el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    return tag === 'button' || el.getAttribute('role') === 'button';
                };

                const walk = (el) => {
                    let cur = el;
                    while (cur) {
                        if (isScrollable(cur)) {
                            cur.scrollTop = cur.scrollHeight;
                        }
                        cur = cur.parentElement;
                    }
                };

                const candidates = Array.from(document.querySelectorAll('*'))
                    .filter((el) => textMatches(el) && buttonish(el));

                if (candidates.length) {
                    const target = candidates[0];
                    walk(target);
                    target.scrollIntoView({ block: 'center', inline: 'nearest' });
                    return true;
                }

                for (const el of Array.from(document.querySelectorAll('*'))) {
                    if (isScrollable(el)) {
                        el.scrollTop = el.scrollHeight;
                    }
                }
                window.scrollTo(0, document.body.scrollHeight);
                return false;
            }"""
        )
        await page.wait_for_timeout(250)

async def create_facebook_listing(
    image_paths: List[str],
    title: str,
    price: int,
    condition: str,
    category: str,
    description: str,
    cdp_port: int,
    status_callback: StatusCallback = None,
):
    abs_img_paths = [os.path.abspath(p) for p in image_paths]
    
    async with async_playwright() as p:
        try:
            await _report(status_callback, "connect", 8, "Connecting to your browser session")
            print(f"Connecting to user browser via CDP on port {cdp_port}...")
            browser = await _connect_over_cdp_with_retry(p, cdp_port)
            
            context = browser.contexts[0]
            page = await context.new_page()
            await _force_desktop_facebook_page(context, page)
            
            await _report(status_callback, "navigate", 18, "Opening Facebook Marketplace create form")
            print("Navigating to Facebook Marketplace...")
            await page.goto("https://www.facebook.com/marketplace/create/item")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(800)

            if await _facebook_login_required(page):
                raise RuntimeError("Facebook is asking you to log in. Open the Facebook Browser, log into Facebook there, then tap Post again.")
            
            await _report(status_callback, "wait_form", 28, "Waiting for the Facebook form to load")
            title_field = await _wait_for_facebook_create_form(page)
            
            await _report(status_callback, "upload", 42, f"Uploading {len(abs_img_paths)} photo{'s' if len(abs_img_paths) != 1 else ''}")
            print(f"Uploading {len(abs_img_paths)} photos...")
            file_input = page.locator('input[type="file"][accept*="image"]')
            await file_input.set_input_files(abs_img_paths)
            
            await _report(status_callback, "title", 56, "Filling title")
            await title_field.fill(title)

            await _report(status_callback, "price", 66, "Filling price")
            await page.get_by_label("Price", exact=True).fill(str(price))

            await _report(status_callback, "category", 74, "Selecting category")
            category_selected = await _select_marketplace_option(page, "Category", category)
            if not category_selected:
                await _report(status_callback, "category", 74, "Category selector not found, leaving it for review")

            await _report(status_callback, "condition", 82, "Selecting condition")
            condition_selected = await _select_marketplace_option(page, "Condition", condition)
            if not condition_selected:
                await _report(status_callback, "condition", 82, "Condition selector not found, leaving it for review")
            await _ensure_field_value(title_field, title)

            await _report(status_callback, "description", 92, "Filling description")
            await page.get_by_label("Description", exact=True).fill(description)
            
            await _report(status_callback, "complete", 100, "Facebook draft filled. Review it in the browser and publish.")
            print("Done! Review the browser window.")
            
        except Exception as e:
            await _report(status_callback, "error", 100, f"Fill failed: {e}")
            print(f"Playwright CDP error: {e}")
            raise
