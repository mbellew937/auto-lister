import asyncio
from playwright.async_api import async_playwright
import os
import json
import sys

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 15; SM-S938U) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.6367.201 Safari/537.36"
)

STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-web-security",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-extensions-except=",
    "--disable-default-apps",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--lang=en-US",
    "--accept-lang=en-US,en;q=0.9",
]

STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
    Object.defineProperty(navigator, 'permissions', {
        get: () => ({ query: () => Promise.resolve({ state: 'granted' }) })
    });
"""

DESKTOP_BOUNDS = {
    "windowState": "normal",
    "left": 0,
    "top": 0,
    "width": 1600,
    "height": 900,
}

MOBILE_SCREEN_WIDTH = int(os.environ.get("AUTO_MARKETPLACE_MOBILE_SCREEN_WIDTH", "1080"))
MOBILE_SCREEN_HEIGHT = int(os.environ.get("AUTO_MARKETPLACE_MOBILE_SCREEN_HEIGHT", "1960"))
MOBILE_VIEWPORT_WIDTH = int(os.environ.get("AUTO_MARKETPLACE_MOBILE_VIEWPORT_WIDTH", "360"))
MOBILE_VIEWPORT_HEIGHT = int(os.environ.get("AUTO_MARKETPLACE_MOBILE_VIEWPORT_HEIGHT", "653"))
MOBILE_DEVICE_SCALE_FACTOR = float(os.environ.get("AUTO_MARKETPLACE_MOBILE_DEVICE_SCALE_FACTOR", "3"))

def _update_json_file(path: str, updater):
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.load(f)
        updater(data)
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Could not update browser state file {path}: {e}")

def _sanitize_profile_state(profile_dir: str, window_width: int, window_height: int, maximized: bool):
    prefs_path = os.path.join(profile_dir, "Default", "Preferences")
    local_state_path = os.path.join(profile_dir, "Local State")

    def update_prefs(data):
        profile = data.setdefault("profile", {})
        profile["exit_type"] = "Normal"
        browser = data.setdefault("browser", {})
        window = browser.setdefault("window_placement", {})
        window["maximized"] = maximized
        window["left"] = 0
        window["top"] = 0
        window["right"] = window_width
        window["bottom"] = window_height

    def update_local_state(data):
        profile = data.setdefault("profile", {})
        cache = profile.setdefault("info_cache", {}).setdefault("Default", {})
        cache["active_time"] = cache.get("active_time", 0)

    _update_json_file(prefs_path, update_prefs)
    _update_json_file(local_state_path, update_local_state)

async def _enforce_desktop_window(cdp_session):
    try:
        window = await cdp_session.send("Browser.getWindowForTarget")
        bounds = window.get("bounds", {})
        if bounds.get("windowState") == "minimized":
            await cdp_session.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": DESKTOP_BOUNDS},
            )
            await cdp_session.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": {"windowState": "maximized"}},
            )
        elif bounds.get("windowState") != "maximized":
            await cdp_session.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": {"windowState": "maximized"}},
            )
    except Exception as e:
        print(f"Could not enforce browser window state: {e}")

async def _enforce_mobile_browser(context, page):
    try:
        cdp = await context.new_cdp_session(page)
        try:
            window = await cdp.send("Browser.getWindowForTarget")
            await cdp.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window["windowId"],
                    "bounds": {
                        "windowState": "normal",
                        "left": 0,
                        "top": 0,
                        "width": MOBILE_VIEWPORT_WIDTH,
                        "height": MOBILE_VIEWPORT_HEIGHT,
                    },
                },
            )
        except Exception:
            pass
        await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    except Exception as e:
        print(f"Could not enforce mobile browser metrics: {e}")

async def run(user_id: str, display: str, cdp_port: int, device: str):
    data_dir = os.environ.get(
        "AUTO_MARKETPLACE_DATA_DIR",
        os.environ.get("AUTO_MARKETPLACE_BASE_DIR", os.path.dirname(os.path.abspath(__file__))),
    )
    profile_dir = os.path.join(data_dir, "profiles", f"{user_id}-{device}")
    os.makedirs(profile_dir, exist_ok=True)
    os.environ["DISPLAY"] = display

    is_mobile = device == "mobile"
    window_width = MOBILE_VIEWPORT_WIDTH if is_mobile else DESKTOP_BOUNDS["width"]
    window_height = MOBILE_VIEWPORT_HEIGHT if is_mobile else DESKTOP_BOUNDS["height"]
    _sanitize_profile_state(profile_dir, window_width, window_height, maximized=not is_mobile)

    args = STEALTH_ARGS + [
        f"--window-size={window_width},{window_height}",
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=0.0.0.0",
    ]
    if not is_mobile:
        args.append("--start-maximized")
    else:
        args.extend([
            "--high-dpi-support=1",
            f"--force-device-scale-factor={MOBILE_DEVICE_SCALE_FACTOR:g}",
            "--touch-events=enabled",
        ])

    kwargs = dict(
        user_data_dir=profile_dir,
        headless=False,
        args=args,
    )

    if is_mobile:
        kwargs.update(
            user_agent=MOBILE_UA,
            no_viewport=True,
        )
    else:
        kwargs.update(
            user_agent=DESKTOP_UA,
            no_viewport=True,
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(**kwargs)
        page = browser.pages[0] if browser.pages else await browser.new_page()
        if is_mobile:
            await _enforce_mobile_browser(browser, page)
        else:
            try:
                cdp = await browser.new_cdp_session(page)
                window = await cdp.send("Browser.getWindowForTarget")
                await cdp.send("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": DESKTOP_BOUNDS})
                await page.wait_for_timeout(250)
                await _enforce_desktop_window(cdp)
            except Exception as e:
                print(f"Could not maximize browser window: {e}")
        await page.add_init_script(STEALTH_JS)
        await page.goto("https://www.facebook.com")
        print(f"Browser for {user_id} ({device}) launched on {display} (CDP: {cdp_port})")

        # Keep stealth JS active on every new page in this context
        await browser.add_init_script(STEALTH_JS)

        while True:
            if not is_mobile:
                await _enforce_desktop_window(cdp)
            else:
                for candidate in list(browser.pages):
                    await _enforce_mobile_browser(browser, candidate)
            await asyncio.sleep(2)

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python launch_browser.py <user_id> <display> <cdp_port> [device]")
        sys.exit(1)
    u_id   = sys.argv[1]
    disp   = sys.argv[2]
    port   = int(sys.argv[3])
    dev    = sys.argv[4] if len(sys.argv) > 4 else "desktop"
    asyncio.run(run(u_id, disp, port, dev))
