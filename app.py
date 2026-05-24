import os
import tempfile
import json
import asyncio
import re
import base64
import hashlib
import hmac
import secrets
import mimetypes
import ipaddress
from html import escape
from urllib.parse import quote_plus, urlparse, urlencode
from typing import Dict, List, Optional
from fastapi import FastAPI, File, UploadFile, Request, BackgroundTasks, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from google import genai
from google.genai import types
import websockets
import requests

from session_manager import SessionManager
from playwright_poster import create_facebook_listing, reveal_publish_button
import stripe


def is_private_distribution_url(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").strip().lower()
        if not host:
            return False
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            return True
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


# Configuration
APP_DIR = os.environ.get("AUTO_MARKETPLACE_APP_DIR", os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = os.environ.get(
    "AUTO_MARKETPLACE_DATA_DIR",
    os.environ.get("AUTO_MARKETPLACE_BASE_DIR", APP_DIR),
)
NOVNC_DIR = os.environ.get("AUTO_MARKETPLACE_NOVNC_DIR", os.path.join(APP_DIR, "noVNC"))
AUTH_PROVIDER = os.environ.get("AUTO_MARKETPLACE_AUTH_PROVIDER", "").strip().lower()
if AUTH_PROVIDER not in {"local", "oidc"}:
    AUTH_PROVIDER = "local"
AUTH_PROVIDER_JS = json.dumps(AUTH_PROVIDER)
OIDC_ISSUER = os.environ.get("OIDC_ISSUER_URL", "").strip().rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "").strip()
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "https://marketplace.mrbtechnologies.com/api/auth/callback").strip()
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid profile email").strip() or "openid profile email"
OIDC_PROVIDER_LABEL = os.environ.get("OIDC_PROVIDER_LABEL", "Sign in").strip() or "Sign in"
OIDC_PROVIDER_LABEL_JS = json.dumps(OIDC_PROVIDER_LABEL)
OIDC_PENDING_COOKIE = "auto_lister_oidc"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
OPENAI_CHAT_COMPLETIONS_URL = os.environ.get(
    "OPENAI_CHAT_COMPLETIONS_URL",
    "https://api.openai.com/v1/chat/completions",
).strip() or "https://api.openai.com/v1/chat/completions"
CREDIT_LABEL = os.environ.get("AUTO_MARKETPLACE_CREDIT_LABEL", "MRB Technologies").strip() or "MRB Technologies"
CREDIT_URL = os.environ.get("AUTO_MARKETPLACE_CREDIT_URL", "https://mrbtechnologies.com").strip() or "https://mrbtechnologies.com"
HOSTED_OFFER_URL = os.environ.get("AUTO_MARKETPLACE_HOSTED_URL", "https://marketplace.mrbtechnologies.com").strip()
HOSTED_OFFER_PRICE = os.environ.get("AUTO_MARKETPLACE_HOSTED_PRICE", "$1").strip() or "$1"
HOSTED_OFFER_COMPARE_AT_PRICE = os.environ.get("AUTO_MARKETPLACE_HOSTED_COMPARE_AT_PRICE", "$5").strip()
HOSTED_FREE_SIGNUP_LIMIT = os.environ.get("AUTO_MARKETPLACE_FREE_SIGNUP_LIMIT", "25").strip() or "25"
HOSTED_FREE_POSTS = os.environ.get("AUTO_MARKETPLACE_FREE_POSTS", "3").strip() or "3"
SUPPORT_EMAIL = os.environ.get("AUTO_MARKETPLACE_SUPPORT_EMAIL", "support@mrbtechnologies.com").strip() or "support@mrbtechnologies.com"
SUPPORT_HELPDESK_URL = os.environ.get("AUTO_MARKETPLACE_SUPPORT_HELPDESK_URL", "https://helpdesk.mrbtechnologies.com").strip()
SUPPORT_GITHUB_ISSUES_URL = os.environ.get(
    "AUTO_MARKETPLACE_SUPPORT_GITHUB_ISSUES_URL",
    "https://github.com/mbellew937/auto-lister/issues",
).strip()
SUPPORT_MAILTO_URL = (
    f"mailto:{SUPPORT_EMAIL}?subject={quote_plus('Auto-Lister support request')}"
    f"&body={quote_plus('What happened?\\n\\nPage or action:\\n\\nAccount email:\\n\\nScreenshots or logs:\\n')}"
)
SUPPORT_URL = os.environ.get("AUTO_MARKETPLACE_SUPPORT_URL", SUPPORT_MAILTO_URL).strip() or SUPPORT_MAILTO_URL


def parse_nonnegative_int(value: str, default: int) -> int:
    try:
        parsed = int(str(value).strip())
        return max(0, parsed)
    except (TypeError, ValueError):
        return default


HOSTED_FREE_SIGNUP_LIMIT_COUNT = parse_nonnegative_int(HOSTED_FREE_SIGNUP_LIMIT, 25)
HOSTED_FREE_POSTS_COUNT = parse_nonnegative_int(HOSTED_FREE_POSTS, 3)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
STRIPE_SUCCESS_URL = os.environ.get("STRIPE_SUCCESS_URL", "https://marketplace.mrbtechnologies.com/dashboard")
STRIPE_CANCEL_URL = os.environ.get("STRIPE_CANCEL_URL", "https://marketplace.mrbtechnologies.com/dashboard")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
STRIPE_ENABLED = bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET and STRIPE_PRICE_ID)

SELF_HOST_GUIDE_PATH = "/self-host"
PACKAGE_DOWNLOAD_FILENAME = os.environ.get(
    "AUTO_MARKETPLACE_PACKAGE_FILENAME",
    "auto-lister-self-host.tar.gz",
).strip() or "auto-lister-self-host.tar.gz"
DOWNLOAD_DIR = os.environ.get(
    "AUTO_MARKETPLACE_DOWNLOAD_DIR",
    os.path.join(APP_DIR, "downloads"),
).strip() or os.path.join(APP_DIR, "downloads")
PACKAGE_DOWNLOAD_URL = os.environ.get(
    "AUTO_MARKETPLACE_PACKAGE_DOWNLOAD_URL",
    f"https://marketplace.mrbtechnologies.com/downloads/{PACKAGE_DOWNLOAD_FILENAME}",
).strip()
if not PACKAGE_DOWNLOAD_URL:
    PACKAGE_DOWNLOAD_URL = f"https://marketplace.mrbtechnologies.com/downloads/{PACKAGE_DOWNLOAD_FILENAME}"
PUBLIC_REPO_URL = os.environ.get(
    "AUTO_MARKETPLACE_PUBLIC_REPO_URL",
    "https://github.com/mbellew937/auto-lister.git",
).strip()
REPO_DOWNLOAD_URL = (os.environ.get("AUTO_MARKETPLACE_REPO_DOWNLOAD_URL") or PUBLIC_REPO_URL).strip()
if is_private_distribution_url(REPO_DOWNLOAD_URL):
    REPO_DOWNLOAD_URL = ""
CASHAPP_HANDLE = os.environ.get("AUTO_MARKETPLACE_CASHAPP_HANDLE", "$michaelbellew").strip() or "$michaelbellew"
VENMO_HANDLE = os.environ.get("AUTO_MARKETPLACE_VENMO_HANDLE", "@mikebellew").strip() or "@mikebellew"

if CASHAPP_HANDLE.startswith("@"):
    CASHAPP_HANDLE = "$" + CASHAPP_HANDLE[1:]
if CASHAPP_HANDLE and not CASHAPP_HANDLE.startswith("$"):
    CASHAPP_HANDLE = f"${CASHAPP_HANDLE}"
CASHAPP_LINK = f"https://cash.app/{CASHAPP_HANDLE}"

if VENMO_HANDLE.startswith("$"):
    VENMO_HANDLE = f"@{VENMO_HANDLE[1:]}"
VENMO_USERNAME = VENMO_HANDLE.lstrip("@")
VENMO_LINK = f"https://venmo.com/{VENMO_USERNAME}"

CASHAPP_LINK_QR = f"https://api.qrserver.com/v1/create-qr-code/?size=220x220&data={quote_plus(CASHAPP_LINK)}"
VENMO_LINK_QR = f"https://api.qrserver.com/v1/create-qr-code/?size=220x220&data={quote_plus(VENMO_LINK)}"


def to_hosted_login_url(value: str) -> str:
    if not value:
        return "https://marketplace.mrbtechnologies.com/api/auth/login"
    value = value.strip().rstrip("/")
    if value.endswith("/api/auth/login"):
        return value
    if value.endswith("/login"):
        value = value[:-6]
    if not value:
        return "https://marketplace.mrbtechnologies.com/api/auth/login"
    return f"{value}/api/auth/login"


HOSTED_LAUNCH_URL = to_hosted_login_url(HOSTED_OFFER_URL)
HOSTED_LAUNCH_URL_HTML = escape(HOSTED_LAUNCH_URL, quote=True)
PACKAGE_DOWNLOAD_URL_HTML = escape(PACKAGE_DOWNLOAD_URL, quote=True)
REPO_DOWNLOAD_URL_HTML = escape(REPO_DOWNLOAD_URL, quote=True)
REPO_BLOCK_ATTR_HTML = "" if REPO_DOWNLOAD_URL else ' style="display:none;"'
CASHAPP_HANDLE_HTML = escape(CASHAPP_HANDLE, quote=True)
VENMO_HANDLE_HTML = escape(VENMO_HANDLE, quote=True)
CASHAPP_LINK_HTML = escape(CASHAPP_LINK, quote=True)
VENMO_LINK_HTML = escape(VENMO_LINK, quote=True)
CASHAPP_QR_HTML = escape(CASHAPP_LINK_QR, quote=True)
VENMO_QR_HTML = escape(VENMO_LINK_QR, quote=True)
SUPPORT_EMAIL_HTML = escape(SUPPORT_EMAIL)
SUPPORT_MAILTO_URL_HTML = escape(SUPPORT_MAILTO_URL, quote=True)
SUPPORT_URL_HTML = escape(SUPPORT_URL, quote=True)
SUPPORT_HELPDESK_URL_HTML = escape(SUPPORT_HELPDESK_URL, quote=True)
SUPPORT_GITHUB_ISSUES_URL_HTML = escape(SUPPORT_GITHUB_ISSUES_URL, quote=True)
SUPPORT_HELPDESK_BLOCK_ATTR_HTML = "" if SUPPORT_HELPDESK_URL else ' style="display:none;"'
SUPPORT_GITHUB_ISSUES_BLOCK_ATTR_HTML = "" if SUPPORT_GITHUB_ISSUES_URL else ' style="display:none;"'
CREDIT_HTML = (
    f'<div class="credit">Built by '
    f'<a href="{escape(CREDIT_URL, quote=True)}" target="_blank" rel="noopener noreferrer">'
    f'{escape(CREDIT_LABEL)}</a> <span class="credit-sep">|</span> '
    f'<a href="/support" data-track-action="support-open">Support</a></div>'
)
HOSTED_OFFER_HTML = (
    f'<div class="hosted-offer">Don\'t have a homelab? '
    f'<a href="{HOSTED_LAUNCH_URL_HTML}" target="_blank" rel="noopener noreferrer">'
    f'Run it on mine for '
    f'<span class="old-price">{escape(HOSTED_OFFER_COMPARE_AT_PRICE)}</span> '
    f'<span class="sale-price">{escape(HOSTED_OFFER_PRICE)}</span> per post for a limited time.</a>'
    f'<span class="hosted-bonus">First {HOSTED_FREE_SIGNUP_LIMIT_COUNT} sign-ups get '
    f'{HOSTED_FREE_POSTS_COUNT} free posts. New hosted users after that start with 0 free posts. '
    f'Regenerations do not count; only clicking Publish uses a post.</span>'
    f'</div>'
    if HOSTED_OFFER_URL
    else ""
)


def build_matomo_tracking_html() -> str:
    matomo_url = os.environ.get("MATOMO_URL", "").strip().rstrip("/")
    matomo_site_id = os.environ.get("MATOMO_SITE_ID", "").strip()
    if not matomo_url or not matomo_site_id:
        return ""
    matomo_url_js = json.dumps(f"{matomo_url}/")
    matomo_site_id_js = json.dumps(matomo_site_id)
    return """
    <script>
        var _paq = window._paq = window._paq || [];
        window.trackAutoListerEvent = window.trackAutoListerEvent || function(category, action, name, value) {
            try {
                if (!category || !action || !window._paq || typeof window._paq.push !== "function") return;
                var event = ["trackEvent", String(category), String(action)];
                if (name !== undefined && name !== null && name !== "") event.push(String(name));
                if (Number.isFinite(Number(value))) event.push(Number(value));
                window._paq.push(event);
            } catch (e) {}
        };
        _paq.push(["trackPageView"]);
        _paq.push(["enableLinkTracking"]);
        (function() {
            var u = __MATOMO_URL__;
            _paq.push(["setTrackerUrl", u + "matomo.php"]);
            _paq.push(["setSiteId", __MATOMO_SITE_ID__]);
            var d = document, g = d.createElement("script"), s = d.getElementsByTagName("script")[0];
            g.async = true;
            g.src = u + "matomo.js";
            s.parentNode.insertBefore(g, s);
        })();
    </script>
""".replace("__MATOMO_URL__", matomo_url_js).replace("__MATOMO_SITE_ID__", matomo_site_id_js)


MATOMO_TRACKING_HTML = build_matomo_tracking_html()


def with_matomo_tracking(html: str) -> str:
    if not MATOMO_TRACKING_HTML:
        return html
    if "</head>" in html:
        return html.replace("</head>", f"{MATOMO_TRACKING_HTML}\n</head>", 1)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {MATOMO_TRACKING_HTML}
</head>
<body>{html}</body>
</html>"""


def tracked_html_response(html: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(with_matomo_tracking(html), status_code=status_code)

# Initialize
app = FastAPI()
session_manager = SessionManager(APP_DIR, data_dir=BASE_DIR, novnc_dir=NOVNC_DIR)
pending_listings: Dict[str, dict] = {}
fill_jobs: Dict[str, dict] = {}
storage_analysis_jobs = set()

# Google AI Client
try:
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
except Exception as e:
    print(f"Warning: Gemini Init failed: {e}")
    client = None

# UI Templates (Inline for simplicity)
MARKETING_HTML = ("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Auto-Lister - AI Facebook Marketplace Assistant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Self-host Auto-Lister to automatically identify, price, and draft Facebook Marketplace listings from your item photos using AI.">
    <meta name="keywords" content="Facebook Marketplace, Auto-Lister, AI listing, automatic listing, self-hosted, homelab, marketplace automation">
    <meta name="theme-color" content="#080d14">
    <meta property="og:title" content="Auto-Lister - AI Facebook Marketplace Assistant">
    <meta property="og:description" content="Turn item photos into researched Facebook Marketplace drafts instantly. Self-host or use our hosted version.">
    <meta property="og:type" content="website">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Outfit:wght@500;700;900&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        html { scroll-behavior: smooth; }
        body {
            font-family: 'Inter', system-ui, sans-serif;
            background: #080d14;
            color: #e5e7eb;
            min-height: 100dvh;
            overflow-x: hidden;
            -webkit-font-smoothing: antialiased;
        }
        a { color: inherit; transition: color 0.2s; }
        .topbar {
            position: fixed; inset: 0 0 auto 0; z-index: 20;
            display: flex; justify-content: center;
            background: rgba(8,13,20,0.85);
            backdrop-filter: blur(16px);
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .nav {
            width: calc(100vw - 32px); max-width: 1200px;
            min-height: 72px;
            display: flex; align-items: center; justify-content: space-between; gap: 18px;
        }
        .brand {
            display: inline-flex; align-items: center; gap: 12px;
            color: #f8fafc; text-decoration: none; font-weight: 900;
            font-family: 'Outfit', sans-serif; font-size: 1.3rem; letter-spacing: -0.02em;
        }
        .brand-mark {
            width: 42px; height: 42px; border-radius: 12px;
            display: grid; place-items: center;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            color: white; font-weight: 900; font-size: 1.4rem;
            box-shadow: 0 4px 12px rgba(139,92,246,0.3);
        }
        .nav-links { display: flex; align-items: center; gap: 24px; }
        .nav-links a {
            color: #94a3b8; text-decoration: none; font-weight: 600; font-size: 0.95rem;
        }
        .nav-links a:hover { color: #f8fafc; }
        .nav-cta {
            min-height: 42px; padding: 0 16px; border-radius: 10px;
            background: linear-gradient(135deg, #f8fafc, #e2e8f0); color: #0f172a !important;
            display: inline-flex; align-items: center; justify-content: center; font-weight: 700 !important;
            box-shadow: 0 4px 12px rgba(255,255,255,0.1); transition: transform 0.2s, box-shadow 0.2s;
        }
        .nav-cta:hover { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(255,255,255,0.15); }
        .hero {
            position: relative; min-height: 85dvh; overflow: hidden;
            display: flex; align-items: center;
            padding: 120px 0 80px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .hero::before {
            content: ""; position: absolute; top: -20%; left: -10%; width: 50%; height: 50%;
            background: radial-gradient(circle, rgba(59,130,246,0.15) 0%, transparent 60%);
            pointer-events: none;
        }
        .hero::after {
            content: ""; position: absolute; inset: 0; z-index: 2;
            background: linear-gradient(90deg, rgba(8,13,20,0.98) 0%, rgba(8,13,20,0.9) 45%, rgba(8,13,20,0.3) 75%, rgba(8,13,20,0.1) 100%);
            pointer-events: none;
        }
        .hero-scene {
            position: absolute; inset: 0; z-index: 1;
            background:
                linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px),
                #0a111b;
            background-size: 50px 50px;
            background-position: center center;
        }
        .scene-shell {
            position: absolute; right: 2vw; top: 12vh; width: min(700px, 50vw);
            min-height: 600px;
        }
        .browser {
            position: absolute; right: 0; top: 40px; width: 600px; max-width: 100%;
            border: 1px solid rgba(255,255,255,0.1); border-radius: 12px;
            background: #0f172a;
            box-shadow: 0 40px 100px rgba(0,0,0,0.6), 0 0 0 1px rgba(59,130,246,0.1);
            overflow: hidden;
        }
        .browser-bar {
            height: 42px; display: flex; align-items: center; gap: 8px; padding: 0 16px;
            border-bottom: 1px solid rgba(255,255,255,0.08); background: #0b1120;
        }
        .dot { width: 10px; height: 10px; border-radius: 50%; background: #475569; }
        .dot:nth-child(1) { background: #ef4444; } .dot:nth-child(2) { background: #f59e0b; } .dot:nth-child(3) { background: #22c55e; }
        .address { margin-left: 12px; height: 22px; flex: 1; border-radius: 6px; background: #1e293b; }
        .browser-body { display: grid; grid-template-columns: 180px 1fr; min-height: 420px; }
        .photo-rail { padding: 20px; border-right: 1px solid rgba(255,255,255,0.08); background: #0f172a; }
        .photo-tile {
            height: 110px; border-radius: 10px; margin-bottom: 14px;
            border: 1px solid rgba(255,255,255,0.05);
            background: linear-gradient(145deg, #1e293b, #0f172a);
        }
        .photo-tile:nth-child(1) { background: linear-gradient(135deg, rgba(59,130,246,0.2), transparent 60%), #1e293b; }
        .photo-tile:nth-child(2) { background: linear-gradient(135deg, rgba(16,185,129,0.15), transparent 60%), #1e293b; }
        .photo-tile:nth-child(3) { background: linear-gradient(135deg, rgba(139,92,246,0.15), transparent 60%), #1e293b; }
        .listing-preview { padding: 24px; background: #0b1120; }
        .status-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
        .status-pill {
            display: inline-flex; align-items: center; gap: 8px; height: 30px; padding: 0 12px;
            border-radius: 999px; color: #6ee7b7; background: rgba(16,185,129,0.1);
            border: 1px solid rgba(16,185,129,0.2); font-size: 0.8rem; font-weight: 700;
        }
        .status-pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: #10b981; box-shadow: 0 0 8px #10b981; }
        .price-chip { color: #fcd34d; font-weight: 900; font-size: 1.4rem; font-family: 'Outfit', sans-serif; }
        .title-line { height: 20px; width: 85%; border-radius: 6px; background: #e2e8f0; margin-bottom: 16px; }
        .text-line { height: 12px; border-radius: 6px; background: #334155; margin-bottom: 10px; }
        .text-line.wide { width: 95%; }
        .text-line.mid { width: 80%; }
        .comps { display: grid; gap: 10px; margin: 24px 0; }
        .comp-row {
            display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center;
            min-height: 48px; border: 1px solid rgba(255,255,255,0.06); border-radius: 10px;
            padding: 12px 16px; background: rgba(255,255,255,0.02);
        }
        .comp-name { height: 10px; width: 160px; border-radius: 5px; background: #475569; }
        .comp-price { color: #f8fafc; font-weight: 800; font-size: 0.9rem; }
        .publish-row { display: flex; gap: 12px; margin-top: 24px; }
        .mock-btn { height: 44px; border-radius: 10px; flex: 1; background: linear-gradient(135deg, #3b82f6, #2563eb); }
        .mock-btn.alt { flex: 0.4; background: #1e293b; border: 1px solid rgba(255,255,255,0.1); }
        .phone {
            position: absolute; left: 0; bottom: 0; width: 200px;
            border-radius: 32px; padding: 12px; background: #0f172a;
            border: 1px solid rgba(255,255,255,0.1);
            box-shadow: 0 30px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(59,130,246,0.2);
            z-index: 10;
        }
        .phone-screen {
            min-height: 400px; border-radius: 22px; background: #0b1120; padding: 16px;
            border: 1px solid rgba(255,255,255,0.05); display: flex; flex-direction: column;
        }
        .phone-image { height: 150px; border-radius: 14px; background: linear-gradient(145deg, #1e293b, #0f172a); margin-bottom: 16px; }
        .phone-line { height: 12px; border-radius: 6px; background: #334155; margin-bottom: 10px; }
        .phone-line.short { width: 60%; }
        .phone-action { margin-top: auto; height: 48px; border-radius: 12px; background: linear-gradient(135deg, #10b981, #059669); }
        .container { width: calc(100vw - 32px); max-width: 1200px; margin: 0 auto; position: relative; z-index: 3; }
        .hero-copy { width: min(680px, 100%); min-width: 0; }
        .eyebrow {
            display: inline-flex; align-items: center; gap: 8px; color: #3b82f6; font-size: 0.85rem; font-weight: 800;
            letter-spacing: 0.05em; margin-bottom: 24px; text-transform: uppercase;
            background: rgba(59,130,246,0.1); padding: 6px 14px; border-radius: 999px; border: 1px solid rgba(59,130,246,0.2);
        }
        h1 {
            color: #f8fafc; font-size: clamp(3rem, 5vw, 5rem); line-height: 1.05; letter-spacing: -0.03em;
            font-family: 'Outfit', sans-serif; margin-bottom: 24px; font-weight: 900;
        }
        .lead {
            color: #94a3b8; font-size: 1.15rem; line-height: 1.7;
            max-width: 620px; margin-bottom: 36px;
        }
        .actions { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 36px; }
        .btn {
            min-height: 52px; border-radius: 12px; padding: 0 24px;
            display: inline-flex; align-items: center; justify-content: center;
            text-decoration: none; font-weight: 700; font-size: 1.05rem;
            transition: all 0.2s;
        }
        .btn.primary { background: linear-gradient(135deg, #f8fafc, #e2e8f0); color: #0f172a; box-shadow: 0 4px 14px rgba(255,255,255,0.1); }
        .btn.primary:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(255,255,255,0.2); }
        .btn.secondary { background: rgba(255,255,255,0.05); color: #f8fafc; border: 1px solid rgba(255,255,255,0.1); backdrop-filter: blur(8px); }
        .btn.secondary:hover { background: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.2); }
        .btn.green { background: linear-gradient(135deg, #10b981, #059669); color: #ffffff; box-shadow: 0 4px 14px rgba(16,185,129,0.2); border: none; }
        .btn.green:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(16,185,129,0.3); }
        
        .hero-meta {
            display: flex; gap: 16px; flex-wrap: wrap; color: #94a3b8; font-size: 0.9rem;
        }
        .meta-item {
            display: inline-flex; align-items: center; gap: 8px;
        }
        .meta-item::before { content: "✓"; color: #10b981; font-weight: 900; }
        
        .mobile-showcase { display: none; }
        @media (max-width: 1024px) {
            .hero { min-height: auto; padding: 120px 0 60px; }
            .hero::after { background: rgba(8,13,20,0.85); }
            .scene-shell { opacity: 0.3; right: -20%; }
        }
        @media (max-width: 768px) {
            .topbar { background: rgba(8,13,20,0.95); }
            .nav { min-height: 60px; width: calc(100vw - 24px); gap: 12px; }
            .brand { font-size: 1.05rem; gap: 9px; }
            .brand-mark { width: 36px; height: 36px; border-radius: 10px; font-size: 1.15rem; }
            .nav-cta { min-height: 40px; padding: 0 14px; border-radius: 9px; font-size: 0.9rem !important; white-space: nowrap; }
            .nav-links a:not(.nav-cta) { display: none; }
            .hero { padding: 86px 0 42px; }
            .hero-scene { display: none; }
            .container { width: calc(100% - 64px); }
            .hero-copy, .lead, h1 { max-width: 100%; overflow-wrap: anywhere; }
            .eyebrow { margin-bottom: 16px; font-size: 0.72rem; padding: 6px 11px; }
            h1 { font-size: 2.18rem; margin-bottom: 18px; line-height: 1.04; }
            .lead { font-size: 0.98rem; line-height: 1.55; margin-bottom: 24px; }
            .actions { flex-direction: column; gap: 11px; margin-bottom: 22px; }
            .btn { width: 100%; min-height: 50px; font-size: 0.98rem; }
            .hero-meta { display: grid; grid-template-columns: 1fr; gap: 9px; font-size: 0.82rem; }
            .mobile-showcase { display: block; margin-top: 28px; }
            .mobile-listing {
                padding: 16px; border: 1px solid rgba(255,255,255,0.1);
                border-radius: 16px; background: #0f172a;
                box-shadow: 0 20px 40px rgba(0,0,0,0.4);
                overflow: hidden;
            }
            .mobile-photo {
                aspect-ratio: 16/9; border-radius: 12px; margin-bottom: 16px;
                background: linear-gradient(135deg, rgba(59,130,246,0.2), #1e293b);
                border: 1px solid rgba(255,255,255,0.05);
            }
            .mobile-listing-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
            .mobile-listing-title { color: #f8fafc; font-weight: 800; font-size: 1.1rem; }
            .mobile-listing-price { color: #fcd34d; font-weight: 900; font-size: 1.2rem; }
            .mobile-listing-line { height: 10px; border-radius: 5px; background: #334155; margin-bottom: 10px; }
            .mobile-publish {
                margin-top: 20px; height: 48px; border-radius: 12px; display: grid; place-items: center;
                background: linear-gradient(135deg, #10b981, #059669); color: white; font-weight: 800;
            }
            .section { padding: 56px 0; }
            .section-head { margin-bottom: 28px; }
            h2 { font-size: 1.55rem; line-height: 1.18; overflow-wrap: anywhere; }
            .section-copy { font-size: 1rem; line-height: 1.55; }
            .grid-three { grid-template-columns: 1fr; gap: 14px; }
            .feature { padding: 22px; border-radius: 12px; }
            .setup-layout { gap: 18px; }
            .terminal { border-radius: 10px; }
            pre { padding: 16px; font-size: 0.78rem; line-height: 1.55; }
            .hosted { padding: 22px; border-radius: 12px; }
        }
        @media (max-width: 520px) {
            .hero { padding-top: 82px; }
            .hero-copy, .section-head, .actions, .hero-meta { width: min(100%, 300px); max-width: 300px; }
            h1 { font-size: 1.86rem; }
            .lead { font-size: 0.9rem; }
            h2 { max-width: 300px; }
            .mobile-showcase { display: none; }
        }

        .section {
            padding: 100px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            background: #0b1120;
        }
        .section.alt { background: #080d14; }
        .section-head { max-width: 800px; margin-bottom: 48px; }
        .section-kicker {
            color: #3b82f6; font-size: 0.85rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em;
            margin-bottom: 16px; display: inline-block;
        }
        h2 { color: #f8fafc; font-size: clamp(2rem, 3vw, 2.5rem); line-height: 1.15; letter-spacing: -0.02em; margin-bottom: 20px; font-family: 'Outfit', sans-serif; font-weight: 800; }
        .section-copy { color: #94a3b8; font-size: 1.1rem; line-height: 1.7; }
        
        .grid-three { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 24px; }
        .feature {
            border: 1px solid rgba(255,255,255,0.08); border-radius: 16px;
            padding: 32px; background: #0f172a; transition: transform 0.2s, box-shadow 0.2s;
        }
        .feature:hover { transform: translateY(-5px); box-shadow: 0 12px 30px rgba(0,0,0,0.3); border-color: rgba(255,255,255,0.15); }
        .feature-icon { width: 48px; height: 48px; border-radius: 12px; background: rgba(59,130,246,0.1); color: #60a5fa; display: grid; place-items: center; font-size: 1.5rem; margin-bottom: 24px; border: 1px solid rgba(59,130,246,0.2); }
        .feature-label { color: #f8fafc; font-weight: 800; font-size: 1.25rem; margin-bottom: 12px; font-family: 'Outfit', sans-serif; }
        .feature p { color: #94a3b8; line-height: 1.6; font-size: 1rem; }
        
        .setup-layout { display: grid; grid-template-columns: minmax(0, 1fr) 400px; gap: 32px; align-items: start; }
        @media (max-width: 900px) { .setup-layout { grid-template-columns: 1fr; } }
        
        .terminal {
            background: #05080f; border: 1px solid rgba(255,255,255,0.1); border-radius: 12px;
            overflow: hidden; box-shadow: 0 20px 40px rgba(0,0,0,0.5);
        }
        .terminal-top {
            height: 40px; display: flex; align-items: center; gap: 8px; padding: 0 16px;
            background: #0f172a; border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        pre {
            white-space: pre-wrap; color: #a7f3d0; font: 0.95rem/1.7 'Fira Code', ui-monospace, SFMono-Regular, monospace;
            padding: 24px; overflow: auto; margin: 0;
        }
        .hosted {
            border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; padding: 32px;
            background: linear-gradient(145deg, #0f172a, #080d14); position: relative; overflow: hidden;
        }
        .hosted::before {
            content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 4px;
            background: linear-gradient(90deg, #3b82f6, #10b981);
        }
        .hosted h3 { color: #f8fafc; font-size: 1.5rem; margin-bottom: 16px; font-family: 'Outfit', sans-serif; }
        .hosted p { color: #94a3b8; line-height: 1.6; font-size: 1rem; margin-bottom: 24px; }
        .old-price { color: #64748b; text-decoration: line-through; margin-right: 8px; }
        .sale-price { color: #fcd34d; font-weight: 900; font-size: 1.4rem; }
        .bonus {
            color: #cbd5e1; font-size: 0.9rem; line-height: 1.6;
            border-top: 1px solid rgba(255,255,255,0.08); padding-top: 20px; margin-top: 24px;
            display: flex; gap: 12px; align-items: flex-start;
        }
        .bonus::before { content: "🎁"; font-size: 1.2rem; }
        
        .footer-credit { color: #64748b; font-size: 0.9rem; padding: 40px 0; background: #05080f; text-align: center; border-top: 1px solid rgba(255,255,255,0.05); }
        .footer-credit a { color: #94a3b8; text-decoration: none; font-weight: 600; }
        .footer-credit a:hover { color: #f8fafc; }
    </style>
</head>
<body>
    <header class="topbar">
        <nav class="nav">
            <a class="brand" href="/"><span class="brand-mark">⚡</span><span>Auto-Lister</span></a>
            <div class="nav-links">
                <a href="#workflow">Workflow</a>
                <a href="__SELF_HOST_GUIDE_LINK__" data-track-action="self-host-guide">Self-host</a>
                <a href="#hosted">Hosted</a>
                <a href="/support" data-track-action="support-open">Support</a>
                <a class="nav-cta" href="__HOSTED_LINK__" data-track-action="hosted-open">Try it out</a>
            </div>
        </nav>
    </header>

    <main>
        <section class="hero">
            <div class="hero-scene" aria-hidden="true">
                <div class="scene-shell">
                    <div class="browser">
                        <div class="browser-bar"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span class="address"></span></div>
                        <div class="browser-body">
                            <div class="photo-rail">
                                <div class="photo-tile"></div>
                                <div class="photo-tile"></div>
                                <div class="photo-tile"></div>
                            </div>
                            <div class="listing-preview">
                                <div class="status-row"><span class="status-pill">AI Priced</span><span class="price-chip">$45</span></div>
                                <div class="title-line"></div>
                                <div class="text-line wide"></div>
                                <div class="text-line wide"></div>
                                <div class="text-line mid"></div>
                                <div class="comps">
                                    <div class="comp-row"><span class="comp-name"></span><span class="comp-price">$48</span></div>
                                    <div class="comp-row"><span class="comp-name"></span><span class="comp-price">$40</span></div>
                                    <div class="comp-row"><span class="comp-name"></span><span class="comp-price">$55</span></div>
                                </div>
                                <div class="publish-row"><span class="mock-btn"></span><span class="mock-btn alt"></span></div>
                            </div>
                        </div>
                    </div>
                    <div class="phone">
                        <div class="phone-screen">
                            <div class="phone-image"></div>
                            <div class="phone-line"></div>
                            <div class="phone-line"></div>
                            <div class="phone-line short"></div>
                            <div class="phone-action"></div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="container">
                <div class="hero-copy">
                    <div class="eyebrow">🚀 The Ultimate Marketplace Tool</div>
                    <h1>Photos in. <br><span style="color: #3b82f6;">Facebook drafts out.</span></h1>
                    <p class="lead">Auto-Lister uses AI to turn your item photos into fully researched Facebook Marketplace drafts. Run it yourself with your own Gemini key, or use our hosted version.</p>
                    <div class="actions">
                        <a class="btn primary" href="__HOSTED_LINK__" data-track-action="hosted-open">Try it out</a>
                        <a class="btn secondary" href="__SELF_HOST_GUIDE_LINK__" data-track-action="self-host-start">Self-host Setup</a>
                    </div>
                    <div class="hero-meta">
                        <span class="meta-item">Docker Compose included</span>
                        <span class="meta-item">Private API Keys</span>
                        <span class="meta-item">Secure Auth</span>
                    </div>
                    <div class="mobile-showcase" aria-hidden="true">
                        <div class="mobile-listing">
                            <div class="mobile-photo"></div>
                            <div class="mobile-listing-head">
                                <span class="mobile-listing-title">Vintage Camera</span>
                                <span class="mobile-listing-price">$45</span>
                            </div>
                            <div class="mobile-listing-line"></div>
                            <div class="mobile-listing-line" style="width: 80%;"></div>
                            <div class="mobile-listing-line" style="width: 60%;"></div>
                            <div class="mobile-publish">Review on Facebook</div>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <section class="section" id="workflow">
            <div class="container">
                <div class="section-head">
                    <div class="section-kicker">How It Works</div>
                    <h2>A workflow built for people clearing real stuff out.</h2>
                    <p class="section-copy">Upload photos from your desktop or phone. Let Gemini identify the item and find pricing context. Review the generated draft before Facebook ever sees a publish click.</p>
                </div>
                <div class="grid-three">
                    <div class="feature">
                        <div class="feature-icon">🔍</div>
                        <div class="feature-label">Identify</div>
                        <p>Automatically detects the product, model clues, condition signals, and likely category directly from the uploaded photos.</p>
                    </div>
                    <div class="feature">
                        <div class="feature-icon">💰</div>
                        <div class="feature-label">Price</div>
                        <p>Uses current retail context and used comparables to suggest a practical, competitive price instead of a random guess.</p>
                    </div>
                    <div class="feature">
                        <div class="feature-icon">📝</div>
                        <div class="feature-label">Draft</div>
                        <p>Fills the Facebook Marketplace form in a secure, reviewable browser session, including your photos, title, price, and copy.</p>
                    </div>
                </div>
            </div>
        </section>

        <section class="section alt" id="self-host">
            <div class="container">
                <div class="section-head">
                    <div class="section-kicker">Deployment</div>
                    <h2>Bring your own server, keys, and customers.</h2>
                    <p class="section-copy">The self-host package ships with Docker, local first-admin setup, environment-based secrets, and optional notes for adding your own Stripe checkout if you want to sell access.</p>
                    <p class="section-copy" style="margin-top: 24px;"><a class="btn secondary" href="__SELF_HOST_GUIDE_LINK__" data-track-action="self-host-guide">View Setup Guide</a></p>
                </div>
                <div class="setup-layout">
                    <div class="terminal">
                        <div class="terminal-top"><span class="dot" style="background:#ef4444"></span><span class="dot" style="background:#f59e0b"></span><span class="dot" style="background:#22c55e"></span></div>
                        <pre><code># Quick start with Docker
git clone __REPO_LINK__
cd auto-lister
cp .env.example .env
nano .env # Add your Gemini API Key
docker compose up -d --build

# Open setup in your browser
open https://your-domain.example.com/setup</code></pre>
                    </div>
                    <aside class="hosted" id="hosted">
                        <h3>No homelab yet?</h3>
                        <p>Use the hosted MRB copy instead. It is priced per published post, so AI regenerations and edits do not burn credits.</p>
                        <p>Limited-time launch price: <br><span class="old-price">""" + escape(HOSTED_OFFER_COMPARE_AT_PRICE) + """</span> <span class="sale-price">""" + escape(HOSTED_OFFER_PRICE) + """</span> / post.</p>
                        <a class="btn green" href="__HOSTED_LINK__" data-track-action="hosted-open" style="width: 100%;">Use Hosted App</a>
                        <div class="bonus">First """ + str(HOSTED_FREE_SIGNUP_LIMIT_COUNT) + """ sign-ups get """ + str(HOSTED_FREE_POSTS_COUNT) + """ free posts. New hosted users after that start with 0 free posts. Only clicking Publish uses a post.</div>
                    </aside>
                </div>
            </div>
        </section>

        <section class="footer-credit">
            <div class="container">""" + CREDIT_HTML + """</div>
        </section>
    </main>
    <script>
        (function() {
            function trackMarketingAction(action) {
                try {
                    if (window._paq && typeof window._paq.push === "function") {
                        window._paq.push(["trackEvent", "marketing", "click", action]);
                    }
                } catch (e) {}
            }

            document.addEventListener("DOMContentLoaded", function() {
                document.querySelectorAll("[data-track-action]").forEach(function(elem) {
                    elem.addEventListener("click", function() {
                        var action = elem.getAttribute("data-track-action");
                        if (action) {
                            trackMarketingAction(action);
                        }
                    });
                });
            });
        })();
    </script>
</body>
</html>
""").replace("__SELF_HOST_GUIDE_LINK__", "/self-host").replace("__HOSTED_LINK__", HOSTED_LAUNCH_URL_HTML).replace("__REPO_LINK__", REPO_DOWNLOAD_URL_HTML)

LOGIN_HTML = """
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <title>Auto-Lister — Facebook Marketplace Made Easy</title>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0\">
    <meta name=\"description\" content=\"Auto-Lister uses AI to identify your items, price them, and post them to Facebook Marketplace automatically.\">
    <meta name=\"theme-color\" content=\"#080d14\">
    <meta property=\"og:title\" content=\"Auto-Lister\">
    <meta property=\"og:description\" content=\"Snap a photo. AI identifies and prices it. One tap to post on Facebook Marketplace.\">
    <meta property=\"og:type\" content=\"website\">
    <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
    <link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap\" rel=\"stylesheet\">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', system-ui, sans-serif;
            background: #080d14;
            background-image: radial-gradient(ellipse 80% 50% at 50% -10%, rgba(59,130,246,0.14) 0%, transparent 65%);
            color: #e2e8f0;
            min-height: 100dvh;
            display: flex; flex-direction: column;
            align-items: center; justify-content: center;
            padding: 1.5rem 1rem;
            -webkit-font-smoothing: antialiased;
        }
        .brand-wrap {
            display: flex; flex-direction: column; align-items: center; gap: 0.6rem;
            margin-bottom: 1.75rem;
        }
        .brand-mark {
            width: 54px; height: 54px; border-radius: 16px;
            background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
            display: flex; align-items: center; justify-content: center; font-size: 1.5rem;
            box-shadow: 0 0 0 1px rgba(139,92,246,0.35), 0 8px 28px rgba(59,130,246,0.22);
        }
        .brand-name { font-size: 1.55rem; font-weight: 900; letter-spacing: -0.03em; color: #f1f5f9; }
        .brand-tag  { font-size: 0.8rem; color: #475569; font-weight: 500; margin-top: 1px; }
        .card {
            width: 100%; max-width: 420px;
            background: rgba(15,25,35,0.98);
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 22px; padding: 2rem;
            box-shadow: 0 32px 72px rgba(0,0,0,0.55), 0 0 0 1px rgba(59,130,246,0.05);
        }
        .credit {
            max-width: 420px; text-align: center; line-height: 1.45;
            color: #64748b; font-size: 0.8rem;
        }
        .credit { margin-top: 0.65rem; }
        .credit a { color: #93c5fd; text-decoration: none; font-weight: 700; }
        .credit a:hover { color: #bfdbfe; text-decoration: underline; }
        .features {
            display: flex; flex-direction: column; gap: 0.55rem;
            margin-bottom: 1.75rem;
        }
        .feat-row { display: flex; align-items: center; gap: 0.65rem; font-size: 0.83rem; color: #64748b; }
        .feat-icon {
            width: 22px; height: 22px; border-radius: 6px; flex-shrink: 0;
            display: flex; align-items: center; justify-content: center; font-size: 0.72rem;
            background: rgba(59,130,246,0.12); border: 1px solid rgba(59,130,246,0.2); color: #60a5fa;
            font-weight: 800;
        }
        .tabs {
            display: flex; background: rgba(0,0,0,0.3); padding: 4px; border-radius: 12px;
            margin-bottom: 1.5rem;
        }
        .tab {
            flex: 1; padding: 10px; border-radius: 9px; border: none; background: transparent;
            color: #64748b; cursor: pointer; font-weight: 600; font-size: 0.88rem;
            transition: all 0.18s; font-family: inherit;
        }
        .tab.active {
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white; box-shadow: 0 4px 12px rgba(59,130,246,0.28);
        }
        .input-group { margin-bottom: 1rem; }
        label { display: block; margin-bottom: 0.4rem; font-size: 0.78rem; color: #94a3b8; font-weight: 600; letter-spacing: 0.01em; }
        input[type=email], input[type=password], input[type=text] {
            width: 100%; padding: 13px 15px; border-radius: 10px;
            background: rgba(0,0,0,0.28); border: 1px solid rgba(255,255,255,0.09);
            color: #f1f5f9; font-size: 0.95rem; outline: none;
            transition: border-color 0.18s, box-shadow 0.18s;
            -webkit-appearance: none; font-family: inherit;
        }
        input:focus { border-color: rgba(59,130,246,0.55); box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }
        input::placeholder { color: #334155; }
        .btn-primary {
            width: 100%; background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white; border: none; padding: 14px; border-radius: 12px;
            font-weight: 700; cursor: pointer; font-size: 0.95rem;
            transition: opacity 0.15s, transform 0.1s, box-shadow 0.18s;
            margin-top: 0.35rem; font-family: inherit; letter-spacing: -0.01em;
            box-shadow: 0 4px 16px rgba(59,130,246,0.22);
        }
        .btn-primary:hover { box-shadow: 0 6px 22px rgba(59,130,246,0.35); opacity: 0.95; }
        .btn-primary:active { transform: scale(0.98); opacity: 0.88; }
        .btn-primary:disabled { opacity: 0.5; pointer-events: none; }
        .divider {
            display: flex; align-items: center; text-align: center;
            margin: 1.4rem 0; color: #1e293b; font-size: 0.72rem; font-weight: 700;
            text-transform: uppercase; letter-spacing: 0.08em; color: #334155;
        }
        .divider::before, .divider::after { content: ''; flex: 1; border-bottom: 1px solid #1e293b; }
        .divider::before { margin-right: 0.75rem; }
        .divider::after  { margin-left:  0.75rem; }
        .btn-google {
            background: rgba(255,255,255,0.05); color: #cbd5e1;
            border: 1px solid rgba(255,255,255,0.1);
            padding: 13px; border-radius: 12px; font-weight: 600; cursor: pointer;
            display: flex; align-items: center; justify-content: center; gap: 10px;
            width: 100%; font-size: 0.9rem; transition: background 0.15s, border-color 0.15s;
            font-family: inherit;
        }
        .btn-google:hover { background: rgba(255,255,255,0.09); border-color: rgba(255,255,255,0.16); }
        .btn-google img { height: 17px; border-radius: 2px; }
        #error-msg {
            color: #fca5a5; background: rgba(239,68,68,0.08); padding: 11px 14px;
            border-radius: 10px; margin-bottom: 1.25rem; font-size: 0.84rem;
            display: none; border: 1px solid rgba(239,68,68,0.2); line-height: 1.45;
        }
        @media (max-width: 520px) {
            body {
                justify-content: flex-start;
                padding: max(16px, env(safe-area-inset-top)) 14px max(18px, env(safe-area-inset-bottom));
            }
            .brand-wrap {
                margin: 0.35rem 0 1.15rem;
                gap: 0.45rem;
            }
            .brand-mark {
                width: 46px; height: 46px; border-radius: 14px; font-size: 1.25rem;
            }
            .brand-name { font-size: 1.32rem; }
            .brand-tag { font-size: 0.74rem; text-align: center; }
            .card {
                max-width: none;
                padding: 1.2rem;
                border-radius: 18px;
            }
            .features {
                gap: 0.5rem;
                margin-bottom: 1.1rem;
            }
            .feat-row { font-size: 0.78rem; line-height: 1.35; align-items: flex-start; }
            .tabs { margin-bottom: 1rem; }
            .tab { padding: 9px; font-size: 0.82rem; }
            input[type=email], input[type=password], input[type=text] {
                min-height: 48px;
                font-size: 1rem;
            }
            .btn-primary, .btn-google {
                min-height: 50px;
                font-size: 0.95rem;
            }
        }
    </style>
</head>
<body>
    <div class=\"brand-wrap\">
        <div class=\"brand-mark\">⚡</div>
        <div class=\"brand-name\">Auto-Lister</div>
        <div class=\"brand-tag\">Facebook Marketplace · Powered by AI</div>
    </div>
    <div class=\"card\">
        <div class=\"features\" id=\"feature-list\">
            <div class=\"feat-row\"><div class=\"feat-icon\">📷</div>Snap a photo or pick from your gallery</div>
            <div class=\"feat-row\"><div class=\"feat-icon\">🤖</div>AI identifies the item and sets a fair price</div>
            <div class=\"feat-row\"><div class=\"feat-icon\">🚀</div>One tap fills the entire Facebook listing form</div>
        </div>
        <div id=\"error-msg\"></div>
        <div class=\"tabs\">
            <button class=\"tab active\" id=\"login-tab\" onclick=\"switchTab('login')\">Sign In</button>
            <button class=\"tab\" id=\"signup-tab\" onclick=\"switchTab('signup')\">Sign Up</button>
        </div>
        <div id=\"login-fields\">
            <div class=\"input-group\">
                <label>Email Address</label>
                <input type=\"email\" id=\"email\" placeholder=\"you@example.com\" autocomplete=\"email\">
            </div>
            <div class=\"input-group\">
                <label>Password</label>
                <input type=\"password\" id=\"password\" placeholder=\"••••••••\" autocomplete=\"current-password\">
            </div>
            <button onclick=\"handleLogin()\" class=\"btn-primary\" id=\"login-btn\">Sign In</button>
        </div>
        <div id=\"signup-fields\" style=\"display:none\">
            <div class=\"input-group\">
                <label>Full Name</label>
                <input type=\"text\" id=\"name\" placeholder=\"John Doe\" autocomplete=\"name\">
            </div>
            <div class=\"input-group\">
                <label>Email Address</label>
                <input type=\"email\" id=\"s-email\" placeholder=\"you@example.com\" autocomplete=\"email\">
            </div>
            <div class=\"input-group\">
                <label>Password</label>
                <input type=\"password\" id=\"s-password\" placeholder=\"Min. 8 characters\" autocomplete=\"new-password\">
            </div>
            <div class=\"input-group\">
                <label>Confirm Password</label>
                <input type=\"password\" id=\"s-password-conf\" placeholder=\"Repeat password\" autocomplete=\"new-password\">
            </div>
            <button onclick=\"handleSignup()\" class=\"btn-primary\" id=\"signup-btn\">Create Account</button>
        </div>
        <button onclick=\"handleOidc()\" class=\"btn-google\" id=\"oidc-btn\">Continue</button>
    </div>
    """ + CREDIT_HTML + """
    <script>
        const AUTH_PROVIDER = """ + AUTH_PROVIDER_JS + """;
        const OIDC_PROVIDER_LABEL = """ + OIDC_PROVIDER_LABEL_JS + """;

        function trackAuthAction(action, name) {
            if (window.trackAutoListerEvent) window.trackAutoListerEvent('auth', action, name || AUTH_PROVIDER);
        }

        async function localFetch(path, body) {
            const res = await fetch(path, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body),
            });
            return res.json();
        }

        function switchTab(mode) {
            if (AUTH_PROVIDER !== 'local') return;
            document.getElementById('login-fields').style.display = mode === 'login' ? 'block' : 'none';
            document.getElementById('signup-fields').style.display = mode === 'signup' ? 'block' : 'none';
            document.getElementById('login-tab').className = mode === 'login' ? 'tab active' : 'tab';
            document.getElementById('signup-tab').className = mode === 'signup' ? 'tab active' : 'tab';
            document.getElementById('error-msg').style.display = 'none';
            document.getElementById('feature-list').style.display = mode === 'login' ? 'flex' : 'none';
        }

        async function handleLogin() {
            if (AUTH_PROVIDER !== 'local') {
                handleOidc();
                return;
            }
            const email = document.getElementById('email').value;
            const pass = document.getElementById('password').value;
            const btn = document.getElementById('login-btn');
            btn.innerText = 'Signing in\u2026'; btn.disabled = true;
            try {
                trackAuthAction('login-start', 'local');
                const result = await localFetch('/api/auth/login', {email, password: pass});
                if (!result.success) throw new Error(result.error || 'Sign in failed.');
                trackAuthAction('login-success', 'local');
                window.location.href = '/dashboard';
            } catch (err) {
                trackAuthAction('login-error', 'local');
                showError(err.message);
                btn.innerText = 'Sign In'; btn.disabled = false;
            }
        }

        async function handleSignup() {
            const name = document.getElementById('name').value;
            const email = document.getElementById('s-email').value;
            const pass = document.getElementById('s-password').value;
            const conf = document.getElementById('s-password-conf').value;
            if (pass !== conf) { showError('Passwords do not match.'); return; }
            const btn = document.getElementById('signup-btn');
            btn.innerText = 'Creating account\u2026'; btn.disabled = true;
            try {
                trackAuthAction('signup-blocked', 'local');
                throw new Error('Self-hosted local auth only allows setup of the first admin.');
            } catch (err) {
                showError(err.message);
                btn.innerText = 'Create Account'; btn.disabled = false;
            }
        }

        function handleOidc() {
            trackAuthAction('oidc-start', OIDC_PROVIDER_LABEL || 'oidc');
            window.location.href = '/api/auth/login';
        }

        function showError(msg) {
            const el = document.getElementById('error-msg');
            el.innerText = msg; el.style.display = 'block';
        }

        (async () => {
            if (AUTH_PROVIDER === 'local') {
                const status = await fetch('/api/auth/status').then(r => r.json());
                if (status.setup_required) {
                    window.location.href = '/setup';
                    return;
                }
                if (status.authenticated) {
                    window.location.href = '/dashboard';
                    return;
                }
                document.getElementById('signup-tab').style.display = 'none';
                document.getElementById('signup-fields').style.display = 'none';
                document.getElementById('oidc-btn').style.display = 'none';
                document.getElementById('feature-list').style.display = 'flex';
                return;
            }
            const status = await fetch('/api/auth/status').then(r => r.json()).catch(() => null);
            if (status && status.authenticated) {
                window.location.href = '/dashboard';
                return;
            }
            document.getElementById('login-fields').style.display = 'none';
            document.getElementById('signup-tab').style.display = 'none';
            document.getElementById('signup-fields').style.display = 'none';
            document.getElementById('login-tab').innerText = OIDC_PROVIDER_LABEL;
            document.getElementById('login-tab').className = 'tab active';
            document.getElementById('oidc-btn').innerText = OIDC_PROVIDER_LABEL;
            document.getElementById('feature-list').style.display = 'flex';
    })();
    </script>
</body>
</html>
"""

SELF_HOST_GUIDE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Self-Host Auto-Lister - Setup Guide</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Step-by-step guide to download and self-host Auto-Lister using Docker, complete with local admin setup and secure API configuration.">
    <meta name="keywords" content="Self-host, Auto-Lister setup, Docker compose, homelab, Facebook Marketplace automation, open source listing tool">
    <meta name="theme-color" content="#080d14">
    <meta property="og:title" content="Self-Host Auto-Lister - Setup Guide">
    <meta property="og:description" content="Get the Auto-Lister repository and deploy your own AI-powered marketplace listing tool in minutes.">
    <meta property="og:type" content="website">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Outfit:wght@500;700;900&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', system-ui, sans-serif;
            background: linear-gradient(130deg, #07101b, #0a1624);
            color: #e2e8f0;
            min-height: 100dvh;
            -webkit-font-smoothing: antialiased;
            display: flex; flex-direction: column; align-items: center; padding: 40px 20px;
        }
        .wrap { width: 100%; max-width: 800px; }
        .header { display: flex; align-items: center; gap: 16px; margin-bottom: 32px; }
        .brand-mark {
            width: 48px; height: 48px; border-radius: 12px;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            display: flex; align-items: center; justify-content: center;
            font-size: 1.5rem; color: white; box-shadow: 0 4px 12px rgba(139,92,246,0.3);
        }
        .header h1 { font-family: 'Outfit', sans-serif; font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; color: #f8fafc; line-height: 1.2; }
        .card {
            border: 1px solid rgba(255,255,255,0.08); border-radius: 20px; padding: 40px;
            background: #0f172a; box-shadow: 0 30px 60px rgba(0,0,0,0.5);
        }
        p { color: #94a3b8; line-height: 1.6; margin-bottom: 16px; font-size: 1.05rem; }
        .meta {
            display: flex; flex-wrap: wrap; gap: 12px; margin: 24px 0 32px;
        }
        .tag {
            display: inline-flex; align-items: center; padding: 6px 14px; border-radius: 999px;
            background: rgba(16,185,129,0.1); color: #10b981; font-size: 0.85rem; font-weight: 700;
            border: 1px solid rgba(16,185,129,0.2);
        }
        .tag::before { content: "✓"; margin-right: 6px; }
        h2 { color: #f8fafc; font-size: 1.4rem; font-weight: 800; margin: 32px 0 16px; display: flex; align-items: center; gap: 12px; }
        h2 span.step-num { 
            background: #3b82f6; color: white; width: 28px; height: 28px; border-radius: 50%; 
            display: inline-flex; align-items: center; justify-content: center; font-size: 1rem;
        }
        .step {
            margin: 16px 0; border: 1px solid rgba(255,255,255,0.05); border-radius: 12px;
            padding: 24px; background: #1e293b; position: relative;
        }
        .step p { font-size: 0.95rem; margin-bottom: 12px; }
        .copy-btn {
            position: absolute; right: 12px; top: 12px; background: rgba(255,255,255,0.1);
            border: none; color: #e2e8f0; padding: 6px 12px; border-radius: 6px; font-size: 0.8rem;
            cursor: pointer; font-weight: 600; transition: background 0.2s;
        }
        .copy-btn:hover { background: rgba(255,255,255,0.2); }
        pre {
            white-space: pre-wrap; background: #0b1120; border: 1px solid rgba(255,255,255,0.05);
            border-radius: 8px; padding: 16px; color: #a7f3d0; font-family: 'Fira Code', monospace;
            font-size: 0.9rem; overflow-x: auto; margin-top: 12px;
        }
        code { color: #fcd34d; }
        a.repo-link {
            color: #60a5fa; text-decoration: none; font-weight: 600;
            background: rgba(59,130,246,0.1); padding: 4px 10px; border-radius: 6px;
        }
        a.repo-link:hover { background: rgba(59,130,246,0.2); }
        
        .support-section { margin-top: 48px; border-top: 1px solid rgba(255,255,255,0.08); padding-top: 32px; }
        .support-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 20px; margin-top: 20px; }
        .support-block {
            border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px;
            background: #1e293b; text-align: center; transition: transform 0.2s;
        }
        .support-block:hover { transform: translateY(-3px); border-color: rgba(59,130,246,0.3); }
        .support-block h3 { color: #f8fafc; font-size: 1.1rem; margin-bottom: 16px; font-weight: 800; }
        .support-block p { color: #94a3b8; font-size: 0.92rem; line-height: 1.55; margin-bottom: 16px; }
        .support-block img { width: 100%; max-width: 180px; background: white; border-radius: 12px; padding: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.2); margin-bottom: 16px; }
        .support-block .handle { display: inline-block; font-size: 1rem; color: #60a5fa; font-weight: 700; text-decoration: none; }
        .support-block .support-btn {
            display: inline-flex; align-items: center; justify-content: center;
            min-height: 40px; padding: 0 16px; border-radius: 10px;
            background: rgba(59,130,246,0.14); color: #bfdbfe;
            border: 1px solid rgba(59,130,246,0.24); font-weight: 800;
            text-decoration: none;
        }
        .support-block .support-btn:hover { background: rgba(59,130,246,0.22); }
        
        .back {
            display: inline-flex; align-items: center; gap: 8px; margin-top: 32px;
            text-decoration: none; background: rgba(255,255,255,0.05); color: #f8fafc;
            border-radius: 10px; padding: 12px 20px; font-weight: 700;
            border: 1px solid rgba(255,255,255,0.1); transition: background 0.2s;
        }
        .back:hover { background: rgba(255,255,255,0.1); }
        .footer { color: #64748b; margin-top: 32px; font-size: 0.85rem; text-align: center; }
        
        @media (max-width: 600px) {
            .card { padding: 24px; }
            .header h1 { font-size: 1.8rem; }
        }
    </style>
    <script>
        function copyCode(btn, preId) {
            const pre = document.getElementById(preId);
            navigator.clipboard.writeText(pre.innerText).then(() => {
                const originalText = btn.innerText;
                btn.innerText = 'Copied!';
                if (window.trackAutoListerEvent) window.trackAutoListerEvent('self-host', 'copy-command', preId);
                setTimeout(() => btn.innerText = originalText, 2000);
            });
        }
    </script>
</head>
<body>
    <main class="wrap">
        <div class="header">
            <div class="brand-mark">⚡</div>
            <h1>Self-host Auto-Lister</h1>
        </div>
        <div class="card">
            <p>Deploy your own instance of Auto-Lister on your homelab or VPS. You control the infrastructure, the users, and the API keys.</p>
            <div class="meta">
                <span class="tag">Private API Keys</span>
                <span class="tag">Docker Compose included</span>
                <span class="tag">Local Admin Setup</span>
            </div>

            <h2><span class="step-num">1</span> Download Release</h2>
            <p>Download the current self-host package from the public release link below.</p>
            <p>Package URL: <a href="__PACKAGE_LINK__" class="repo-link" data-track-action="package-download" target="_blank" rel="noopener noreferrer">__PACKAGE_LINK__</a></p>

            <div class="step">
                <button class="copy-btn" onclick="copyCode(this, 'code-step-1')">Copy</button>
                <p><strong>Install from package:</strong></p>
                <pre id="code-step-1">curl -L -o auto-lister-self-host.tar.gz __PACKAGE_LINK__
mkdir -p auto-lister
tar -xzf auto-lister-self-host.tar.gz -C auto-lister --strip-components=1
cd auto-lister</pre>
            </div>
            <div class="step"__REPO_BLOCK_ATTR__>
                <button class="copy-btn" onclick="copyCode(this, 'code-step-1b')">Copy</button>
                <p><strong>Optional Git clone:</strong></p>
                <pre id="code-step-1b">git clone __REPO_LINK__ auto-lister
cd auto-lister</pre>
            </div>

            <h2><span class="step-num">2</span> Configure Environment</h2>
            <div class="step">
                <button class="copy-btn" onclick="copyCode(this, 'code-step-2')">Copy</button>
                <p>Copy the example environment file and add your Gemini API Key.</p>
                <pre id="code-step-2">cp .env.example .env
nano .env</pre>
            </div>

            <h2><span class="step-num">3</span> Deploy & Setup</h2>
            <div class="step">
                <button class="copy-btn" onclick="copyCode(this, 'code-step-3')">Copy</button>
                <p>Start the Docker stack. Once running, navigate to the setup page in your browser to create your admin account.</p>
                <pre id="code-step-3">docker compose up -d --build

# First launch setup
open https://your-domain-or-ip/setup</pre>
            </div>

            <div class="support-section">
                <h2>Need Help?</h2>
                <p>Open a support request when installation, login, browser sessions, AI analysis, or Facebook fill fails.</p>
                <div class="support-grid">
                    <div class="support-block">
                        <h3>Email</h3>
                        <p>Include what happened, the page or action, account email, and any screenshots or logs.</p>
                        <a href="__SUPPORT_MAILTO_LINK__" class="support-btn" data-track-action="support-email">__SUPPORT_EMAIL__</a>
                    </div>
                    <div class="support-block"__SUPPORT_HELPDESK_BLOCK_ATTR__>
                        <h3>Helpdesk</h3>
                        <p>Use the helpdesk for hosted account, billing, and production support requests.</p>
                        <a href="__SUPPORT_HELPDESK_LINK__" class="support-btn" data-track-action="support-helpdesk" target="_blank" rel="noopener noreferrer">Open Helpdesk</a>
                    </div>
                    <div class="support-block"__SUPPORT_GITHUB_ISSUES_BLOCK_ATTR__>
                        <h3>GitHub Issues</h3>
                        <p>Report self-host bugs with your version, deployment type, and sanitized logs.</p>
                        <a href="__SUPPORT_GITHUB_ISSUES_LINK__" class="support-btn" data-track-action="support-github" target="_blank" rel="noopener noreferrer">Open Issues</a>
                    </div>
                </div>
            </div>

            <div class="support-section">
                <h2>Support the Developer</h2>
                <p>If you find this self-hosted tool valuable, consider sending a tip to support future updates.</p>
                <div class="support-grid">
                    <div class="support-block">
                        <h3>Cash App</h3>
                        <a href="__CASHAPP_LINK__" target="_blank" rel="noopener noreferrer">
                            <img src="__CASHAPP_QR__" alt="Cash App QR Code">
                        </a>
                        <br>
                        <a href="__CASHAPP_LINK__" class="handle" target="_blank" rel="noopener noreferrer">__CASHAPP_HANDLE__</a>
                    </div>
                    <div class="support-block">
                        <h3>Venmo</h3>
                        <a href="__VENMO_LINK__" target="_blank" rel="noopener noreferrer">
                            <img src="__VENMO_QR__" alt="Venmo QR Code">
                        </a>
                        <br>
                        <a href="__VENMO_LINK__" class="handle" target="_blank" rel="noopener noreferrer">__VENMO_HANDLE__</a>
                    </div>
                </div>
            </div>

            <a class="back" href="/">← Back to Product Page</a>
        </div>
        <div class="footer">Optional: Override <code>AUTO_MARKETPLACE_PACKAGE_DOWNLOAD_URL</code> or <code>AUTO_MARKETPLACE_PUBLIC_REPO_URL</code> in your environment to update this page.</div>
    </main>
    <script>
        (function() {
            function trackGuideAction(action) {
                try {
                    if (window._paq && typeof window._paq.push === "function") {
                        window._paq.push(["trackEvent", "marketing", "click", action]);
                    }
                } catch (e) {}
            }

            document.addEventListener("DOMContentLoaded", function() {
                document.querySelectorAll("[data-track-action]").forEach(function(elem) {
                    elem.addEventListener("click", function() {
                        var action = elem.getAttribute("data-track-action");
                        if (action) {
                            trackGuideAction(action);
                        }
                    });
                });
            });
        })();
    </script>
</body>
</html>
""".replace("__PACKAGE_LINK__", PACKAGE_DOWNLOAD_URL_HTML).replace(
    "__REPO_LINK__", REPO_DOWNLOAD_URL_HTML
).replace("__REPO_BLOCK_ATTR__", REPO_BLOCK_ATTR_HTML).replace(
    "__CASHAPP_LINK__", CASHAPP_LINK_HTML
).replace(
    "__CASHAPP_QR__", CASHAPP_QR_HTML
).replace("__CASHAPP_HANDLE__", CASHAPP_HANDLE_HTML).replace(
    "__VENMO_LINK__", VENMO_LINK_HTML
).replace("__VENMO_QR__", VENMO_QR_HTML).replace("__VENMO_HANDLE__", VENMO_HANDLE_HTML).replace(
    "__SUPPORT_MAILTO_LINK__", SUPPORT_MAILTO_URL_HTML
).replace("__SUPPORT_EMAIL__", SUPPORT_EMAIL_HTML).replace(
    "__SUPPORT_HELPDESK_LINK__", SUPPORT_HELPDESK_URL_HTML
).replace("__SUPPORT_HELPDESK_BLOCK_ATTR__", SUPPORT_HELPDESK_BLOCK_ATTR_HTML).replace(
    "__SUPPORT_GITHUB_ISSUES_LINK__", SUPPORT_GITHUB_ISSUES_URL_HTML
).replace("__SUPPORT_GITHUB_ISSUES_BLOCK_ATTR__", SUPPORT_GITHUB_ISSUES_BLOCK_ATTR_HTML)

SUPPORT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Auto-Lister Support</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Contact Auto-Lister support for hosted, billing, installation, browser, AI analysis, and Facebook fill issues.">
    <meta name="theme-color" content="#080d14">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Outfit:wght@500;700;900&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            min-height: 100dvh; font-family: 'Inter', system-ui, sans-serif;
            background: #080d14; color: #e2e8f0;
            -webkit-font-smoothing: antialiased;
        }
        a { color: inherit; }
        .topbar {
            position: sticky; top: 0; z-index: 10;
            background: rgba(8,13,20,0.9); backdrop-filter: blur(16px);
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .nav {
            width: min(1120px, calc(100% - 32px)); min-height: 68px; margin: 0 auto;
            display: flex; align-items: center; justify-content: space-between; gap: 18px;
        }
        .brand {
            display: inline-flex; align-items: center; gap: 12px;
            color: #f8fafc; text-decoration: none; font-weight: 900;
            font-family: 'Outfit', sans-serif; font-size: 1.2rem;
        }
        .brand-mark {
            width: 38px; height: 38px; border-radius: 10px;
            display: grid; place-items: center;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            color: #fff; font-size: 0.82rem; font-weight: 900;
        }
        .nav-links { display: flex; align-items: center; gap: 18px; }
        .nav-links a { color: #94a3b8; font-weight: 700; text-decoration: none; font-size: 0.92rem; }
        .nav-links a:hover { color: #f8fafc; }
        main { width: min(1120px, calc(100% - 32px)); margin: 0 auto; padding: 64px 0; }
        .hero { max-width: 760px; margin-bottom: 36px; }
        .kicker { color: #60a5fa; font-size: 0.78rem; font-weight: 900; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 14px; }
        h1 { color: #f8fafc; font-family: 'Outfit', sans-serif; font-size: clamp(2.2rem, 5vw, 4.2rem); line-height: 1.04; margin-bottom: 18px; }
        .lead { color: #94a3b8; font-size: 1.08rem; line-height: 1.65; max-width: 680px; }
        .actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 26px; }
        .btn {
            display: inline-flex; align-items: center; justify-content: center;
            min-height: 48px; padding: 0 20px; border-radius: 10px;
            border: 1px solid rgba(255,255,255,0.1); text-decoration: none;
            font-weight: 800;
        }
        .btn.primary { background: #f8fafc; color: #0f172a; border-color: #f8fafc; }
        .btn.secondary { background: rgba(255,255,255,0.05); color: #f8fafc; }
        .method-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 18px; margin: 34px 0;
        }
        .method {
            border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;
            background: #0f172a; padding: 24px; min-height: 220px;
            display: flex; flex-direction: column; gap: 14px;
        }
        .method h2 { color: #f8fafc; font-size: 1.1rem; font-weight: 900; }
        .method p { color: #94a3b8; line-height: 1.55; font-size: 0.95rem; }
        .method a { color: #bfdbfe; text-decoration: none; font-weight: 800; margin-top: auto; overflow-wrap: anywhere; }
        .method a:hover { color: #f8fafc; }
        .details {
            border-top: 1px solid rgba(255,255,255,0.08);
            padding-top: 28px; color: #94a3b8; line-height: 1.65;
        }
        .details h2 { color: #f8fafc; font-size: 1.2rem; margin-bottom: 12px; }
        .details ul { margin-left: 20px; display: grid; gap: 8px; }
        .footer { color: #64748b; font-size: 0.9rem; padding: 36px 0; text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }
        .footer a { color: #94a3b8; text-decoration: none; font-weight: 700; }
        @media (max-width: 640px) {
            .nav { min-height: 60px; }
            .nav-links { gap: 12px; }
            .nav-links a:nth-child(2) { display: none; }
            main { padding: 38px 0; }
            .actions { flex-direction: column; }
            .btn { width: 100%; }
        }
    </style>
</head>
<body>
    <header class="topbar">
        <nav class="nav">
            <a class="brand" href="/"><span class="brand-mark">AL</span><span>Auto-Lister</span></a>
            <div class="nav-links">
                <a href="/self-host" data-track-action="self-host-guide">Self-host</a>
                <a href="/api/auth/login" data-track-action="hosted-open">Try it out</a>
            </div>
        </nav>
    </header>
    <main>
        <section class="hero">
            <div class="kicker">Support</div>
            <h1>Get help with Auto-Lister.</h1>
            <p class="lead">Use the quickest support channel for hosted account, billing, installation, browser session, AI analysis, and Facebook fill issues.</p>
            <div class="actions">
                <a class="btn primary" href="__SUPPORT_LINK__" data-track-action="support-primary">Open Support</a>
                <a class="btn secondary" href="__SUPPORT_MAILTO_LINK__" data-track-action="support-email">Email __SUPPORT_EMAIL__</a>
            </div>
        </section>

        <section class="method-grid">
            <article class="method">
                <h2>Email</h2>
                <p>Best for account-specific or private support details.</p>
                <a href="__SUPPORT_MAILTO_LINK__" data-track-action="support-email">__SUPPORT_EMAIL__</a>
            </article>
            <article class="method"__SUPPORT_HELPDESK_BLOCK_ATTR__>
                <h2>Helpdesk</h2>
                <p>Best for hosted app access, billing questions, and production incidents.</p>
                <a href="__SUPPORT_HELPDESK_LINK__" data-track-action="support-helpdesk" target="_blank" rel="noopener noreferrer">Open Helpdesk</a>
            </article>
            <article class="method"__SUPPORT_GITHUB_ISSUES_BLOCK_ATTR__>
                <h2>GitHub Issues</h2>
                <p>Best for self-host bugs, installation problems, and reproducible defects.</p>
                <a href="__SUPPORT_GITHUB_ISSUES_LINK__" data-track-action="support-github" target="_blank" rel="noopener noreferrer">Open Issues</a>
            </article>
        </section>

        <section class="details">
            <h2>Include These Details</h2>
            <ul>
                <li>What happened and what you expected instead.</li>
                <li>The page or action where it failed.</li>
                <li>Your account email, if it is a hosted app issue.</li>
                <li>Browser, deployment type, version, and sanitized logs for self-host issues.</li>
                <li>Screenshots when the browser session or Facebook fill looks wrong.</li>
            </ul>
        </section>
    </main>
    <div class="footer">""" + CREDIT_HTML + """</div>
    <script>
        (function() {
            function trackSupportAction(action) {
                try {
                    if (window.trackAutoListerEvent) {
                        window.trackAutoListerEvent("support", "click", action);
                    } else if (window._paq && typeof window._paq.push === "function") {
                        window._paq.push(["trackEvent", "support", "click", action]);
                    }
                } catch (e) {}
            }
            document.addEventListener("DOMContentLoaded", function() {
                document.querySelectorAll("[data-track-action]").forEach(function(elem) {
                    elem.addEventListener("click", function() {
                        var action = elem.getAttribute("data-track-action");
                        if (action) trackSupportAction(action);
                    });
                });
            });
        })();
    </script>
</body>
</html>
""".replace("__SUPPORT_LINK__", SUPPORT_URL_HTML).replace(
    "__SUPPORT_MAILTO_LINK__", SUPPORT_MAILTO_URL_HTML
).replace("__SUPPORT_EMAIL__", SUPPORT_EMAIL_HTML).replace(
    "__SUPPORT_HELPDESK_LINK__", SUPPORT_HELPDESK_URL_HTML
).replace("__SUPPORT_HELPDESK_BLOCK_ATTR__", SUPPORT_HELPDESK_BLOCK_ATTR_HTML).replace(
    "__SUPPORT_GITHUB_ISSUES_LINK__", SUPPORT_GITHUB_ISSUES_URL_HTML
).replace("__SUPPORT_GITHUB_ISSUES_BLOCK_ATTR__", SUPPORT_GITHUB_ISSUES_BLOCK_ATTR_HTML)

EMBEDDED_VNC_HTML = """
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover\">
    <title>Embedded VNC</title>
    <style>
        :root { --app-vh: 100dvh; }
        html, body {
            margin: 0;
            width: 100%;
            height: var(--app-vh);
            min-height: var(--app-vh);
            overflow: hidden;
            background: #000;
        }
        body {
            position: relative;
        }
        #screen {
            width: 100%;
            height: var(--app-vh);
            overflow: hidden;
            position: relative;
        }
        #status {
            position: absolute;
            top: 12px;
            left: 12px;
            z-index: 10;
            padding: 6px 10px;
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.78);
            color: #cbd5e1;
            font: 600 12px/1.2 Inter, system-ui, sans-serif;
            letter-spacing: 0.01em;
            backdrop-filter: blur(8px);
        }
        #status.hidden {
            display: none;
        }
        #keyboardDock {
            position: fixed;
            left: max(10px, env(safe-area-inset-left));
            right: max(10px, env(safe-area-inset-right));
            bottom: calc(10px + env(safe-area-inset-bottom));
            z-index: 20;
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px;
            border: 1px solid rgba(148, 163, 184, 0.24);
            border-radius: 14px;
            background: rgba(15, 23, 42, 0.88);
            box-shadow: 0 18px 48px rgba(0, 0, 0, 0.45);
            backdrop-filter: blur(12px);
        }
        #keyboardDock.collapsed {
            left: auto;
            right: max(10px, env(safe-area-inset-right));
            top: calc(10px + env(safe-area-inset-top));
            bottom: auto;
            width: auto;
            border: 0;
            background: transparent;
            box-shadow: none;
            backdrop-filter: none;
            padding: 0;
        }
        #keyboardDock.collapsed #keyboardInput,
        #keyboardDock.collapsed #keyboardDoneBtn {
            display: none;
        }
        #keyboardDock.collapsed .keyboard-btn {
            background: rgba(15, 23, 42, 0.88);
            box-shadow: 0 10px 26px rgba(0, 0, 0, 0.36);
        }
        #keyboardInput {
            flex: 1 1 auto;
            min-width: 0;
            height: 42px;
            resize: none;
            border: 1px solid rgba(148, 163, 184, 0.24);
            border-radius: 10px;
            padding: 10px 12px;
            background: rgba(15, 23, 42, 0.96);
            color: #e2e8f0;
            caret-color: #e2e8f0;
            font: 600 16px/1.2 Inter, system-ui, sans-serif;
            outline: none;
        }
        #keyboardInput:focus {
            border-color: rgba(96, 165, 250, 0.7);
        }
        .keyboard-btn {
            height: 42px;
            flex: 0 0 auto;
            border: 1px solid rgba(148, 163, 184, 0.24);
            border-radius: 10px;
            padding: 0 12px;
            background: rgba(30, 41, 59, 0.96);
            color: #cbd5e1;
            font: 800 13px/1 Inter, system-ui, sans-serif;
        }
        .scroll-key {
            min-width: 42px;
            padding: 0 10px;
            font-size: 16px;
        }
        @media (pointer: fine) {
            #keyboardDock { display: none; }
        }
    </style>
    <script type=\"module\">
        import RFB from '/novnc/core/rfb.js';

        const params = new URLSearchParams(window.location.search);
        const path = params.get('path') || 'websockify';
        const host = params.get('host') || window.location.host;
        const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
        const url = `${protocol}://${host}/${path}`;
        let screen = null;
        let status = null;
        let keyboardDock = null;
        let keyboardInput = null;
        let reconnectTimer = null;
        let rfb = null;
        let keyboardInputHandled = false;

        function trackBrowserAction(action) {
            if (window.trackAutoListerEvent) window.trackAutoListerEvent('browser', action, 'embedded-vnc');
        }

        const KEY_BACKSPACE = 0xff08;
        const KEY_TAB = 0xff09;
        const KEY_RETURN = 0xff0d;
        const KEY_PAGE_UP = 0xff55;
        const KEY_PAGE_DOWN = 0xff56;
        const KEY_ARROW_UP = 0xff52;
        const KEY_ARROW_DOWN = 0xff54;

        function keysymForChar(ch) {
            if (ch === '\\n' || ch === '\\r') return KEY_RETURN;
            if (ch === '\\t') return KEY_TAB;
            const codePoint = ch.codePointAt(0);
            if (!Number.isFinite(codePoint)) return null;
            return codePoint <= 0xff ? codePoint : 0x01000000 + codePoint;
        }

        function sendVncKey(keysym) {
            if (!rfb || !keysym) return;
            try { rfb.sendKey(keysym); } catch (e) {}
        }

        function sendVncText(text) {
            if (!text) return;
            for (const ch of text) {
                const keysym = keysymForChar(ch);
                if (keysym) sendVncKey(keysym);
            }
        }

        function focusKeyboardInput() {
            if (!keyboardInput) return;
            if (keyboardDock) keyboardDock.classList.remove('collapsed');
            try {
                keyboardInput.focus({ preventScroll: true });
                keyboardInput.setSelectionRange(keyboardInput.value.length, keyboardInput.value.length);
            } catch (e) {
                keyboardInput.focus();
            }
        }

        function blurKeyboardInput() {
            if (keyboardInput) keyboardInput.blur();
            if (keyboardDock) keyboardDock.classList.add('collapsed');
            if (rfb) {
                try { rfb.focus({ preventScroll: true }); } catch (e) { try { rfb.focus(); } catch (_) {} }
            }
        }

        function sendRemoteScroll(direction) {
            const keysym = direction < 0 ? KEY_PAGE_UP : KEY_PAGE_DOWN;
            sendVncKey(keysym);
            window.setTimeout(() => sendVncKey(direction < 0 ? KEY_ARROW_UP : KEY_ARROW_DOWN), 35);
        }

        function clearKeyboardInputSoon() {
            window.setTimeout(() => {
                if (keyboardInput) keyboardInput.value = '';
                keyboardInputHandled = false;
            }, 0);
        }

        function syncVncViewportHeight() {
            const viewport = window.visualViewport;
            const height = Math.max(
                320,
                Math.round((viewport && viewport.height) || window.innerHeight || document.documentElement.clientHeight || 0)
            );
            document.documentElement.style.setProperty('--app-vh', `${height}px`);
        }

        function setStatus(message, persistent = true) {
            if (!status) return;
            status.textContent = message;
            status.classList.toggle('hidden', !persistent && !message);
        }

        function scheduleReconnect() {
            if (reconnectTimer) return;
            reconnectTimer = window.setTimeout(() => {
                reconnectTimer = null;
                connect();
            }, 1200);
        }

        function connect() {
            setStatus('Connecting…');
            if (rfb) {
                try { rfb.disconnect(); } catch (e) {}
            }
            rfb = new RFB(screen, url);
            rfb.viewOnly = false;
            rfb.scaleViewport = true;
            rfb.resizeSession = false;
            rfb.focusOnClick = true;

            rfb.addEventListener('connect', () => {
                trackBrowserAction('connect');
                setStatus('', false);
            });

            rfb.addEventListener('disconnect', (event) => {
                trackBrowserAction(event.detail.clean ? 'disconnect' : 'disconnect-unexpected');
                setStatus(event.detail.clean ? 'Disconnected' : 'Reconnecting…');
                scheduleReconnect();
            });

            rfb.addEventListener('credentialsrequired', () => {
                setStatus('VNC password required');
            });
        }

        window.addEventListener('load', () => {
            syncVncViewportHeight();
            screen = document.getElementById('screen');
            status = document.getElementById('status');
            keyboardDock = document.getElementById('keyboardDock');
            keyboardInput = document.getElementById('keyboardInput');
            document.getElementById('keyboardFocusBtn')?.addEventListener('click', focusKeyboardInput);
            document.getElementById('keyboardDoneBtn')?.addEventListener('click', blurKeyboardInput);
            document.getElementById('keyboardScrollUpBtn')?.addEventListener('click', () => sendRemoteScroll(-1));
            document.getElementById('keyboardScrollDownBtn')?.addEventListener('click', () => sendRemoteScroll(1));
            screen?.addEventListener('touchend', (event) => {
                const touch = event.changedTouches && event.changedTouches[0];
                const viewportHeight = (window.visualViewport && window.visualViewport.height) || window.innerHeight || 0;
                if (touch && viewportHeight && touch.clientY > viewportHeight - 128) return;
                window.setTimeout(focusKeyboardInput, 80);
            }, { passive: true });
            keyboardInput?.addEventListener('beforeinput', (event) => {
                if (event.inputType === 'insertText' && event.data) {
                    event.preventDefault();
                    keyboardInputHandled = true;
                    sendVncText(event.data);
                    clearKeyboardInputSoon();
                } else if (event.inputType === 'insertLineBreak') {
                    event.preventDefault();
                    keyboardInputHandled = true;
                    sendVncKey(KEY_RETURN);
                    clearKeyboardInputSoon();
                } else if (event.inputType === 'deleteContentBackward') {
                    event.preventDefault();
                    keyboardInputHandled = true;
                    sendVncKey(KEY_BACKSPACE);
                    clearKeyboardInputSoon();
                }
            });
            keyboardInput?.addEventListener('input', () => {
                if (keyboardInputHandled) return;
                sendVncText(keyboardInput.value);
                keyboardInput.value = '';
            });
            keyboardInput?.addEventListener('keydown', (event) => {
                if (event.key === 'Backspace') {
                    event.preventDefault();
                    sendVncKey(KEY_BACKSPACE);
                } else if (event.key === 'Enter') {
                    event.preventDefault();
                    sendVncKey(KEY_RETURN);
                } else if (event.key === 'Tab') {
                    event.preventDefault();
                    sendVncKey(KEY_TAB);
                }
                event.stopPropagation();
            });
            connect();
        }, { once: true });
        window.addEventListener('resize', syncVncViewportHeight, { passive: true });
        window.addEventListener('orientationchange', syncVncViewportHeight, { passive: true });
        if (window.visualViewport) {
            window.visualViewport.addEventListener('resize', syncVncViewportHeight, { passive: true });
            window.visualViewport.addEventListener('scroll', syncVncViewportHeight, { passive: true });
        }
        window.addEventListener('beforeunload', () => {
            if (reconnectTimer) {
                window.clearTimeout(reconnectTimer);
            }
            if (rfb) {
                try { rfb.disconnect(); } catch (e) {}
            }
        });
    </script>
</head>
<body>
    <div id=\"status\">Connecting…</div>
    <div id=\"screen\"></div>
    <div id=\"keyboardDock\" class=\"collapsed\">
        <button id=\"keyboardFocusBtn\" class=\"keyboard-btn\" type=\"button\">Keyboard</button>
        <button id=\"keyboardScrollUpBtn\" class=\"keyboard-btn scroll-key\" type=\"button\" aria-label=\"Scroll up\">↑</button>
        <button id=\"keyboardScrollDownBtn\" class=\"keyboard-btn scroll-key\" type=\"button\" aria-label=\"Scroll down\">↓</button>
        <textarea id=\"keyboardInput\" rows=\"1\" inputmode=\"text\" autocomplete=\"off\" autocorrect=\"off\" autocapitalize=\"none\" spellcheck=\"false\" aria-label=\"VNC keyboard input\"></textarea>
        <button id=\"keyboardDoneBtn\" class=\"keyboard-btn\" type=\"button\">Done</button>
    </div>
</body>
</html>
"""

SETUP_HTML = """
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <title>Set Up Auto-Lister</title>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover\">
    <meta name=\"theme-color\" content=\"#080d14\">
    <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
    <link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap\" rel=\"stylesheet\">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', system-ui, sans-serif;
            background: #080d14;
            color: #e2e8f0;
            min-height: 100dvh;
            display: flex; align-items: center; justify-content: center;
            padding: 1.5rem 1rem;
            -webkit-font-smoothing: antialiased;
        }
        .wrap { width: 100%; max-width: 430px; }
        .brand { text-align: center; margin-bottom: 1.5rem; }
        .brand-mark {
            width: 54px; height: 54px; border-radius: 16px; margin: 0 auto 0.7rem;
            background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
            display: flex; align-items: center; justify-content: center; font-size: 1.5rem;
        }
        h1 { color: #f8fafc; font-size: 1.5rem; letter-spacing: 0; }
        .sub { color: #64748b; font-size: 0.88rem; margin-top: 0.35rem; line-height: 1.45; }
        .card {
            background: rgba(15,25,35,0.98); border: 1px solid rgba(255,255,255,0.07);
            border-radius: 12px; padding: 1.5rem; box-shadow: 0 32px 72px rgba(0,0,0,0.55);
        }
        .input-group { margin-bottom: 1rem; }
        label { display: block; margin-bottom: 0.4rem; font-size: 0.78rem; color: #94a3b8; font-weight: 700; }
        input {
            width: 100%; padding: 13px 15px; border-radius: 10px;
            background: rgba(0,0,0,0.28); border: 1px solid rgba(255,255,255,0.09);
            color: #f1f5f9; font-size: 0.95rem; outline: none; font-family: inherit;
        }
        input:focus { border-color: rgba(59,130,246,0.55); box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }
        button {
            width: 100%; background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white; border: none; padding: 14px; border-radius: 12px;
            font-weight: 800; cursor: pointer; font-size: 0.95rem; font-family: inherit;
        }
        button:disabled { opacity: 0.55; cursor: default; }
        #error-msg {
            color: #fca5a5; background: rgba(239,68,68,0.08); padding: 11px 14px;
            border-radius: 10px; margin-bottom: 1rem; font-size: 0.84rem; display: none;
            border: 1px solid rgba(239,68,68,0.2); line-height: 1.45;
        }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"brand\">
            <div class=\"brand-mark\">⚡</div>
            <h1>Create the first admin</h1>
            <div class=\"sub\">This self-hosted install needs one local admin account before anyone can use it.</div>
        </div>
        <div class=\"card\">
            <div id=\"error-msg\"></div>
            <div class=\"input-group\">
                <label>Name</label>
                <input id=\"name\" type=\"text\" autocomplete=\"name\" placeholder=\"Your name\">
            </div>
            <div class=\"input-group\">
                <label>Email</label>
                <input id=\"email\" type=\"email\" autocomplete=\"email\" placeholder=\"you@example.com\">
            </div>
            <div class=\"input-group\">
                <label>Password</label>
                <input id=\"password\" type=\"password\" autocomplete=\"new-password\" placeholder=\"Min. 8 characters\">
            </div>
            <button id=\"setup-btn\" onclick=\"setupAdmin()\">Create Admin</button>
        </div>
    </div>
    <script>
        function trackSetupAction(action) {
            if (window.trackAutoListerEvent) window.trackAutoListerEvent('auth', action, 'setup');
        }

        async function setupAdmin() {
            const btn = document.getElementById('setup-btn');
            const payload = {
                name: document.getElementById('name').value.trim(),
                email: document.getElementById('email').value.trim(),
                password: document.getElementById('password').value,
            };
            btn.disabled = true;
            btn.innerText = 'Creating admin…';
            try {
                trackSetupAction('setup-start');
                const res = await fetch('/api/auth/setup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
                const result = await res.json();
                if (!result.success) throw new Error(result.error || 'Setup failed.');
                trackSetupAction('setup-success');
                window.location.href = '/dashboard';
            } catch (err) {
                trackSetupAction('setup-error');
                const el = document.getElementById('error-msg');
                el.innerText = err.message;
                el.style.display = 'block';
                btn.disabled = false;
                btn.innerText = 'Create Admin';
            }
        }
        (async () => {
            const res = await fetch('/api/auth/status');
            const status = await res.json();
            if (status.provider !== 'local' || !status.setup_required) window.location.href = '/login';
        })();
    </script>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <title>Auto-Lister</title>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover\">
    <meta name=\"theme-color\" content=\"#080d14\">
    <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
    <link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap\" rel=\"stylesheet\">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --bg:        #080d14;
            --surface:   #0f1923;
            --surface2:  #162032;
            --border:    rgba(255,255,255,0.07);
            --border2:   rgba(255,255,255,0.12);
            --text:      #e2e8f0;
            --muted:     #64748b;
            --accent:    #3b82f6;
            --accent-h:  #2563eb;
            --green:     #10b981;
            --green-h:   #059669;
            --amber:     #f59e0b;
            --red:       #ef4444;
            --radius-sm: 10px;
            --radius:    14px;
            --radius-lg: 20px;
            --safe-top: max(env(safe-area-inset-top), 0px);
            --safe-right: max(env(safe-area-inset-right), 0px);
            --safe-bottom: max(env(safe-area-inset-bottom), 0px);
            --safe-left: max(env(safe-area-inset-left), 0px);
            --app-vh: 100dvh;
        }
        html {
            min-height: 100%;
            width: 100%;
            max-width: 100%;
            font-size: 16px;
            overflow-x: hidden;
        }
        body { min-height: var(--app-vh, 100dvh); }
        body {
            font-family: 'Inter', system-ui, sans-serif;
            background: var(--bg);
            background-image: radial-gradient(ellipse 90% 35% at 50% 0%, rgba(59,130,246,0.07) 0%, transparent 60%);
            color: var(--text);
            font-size: 16px;
            -webkit-font-smoothing: antialiased;
            width: 100%;
            max-width: 100%;
            overflow-x: hidden;
            overflow-y: auto;
            overscroll-behavior-x: none;
            touch-action: pan-y pinch-zoom;
            -webkit-overflow-scrolling: touch;
        }
        a { color: #60a5fa; text-decoration: none; }
        a:hover { text-decoration: underline; }
        img, video, canvas, svg { max-width: 100%; }
        input[type=\"file\"] { display: none; }

        /* ── HEADER ── */
        .hdr {
            position: sticky; top: 0; z-index: 50;
            display: flex; align-items: center; justify-content: space-between;
            gap: 0.75rem;
            padding: 0 1.25rem; height: 56px;
            background: rgba(8,13,20,0.85); backdrop-filter: blur(12px);
            border-bottom: 1px solid var(--border);
            width: 100%;
            max-width: 100%;
            overflow-x: clip;
        }
        .hdr-brand { display: flex; align-items: center; gap: 0.6rem; min-width: 0; flex: 1 1 auto; }
        .hdr-logo { width: 30px; height: 30px; border-radius: 8px; background: linear-gradient(135deg,#3b82f6,#8b5cf6); display: flex; align-items: center; justify-content: center; font-size: 1rem; box-shadow: 0 0 0 1px rgba(139,92,246,0.35), 0 3px 10px rgba(59,130,246,0.22); }
        .hdr-title { font-size: 1rem; font-weight: 800; letter-spacing: -0.02em; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .hdr-right { display: flex; align-items: center; gap: 0.75rem; flex: 0 0 auto; min-width: 0; }
        #user-email { color: var(--muted); font-size: 0.8rem; display: none; }
        @media(min-width:640px){ #user-email { display: block; } }
        .btn-ghost { background: transparent; border: 1px solid var(--border2); color: #94a3b8; padding: 6px 14px; border-radius: 8px; font-size: 0.82rem; font-weight: 600; cursor: pointer; transition: background 0.15s; }
        .btn-ghost:hover { background: rgba(255,255,255,0.06); }
        a.btn-ghost { text-decoration: none; display: inline-flex; align-items: center; justify-content: center; }
        a.btn-ghost:hover { text-decoration: none; }

        /* ══ MOBILE LAYOUT ══ */
        .m-layout {
            display: flex; flex-direction: column;
            min-height: calc(var(--app-vh, 100dvh) - 56px);
            width: 100%;
            max-width: 100%;
            margin: 0;
            padding-bottom: calc(14px + var(--safe-bottom));
            overflow-x: clip;
            overflow-y: visible;
            touch-action: pan-y pinch-zoom;
        }
        @media(min-width:1024px){ .m-layout { display: none !important; } }

        /* step system */
        .m-step {
            display: none; flex-direction: column; flex: 1 1 auto;
            min-height: calc(var(--app-vh, 100dvh) - 56px - 14px - var(--safe-bottom));
            width: 100%;
            max-width: 100%;
            padding: 0.8rem max(0.5rem, var(--safe-right)) 1rem max(0.5rem, var(--safe-left));
            gap: 1rem;
            overflow-x: clip;
            overflow-y: visible;
        }
        .m-step.active { display: flex; }
        .m-step-head { display: flex; flex-direction: column; gap: 0.35rem; flex-shrink: 0; min-width: 0; }
        .m-step-kicker {
            font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.08em;
            color: #60a5fa; font-weight: 800;
        }
        .m-summary-row {
            display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.75rem;
            width: 100%;
            min-width: 0;
        }
        .m-summary-pill {
            background: linear-gradient(180deg, rgba(22,32,50,0.98), rgba(15,25,35,0.96));
            border: 1px solid var(--border2); border-radius: var(--radius);
            padding: 0.85rem 0.95rem;
            display: flex; flex-direction: column; gap: 0.22rem;
            min-width: 0;
        }
        .m-summary-pill strong { font-size: 1rem; font-weight: 800; color: var(--text); }
        .m-summary-pill span { font-size: 0.78rem; color: var(--muted); }

        /* drafts drawer */
        .drafts-drawer {
            background: linear-gradient(135deg, rgba(245,158,11,0.09), rgba(234,179,8,0.04));
            border: 1px solid rgba(245,158,11,0.22);
            border-radius: var(--radius);
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0,0,0,0.2), 0 0 0 1px rgba(245,158,11,0.06);
        }
        .drafts-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 0.875rem 1rem; cursor: pointer; user-select: none;
            -webkit-tap-highlight-color: transparent;
        }
        .drafts-header-left { display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; font-weight: 700; color: #fde68a; }
        .drafts-chevron { color: #fde68a; font-size: 0.75rem; transition: transform 0.2s; }
        .drafts-chevron.open { transform: rotate(180deg); }
        .drafts-body { border-top: 1px solid rgba(245,158,11,0.15); }
        .draft-item {
            display: flex; align-items: center; gap: 0.75rem;
            padding: 0.875rem 1rem; border-bottom: 1px solid rgba(255,255,255,0.05);
            cursor: pointer; -webkit-tap-highlight-color: transparent; transition: background 0.15s;
        }
        .draft-item:last-child { border-bottom: none; }
        .draft-item:active { background: rgba(255,255,255,0.04); }
        .draft-icon { width: 40px; height: 40px; border-radius: 10px; background: var(--surface2); display: flex; align-items: center; justify-content: center; font-size: 1.1rem; flex-shrink: 0; }
        .draft-info { flex: 1; min-width: 0; }
        .draft-title { font-size: 0.9rem; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .draft-meta { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }
        .draft-actions { display: flex; align-items: center; gap: 0.45rem; flex-shrink: 0; }
        .draft-resume-btn {
            flex-shrink: 0; background: var(--accent); color: white; border: none;
            border-radius: 8px; padding: 7px 14px; font-size: 0.8rem; font-weight: 700;
            cursor: pointer; transition: background 0.15s;
        }
        .draft-resume-btn:hover { background: var(--accent-h); }
        .draft-delete-btn {
            flex-shrink: 0; background: rgba(239,68,68,0.12); color: #fca5a5; border: 1px solid rgba(239,68,68,0.25);
            border-radius: 8px; padding: 7px 12px; font-size: 0.8rem; font-weight: 700;
            cursor: pointer; transition: background 0.15s, border-color 0.15s;
        }
        .draft-delete-btn:hover { background: rgba(239,68,68,0.18); border-color: rgba(239,68,68,0.38); }

        .storage-grid {
            display: grid;
            grid-template-columns: minmax(0, 1fr);
            gap: 0.7rem;
            min-width: 0;
        }
        .storage-action {
            border: 1px solid var(--border2);
            border-radius: 14px;
            background: rgba(22,32,50,0.96);
            color: var(--text);
            min-height: 62px;
            padding: 0.8rem 0.95rem;
            font-size: 0.88rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.45rem;
            cursor: pointer;
            min-width: 0;
            white-space: normal;
            text-align: center;
            overflow-wrap: anywhere;
        }
        .storage-action:active { border-color: var(--accent); }
        .storage-status { margin-top: 0.8rem; }
        .storage-stack { display: flex; flex-direction: column; gap: 0.7rem; width: 100%; min-width: 0; flex-shrink: 0; }
        .storage-subhead {
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--muted);
            font-weight: 700;
        }

        /* photo step */
        .step-heading { font-size: 1.4rem; font-weight: 900; letter-spacing: -0.03em; line-height: 1.05; }
        .step-sub { font-size: 0.92rem; color: var(--muted); line-height: 1.45; }

        .photo-grid {
            display: grid;
            grid-template-columns: minmax(0, 1fr);
            grid-template-rows: minmax(0, 1fr);
            align-items: stretch;
            gap: 0.75rem;
            width: 100%;
            min-width: 0;
            min-height: clamp(220px, 30svh, 560px);
            flex: 1 1 auto;
        }
        .photo-card {
            background: linear-gradient(160deg, rgba(59,130,246,0.07) 0%, rgba(22,32,50,0.98) 100%);
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 18px; padding: 1.25rem 1rem;
            display: flex; flex-direction: column; align-items: center; gap: 0.65rem;
            cursor: pointer; -webkit-tap-highlight-color: transparent;
            transition: background 0.18s, border-color 0.18s, transform 0.12s, box-shadow 0.18s;
            height: 100%;
            min-height: 0;
            justify-content: center;
            box-shadow: 0 4px 18px rgba(0,0,0,0.22);
            min-width: 0;
            overflow: hidden;
        }
        .photo-card:hover { background: linear-gradient(160deg, rgba(59,130,246,0.12) 0%, rgba(30,45,65,0.98) 100%); border-color: rgba(59,130,246,0.32); box-shadow: 0 6px 24px rgba(59,130,246,0.1); }
        .photo-card:active { transform: scale(0.97); border-color: var(--accent); }
        .photo-card-icon { font-size: 2.1rem; }
        .photo-card-label { font-size: 0.95rem; font-weight: 800; overflow-wrap: anywhere; text-align: center; }
        .photo-card-sub { font-size: 0.75rem; color: var(--muted); text-align: center; overflow-wrap: anywhere; }

        .m-preview-wrap {
            background: rgba(15,25,35,0.88);
            border: 1px solid var(--border2);
            border-radius: var(--radius);
            padding: 0.9rem;
            width: 100%;
            min-width: 0;
            flex-shrink: 0;
        }
        .m-mini-label {
            font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em;
            color: var(--muted); font-weight: 700; margin-bottom: 0.6rem;
        }
        .previews { gap: 8px; }
        .m-layout .previews {
            display: grid;
            grid-auto-flow: column;
            grid-auto-columns: 86px;
            overflow-x: auto;
            padding-bottom: 2px;
            scrollbar-width: none;
        }
        .m-layout .previews::-webkit-scrollbar { display: none; }
        .d-layout .previews { display: flex; flex-wrap: wrap; }
        .previews img { height: 86px; width: 86px; border-radius: 12px; object-fit: cover; border: 2px solid var(--border2); flex-shrink: 0; }

        /* action buttons */
        .btn-action {
            width: 100%; padding: 17px; border-radius: var(--radius); border: none;
            font-size: 1rem; font-weight: 700; cursor: pointer; letter-spacing: -0.01em;
            transition: opacity 0.15s, transform 0.1s; -webkit-tap-highlight-color: transparent;
            display: flex; align-items: center; justify-content: center; text-align: center;
        }
        a.btn-action:hover { text-decoration: none; }
        .btn-action:active { transform: scale(0.98); }
        .btn-action:disabled { opacity: 0.45; pointer-events: none; }
        .btn-action.blue { background: linear-gradient(135deg, #3b82f6, #2563eb); color: white; box-shadow: 0 4px 16px rgba(59,130,246,0.25); }
        .btn-action.green { background: linear-gradient(135deg, #10b981, #059669); color: white; box-shadow: 0 4px 16px rgba(16,185,129,0.22); }
        .btn-action.slate { background: var(--surface2); color: #94a3b8; border: 1px solid var(--border2); }
        .m-action-tray {
            position: sticky;
            bottom: 0;
            margin-top: auto;
            padding: 0.9rem;
            background: linear-gradient(180deg, rgba(8,13,20,0), rgba(8,13,20,0.82) 18%, rgba(8,13,20,0.98));
            backdrop-filter: blur(12px);
            z-index: 8;
            width: 100%;
            min-width: 0;
            flex-shrink: 0;
        }
        .m-action-stack {
            display: flex; flex-direction: column; gap: 0.7rem;
            background: rgba(8,13,20,0.72);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 0.85rem;
            box-shadow: 0 18px 36px rgba(0,0,0,0.28);
            min-width: 0;
        }
        .m-secondary-row { display: grid; grid-template-columns: 1fr; gap: 0.7rem; }

        /* status pill */
        .status-pill {
            display: flex; align-items: center; gap: 0.65rem;
            padding: 0.875rem 1rem; border-radius: var(--radius-sm);
            font-size: 0.88rem; font-weight: 500; line-height: 1.4;
            background: var(--surface); border: 1px solid var(--border2); color: var(--muted);
        }
        .status-pill.busy { border-color: rgba(59,130,246,0.4); color: #93c5fd; background: rgba(59,130,246,0.07); }
        .status-pill.ok   { border-color: rgba(16,185,129,0.4); color: #6ee7b7; background: rgba(16,185,129,0.07); }
        .status-pill.err  { border-color: rgba(239,68,68,0.4);  color: #fca5a5; background: rgba(239,68,68,0.07); }
        .spin { display: inline-block; animation: spin 0.8s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* results card */
        .result-card {
            background: var(--surface); border: 1px solid var(--border2);
            border-radius: var(--radius); overflow: hidden;
            box-shadow: 0 18px 36px rgba(0,0,0,0.22);
        }
        .result-card-hero {
            background: linear-gradient(135deg, rgba(59,130,246,0.13) 0%, rgba(139,92,246,0.08) 60%, rgba(16,185,129,0.05) 100%);
            padding: 1.25rem; border-bottom: 1px solid var(--border);
            display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem;
            position: relative;
        }
        .result-card-hero::before {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
            background: linear-gradient(90deg, #3b82f6, #8b5cf6, #10b981);
            border-radius: 2px 2px 0 0;
        }
        .result-item-title { font-size: 1.05rem; font-weight: 800; letter-spacing: -0.02em; line-height: 1.3; }
        .result-price { font-size: 1.75rem; font-weight: 900; color: #34d399; letter-spacing: -0.04em; white-space: nowrap; }
        .result-price-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; text-align: right; }
        .result-body { padding: 1rem 1.25rem; display: flex; flex-direction: column; gap: 0.875rem; }
        .meta-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
        .meta-chip { background: var(--surface2); border-radius: 8px; padding: 0.6rem 0.75rem; }
        .meta-chip-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; }
        .meta-chip-value { font-size: 0.9rem; font-weight: 600; color: var(--text); margin-top: 2px; }
        .conf-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
        .conf-label { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; }
        .conf-score { font-size: 0.85rem; font-weight: 800; }
        .conf-bar-bg { height: 5px; background: rgba(255,255,255,0.08); border-radius: 99px; overflow: hidden; }
        .conf-bar { height: 100%; border-radius: 99px; transition: width 0.6s cubic-bezier(0.4,0,0.2,1); }
        .conf-reason { font-size: 0.78rem; color: var(--muted); margin-top: 5px; line-height: 1.5; }
        .section-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; margin-bottom: 0.4rem; }
        .pricing-text { font-size: 0.875rem; color: #cbd5e1; line-height: 1.5; }
        .draft-description {
            white-space: pre-wrap;
            background: rgba(22,32,50,0.86);
            border: 1px solid var(--border2);
            border-radius: 10px;
            padding: 0.8rem 0.9rem;
            color: #dbeafe;
            font-size: 0.92rem;
            line-height: 1.48;
        }
        .comp-list { list-style: none; display: flex; flex-direction: column; gap: 0.4rem; }
        .comp-list li { font-size: 0.85rem; color: #94a3b8; padding: 0.5rem 0.75rem; background: var(--surface2); border-radius: 8px; line-height: 1.4; }
        .comp-list li a { color: #60a5fa; }
        .m-post-card {
            background: rgba(15,25,35,0.94);
            border: 1px solid var(--border2);
            border-radius: 18px;
            padding: 0.95rem;
            display: flex;
            flex-direction: column;
            gap: 0.85rem;
        }

        /* correction area */
        .correction-card { background: var(--surface); border: 1px solid var(--border2); border-radius: var(--radius); padding: 1rem; }
        .correction-label { font-size: 0.8rem; font-weight: 600; color: var(--muted); margin-bottom: 0.5rem; }
        textarea {
            width: 100%; min-height: 80px; resize: none;
            background: var(--surface2); border: 1px solid var(--border2);
            border-radius: var(--radius-sm); padding: 0.75rem; color: var(--text);
            font-size: 0.95rem; font-family: inherit; line-height: 1.5;
            -webkit-appearance: none; transition: border-color 0.15s;
        }
        textarea:focus { outline: none; border-color: var(--accent); }

        /* ══ DESKTOP LAYOUT ══ */
        .d-layout { display: none; }
        @media(min-width:1024px){
            .d-layout {
                display: grid; grid-template-columns: 1fr 380px;
                gap: 1.25rem; padding: 1.25rem;
                height: calc(var(--app-vh, 100dvh) - 56px);
                min-height: 0;
            }
        }
        .d-browser-card {
            background: var(--surface); border: 1px solid var(--border);
            border-radius: var(--radius-lg); overflow: hidden; display: flex; flex-direction: column; min-height: 0;
        }
        .d-browser-bar {
            display: flex; align-items: center; gap: 0.5rem;
            padding: 0.75rem 1rem; border-bottom: 1px solid var(--border); flex-shrink: 0;
        }
        .d-browser-dot { width: 10px; height: 10px; border-radius: 50%; }
        .browser-frame { flex: 1; min-height: 0; height: 100%; border: none; background: #000; width: 100%; }

        .d-panel {
            background: var(--surface); border: 1px solid var(--border);
            border-radius: var(--radius-lg); display: flex; flex-direction: column;
            overflow: hidden;
        }
        .d-panel-inner { padding: 1.25rem; flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 1rem; }
        .d-section-title { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); font-weight: 700; }

        .d-draft-item {
            display: flex; align-items: center; gap: 0.75rem;
            padding: 0.75rem; border-radius: var(--radius-sm);
            border: 1px solid var(--border); cursor: pointer;
            transition: background 0.15s, border-color 0.15s;
        }
        .d-draft-item:hover { background: var(--surface2); border-color: var(--border2); }
        .d-draft-icon { width: 36px; height: 36px; border-radius: 8px; background: var(--surface2); display: flex; align-items: center; justify-content: center; font-size: 1rem; flex-shrink: 0; }
        .d-draft-title { font-size: 0.875rem; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .d-draft-meta { font-size: 0.75rem; color: var(--muted); }
        .d-draft-actions { display: flex; align-items: center; gap: 0.45rem; flex-shrink: 0; }
        .d-draft-badge { flex-shrink: 0; background: rgba(59,130,246,0.15); color: #60a5fa; border: 1px solid rgba(59,130,246,0.25); border-radius: 6px; padding: 3px 9px; font-size: 0.75rem; font-weight: 700; }
        .d-draft-delete { flex-shrink: 0; background: rgba(239,68,68,0.12); color: #fca5a5; border: 1px solid rgba(239,68,68,0.25); border-radius: 6px; padding: 3px 9px; font-size: 0.75rem; font-weight: 700; cursor: pointer; }
        .d-storage-badge { flex-shrink: 0; background: rgba(16,185,129,0.12); color: #6ee7b7; border: 1px solid rgba(16,185,129,0.24); border-radius: 6px; padding: 3px 9px; font-size: 0.75rem; font-weight: 700; cursor: pointer; }

        .upload-zone-d {
            border: 2px dashed var(--border2); border-radius: var(--radius); padding: 2rem 1.5rem;
            text-align: center; cursor: pointer; transition: border-color 0.2s, background 0.2s;
        }
        .upload-zone-d:hover { border-color: var(--accent); background: rgba(59,130,246,0.04); }
        .upload-zone-d-icon { font-size: 2rem; margin-bottom: 0.5rem; }
        .upload-zone-d-label { font-size: 0.95rem; font-weight: 700; }
        .upload-zone-d-sub { font-size: 0.8rem; color: var(--muted); margin-top: 0.2rem; }

        .btn-d { width: 100%; padding: 13px; border-radius: var(--radius-sm); border: none; font-size: 0.9rem; font-weight: 700; cursor: pointer; transition: background 0.15s, opacity 0.15s; }
        .btn-d:disabled { opacity: 0.45; pointer-events: none; }
        .btn-d.accent { background: linear-gradient(135deg, #3b82f6, #2563eb); color: white; box-shadow: 0 3px 12px rgba(59,130,246,0.22); }
        .btn-d.accent:hover { opacity: 0.92; }
        .btn-d.green { background: linear-gradient(135deg, #10b981, #059669); color: white; box-shadow: 0 3px 12px rgba(16,185,129,0.2); }
        .btn-d.green:hover { opacity: 0.92; }
        .btn-d.slate { background: var(--surface2); color: #94a3b8; border: 1px solid var(--border2); }
        .btn-d-row { display: grid; grid-template-columns: 1fr 1fr; gap: 0.6rem; }

        .d-result-block { background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0.875rem; display: flex; flex-direction: column; gap: 0.5rem; }
        .d-result-subtitle { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); font-weight: 700; margin-bottom: 0.15rem; }
        .d-result-row { display: flex; justify-content: space-between; align-items: baseline; gap: 0.5rem; font-size: 0.85rem; }
        .d-result-key { color: var(--muted); font-weight: 500; flex-shrink: 0; }
        .d-result-val { color: var(--text); font-weight: 600; text-align: right; word-break: break-word; }
        .d-result-draft { background: rgba(8,13,20,0.3); border: 1px solid var(--border); border-radius: 12px; padding: 0.875rem; display: flex; flex-direction: column; gap: 0.65rem; }
        .d-draft-desc { white-space: pre-wrap; line-height: 1.5; color: #cbd5e1; font-size: 0.84rem; background: rgba(8,13,20,0.35); border: 1px solid rgba(255,255,255,0.06); border-radius: 10px; padding: 0.75rem; }
        .d-status { font-size: 0.85rem; color: var(--muted); padding: 0.5rem 0; }
        .d-status.ok { color: #6ee7b7; }
        .d-status.err { color: #fca5a5; }
        .fill-card {
            background: rgba(8,13,20,0.28);
            border: 1px solid var(--border2);
            border-radius: var(--radius-sm);
            padding: 0.9rem;
            display: flex;
            flex-direction: column;
            gap: 0.7rem;
        }
        .fill-head { display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; }
        .fill-title { font-size: 0.82rem; font-weight: 700; color: var(--text); }
        .fill-pct { font-size: 0.78rem; font-weight: 700; color: var(--muted); }
        .fill-msg { font-size: 0.84rem; color: #cbd5e1; line-height: 1.4; }
        .fill-bar { height: 7px; border-radius: 999px; background: rgba(255,255,255,0.08); overflow: hidden; }
        .fill-bar > span { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg,#3b82f6,#22c55e); transition: width 0.25s ease; }
        .fill-log { display: flex; flex-direction: column; gap: 0.4rem; }
        .fill-log-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 0.6rem; font-size: 0.76rem; color: var(--muted); }
        .fill-log-row strong { color: #cbd5e1; font-weight: 600; }

        .divider { height: 1px; background: var(--border); }

        @media(max-width:420px){
            .hdr { padding: 0 max(0.9rem, var(--safe-right)) 0 max(0.9rem, var(--safe-left)); }
            .step-heading { font-size: 1.28rem; }
            .result-card-hero { flex-direction: column; }
            .result-price-label, .result-price { text-align: left; }
            .meta-grid { grid-template-columns: 1fr; }
        }
        @media(max-width:340px){
            .photo-grid, .m-summary-row, .storage-grid { grid-template-columns: 1fr; }
        }
        @media (max-width: 1023px) {
            html {
                font-size: 16px;
                overflow-x: hidden !important;
                max-width: 100%;
            }
            body {
                overflow-x: hidden !important;
                max-width: 100%;
            }
            #user-email { display: none !important; }
            .hdr {
                height: clamp(60px, 7.4svh, 68px);
                padding: 0 max(0.9rem, var(--safe-right)) 0 max(0.9rem, var(--safe-left));
                overflow-x: hidden;
                overflow-x: clip;
            }
            .hdr-logo {
                width: clamp(34px, 4.6svh, 40px);
                height: clamp(34px, 4.6svh, 40px);
                border-radius: 10px;
                font-size: 1.1rem;
            }
            .hdr-title { font-size: clamp(1.18rem, 2.4vw, 1.35rem); }
            .btn-ghost {
                min-height: clamp(42px, 5.4svh, 50px);
                padding: clamp(0.45rem, 0.9svh, 0.6rem) clamp(0.75rem, 1.9vw, 0.95rem);
                font-size: clamp(1rem, 2vw, 1.12rem);
            }
            .m-layout {
                min-height: calc(var(--app-vh, 100dvh) - clamp(60px, 7.4svh, 68px));
                max-width: 100%;
                margin: 0;
                overflow-x: hidden;
                overflow-x: clip;
            }
            .m-step {
                min-height: calc(var(--app-vh, 100dvh) - clamp(60px, 7.4svh, 68px) - 14px - var(--safe-bottom));
                padding:
                    clamp(0.72rem, 1.2svh, 1rem)
                    max(clamp(0.72rem, 2.1vw, 0.95rem), var(--safe-right))
                    clamp(0.8rem, 1.35svh, 1.05rem)
                    max(clamp(0.72rem, 2.1vw, 0.95rem), var(--safe-left));
                gap: clamp(0.65rem, 1.18svh, 0.95rem);
                overflow-x: hidden;
                overflow-x: clip;
            }
            .hdr *,
            .m-layout * {
                max-width: 100%;
                touch-action: pan-y pinch-zoom;
            }
            .m-step-kicker { font-size: clamp(0.88rem, 1.8vw, 1rem); }
            .step-heading { font-size: clamp(2rem, 4vw, 2.4rem); line-height: 1.08; }
            .step-sub { font-size: clamp(1.08rem, 2.35vw, 1.35rem); line-height: 1.42; }
            .drafts-header { padding: clamp(0.78rem, 1.35svh, 0.95rem) clamp(0.85rem, 2.3vw, 1rem); }
            .drafts-header-left { font-size: clamp(1.05rem, 2.3vw, 1.25rem); }
            .m-summary-row { gap: clamp(0.55rem, 1svh, 0.75rem); }
            .m-summary-pill {
                padding: clamp(0.72rem, 1.25svh, 0.95rem) clamp(0.82rem, 2.2vw, 1rem);
                border-radius: 14px;
                gap: 0.18rem;
            }
            .m-summary-pill strong { font-size: clamp(1.22rem, 2.5vw, 1.45rem); }
            .m-summary-pill span { font-size: clamp(1rem, 2.1vw, 1.18rem); line-height: 1.3; }
            .photo-grid {
                min-height: clamp(190px, 28svh, 340px);
                flex: 0 0 auto;
            }
            .photo-card {
                border-radius: clamp(14px, 2.1svh, 18px);
                padding: clamp(0.8rem, 1.7svh, 1.25rem) clamp(0.78rem, 2vw, 1rem);
                gap: clamp(0.42rem, 0.9svh, 0.65rem);
            }
            .photo-card-icon { font-size: clamp(2.25rem, 4vw, 3rem); }
            .photo-card-label { font-size: clamp(1.25rem, 2.4vw, 1.55rem); }
            .photo-card-sub { font-size: clamp(1.02rem, 2.1vw, 1.25rem); }
            .storage-stack { gap: clamp(0.45rem, 0.85svh, 0.7rem); }
            .storage-grid { gap: clamp(0.45rem, 0.85svh, 0.7rem); }
            .storage-action {
                min-height: clamp(58px, 7.2svh, 68px);
                padding: clamp(0.58rem, 1.15svh, 0.8rem) clamp(0.75rem, 2.1vw, 0.95rem);
                border-radius: clamp(12px, 1.8svh, 14px);
                font-size: clamp(1.08rem, 2.2vw, 1.3rem);
            }
            .storage-subhead { line-height: 1; }
            .storage-subhead,
            .m-mini-label,
            .section-label,
            .result-price-label,
            .meta-chip-label,
            .conf-label {
                font-size: clamp(0.86rem, 1.7vw, 1rem);
            }
            .btn-action {
                min-height: clamp(54px, 6.8svh, 66px);
                padding: clamp(0.72rem, 1.4svh, 1rem) clamp(0.85rem, 2.2vw, 1rem);
                font-size: clamp(1.1rem, 2.2vw, 1.3rem);
            }
            .m-action-tray { padding: clamp(0.55rem, 1svh, 0.85rem) clamp(0.7rem, 2vw, 0.9rem); }
            .m-action-stack {
                border-radius: 16px;
                gap: clamp(0.5rem, 0.9svh, 0.7rem);
                padding: clamp(0.65rem, 1.2svh, 0.85rem);
            }
            .status-pill,
            .pricing-text,
            .comp-list li {
                font-size: clamp(1rem, 2vw, 1.18rem);
            }
            .result-item-title { font-size: clamp(1.25rem, 2.5vw, 1.55rem); }
            .result-price { font-size: clamp(1.9rem, 3.4vw, 2.3rem); }
            .meta-chip-value,
            .conf-score {
                font-size: clamp(1rem, 2vw, 1.18rem);
            }
            .conf-reason,
            .correction-label,
            .fill-msg {
                font-size: clamp(0.98rem, 1.9vw, 1.12rem);
            }
        }
        @media (min-width: 700px) and (max-width: 1023px) {
            .hdr { height: clamp(64px, 7.2svh, 76px); }
            .hdr-logo {
                width: clamp(38px, 4.2svh, 46px);
                height: clamp(38px, 4.2svh, 46px);
                border-radius: 12px;
            }
            .btn-ghost { min-height: clamp(44px, 5svh, 54px); }
            .m-layout {
                min-height: calc(var(--app-vh, 100dvh) - clamp(64px, 7.2svh, 76px));
            }
            .m-step {
                min-height: calc(var(--app-vh, 100dvh) - clamp(64px, 7.2svh, 76px) - 14px - var(--safe-bottom));
                padding-top: 0.8rem;
                padding-bottom: 0.9rem;
            }
            .photo-grid {
                min-height: clamp(280px, 28svh, 460px);
            }
        }

        /* ── Tutorial overlay ── */
        .tut-backdrop {
            position: fixed; inset: 0; z-index: 9999;
            background: rgba(0,0,0,0.82);
            backdrop-filter: blur(7px); -webkit-backdrop-filter: blur(7px);
            display: flex; align-items: center; justify-content: center;
            padding: 1rem;
            animation: tut-fade-in 0.25s ease-out;
        }
        @keyframes tut-fade-in { from { opacity: 0; } to { opacity: 1; } }
        .tut-card {
            width: 100%; max-width: 400px;
            background: linear-gradient(160deg, #111820 0%, #0c1118 100%);
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 22px; overflow: hidden;
            box-shadow: 0 0 0 1px rgba(59,130,246,0.1), 0 32px 80px rgba(0,0,0,0.8);
            animation: tut-scale-in 0.28s cubic-bezier(0.34,1.46,0.64,1);
        }
        @keyframes tut-scale-in { from { opacity: 0; transform: scale(0.93); } to { opacity: 1; transform: scale(1); } }
        .tut-progress { height: 2px; background: rgba(255,255,255,0.06); }
        .tut-progress-fill { height: 100%; transition: width 0.35s ease, background-color 0.35s ease; }
        .tut-body { padding: 1.75rem 1.75rem 1.5rem; }
        .tut-icon {
            width: 48px; height: 48px; border-radius: 14px; margin-bottom: 1.25rem;
            display: flex; align-items: center; justify-content: center; font-size: 1.4rem;
        }
        .tut-step-label {
            font-size: 0.7rem; font-weight: 800; text-transform: uppercase;
            letter-spacing: 0.16em; margin-bottom: 0.5rem;
        }
        .tut-title { font-size: 1.25rem; font-weight: 900; letter-spacing: -0.025em; color: #f1f5f9; margin-bottom: 0.35rem; }
        .tut-sub   { font-size: 0.78rem; font-weight: 500; margin-bottom: 0.875rem; }
        .tut-desc  { font-size: 0.875rem; color: #94a3b8; line-height: 1.55; margin-bottom: 1.1rem; }
        .tut-bullets { display: flex; flex-direction: column; gap: 0.45rem; margin-bottom: 1.5rem; }
        .tut-bullet { display: flex; align-items: flex-start; gap: 0.6rem; font-size: 0.84rem; color: #64748b; }
        .tut-bullet-dot { width: 6px; height: 6px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; }
        .tut-nav { display: flex; align-items: center; justify-content: space-between; }
        .tut-dots { display: flex; align-items: center; gap: 6px; }
        .tut-dot { height: 6px; border-radius: 99px; cursor: pointer; transition: width 0.25s ease, background 0.25s ease; }
        .tut-btn-back {
            display: flex; align-items: center; gap: 4px;
            padding: 8px 14px; border-radius: 10px; border: none;
            background: transparent; color: #475569; font-size: 0.84rem; font-weight: 600;
            cursor: pointer; font-family: inherit; transition: color 0.15s;
        }
        .tut-btn-back:hover { color: #94a3b8; }
        .tut-btn-next {
            display: flex; align-items: center; gap: 5px;
            padding: 9px 18px; border-radius: 10px; border: none;
            color: white; font-size: 0.875rem; font-weight: 700;
            cursor: pointer; font-family: inherit;
            transition: opacity 0.15s, transform 0.1s;
        }
        .tut-btn-next:hover { opacity: 0.9; }
        .tut-btn-next:active { transform: scale(0.97); }
        .tut-close {
            position: absolute; top: 1rem; right: 1rem;
            width: 28px; height: 28px; border-radius: 8px; border: none;
            background: rgba(255,255,255,0.06); color: #475569;
            font-size: 0.85rem; cursor: pointer; display: flex; align-items: center; justify-content: center;
            transition: background 0.15s, color 0.15s; font-family: inherit;
        }
        .tut-close:hover { background: rgba(255,255,255,0.1); color: #94a3b8; }
        @media(max-width:600px){
            .tut-backdrop {
                align-items: stretch;
                justify-content: stretch;
                padding: 0;
            }
            .tut-card {
                max-width: none;
                height: var(--app-vh, 100dvh);
                min-height: var(--app-vh, 100dvh);
                border-radius: 0;
                display: flex;
                flex-direction: column;
            }
            .tut-progress { flex-shrink: 0; }
            .tut-body {
                flex: 1;
                min-height: 0;
                overflow-y: auto;
                padding: calc(1.35rem + var(--safe-top)) 1.25rem calc(1.2rem + var(--safe-bottom));
                display: flex;
                flex-direction: column;
            }
            .tut-icon { width: 44px; height: 44px; margin-bottom: 1rem; }
            .tut-desc { margin-bottom: 1rem; }
            .tut-bullets { margin-bottom: 1rem; }
            .tut-nav {
                margin-top: auto;
                padding-top: 1rem;
                gap: 0.75rem;
            }
            .tut-btn-next {
                min-height: 46px;
                padding: 0 18px;
                flex-shrink: 0;
            }
            .tut-close {
                top: calc(0.85rem + var(--safe-top));
                right: 0.85rem;
                width: 34px;
                height: 34px;
            }
        }
    </style>
</head>
<body>

<!-- HEADER -->
<header class=\"hdr\">
    <div class=\"hdr-brand\">
        <div class=\"hdr-logo\">⚡</div>
        <span class=\"hdr-title\">Auto-Lister</span>
    </div>
    <div class=\"hdr-right\">
        <span id=\"user-credits\" style=\"display:none; color:#10b981; font-weight:800; font-size:0.85rem; margin-right:8px;\"></span>
        <button id=\"buy-credits-btn\" class=\"btn-ghost\" style=\"display:none; border-color:rgba(16,185,129,0.3); color:#10b981;\" onclick=\"buyCredits()\">Buy Posts</button>
        <span id=\"user-email\"></span>
        <a class=\"btn-ghost\" href=\"/support\" target=\"_blank\" rel=\"noopener noreferrer\" onclick=\"trackAppAction('support-open')\">Support</a>
        <button class=\"btn-ghost\" onclick=\"signOut()\">Sign out</button>
    </div>
</header>

<!-- ══════════════ MOBILE ══════════════ -->
<div class=\"m-layout\" id=\"mobileLayout\">

    <!-- Drafts drawer (shown when drafts exist) -->
    <div id=\"mDraftsDrawer\" style=\"display:none; padding: 0.55rem max(0.5rem, var(--safe-right)) 0 max(0.5rem, var(--safe-left));\">
        <div class=\"drafts-drawer\">
            <div class=\"drafts-header\" onclick=\"toggleDraftsDrawer()\">
                <div class=\"drafts-header-left\">
                    <span>📋</span>
                    <span id=\"mDraftsCount\">Saved Drafts</span>
                </div>
                <span class=\"drafts-chevron\" id=\"mDraftsChevron\">▼</span>
            </div>
            <div class=\"drafts-body\" id=\"mDraftsBody\" style=\"display:none;\"></div>
        </div>
    </div>

    <div id=\"mStorageDrawer\" style=\"display:none; padding: 0.55rem max(0.5rem, var(--safe-right)) 0 max(0.5rem, var(--safe-left));\">
        <div class=\"drafts-drawer\" style=\"background:linear-gradient(135deg, rgba(16,185,129,0.08), rgba(34,197,94,0.04)); border-color: rgba(16,185,129,0.2); box-shadow:none;\">
            <div class=\"drafts-header\" onclick=\"toggleStorageDrawer()\">
                <div class=\"drafts-header-left\" style=\"color:#86efac;\">
                    <span>🖼️</span>
                    <span id=\"mStorageCount\">Photo Storage</span>
                </div>
                <span class=\"drafts-chevron\" id=\"mStorageChevron\" style=\"color:#86efac;\">▼</span>
            </div>
            <div class=\"drafts-body\" id=\"mStorageBody\" style=\"display:none; border-top-color: rgba(16,185,129,0.15);\"></div>
        </div>
    </div>

    <!-- STEP 1: Take / choose photos -->
    <div class=\"m-step active\" id=\"mStep1\">
        <div class=\"m-step-head\">
            <div class=\"m-step-kicker\">New Listing</div>
            <div class=\"step-heading\">What are you selling?</div>
            <div class=\"step-sub\">Add photos from your camera, gallery, or files. AI analyzes them immediately.</div>
        </div>

        <div class=\"m-summary-row\">
            <div class=\"m-summary-pill\">
                <strong id=\"mPhotoCount\">0 photos</strong>
                <span>Selected for this draft</span>
            </div>
            <div class=\"m-summary-pill\">
                <strong>Auto analyze</strong>
                <span>Starts right after photo pick</span>
            </div>
        </div>

        <div class=\"photo-grid\">
            <div class=\"photo-card\" onclick=\"document.getElementById('photoInput').click()\">
                <span class=\"photo-card-icon\">🖼️</span>
                <span class=\"photo-card-label\">Add Photos</span>
                <span class=\"photo-card-sub\">Camera, gallery, or files</span>
            </div>
        </div>

        <input type=\"file\" id=\"photoInput\" accept=\"image/*\" multiple onchange=\"handleFiles(this.files)\">
        <input type=\"file\" id=\"storagePhotoInput\" accept=\"image/*\" multiple onchange=\"handleStorageFiles(this.files, this)\">
        <input type=\"file\" id=\"draftPhotoInput\" accept=\"image/*\" multiple onchange=\"handleDraftUploadFiles(this.files, this)\">

        <div class=\"storage-stack\">
            <div class=\"storage-subhead\">Photo Storage</div>
            <div class=\"storage-grid\">
                <button class=\"storage-action\" onclick=\"document.getElementById('storagePhotoInput').click()\">📥 Store Photos</button>
            </div>
            <div class=\"storage-subhead\">Draft Upload</div>
            <div class=\"storage-grid\">
                <button class=\"storage-action\" onclick=\"document.getElementById('draftPhotoInput').click()\">📝 Add to Draft</button>
            </div>
        </div>
        <div id=\"mStorageStatus\" class=\"status-pill storage-status\" style=\"display:none;\"></div>

        <div id=\"mPreviewsWrap\" class=\"m-preview-wrap\" style=\"display:none;\">
            <div class=\"m-mini-label\">Selected Photos</div>
            <div id=\"mPreviews\" class=\"previews\"></div>
        </div>

        <div id=\"mAnalyzeTray\" class=\"m-action-tray\" style=\"display:none;\">
            <div class=\"m-action-stack\">
                <button id=\"mAnalyzeBtn\" class=\"btn-action green\" style=\"display:none\" onclick=\"mobileAnalyze(false)\">Analyze with AI</button>
                <div class=\"m-secondary-row\">
                    <button id=\"addMoreBtn\" style=\"display:none\" class=\"btn-action slate\" onclick=\"document.getElementById('photoInput').click()\">+ Add More Photos</button>
                </div>
            </div>
        </div>
    </div>

    <!-- STEP 2: Results -->
    <div class=\"m-step\" id=\"mStep2\">
        <div class=\"m-step-head\">
            <div class=\"m-step-kicker\">Review Draft</div>
            <div class=\"step-heading\">Check the listing</div>
            <div class=\"step-sub\">Tighten anything that looks off, then send the draft to Facebook.</div>
        </div>

        <div id=\"mStatus\" class=\"status-pill busy\" style=\"display:none;\"></div>

        <div id=\"mResults\" style=\"display:none; flex-direction:column; gap:1rem;\">
            <div class=\"result-card\" id=\"mResultPanel\"></div>

            <div class=\"correction-card\">
                <div class=\"correction-label\">Something off? Add a correction:</div>
                <textarea id=\"mCorrectionBox\" placeholder=\"e.g. This is the 4 ft kit, not 6 ft. No extension included.\"></textarea>
                <button class=\"btn-action slate\" style=\"margin-top:0.6rem; padding:12px;\" onclick=\"mobileRefine()\">Re-analyze</button>
            </div>

            <div id=\"mPostCard\" class=\"m-post-card\" style=\"display:none;\">
                <button id=\"mPostBtn\" class=\"btn-action green\" onclick=\"mobilePost()\">Post to Facebook Marketplace</button>
                <a id=\"mFacebookBrowserBtn\" class=\"btn-action slate\" href=\"#\" onclick=\"openMobileFacebookBrowser(); return false;\">Open Facebook Browser</a>
                <div id=\"mPostStatus\" class=\"status-pill\" style=\"display:none;\"></div>
                <div id=\"mFillStatus\" class=\"fill-card\" style=\"display:none;\"></div>
            </div>
        </div>

        <div class=\"m-action-tray\">
            <div class=\"m-action-stack\">
                <button class=\"btn-action slate\" onclick=\"mobileReset()\">← Start Over</button>
            </div>
        </div>
    </div>

</div>

<!-- ══════════════ DESKTOP ══════════════ -->
<div class=\"d-layout\" id=\"desktopLayout\">

    <!-- Left: browser -->
    <div class=\"d-browser-card\">
        <div class=\"d-browser-bar\">
            <div class=\"d-browser-dot\" style=\"background:#ef4444;\"></div>
            <div class=\"d-browser-dot\" style=\"background:#f59e0b;\"></div>
            <div class=\"d-browser-dot\" style=\"background:#22c55e;\"></div>
            <span style=\"font-size:0.78rem;color:var(--muted);margin-left:0.5rem;\">Facebook Marketplace — Your browser session</span>
        </div>
        <iframe id=\"vnc-iframe\" src=\"\" class=\"browser-frame\"></iframe>
    </div>

    <!-- Right: control panel -->
    <div class=\"d-panel\">
        <div class=\"d-panel-inner\" id=\"dPanelInner\">

            <!-- Drafts section -->
            <div id=\"dDraftsSection\" style=\"display:none;\">
                <div class=\"d-section-title\" style=\"margin-bottom:0.6rem;\">Saved Drafts</div>
                <div id=\"dDraftsList\" style=\"display:flex;flex-direction:column;gap:0.5rem;\"></div>
                <div class=\"divider\" style=\"margin-top:0.875rem;\"></div>
            </div>

            <!-- Upload section -->
            <div>
                <div class=\"d-section-title\" style=\"margin-bottom:0.75rem;\">New Listing</div>
                <label class=\"upload-zone-d\" style=\"display:block;\">
                    <input type=\"file\" id=\"fileInput\" accept=\"image/*\" multiple onchange=\"desktopFileChange(this.files)\">
                    <div class=\"upload-zone-d-icon\">📁</div>
                    <div class=\"upload-zone-d-label\">Select Photos</div>
                    <div class=\"upload-zone-d-sub\">Click to browse</div>
                </label>
                <div id=\"dPreviews\" class=\"previews\" style=\"margin-top:0.75rem;\"></div>
                <button id=\"dAnalyzeBtn\" class=\"btn-d accent\" style=\"display:none;margin-top:0.75rem;\" onclick=\"desktopAnalyze()\">Analyze with AI</button>
            </div>

            <div>
                <div class=\"d-section-title\" style=\"margin-bottom:0.75rem;\">Photo Storage</div>
                <label class=\"upload-zone-d\" style=\"display:block;\">
                    <input type=\"file\" id=\"storageFileInput\" accept=\"image/*\" multiple onchange=\"handleStorageFiles(this.files, this)\">
                    <div class=\"upload-zone-d-icon\">🗂️</div>
                    <div class=\"upload-zone-d-label\">Upload to Storage</div>
                </label>
                <label class=\"upload-zone-d\" style=\"display:block; margin-top:0.75rem;\">
                    <input type=\"file\" id=\"draftFileInput\" accept=\"image/*\" multiple onchange=\"handleDraftUploadFiles(this.files, this)\">
                    <div class=\"upload-zone-d-icon\">📝</div>
                    <div class=\"upload-zone-d-label\">Upload to Drafts</div>
                </label>
                <div id=\"dStorageStatus\" class=\"d-status\" style=\"display:none;\"></div>
                <div id=\"dStorageList\" style=\"display:flex;flex-direction:column;gap:0.5rem;margin-top:0.75rem;\"></div>
            </div>

            <!-- Status -->
            <div id=\"dStatus\" class=\"d-status\" style=\"display:none;\"></div>
            <div id=\"dFillStatus\" class=\"fill-card\" style=\"display:none;\"></div>

            <!-- Results -->
            <div id=\"dResults\" style=\"display:none; flex-direction:column; gap:0.875rem;\">
                <div class=\"divider\"></div>
                <div id=\"dResultBlock\" class=\"d-result-block\"></div>
                <div>
                    <div class=\"d-section-title\" style=\"margin-bottom:0.5rem;\">Correction</div>
                    <textarea id=\"correctionBox\" placeholder=\"e.g. This is not the 4 ft barrel kit.\" style=\"min-height:70px;\"></textarea>
                </div>
                <div class=\"btn-d-row\">
                    <button class=\"btn-d slate\" onclick=\"desktopRefine()\">Re-analyze</button>
                    <button id=\"dPostBtn\" class=\"btn-d green\" onclick=\"desktopDraft()\">Post to Facebook</button>
                </div>
                <button class=\"btn-d slate\" onclick=\"desktopRevealPublish()\">Reveal Publish Button</button>
            </div>

        </div>
    </div>

</div>

<!-- ── Tutorial overlay ── -->
<div id=\"tutOverlay\" class=\"tut-backdrop\" style=\"display:none;\" role=\"dialog\" aria-modal=\"true\">
    <div class=\"tut-card\" style=\"position:relative;\">
        <button class=\"tut-close\" onclick=\"tutDismiss()\" aria-label=\"Close tutorial\">✕</button>
        <div class=\"tut-progress\"><div class=\"tut-progress-fill\" id=\"tutProgress\"></div></div>
        <div class=\"tut-body\">
            <div class=\"tut-icon\" id=\"tutIcon\"></div>
            <div class=\"tut-step-label\" id=\"tutLabel\"></div>
            <div class=\"tut-title\" id=\"tutTitle\"></div>
            <div class=\"tut-sub\" id=\"tutSub\"></div>
            <div class=\"tut-desc\" id=\"tutDesc\"></div>
            <div class=\"tut-bullets\" id=\"tutBullets\"></div>
            <div class=\"tut-nav\">
                <button class=\"tut-btn-back\" id=\"tutBack\" onclick=\"tutStep(-1)\">&#8592; Back</button>
                <div class=\"tut-dots\" id=\"tutDots\"></div>
                <button class=\"tut-btn-next\" id=\"tutNext\" onclick=\"tutStep(1)\"></button>
            </div>
        </div>
    </div>
</div>

<script>
const AUTH_PROVIDER = """ + AUTH_PROVIDER_JS + """;

function syncAppViewportHeight() {
    const viewport = window.visualViewport;
    const height = Math.max(
        320,
        Math.round((viewport && viewport.height) || window.innerHeight || document.documentElement.clientHeight || 0)
    );
    document.documentElement.style.setProperty('--app-vh', `${height}px`);
}

let horizontalLockQueued = false;
function lockHorizontalViewport() {
    horizontalLockQueued = false;
    const root = document.scrollingElement || document.documentElement;
    const y = window.scrollY || root.scrollTop || document.documentElement.scrollTop || document.body.scrollTop || 0;
    if (window.scrollX !== 0 || root.scrollLeft !== 0 || document.documentElement.scrollLeft !== 0 || document.body.scrollLeft !== 0) {
        window.scrollTo(0, y);
        root.scrollLeft = 0;
        document.documentElement.scrollLeft = 0;
        document.body.scrollLeft = 0;
    }
}

function scheduleHorizontalViewportLock() {
    if (horizontalLockQueued) return;
    horizontalLockQueued = true;
    window.requestAnimationFrame(lockHorizontalViewport);
}

syncAppViewportHeight();
scheduleHorizontalViewportLock();
window.addEventListener('resize', syncAppViewportHeight, { passive: true });
window.addEventListener('resize', scheduleHorizontalViewportLock, { passive: true });
window.addEventListener('orientationchange', syncAppViewportHeight, { passive: true });
window.addEventListener('orientationchange', scheduleHorizontalViewportLock, { passive: true });
window.addEventListener('scroll', scheduleHorizontalViewportLock, { passive: true });
window.addEventListener('load', scheduleHorizontalViewportLock, { once: true, passive: true });
if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', syncAppViewportHeight, { passive: true });
    window.visualViewport.addEventListener('scroll', syncAppViewportHeight, { passive: true });
    window.visualViewport.addEventListener('resize', scheduleHorizontalViewportLock, { passive: true });
    window.visualViewport.addEventListener('scroll', scheduleHorizontalViewportLock, { passive: true });
}

let userId = null, selectedFiles = [], currentDraftId = null, hasAnalysis = false;
let fillPollTimer = null;
let sharedStatePollTimer = null;
let mobileAnalyzeInFlight = false;
let storageCreateInFlight = false;
const isMobile = window.innerWidth < 1024;
const device = isMobile ? 'mobile' : 'desktop';

function trackAppAction(action, name='', value) {
    if (window.trackAutoListerEvent) window.trackAutoListerEvent('app', action, name || device, value);
}

// ── draft IDs stored separately so onclick can look them up without HTML escaping issues ──
const draftMap = {};
const photoSetMap = {};

async function loadCredits(userId) {
    if (!userId) return;
    try {
        const res = await fetch('/api/credits', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ userId })
        });
        const data = await res.json();
        if (data.success && data.credits !== "∞") {
            const btn = document.getElementById('buy-credits-btn');
            if (btn) btn.style.display = 'inline-block';
            document.getElementById('user-credits').textContent = 'Posts: ' + data.credits;
            document.getElementById('user-credits').style.display = 'inline-block';
        }
    } catch (e) {}
}

async function buyCredits() {
    try {
        trackAppAction('checkout-start');
        const res = await fetch('/api/checkout', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ userId })
        });
        const data = await res.json();
        if (data.success && data.url) {
            trackAppAction('checkout-redirect');
            window.location.href = data.url;
        } else {
            trackAppAction('checkout-error');
            alert("Checkout Error: " + data.error);
        }
    } catch (e) {
        trackAppAction('checkout-error');
        alert("Checkout Error.");
    }
}

async function init() {
    try {
        const res = await fetch('/api/auth/me');
        const result = await res.json();
        if (!result.success) {
            window.location.href = result.setup_required ? '/setup' : '/login';
            return;
        }
        const user = result.user;
        userId = user.$id;
        document.getElementById('user-email').innerText = user.email;
        loadCredits(userId);
        if (!isMobile) {
            document.getElementById('vnc-iframe').src =
                `/embedded-vnc?path=${encodeURIComponent(`vnc/${userId}/${device}`)}`;
        }
        await loadDraftsList();
        await loadPhotoStorageList();
        scheduleSharedStateRefresh();
        tutMaybeShow();
    } catch(e) { window.location.href = '/login'; }
}

async function signOut() {
    trackAppAction('logout');
    window.location.href = '/api/auth/logout';
}

function openMobileFacebookBrowser() {
    if (!userId) return;
    trackAppAction('open-browser', 'mobile');
    window.open(`/embedded-vnc?path=${encodeURIComponent(`vnc/${userId}/${device}`)}`, '_blank', 'noopener');
}

// ── DRAFTS ──
async function loadDraftsList() {
    try {
        const res = await fetch(`/api/drafts?userId=${userId}`);
        const data = await res.json();
        if (data.success && data.drafts.length) renderDraftsList(data.drafts);
        else {
            const mD = document.getElementById('mDraftsDrawer');
            if (mD) mD.style.display = 'none';
            const dD = document.getElementById('dDraftsSection');
            if (dD) dD.style.display = 'none';
        }
    } catch(e) {}
}

async function loadPhotoStorageList() {
    try {
        const res = await fetch(`/api/photo-storage?userId=${userId}`);
        const data = await res.json();
        renderPhotoStorageList(data.success ? (data.photo_sets || []) : []);
    } catch(e) {
        renderPhotoStorageList([]);
    }
}

function renderDraftsList(drafts) {
    drafts.forEach(d => { draftMap[d.id] = d.id; });

    if (isMobile) {
        const drawer = document.getElementById('mDraftsDrawer');
        const body = document.getElementById('mDraftsBody');
        const countEl = document.getElementById('mDraftsCount');
        drawer.style.display = 'block';
        countEl.textContent = `${drafts.length} Saved Draft${drafts.length > 1 ? 's' : ''}`;
        body.innerHTML = drafts.map(d => `
            <div class="draft-item" onclick="resumeDraft('${d.id}')">
                <div class="draft-icon">📦</div>
                <div class="draft-info">
                    <div class="draft-title">${escHtml(d.title||'Untitled')}</div>
                    <div class="draft-meta">${escHtml(draftMetaText(d))}</div>
                </div>
                <div class="draft-actions">
                    <button class="draft-resume-btn" onclick="event.stopPropagation();resumeDraft('${d.id}')">${d.status === 'photos_only' ? 'Analyze' : 'Resume'}</button>
                    <button class="draft-delete-btn" onclick="event.stopPropagation();deleteDraft('${d.id}')">Delete</button>
                </div>
            </div>
        `).join('');
    } else {
        const sec = document.getElementById('dDraftsSection');
        const list = document.getElementById('dDraftsList');
        sec.style.display = 'block';
        list.innerHTML = drafts.map(d => `
            <div class="d-draft-item" onclick="resumeDraft('${d.id}')">
                <div class="d-draft-icon">📦</div>
                <div style="flex:1;min-width:0;">
                    <div class="d-draft-title">${escHtml(d.title||'Untitled')}</div>
                    <div class="d-draft-meta">${escHtml(draftMetaText(d))}</div>
                </div>
                <div class="d-draft-actions">
                    <span class="d-draft-badge" onclick="event.stopPropagation();resumeDraft('${d.id}')">${d.status === 'photos_only' ? 'Analyze' : 'Resume'}</span>
                    <button class="d-draft-delete" onclick="event.stopPropagation();deleteDraft('${d.id}')">Delete</button>
                </div>
            </div>
        `).join('');
    }
}

function toggleDraftsDrawer() {
    const body = document.getElementById('mDraftsBody');
    const chev = document.getElementById('mDraftsChevron');
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
    chev.classList.toggle('open', !open);
}

function renderPhotoStorageList(photoSets) {
    photoSets.forEach(s => { photoSetMap[s.id] = s.id; });

    if (isMobile) {
        const drawer = document.getElementById('mStorageDrawer');
        const body = document.getElementById('mStorageBody');
        const countEl = document.getElementById('mStorageCount');
        if (!photoSets.length) {
            drawer.style.display = 'none';
            return;
        }
        drawer.style.display = 'block';
        countEl.textContent = `${photoSets.length} Photo Set${photoSets.length > 1 ? 's' : ''}`;
        body.innerHTML = photoSets.map(s => `
            <div class="draft-item" onclick="createListingFromStorage('${s.id}')">
                <div class="draft-icon">🖼️</div>
                <div class="draft-info">
                    <div class="draft-title">${escHtml(photoSetTitle(s))}</div>
                    <div class="draft-meta">${escHtml(photoSetMetaText(s))}</div>
                </div>
                <div class="draft-actions">
                    <button type="button" class="draft-resume-btn" onclick="event.stopPropagation();createListingFromStorage('${s.id}')">Analyze</button>
                </div>
            </div>
        `).join('');
    } else {
        const list = document.getElementById('dStorageList');
        list.innerHTML = photoSets.map(s => `
            <div class="d-draft-item" onclick="createListingFromStorage('${s.id}')">
                <div class="d-draft-icon">🖼️</div>
                <div style="flex:1;min-width:0;">
                    <div class="d-draft-title">${escHtml(photoSetTitle(s))}</div>
                    <div class="d-draft-meta">${escHtml(photoSetMetaText(s))}</div>
                </div>
                <div class="d-draft-actions">
                    <button type="button" class="d-storage-badge" onclick="event.stopPropagation();createListingFromStorage('${s.id}')">Analyze</button>
                </div>
            </div>
        `).join('');
    }
}

function toggleStorageDrawer() {
    const body = document.getElementById('mStorageBody');
    const chev = document.getElementById('mStorageChevron');
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : 'block';
    chev.classList.toggle('open', !open);
}

async function resumeDraft(draftId) {
    try {
        trackAppAction('draft-resume-start');
        if (isMobile) {
            showStep('mStep2');
            setMStatus('Loading draft…');
            document.getElementById('mResults').style.display = 'none';
        } else {
            setDStatus('Loading draft…');
            document.getElementById('dResults').style.display = 'none';
        }
        const res = await fetch('/api/resume', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ userId, draftId }),
        });
        const result = await res.json();
        if (!result.success) { trackAppAction('draft-resume-error'); alert('Could not resume: ' + result.error); return; }
        currentDraftId = result.draft_id; hasAnalysis = true;
        trackAppAction('draft-resume-success');
        if (isMobile) {
            document.getElementById('mDraftsDrawer').style.display = 'none';
            showStep('mStep2');
            setMStatus('');
            renderMobileResults(result.details);
        } else {
            renderDesktopResults(result.details);
            setDStatus('Draft resumed — review and post.', 'ok');
        }
    } catch(e) { trackAppAction('draft-resume-error'); alert('Error resuming draft.'); }
}

async function deleteDraft(draftId) {
    if (!confirm('Delete this saved draft?')) return;
    try {
        trackAppAction('draft-delete-start');
        const res = await fetch('/api/delete-draft', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ userId, draftId }),
        });
        const result = await res.json();
        if (!result.success) { trackAppAction('draft-delete-error'); alert('Could not delete draft: ' + result.error); return; }
        trackAppAction('draft-delete-success');
        if (currentDraftId === draftId) {
            currentDraftId = null;
            hasAnalysis = false;
            const dResults = document.getElementById('dResults');
            const mResults = document.getElementById('mResults');
            if (dResults) dResults.style.display = 'none';
            if (mResults) mResults.style.display = 'none';
        }
        await loadDraftsList();
        if (isMobile) setMStatus('Draft deleted.', 'ok');
        else setDStatus('Draft deleted.', 'ok');
    } catch(e) { trackAppAction('draft-delete-error'); alert('Error deleting draft.'); }
}

// ── HELPERS ──
function escHtml(v) {
    return String(v??'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function confColor(c) { return c===null?'#94a3b8':c>=80?'#22c55e':c>=55?'#f59e0b':'#ef4444'; }
function fmtAge(iso) {
    if (!iso) return '';
    const s = (Date.now() - new Date(iso+'Z').getTime()) / 1000;
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s/60)+'m ago';
    if (s < 86400) return Math.floor(s/3600)+'h ago';
    return Math.floor(s/86400)+'d ago';
}
function buildCompsHtml(comps) {
    if (!comps?.length) return '<li style="color:var(--muted);">No comps found.</li>';
    return comps.map(c => {
        const price = c.price==null ? 'unknown' : `$${c.price}`;
        const link = c.url ? ` <a href="${escHtml(c.url)}" target="_blank" rel="noreferrer">↗</a>` : '';
        return `<li><strong>${escHtml(c.source||'')}</strong> — ${escHtml(c.title||'')} (${escHtml(price)})${link}</li>`;
    }).join('');
}

function draftMetaText(d) {
    if (d.status === 'photos_only') {
        const count = Number(d.photo_count || (d.image_paths || []).length || 0);
        return `${count} photo${count === 1 ? '' : 's'} · ${fmtAge(d.saved_at)} · Needs analysis`;
    }
    const base = `${d.price != null ? '$'+d.price+' · ' : ''}${fmtAge(d.saved_at)}`;
    return d.missing_images ? `${base} · Missing ${d.missing_images} photo${d.missing_images > 1 ? 's' : ''}` : base;
}

function photoSetTitle(s) {
    const count = Number(s.photo_count || (s.image_paths || []).length || 0);
    return count === 1 ? '1 stored photo' : `${count} stored photos`;
}

function photoSetMetaText(s) {
    const count = Number(s.photo_count || (s.image_paths || []).length || 0);
    return `${count} photo${count === 1 ? '' : 's'} · ${fmtAge(s.saved_at)}`;
}

function setStorageStatus(msg, type='busy') {
    const id = isMobile ? 'mStorageStatus' : 'dStorageStatus';
    const el = document.getElementById(id);
    if (!el) return;
    if (!msg) {
        el.style.display = 'none';
        return;
    }
    el.className = (isMobile ? 'status-pill' : 'd-status') + (type === 'ok' ? ' ok' : type === 'err' ? ' err' : ' busy');
    if (isMobile) {
        const spinner = type === 'busy' ? '<span class="spin">⟳</span> ' : '';
        el.innerHTML = spinner + escHtml(msg);
    } else {
        el.textContent = msg;
    }
    el.style.display = 'block';
}

function stopFillPolling() {
    if (fillPollTimer) {
        clearTimeout(fillPollTimer);
        fillPollTimer = null;
    }
}

function scheduleSharedStateRefresh() {
    if (sharedStatePollTimer) clearTimeout(sharedStatePollTimer);
    sharedStatePollTimer = setTimeout(async () => {
        if (userId) {
            await loadDraftsList();
            await loadPhotoStorageList();
        }
        scheduleSharedStateRefresh();
    }, 4000);
}

function buildFillStatusHtml(data) {
    const entries = (data.entries || []).slice(-6).reverse().map(entry => `
        <div class="fill-log-row">
            <strong>${escHtml(entry.progress != null ? entry.progress + '%' : '')}</strong>
            <span>${escHtml(entry.message || '')}</span>
        </div>
    `).join('');
    return `
        <div class="fill-head">
            <div class="fill-title">${escHtml(data.state === 'error' ? 'Facebook Fill Failed' : data.state === 'complete' ? 'Facebook Fill Complete' : 'Filling Facebook Draft')}</div>
            <div class="fill-pct">${Number.isFinite(Number(data.progress)) ? Number(data.progress) + '%' : ''}</div>
        </div>
        <div class="fill-msg">${escHtml(data.message || '')}</div>
        <div class="fill-bar"><span style="width:${Math.max(0, Math.min(100, Number(data.progress) || 0))}%;"></span></div>
        <div class="fill-log">${entries || '<div class="fill-log-row"><span>No steps yet.</span></div>'}</div>
    `;
}

function showFillStatus(data) {
    const id = isMobile ? 'mFillStatus' : 'dFillStatus';
    const el = document.getElementById(id);
    if (!el) return;
    if (!data || !data.state || data.state === 'idle') {
        el.style.display = 'none';
        return;
    }
    el.innerHTML = buildFillStatusHtml(data);
    el.style.display = 'flex';
}

async function pollFillStatus() {
    stopFillPolling();
    try {
        const res = await fetch(`/api/fill-status?userId=${encodeURIComponent(userId)}`);
        const data = await res.json();
        if (!data.success) return;
        showFillStatus(data);
        if (data.state === 'queued' || data.state === 'running') {
            fillPollTimer = setTimeout(pollFillStatus, 900);
            return;
        }
        if (data.state === 'complete' || data.state === 'error') {
            trackAppAction('facebook-fill-' + data.state, device, Number(data.progress) || 0);
        }
        const postBtn = document.getElementById(isMobile ? 'mPostBtn' : 'dPostBtn');
        if (postBtn) {
            postBtn.disabled = false;
            postBtn.textContent = isMobile ? 'Post to Facebook Marketplace' : 'Post to Facebook';
        }
        if (!isMobile) {
            setDStatus(data.state === 'complete' ? 'Facebook draft filled. Review it in the browser panel.' : (data.message || 'Facebook fill stopped.'), data.state === 'complete' ? 'ok' : 'err');
        } else {
            const st = document.getElementById('mPostStatus');
            st.className = 'status-pill ' + (data.state === 'complete' ? 'ok' : 'err');
            st.textContent = data.message || (data.state === 'complete' ? 'Facebook draft filled.' : 'Facebook fill failed.');
            st.style.display = 'flex';
        }
    } catch(e) {
        fillPollTimer = setTimeout(pollFillStatus, 1500);
    }
}

// ══ MOBILE ══
function showStep(id) {
    document.querySelectorAll('.m-step').forEach(s => s.classList.remove('active'));
    document.getElementById(id).classList.add('active');
}

function setMStatus(msg, type='busy') {
    const el = document.getElementById('mStatus');
    if (!msg) { el.style.display='none'; return; }
    el.className = 'status-pill ' + type;
    const spinner = type==='busy' ? '<span class="spin">⟳</span> ' : '';
    el.innerHTML = spinner + escHtml(msg);
    el.style.display = 'flex';
}

function handleFiles(files) {
    if (!files?.length) return;
    trackAppAction('photos-selected', device, files.length);
    for (const f of files) selectedFiles.push(f);
    updateMobilePhotoSelection();
    document.getElementById('photoInput').value = '';
    if (isMobile) mobileAnalyze(true);
}

function handleStorageFiles(files, input) {
    if (!files?.length) return;
    trackAppAction('storage-upload-selected', device, files.length);
    uploadPhotosToStorage(files).finally(() => {
        if (input) input.value = '';
    });
}

function handleDraftUploadFiles(files, input) {
    if (!files?.length) return;
    trackAppAction('draft-upload-selected', device, files.length);
    uploadPhotosToDrafts(files).finally(() => {
        if (input) input.value = '';
    });
}

function updateMobilePhotoSelection() {
    const count = selectedFiles.length;
    const countEl = document.getElementById('mPhotoCount');
    const shell = document.getElementById('mPreviewsWrap');
    const wrap = document.getElementById('mPreviews');
    if (countEl) countEl.textContent = `${count} photo${count === 1 ? '' : 's'}`;
    if (wrap) wrap.innerHTML = '';
    for (const f of selectedFiles) {
        const img = document.createElement('img');
        img.src = URL.createObjectURL(f);
        wrap.appendChild(img);
    }
    document.getElementById('mAnalyzeBtn').style.display = count ? 'block' : 'none';
    document.getElementById('addMoreBtn').style.display = count ? 'block' : 'none';
    document.getElementById('mAnalyzeTray').style.display = count ? 'block' : 'none';
    if (shell) shell.style.display = count ? 'block' : 'none';
}

async function mobileAnalyze(autoTriggered=false) {
    if (!selectedFiles.length || mobileAnalyzeInFlight) return;
    mobileAnalyzeInFlight = true;
    trackAppAction(autoTriggered ? 'analyze-auto-start' : 'analyze-start', 'mobile', selectedFiles.length);
    showStep('mStep2');
    setMStatus(autoTriggered ? 'Uploading photos and auto-analyzing…' : 'Analyzing photos and looking up comps…');
    document.getElementById('mResults').style.display = 'none';
    document.getElementById('mPostCard').style.display = 'none';
    document.getElementById('mPostStatus').style.display = 'none';
    document.getElementById('mFillStatus').style.display = 'none';
    document.getElementById('mAnalyzeBtn').disabled = true;
    document.getElementById('addMoreBtn').disabled = true;
    const fd = new FormData();
    for (const f of selectedFiles) fd.append('files', f);
    try {
        const res = await fetch(`/api/upload?userId=${userId}&device=${device}`, {method:'POST',body:fd});
        const r = await res.json();
        if (r.success) { trackAppAction('analyze-success', 'mobile'); currentDraftId = r.draft_id; setMStatus(''); renderMobileResults(r.details); loadDraftsList(); }
        else { trackAppAction('analyze-error', 'mobile'); setMStatus(r.error, 'err'); }
    } catch(e) { trackAppAction('analyze-error', 'mobile'); setMStatus('Network error. Try again.', 'err'); }
    finally {
        mobileAnalyzeInFlight = false;
        document.getElementById('mAnalyzeBtn').disabled = false;
        document.getElementById('addMoreBtn').disabled = false;
    }
}

async function uploadPhotosToStorage(files) {
    setStorageStatus('Uploading to storage…');
    trackAppAction('storage-upload-start', device, files.length);
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    try {
        const res = await fetch(`/api/photo-storage/upload?userId=${userId}&device=${device}`, { method: 'POST', body: fd });
        const result = await res.json();
        if (!result.success) {
            trackAppAction('storage-upload-error');
            setStorageStatus(result.error || 'Upload failed.', 'err');
            return;
        }
        trackAppAction('storage-upload-success', device, files.length);
        setStorageStatus('Stored.', 'ok');
        await loadPhotoStorageList();
    } catch(e) {
        trackAppAction('storage-upload-error');
        setStorageStatus('Network error. Try again.', 'err');
    }
}

async function uploadPhotosToDrafts(files) {
    setStorageStatus('Uploading to drafts…');
    trackAppAction('draft-upload-start', device, files.length);
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    try {
        const res = await fetch(`/api/drafts/upload?userId=${userId}&device=${device}`, { method: 'POST', body: fd });
        const result = await res.json();
        if (!result.success) {
            trackAppAction('draft-upload-error');
            setStorageStatus(result.error || 'Upload failed.', 'err');
            return;
        }
        trackAppAction('draft-upload-success', device, files.length);
        setStorageStatus('Draft saved.', 'ok');
        await loadDraftsList();
    } catch(e) {
        trackAppAction('draft-upload-error');
        setStorageStatus('Network error. Try again.', 'err');
    }
}

function renderMobileResults(d) {
    hasAnalysis = true;
    const conf = Number.isFinite(Number(d.confidence)) ? Number(d.confidence) : null;
    const cc = confColor(conf);
    document.getElementById('mResultPanel').innerHTML = `
        <div class="result-card-hero">
            <div style="flex:1;min-width:0;">
                <div style="font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--muted);font-weight:600;margin-bottom:4px;">Item identified</div>
                <div class="result-item-title">${escHtml(d.title||'Unknown item')}</div>
            </div>
            <div style="text-align:right;flex-shrink:0;">
                <div class="result-price-label">Suggested</div>
                <div class="result-price">${d.price!=null?'$'+d.price:'—'}</div>
            </div>
        </div>
        <div class="result-body">
            <div class="meta-grid">
                <div class="meta-chip">
                    <div class="meta-chip-label">Condition</div>
                    <div class="meta-chip-value">${escHtml(d.condition||'—')}</div>
                </div>
                <div class="meta-chip">
                    <div class="meta-chip-label">Retail price</div>
                    <div class="meta-chip-value">${d.original_price!=null?'$'+d.original_price:'—'}</div>
                </div>
            </div>
            ${conf!==null ? `
            <div>
                <div class="conf-row">
                    <span class="conf-label">AI Confidence</span>
                    <span class="conf-score" style="color:${cc};">${conf}/100</span>
                </div>
                <div class="conf-bar-bg"><div class="conf-bar" style="width:${conf}%;background:${cc};"></div></div>
                <div class="conf-reason">${escHtml(d.confidence_reason||'')}</div>
            </div>` : ''}
            <div>
                <div class="section-label">Generated description</div>
                <div class="draft-description">${escHtml(d.description||'—')}</div>
            </div>
            <div>
                <div class="section-label">Pricing rationale</div>
                <div class="pricing-text">${escHtml(d.pricing_summary||'—')}</div>
            </div>
            <div>
                <div class="section-label">Used comps</div>
                <ol class="comp-list">${buildCompsHtml(d.used_comparables)}</ol>
            </div>
        </div>
    `;
    document.getElementById('mPostCard').style.display = 'block';
    document.getElementById('mPostStatus').style.display = 'none';
    const btn = document.getElementById('mPostBtn');
    btn.disabled = false; btn.textContent = 'Post to Facebook Marketplace';
    document.getElementById('mResults').style.display = 'flex';
}

async function mobilePost() {
    stopFillPolling();
    trackAppAction('facebook-fill-start', 'mobile');
    const btn = document.getElementById('mPostBtn');
    const st = document.getElementById('mPostStatus');
    btn.disabled = true; btn.textContent = 'Posting…';
    st.className = 'status-pill busy';
    st.innerHTML = '<span class="spin">⟳</span> Starting Facebook fill…';
    st.style.display = 'flex';
    try {
        const res = await fetch('/api/create-draft', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ userId, device }),
        });
        const r = await res.json();
        if (r.success) {
            trackAppAction('facebook-fill-queued', 'mobile');
            st.className = 'status-pill busy';
            st.innerHTML = '<span class="spin">⟳</span> Playwright is working through the Facebook form…';
            showFillStatus({ state: 'queued', progress: 1, message: 'Queued Facebook fill', entries: [] });
            pollFillStatus();
            loadDraftsList();
        } else {
            trackAppAction('facebook-fill-error', 'mobile');
            st.className = 'status-pill err'; st.textContent = r.error;
            btn.disabled = false; btn.textContent = 'Post to Facebook Marketplace';
        }
    } catch(e) {
        trackAppAction('facebook-fill-error', 'mobile');
        st.className = 'status-pill err'; st.textContent = 'Network error. Try again.';
        btn.disabled = false; btn.textContent = 'Post to Facebook Marketplace';
    }
}

async function mobileRefine() {
    const correction = document.getElementById('mCorrectionBox').value.trim();
    if (!correction) { alert('Add a correction first.'); return; }
    trackAppAction('refine-start', 'mobile');
    setMStatus('Re-running with your correction…');
    document.getElementById('mResults').style.display = 'none';
    try {
        const res = await fetch('/api/refine', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ userId, correction }),
        });
        const r = await res.json();
        if (r.success) { trackAppAction('refine-success', 'mobile'); setMStatus(''); renderMobileResults(r.details); }
        else { trackAppAction('refine-error', 'mobile'); setMStatus(r.error, 'err'); }
    } catch(e) { trackAppAction('refine-error', 'mobile'); setMStatus('Network error.', 'err'); }
}

function mobileReset() {
    trackAppAction('reset', 'mobile');
    selectedFiles = []; hasAnalysis = false; currentDraftId = null;
    mobileAnalyzeInFlight = false;
    stopFillPolling();
    document.getElementById('mPreviews').innerHTML = '';
    document.getElementById('mPreviewsWrap').style.display = 'none';
    document.getElementById('mAnalyzeBtn').style.display = 'none';
    document.getElementById('mAnalyzeTray').style.display = 'none';
    document.getElementById('addMoreBtn').style.display = 'none';
    document.getElementById('mStatus').style.display = 'none';
    document.getElementById('mResults').style.display = 'none';
    document.getElementById('mCorrectionBox').value = '';
    document.getElementById('mPostCard').style.display = 'none';
    document.getElementById('mPostStatus').style.display = 'none';
    document.getElementById('mFillStatus').style.display = 'none';
    document.getElementById('mPhotoCount').textContent = '0 photos';
    showStep('mStep1'); loadDraftsList();
}

// ══ DESKTOP ══
function setDStatus(msg, type='') {
    const el = document.getElementById('dStatus');
    if (!msg) { el.style.display='none'; return; }
    el.className = 'd-status ' + type;
    el.textContent = msg;
    el.style.display = 'block';
}

function desktopFileChange(files) {
    const prev = document.getElementById('dPreviews');
    prev.innerHTML = '';
    document.getElementById('dResults').style.display = 'none';
    setDStatus('');
    if (files.length) {
        trackAppAction('photos-selected', 'desktop', files.length);
        for (const f of files) {
            const img = document.createElement('img');
            img.src = URL.createObjectURL(f);
            prev.appendChild(img);
        }
        document.getElementById('dAnalyzeBtn').style.display = 'block';
    }
}

function renderDesktopResults(d) {
    const conf = Number.isFinite(Number(d.confidence)) ? Number(d.confidence) : null;
    const cc = confColor(conf);
    document.getElementById('dResultBlock').innerHTML = `
        <div class="d-result-subtitle">Facebook Draft Preview</div>
        <div class="d-result-draft">
            <div class="d-result-row"><span class="d-result-key">Title</span><span class="d-result-val">${escHtml(d.title||'—')}</span></div>
            <div class="d-result-row"><span class="d-result-key">Price</span><span class="d-result-val" style="color:#34d399;font-size:1.1rem;">${d.price!=null?'$'+d.price:'—'}</span></div>
            <div class="d-result-row"><span class="d-result-key">Category</span><span class="d-result-val">${escHtml(d.category||'—')}</span></div>
            <div class="d-result-row"><span class="d-result-key">Condition</span><span class="d-result-val">${escHtml(d.condition||'—')}</span></div>
            <div>
                <div class="d-result-key" style="margin-bottom:0.35rem;">Description</div>
                <div class="d-draft-desc">${escHtml(d.description||'—')}</div>
            </div>
        </div>
        <div class="d-result-subtitle" style="margin-top:0.35rem;">Analysis Signals</div>
        <div class="d-result-row"><span class="d-result-key">Title</span><span class="d-result-val">${escHtml(d.title||'—')}</span></div>
        <div class="d-result-row"><span class="d-result-key">Retail</span><span class="d-result-val">${d.original_price!=null?'$'+d.original_price:'—'} ${d.product_url?`<a href="${escHtml(d.product_url)}" target="_blank" rel="noreferrer">↗</a>`:''}</span></div>
        <div class="d-result-row"><span class="d-result-key">Confidence</span><span class="d-result-val" style="color:${cc};">${conf??'—'}/100</span></div>
        <div style="font-size:0.8rem;color:var(--muted);line-height:1.4;">${escHtml(d.pricing_summary||'')}</div>
        <div style="margin-top:0.25rem;"><ol class="comp-list">${buildCompsHtml(d.used_comparables)}</ol></div>
    `;
    document.getElementById('dResults').style.display = 'flex';
    hasAnalysis = true;
}

async function desktopAnalyze() {
    const files = document.getElementById('fileInput').files;
    if (!files.length) return;
    trackAppAction('analyze-start', 'desktop', files.length);
    setDStatus('Analyzing with AI…');
    document.getElementById('dAnalyzeBtn').disabled = true;
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    try {
        const res = await fetch(`/api/upload?userId=${userId}&device=${device}`, {method:'POST',body:fd});
        const r = await res.json();
        if (r.success) { trackAppAction('analyze-success', 'desktop'); currentDraftId = r.draft_id; renderDesktopResults(r.details); setDStatus('Review and correct if needed.'); loadDraftsList(); }
        else { trackAppAction('analyze-error', 'desktop'); setDStatus(r.error, 'err'); }
    } catch(e) { trackAppAction('analyze-error', 'desktop'); setDStatus('Error analyzing.', 'err'); }
    document.getElementById('dAnalyzeBtn').disabled = false;
}

async function createListingFromStorage(photoSetId) {
    if (!photoSetId || storageCreateInFlight) return false;
    storageCreateInFlight = true;
    trackAppAction('storage-create-listing-start');
    if (isMobile) {
        showStep('mStep2');
        setMStatus('Analyzing stored photos…');
        document.getElementById('mResults').style.display = 'none';
        document.getElementById('mPostCard').style.display = 'none';
        document.getElementById('mPostStatus').style.display = 'none';
        document.getElementById('mFillStatus').style.display = 'none';
    } else {
        setDStatus('Analyzing stored photos…');
        document.getElementById('dResults').style.display = 'none';
    }
    try {
        const res = await fetch('/api/photo-storage/create-listing', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ userId, photoSetId }),
        });
        const result = await res.json();
        if (!result.success) {
            trackAppAction('storage-create-listing-error');
            if (isMobile) setMStatus(result.error || 'Could not create listing.', 'err');
            else setDStatus(result.error || 'Could not create listing.', 'err');
            return false;
        }
        currentDraftId = result.draft_id;
        hasAnalysis = true;
        trackAppAction('storage-create-listing-success');
        await loadDraftsList();
        if (isMobile) {
            setMStatus('');
            renderMobileResults(result.details);
        } else {
            renderDesktopResults(result.details);
            setDStatus('Listing created from stored photos.', 'ok');
        }
        return true;
    } catch(e) {
        trackAppAction('storage-create-listing-error');
        if (isMobile) setMStatus('Network error. Try again.', 'err');
        else setDStatus('Network error. Try again.', 'err');
        return false;
    } finally {
        storageCreateInFlight = false;
    }
}

async function desktopRefine() {
    const correction = document.getElementById('correctionBox').value.trim();
    if (!correction) { setDStatus('Add a correction first.'); return; }
    trackAppAction('refine-start', 'desktop');
    setDStatus('Re-running with correction…');
    try {
        const res = await fetch('/api/refine', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ userId, correction }),
        });
        const r = await res.json();
        if (r.success) { trackAppAction('refine-success', 'desktop'); renderDesktopResults(r.details); setDStatus('Updated.', 'ok'); }
        else { trackAppAction('refine-error', 'desktop'); setDStatus(r.error, 'err'); }
    } catch(e) { trackAppAction('refine-error', 'desktop'); setDStatus('Error.', 'err'); }
}

async function desktopDraft() {
    stopFillPolling();
    trackAppAction('facebook-fill-start', 'desktop');
    const btn = document.getElementById('dPostBtn');
    if (btn) btn.disabled = true;
    setDStatus('Posting to Facebook…');
    try {
        const res = await fetch('/api/create-draft', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ userId, device }),
        });
        const r = await res.json();
        if (r.success) {
            trackAppAction('facebook-fill-queued', 'desktop');
            setDStatus('Playwright is filling the Facebook form now…');
            showFillStatus({ state: 'queued', progress: 1, message: 'Queued Facebook fill', entries: [] });
            pollFillStatus();
            loadDraftsList();
        }
        else {
            trackAppAction('facebook-fill-error', 'desktop');
            if (btn) btn.disabled = false;
            setDStatus(r.error, 'err');
        }
    } catch(e) {
        trackAppAction('facebook-fill-error', 'desktop');
        if (btn) btn.disabled = false;
        setDStatus('Error.', 'err');
    }
}

async function desktopRevealPublish() {
    trackAppAction('publish-reveal-start', 'desktop');
    setDStatus('Scrolling the Facebook session to the Publish button…');
    try {
        const res = await fetch('/api/reveal-publish', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ userId, device }),
        });
        const r = await res.json();
        if (r.success) { trackAppAction('publish-reveal-success', 'desktop'); setDStatus('Publish area revealed in the browser panel.', 'ok'); }
        else { trackAppAction('publish-reveal-error', 'desktop'); setDStatus(r.error, 'err'); }
    } catch(e) { trackAppAction('publish-reveal-error', 'desktop'); setDStatus('Error.', 'err'); }
}

// ── First-time Tutorial ──
const TUT_KEY = 'al_tutorial_v1';
const TUT_STEPS = [
    {
        icon: '⚡', color: '#3b82f6',
        label: 'Welcome',
        title: 'Auto-Lister',
        sub: 'Snap. Price. Post.',
        desc: 'Turn any item into a Facebook Marketplace listing in under a minute. This quick tour shows you exactly how.',
        bullets: [
            'Takes less than 60 seconds from photo to posted listing',
            'AI finds the item, researches comps, and sets a fair price',
            'Your drafts are saved so you can come back any time',
        ],
    },
    {
        icon: '📷', color: '#8b5cf6',
        label: 'Step 1 of 4',
        title: 'Add Photos',
        sub: 'Camera, gallery, or files',
        desc: 'Tap Add Photos and choose the source from your phone. You can add multiple photos — the AI uses all of them.',
        bullets: [
            'Multiple angles help the AI identify the item more accurately',
            'Analysis starts automatically as soon as you pick photos',
            'You can also store photos in advance and create listings later',
        ],
    },
    {
        icon: '🤖', color: '#10b981',
        label: 'Step 2 of 4',
        title: 'AI Prices It',
        sub: 'Identification + market research',
        desc: 'The AI identifies the exact item, finds the original retail price, searches real used comps, and suggests a fair listing price.',
        bullets: [
            'You see the item title, condition, category, and suggested price',
            'Pricing rationale and used comparables are shown in full',
            'An AI confidence score tells you how certain the match is',
        ],
    },
    {
        icon: '✏️', color: '#f59e0b',
        label: 'Step 3 of 4',
        title: 'Review & Correct',
        sub: 'Tweak anything that looks off',
        desc: 'If anything in the AI draft is wrong — wrong model, wrong size, wrong condition — just type a correction and hit Re-analyze.',
        bullets: [
            'Corrections are applied on top of the previous analysis',
            'The AI favors your correction over its own initial guess',
            'Re-analyze as many times as you need',
        ],
    },
    {
        icon: '🚀', color: '#06b6d4',
        label: 'Step 4 of 4',
        title: 'Post to Facebook',
        sub: 'One tap, fully automated',
        desc: 'Tap "Post to Facebook Marketplace" and a Playwright automation fills the entire listing form in your Facebook session — title, price, category, description, and photos.',
        bullets: [
            'Watch the progress bar as each field gets filled in',
            'On desktop you can see the browser session live on the left',
            'Drafts are auto-saved — resume from the Drafts section any time',
        ],
    },
];
let tutCurrent = 0;

function tutRender() {
    const s = TUT_STEPS[tutCurrent];
    const total = TUT_STEPS.length;
    const pct = ((tutCurrent + 1) / total) * 100;

    const prog = document.getElementById('tutProgress');
    prog.style.width = pct + '%';
    prog.style.background = `linear-gradient(90deg, ${s.color}cc, ${s.color})`;

    const icon = document.getElementById('tutIcon');
    icon.textContent = s.icon;
    icon.style.background = s.color + '18';
    icon.style.border = '1px solid ' + s.color + '30';
    icon.style.boxShadow = '0 0 20px ' + s.color + '14';

    document.getElementById('tutLabel').textContent = s.label;
    document.getElementById('tutLabel').style.color = s.color;
    document.getElementById('tutTitle').textContent = s.title;
    document.getElementById('tutSub').textContent = s.sub;
    document.getElementById('tutSub').style.color = s.color + 'bb';
    document.getElementById('tutDesc').textContent = s.desc;

    const bulletsEl = document.getElementById('tutBullets');
    bulletsEl.innerHTML = s.bullets.map(b => `
        <div class="tut-bullet">
            <span class="tut-bullet-dot" style="background:${s.color};"></span>
            <span>${b}</span>
        </div>
    `).join('');

    const dotsEl = document.getElementById('tutDots');
    dotsEl.innerHTML = TUT_STEPS.map((_, i) => `
        <div class="tut-dot" onclick="tutGoTo(${i})"
             style="width:${i === tutCurrent ? '20px' : '6px'}; background:${i === tutCurrent ? s.color : i < tutCurrent ? s.color + '50' : '#333'};">
        </div>
    `).join('');

    const backBtn = document.getElementById('tutBack');
    backBtn.style.visibility = tutCurrent === 0 ? 'hidden' : 'visible';

    const nextBtn = document.getElementById('tutNext');
    const isLast = tutCurrent === total - 1;
    nextBtn.textContent = isLast ? 'Get started ' : 'Next ';
    nextBtn.textContent += isLast ? '✓' : '→';
    nextBtn.style.background = `linear-gradient(135deg, ${s.color}, ${s.color}bb)`;
    nextBtn.style.boxShadow = '0 4px 14px ' + s.color + '28';
}

function tutStep(dir) {
    const next = tutCurrent + dir;
    if (next >= TUT_STEPS.length) { tutDismiss(); return; }
    if (next < 0) return;
    tutCurrent = next;
    trackAppAction('tutorial-step', String(tutCurrent + 1));
    tutRender();
}

function tutGoTo(i) { tutCurrent = i; trackAppAction('tutorial-step', String(tutCurrent + 1)); tutRender(); }

function tutDismiss() {
    trackAppAction('tutorial-dismiss', String(tutCurrent + 1));
    localStorage.setItem(TUT_KEY, '1');
    document.getElementById('tutOverlay').style.display = 'none';
}

function tutMaybeShow() {
    if (!localStorage.getItem(TUT_KEY)) {
        tutCurrent = 0;
        tutRender();
        document.getElementById('tutOverlay').style.display = 'flex';
        trackAppAction('tutorial-open');
    }
}

init();
</script>
</body>
</html>
"""

import uuid
import shutil
from datetime import datetime

DRAFTS_DIR = os.path.join(BASE_DIR, "drafts")
PHOTO_STORAGE_DIR = os.path.join(BASE_DIR, "photo_storage")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
SECRET_FILE = os.path.join(BASE_DIR, "secret.key")
SESSION_COOKIE = "auto_lister_session"
os.makedirs(DRAFTS_DIR, exist_ok=True)
os.makedirs(PHOTO_STORAGE_DIR, exist_ok=True)


def _read_json_file(path: str, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return fallback
    except json.JSONDecodeError:
        return fallback


def _write_json_file(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_users() -> List[dict]:
    users = _read_json_file(USERS_FILE, [])
    return users if isinstance(users, list) else []


def _save_users(users: List[dict]):
    _write_json_file(USERS_FILE, users)


def local_setup_required() -> bool:
    return AUTH_PROVIDER == "local" and len(_load_users()) == 0


def get_secret_key() -> bytes:
    configured = os.environ.get("AUTO_MARKETPLACE_SECRET_KEY", "").strip()
    if configured:
        return configured.encode("utf-8")
    try:
        with open(SECRET_FILE, "rb") as f:
            value = f.read().strip()
            if value:
                return value
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(48).encode("utf-8")
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    with open(SECRET_FILE, "wb") as f:
        f.write(value)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return value


def hash_password(password: str, salt: Optional[str] = None) -> dict:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return {"salt": salt, "hash": digest.hex()}


def verify_password(password: str, password_hash: dict) -> bool:
    salt = password_hash.get("salt", "")
    expected = password_hash.get("hash", "")
    if not salt or not expected:
        return False
    actual = hash_password(password, salt)["hash"]
    return hmac.compare_digest(actual, expected)


def sign_session(user_id: str) -> str:
    payload = base64.urlsafe_b64encode(user_id.encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(get_secret_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def parse_session(token: str) -> Optional[str]:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(get_secret_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = payload + "=" * (-len(payload) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def public_user(user: dict) -> dict:
    return {
        "$id": user.get("id"),
        "email": user.get("email", ""),
        "name": user.get("name", ""),
    }


def current_local_user(request: Request) -> Optional[dict]:
    user_id = parse_session(request.cookies.get(SESSION_COOKIE, ""))
    if not user_id:
        return None
    return next((user for user in _load_users() if user.get("id") == user_id), None)


def current_session_user(request: Request) -> Optional[dict]:
    user_id = parse_session(request.cookies.get(SESSION_COOKIE, ""))
    if not user_id:
        return None
    return next((user for user in _load_users() if user.get("id") == user_id), None)


def oidc_enabled() -> bool:
    return bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_REDIRECT_URI)


def oidc_discovery() -> dict:
    response = requests.get(f"{OIDC_ISSUER}/.well-known/openid-configuration", timeout=10)
    response.raise_for_status()
    data = response.json()
    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        if not data.get(key):
            raise RuntimeError(f"OIDC discovery is missing {key}")
    return data


def cookie_max_age(seconds: int) -> int:
    return max(0, int(seconds))


def make_oidc_pending(return_to: str) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    payload = {
        "state": state,
        "verifier": verifier,
        "return_to": return_to if return_to.startswith("/") else "/dashboard",
        "exp": int(datetime.utcnow().timestamp()) + 600,
    }
    return state, verifier, sign_pending_payload(payload)


def sign_pending_payload(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(get_secret_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"


def parse_pending_payload(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    encoded, sig = token.rsplit(".", 1)
    expected = hmac.new(get_secret_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(datetime.utcnow().timestamp()):
        return None
    return payload


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def upsert_oidc_user(profile: dict) -> dict:
    subject = str(profile.get("sub") or "").strip()
    if not subject:
        raise RuntimeError("OIDC profile did not include a subject.")
    email = str(profile.get("email") or profile.get("preferred_username") or "").strip().lower()
    name = str(profile.get("name") or profile.get("preferred_username") or email or subject).strip()
    user_id = f"oidc:{subject}"
    users = _load_users()
    user = next((item for item in users if item.get("id") == user_id), None)
    if user:
        user.update({"name": name, "email": email, "provider": "oidc", "updated_at": datetime.utcnow().isoformat() + "Z"})
    else:
        user = {
            "id": user_id,
            "name": name,
            "email": email,
            "role": "user",
            "provider": "oidc",
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        users.append(user)
    _save_users(users)
    return user

CREDITS_FILE = os.path.join(BASE_DIR, "credits.json")
PAYMENTS_FILE = os.path.join(BASE_DIR, "payments.json")

def _load_credits() -> dict:
    return _read_json_file(CREDITS_FILE, {})

def _save_credits(data: dict):
    _write_json_file(CREDITS_FILE, data)

def _load_payments() -> dict:
    payments = _read_json_file(PAYMENTS_FILE, {})
    return payments if isinstance(payments, dict) else {}

def _save_payments(data: dict):
    _write_json_file(PAYMENTS_FILE, data)

def hosted_signup_index(user_id: str) -> Optional[int]:
    users = [user for user in _load_users() if user.get("id")]
    if not users:
        return None
    users.sort(key=lambda user: (user.get("created_at") or "", user.get("id") or ""))
    for index, user in enumerate(users):
        if user.get("id") == user_id:
            return index
    return None

def get_user_credits(user_id: str) -> int:
    credits = _load_credits()
    if user_id not in credits:
        signup_index = hosted_signup_index(user_id)
        eligible_for_free_posts = (
            signup_index < HOSTED_FREE_SIGNUP_LIMIT_COUNT
            if signup_index is not None
            else len(credits) < HOSTED_FREE_SIGNUP_LIMIT_COUNT
        )
        if eligible_for_free_posts:
            credits[user_id] = HOSTED_FREE_POSTS_COUNT
        else:
            credits[user_id] = 0
        _save_credits(credits)
    return credits[user_id]

def consume_credit(user_id: str) -> bool:
    credits = _load_credits()
    if credits.get(user_id, 0) > 0:
        credits[user_id] -= 1
        _save_credits(credits)
        return True
    return False

def add_credits(user_id: str, amount: int):
    credits = _load_credits()
    if user_id not in credits:
        credits[user_id] = 0
    credits[user_id] += amount
    _save_credits(credits)

def record_paid_credit(session: dict) -> bool:
    user_id = session.get("client_reference_id")
    if not user_id:
        return False
    session_id = session.get("id")
    payments = _load_payments()
    processed = payments.setdefault("processed_checkout_sessions", {})
    if session_id and session_id in processed:
        return False
    add_credits(user_id, 1)
    if session_id:
        processed[session_id] = {
            "user_id": user_id,
            "amount_total": session.get("amount_total"),
            "currency": session.get("currency"),
            "payment_status": session.get("payment_status"),
            "recorded_at": datetime.utcnow().isoformat() + "Z",
        }
        _save_payments(payments)
    return True

def copy_images_to_dir(image_paths: List[str], target_dir: str) -> List[str]:
    os.makedirs(target_dir, exist_ok=True)
    saved_paths = []
    used_names = set()
    for idx, src in enumerate(image_paths):
        base = os.path.basename(src) or f"image_{idx + 1}.jpg"
        if os.path.dirname(os.path.abspath(src)) == os.path.abspath(target_dir) and os.path.exists(src):
            saved_paths.append(src)
            used_names.add(base)
            continue
        name, ext = os.path.splitext(base)
        candidate = base
        suffix = 1
        while candidate in used_names or os.path.exists(os.path.join(target_dir, candidate)):
            candidate = f"{name}_{suffix}{ext}"
            suffix += 1
        dst = os.path.join(target_dir, candidate)
        try:
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
            saved_paths.append(dst)
            used_names.add(candidate)
        except Exception:
            saved_paths.append(src)
    return saved_paths

def drafts_for_user(user_id: str) -> List[dict]:
    results = []
    user_dir = os.path.join(DRAFTS_DIR, user_id)
    if not os.path.isdir(user_dir):
        return results
    for fname in os.listdir(user_dir):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(user_dir, fname)) as f:
                d = json.load(f)
            image_paths = d.get("image_paths", [])
            missing_images = sum(1 for path in image_paths if not os.path.exists(path))
            d["missing_images"] = missing_images
            d["is_ready"] = missing_images == 0
            results.append(d)
        except Exception:
            pass
    results.sort(key=lambda item: item.get("saved_at", ""), reverse=True)
    return results

def save_draft_to_disk(user_id: str, draft_id: str, details: Optional[dict], image_paths: List[str]):
    user_dir = os.path.join(DRAFTS_DIR, user_id)
    os.makedirs(user_dir, exist_ok=True)
    img_dir = os.path.join(user_dir, draft_id)
    saved_paths = copy_images_to_dir(image_paths, img_dir)
    details = details or {}
    payload = {
        "id": draft_id,
        "user_id": user_id,
        "details": details,
        "image_paths": saved_paths,
        "saved_at": datetime.utcnow().isoformat(),
        "title": details.get("title") or (f"{len(saved_paths)} photo{'s' if len(saved_paths) != 1 else ''}"),
        "price": details.get("price"),
        "photo_count": len(saved_paths),
        "status": "ready" if details else "photos_only",
    }
    with open(os.path.join(user_dir, f"{draft_id}.json"), "w") as f:
        json.dump(payload, f)
    return payload

def load_draft_from_disk(user_id: str, draft_id: str) -> Optional[dict]:
    path = os.path.join(DRAFTS_DIR, user_id, f"{draft_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def delete_draft_from_disk(user_id: str, draft_id: str):
    user_dir = os.path.join(DRAFTS_DIR, user_id)
    json_path = os.path.join(user_dir, f"{draft_id}.json")
    img_dir = os.path.join(user_dir, draft_id)
    try:
        os.remove(json_path)
    except Exception:
        pass
    try:
        shutil.rmtree(img_dir, ignore_errors=True)
    except Exception:
        pass

def photo_sets_for_user(user_id: str) -> List[dict]:
    results = []
    user_dir = os.path.join(PHOTO_STORAGE_DIR, user_id)
    if not os.path.isdir(user_dir):
        return results
    for fname in os.listdir(user_dir):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(user_dir, fname)) as f:
                data = json.load(f)
            image_paths = [path for path in data.get("image_paths", []) if os.path.exists(path)]
            data["image_paths"] = image_paths
            data["photo_count"] = len(image_paths)
            data["cover_image"] = image_paths[0] if image_paths else None
            results.append(data)
        except Exception:
            pass
    results.sort(key=lambda item: item.get("saved_at", ""), reverse=True)
    return results

def save_photo_set_to_disk(user_id: str, photo_set_id: str, image_paths: List[str]):
    user_dir = os.path.join(PHOTO_STORAGE_DIR, user_id)
    os.makedirs(user_dir, exist_ok=True)
    img_dir = os.path.join(user_dir, photo_set_id)
    saved_paths = copy_images_to_dir(image_paths, img_dir)
    payload = {
        "id": photo_set_id,
        "user_id": user_id,
        "image_paths": saved_paths,
        "photo_count": len(saved_paths),
        "saved_at": datetime.utcnow().isoformat(),
    }
    with open(os.path.join(user_dir, f"{photo_set_id}.json"), "w") as f:
        json.dump(payload, f)
    return payload

def load_photo_set_from_disk(user_id: str, photo_set_id: str) -> Optional[dict]:
    path = os.path.join(PHOTO_STORAGE_DIR, user_id, f"{photo_set_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        data["image_paths"] = [path for path in data.get("image_paths", []) if os.path.exists(path)]
        data["photo_count"] = len(data["image_paths"])
        data["cover_image"] = data["image_paths"][0] if data["image_paths"] else None
        return data
    except Exception:
        return None

def set_fill_job(user_id: str, state: str, message: str, progress: int = 0, *, step: Optional[str] = None):
    job = fill_jobs.setdefault(user_id, {"entries": []})
    job["state"] = state
    job["message"] = message
    job["progress"] = max(0, min(100, int(progress)))
    job["updated_at"] = datetime.utcnow().isoformat()
    if step:
        entry = {
            "step": step,
            "message": message,
            "progress": job["progress"],
            "at": job["updated_at"],
        }
        entries = job.setdefault("entries", [])
        if not entries or entries[-1].get("message") != message:
            entries.append(entry)
            if len(entries) > 12:
                del entries[:-12]

async def update_fill_job(user_id: str, step: str, progress: int, message: str):
    state = "error" if step == "error" else "complete" if step == "complete" else "running"
    set_fill_job(user_id, state, message, progress, step=step)

async def run_create_draft_fill(user_id: str, session: dict, pending: dict):
    details = pending.get("details") or {}
    set_fill_job(user_id, "queued", "Queued Facebook fill.", 1, step="queued")
    try:
        await create_facebook_listing(
            image_paths=pending["image_paths"],
            title=details.get("title", "Item"),
            price=details.get("price", 0),
            condition=details.get("condition", "Used"),
            category=details.get("category", "Misc"),
            description=details.get("description", ""),
            cdp_port=session["cdp_port"],
            status_callback=lambda step, progress, message: update_fill_job(user_id, step, progress, message),
        )
        job = fill_jobs.get(user_id, {})
        if job.get("state") != "complete":
            set_fill_job(user_id, "complete", "Facebook draft filled. Review it in the browser and publish.", 100, step="complete")
    except Exception as e:
        set_fill_job(user_id, "error", f"Facebook fill failed: {e}", 100, step="error")

@app.get("/")
async def marketing_page(): return tracked_html_response(MARKETING_HTML)

@app.get("/login")
async def login_page():
    if AUTH_PROVIDER == "oidc":
        return RedirectResponse("/api/auth/login", status_code=302)
    return tracked_html_response(LOGIN_HTML)

@app.get("/self-host")
async def self_host_guide_page():
    return tracked_html_response(SELF_HOST_GUIDE_HTML)

@app.get("/support")
async def support_page():
    return tracked_html_response(SUPPORT_HTML)

@app.get("/downloads/{filename}")
async def public_download(filename: str):
    safe_name = os.path.basename(filename)
    if safe_name != filename or safe_name.startswith("."):
        raise HTTPException(status_code=404, detail="Download not found.")
    download_root = os.path.abspath(DOWNLOAD_DIR)
    file_path = os.path.abspath(os.path.join(download_root, safe_name))
    if not file_path.startswith(download_root + os.sep) or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Download not found.")
    media_type, _ = mimetypes.guess_type(file_path)
    return FileResponse(
        file_path,
        media_type=media_type or "application/octet-stream",
        filename=safe_name,
        headers={"Cache-Control": "public, max-age=300"},
    )

@app.get("/setup")
async def setup_page(): return tracked_html_response(SETUP_HTML)

@app.get("/api/auth/status")
async def auth_status(request: Request):
    user = current_session_user(request)
    return {
        "success": True,
        "provider": AUTH_PROVIDER,
        "setup_required": local_setup_required(),
        "authenticated": bool(user),
        "user": public_user(user) if user else None,
    }

@app.get("/api/auth/me")
async def auth_me(request: Request):
    if local_setup_required():
        return {"success": False, "setup_required": True, "error": "Setup is required."}
    user = current_session_user(request)
    if not user:
        return {"success": False, "setup_required": False, "error": "Not signed in."}
    return {"success": True, "user": public_user(user)}


@app.get("/api/auth/login")
async def auth_login_oidc(request: Request, returnTo: str = "/dashboard"):
    if AUTH_PROVIDER == "local":
        return RedirectResponse("/login")
    if not oidc_enabled():
        return JSONResponse(
            {"success": False, "error": "OIDC is not configured for Auto-Lister."},
            status_code=500,
        )
    discovery = oidc_discovery()
    state, verifier, pending = make_oidc_pending(returnTo)
    params = {
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": OIDC_SCOPES,
        "state": state,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(f"{discovery['authorization_endpoint']}?{urlencode(params)}", status_code=302)
    response.set_cookie(OIDC_PENDING_COOKIE, pending, max_age=cookie_max_age(600), httponly=True, secure=True, samesite="lax")
    return response


@app.get("/api/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return tracked_html_response(f"<h1>Sign in failed</h1><p>{escape(error)}</p>", status_code=400)
    pending = parse_pending_payload(request.cookies.get(OIDC_PENDING_COOKIE, ""))
    if not pending or pending.get("state") != state:
        return tracked_html_response("<h1>Sign in failed</h1><p>Invalid or expired sign-in state.</p>", status_code=400)
    discovery = oidc_discovery()
    token_payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OIDC_REDIRECT_URI,
        "code_verifier": pending.get("verifier", ""),
    }
    auth = (OIDC_CLIENT_ID, OIDC_CLIENT_SECRET) if OIDC_CLIENT_SECRET else None
    if not auth:
        token_payload["client_id"] = OIDC_CLIENT_ID
    if OIDC_CLIENT_SECRET and not auth:
        token_payload["client_secret"] = OIDC_CLIENT_SECRET
    token_response = requests.post(discovery["token_endpoint"], data=token_payload, auth=auth, timeout=10)
    if token_response.status_code >= 400:
        return tracked_html_response(
            f"<h1>Sign in failed</h1><p>OIDC token exchange failed: {escape(token_response.text[:500])}</p>",
            status_code=502,
        )
    tokens = token_response.json()
    access_token = tokens.get("access_token")
    if not access_token:
        return tracked_html_response("<h1>Sign in failed</h1><p>OIDC provider did not return an access token.</p>", status_code=502)
    userinfo_response = requests.get(
        discovery["userinfo_endpoint"],
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if userinfo_response.status_code >= 400:
        return tracked_html_response(
            f"<h1>Sign in failed</h1><p>OIDC userinfo failed: {escape(userinfo_response.text[:500])}</p>",
            status_code=502,
        )
    user = upsert_oidc_user(userinfo_response.json())
    response = RedirectResponse(str(pending.get("return_to") or "/dashboard"), status_code=302)
    response.set_cookie(SESSION_COOKIE, sign_session(user["id"]), httponly=True, secure=True, samesite="lax")
    response.delete_cookie(OIDC_PENDING_COOKIE)
    return response

@app.post("/api/auth/setup")
async def auth_setup(request: Request):
    if AUTH_PROVIDER != "local":
        return {"success": False, "error": "Local auth is not enabled."}
    if not local_setup_required():
        return {"success": False, "error": "Setup has already been completed."}
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not name:
        return {"success": False, "error": "Name is required."}
    if "@" not in email:
        return {"success": False, "error": "A valid email is required."}
    if len(password) < 8:
        return {"success": False, "error": "Password must be at least 8 characters."}
    user = {
        "id": str(uuid.uuid4()),
        "name": name,
        "email": email,
        "role": "admin",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "password": hash_password(password),
    }
    _save_users([user])
    response = JSONResponse({"success": True, "user": public_user(user)})
    response.set_cookie(SESSION_COOKIE, sign_session(user["id"]), httponly=True, samesite="lax")
    return response

@app.post("/api/auth/login")
async def auth_login(request: Request):
    if AUTH_PROVIDER != "local":
        return {"success": False, "error": "Local auth is not enabled."}
    if local_setup_required():
        return {"success": False, "setup_required": True, "error": "Setup is required."}
    payload = await request.json()
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    user = next((item for item in _load_users() if item.get("email") == email), None)
    if not user or not verify_password(password, user.get("password", {})):
        return {"success": False, "error": "Invalid email or password."}
    response = JSONResponse({"success": True, "user": public_user(user)})
    response.set_cookie(SESSION_COOKIE, sign_session(user["id"]), httponly=True, samesite="lax")
    return response

@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"success": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/auth/logout")
async def auth_logout_get():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(OIDC_PENDING_COOKIE)
    if AUTH_PROVIDER == "oidc" and OIDC_ISSUER:
        response.headers["Location"] = f"{OIDC_ISSUER}/oidc/v1/end_session?post_logout_redirect={quote_plus('https://marketplace.mrbtechnologies.com/login')}"
    return response

@app.post("/api/credits")
async def get_credits_endpoint(request: Request):
    if not STRIPE_ENABLED:
        return {"success": False, "credits": "∞"}
    payload = await request.json()
    user_id = payload.get("userId")
    if not user_id:
        return {"success": False, "error": "Missing user id."}
    return {"success": True, "credits": get_user_credits(user_id)}

@app.post("/api/checkout")
async def create_checkout_session(request: Request):
    if not STRIPE_ENABLED:
        return {"success": False, "error": "Stripe is not enabled."}
    payload = await request.json()
    user_id = payload.get("userId")
    if not user_id:
        return {"success": False, "error": "Missing user id."}
    
    try:
        session = stripe.checkout.Session.create(
            line_items=[{
                'price': STRIPE_PRICE_ID,
                'quantity': 1,
            }],
            mode='payment',
            success_url=STRIPE_SUCCESS_URL,
            cancel_url=STRIPE_CANCEL_URL,
            client_reference_id=user_id,
            metadata={'auto_lister_user_id': user_id},
        )
        return {"success": True, "url": session.url}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_ENABLED:
        return JSONResponse({"success": False}, status_code=400)
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    
    if event['type'] in {'checkout.session.completed', 'checkout.session.async_payment_succeeded'}:
        session = event['data']['object']
        if event['type'] == 'checkout.session.completed' and session.get('payment_status') != 'paid':
            return {"success": True}
        record_paid_credit(session)
            
    return {"success": True}

@app.get("/dashboard")
async def dashboard_page(): return tracked_html_response(DASHBOARD_HTML)

@app.get("/embedded-vnc")
async def embedded_vnc_page(): return tracked_html_response(EMBEDDED_VNC_HTML)

@app.get("/api/drafts")
async def list_drafts(userId: str):
    return {"success": True, "drafts": drafts_for_user(userId)}

@app.get("/api/photo-storage")
async def list_photo_storage(userId: str):
    return {"success": True, "photo_sets": photo_sets_for_user(userId)}

@app.get("/api/fill-status")
async def fill_status(userId: str):
    job = fill_jobs.get(userId)
    if not job:
        return {"success": True, "state": "idle", "progress": 0, "message": "", "entries": []}
    return {"success": True, **job}

@app.post("/api/delete-draft")
async def delete_draft(request: Request):
    payload = await request.json()
    user_id = payload.get("userId")
    draft_id = payload.get("draftId")
    draft = load_draft_from_disk(user_id, draft_id)
    if not draft:
        return {"success": False, "error": "Draft not found."}
    delete_draft_from_disk(user_id, draft_id)
    pending = pending_listings.get(user_id)
    if pending and pending.get("draft_id") == draft_id:
        pending_listings.pop(user_id, None)
    return {"success": True}

@app.post("/api/upload")
async def handle_upload(userId: str, files: List[UploadFile] = File(...), device: str = "desktop"):
    session_manager.get_or_create_session(userId, device)
    previous = pending_listings.get(userId)
    if previous:
        cleanup_paths(previous.get("image_paths", []))
    tmp_paths = []
    for f in files:
        path = os.path.join(tempfile.gettempdir(), f"up_{userId}_{f.filename}")
        content = await f.read()
        with open(path, "wb") as buf: buf.write(content)
        tmp_paths.append(path)
    try:
        details = await analyze_images(tmp_paths)
    except Exception as e:
        cleanup_paths(tmp_paths)
        return {"success": False, "error": str(e)}
    draft_id = str(uuid.uuid4())
    draft = save_draft_to_disk(userId, draft_id, details, tmp_paths)
    pending_listings[userId] = {"image_paths": draft["image_paths"], "details": details, "draft_id": draft_id}
    return {"success": True, "details": details, "draft_id": draft_id}

@app.post("/api/drafts/upload")
async def upload_draft_photos(userId: str, files: List[UploadFile] = File(...), device: str = "desktop"):
    session_manager.get_or_create_session(userId, device)
    tmp_paths = []
    for f in files:
        path = os.path.join(tempfile.gettempdir(), f"draft_{userId}_{uuid.uuid4().hex}_{f.filename}")
        content = await f.read()
        with open(path, "wb") as buf:
            buf.write(content)
        tmp_paths.append(path)
    try:
        draft = save_draft_to_disk(userId, str(uuid.uuid4()), None, tmp_paths)
    except Exception as e:
        cleanup_paths(tmp_paths)
        return {"success": False, "error": str(e)}
    cleanup_paths(tmp_paths)
    return {"success": True, "draft": draft}

@app.post("/api/photo-storage/upload")
async def upload_photo_storage(userId: str, files: List[UploadFile] = File(...), device: str = "desktop"):
    tmp_paths = []
    for f in files:
        path = os.path.join(tempfile.gettempdir(), f"store_{userId}_{uuid.uuid4().hex}_{f.filename}")
        content = await f.read()
        with open(path, "wb") as buf:
            buf.write(content)
        tmp_paths.append(path)
    try:
        photo_set = save_photo_set_to_disk(userId, str(uuid.uuid4()), tmp_paths)
    except Exception as e:
        cleanup_paths(tmp_paths)
        return {"success": False, "error": str(e)}
    cleanup_paths(tmp_paths)
    return {"success": True, "photo_set": photo_set}

@app.post("/api/photo-storage/create-listing")
async def create_listing_from_photo_storage(request: Request):
    payload = await request.json()
    user_id = payload.get("userId")
    photo_set_id = payload.get("photoSetId")
    if not user_id or not photo_set_id:
        return {"success": False, "error": "Missing stored photo set."}
    job_key = f"{user_id}:{photo_set_id}"
    if job_key in storage_analysis_jobs:
        return {"success": False, "error": "AI analysis is already running for those stored photos."}
    photo_set = load_photo_set_from_disk(user_id, photo_set_id)
    if not photo_set or not photo_set.get("image_paths"):
        return {"success": False, "error": "Stored photos not found."}
    storage_analysis_jobs.add(job_key)
    try:
        details = await analyze_images(photo_set["image_paths"])
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        storage_analysis_jobs.discard(job_key)
    draft_id = str(uuid.uuid4())
    draft = save_draft_to_disk(user_id, draft_id, details, photo_set["image_paths"])
    pending_listings[user_id] = {"image_paths": draft["image_paths"], "details": details, "draft_id": draft_id}
    return {"success": True, "details": details, "draft_id": draft_id}

@app.post("/api/refine")
async def refine_listing(request: Request):
    payload = await request.json()
    user_id = payload.get("userId")
    correction = (payload.get("correction") or "").strip()
    pending = pending_listings.get(user_id)
    if not pending:
        return {"success": False, "error": "No pending listing found. Upload photos first."}
    if not correction:
        return {"success": False, "error": "Correction text is required."}
    try:
        details = await analyze_images(pending["image_paths"], correction=correction, prior_details=pending.get("details"))
    except Exception as e:
        return {"success": False, "error": str(e)}
    pending["details"] = details
    draft_id = pending.get("draft_id")
    if draft_id:
        save_draft_to_disk(user_id, draft_id, details, pending["image_paths"])
    return {"success": True, "details": details}

@app.post("/api/resume")
async def resume_draft(request: Request):
    payload = await request.json()
    user_id = payload.get("userId")
    draft_id = payload.get("draftId")
    draft = load_draft_from_disk(user_id, draft_id)
    if not draft:
        return {"success": False, "error": "Draft not found."}
    details = draft.get("details") or {}
    if draft.get("status") == "photos_only" or not details.get("title"):
        try:
            details = await analyze_images(draft["image_paths"])
        except Exception as e:
            return {"success": False, "error": str(e)}
        draft = save_draft_to_disk(user_id, draft_id, details, draft["image_paths"])
    pending_listings[user_id] = {
        "image_paths": draft["image_paths"],
        "details": draft["details"],
        "draft_id": draft_id,
    }
    return {"success": True, "details": draft["details"], "draft_id": draft_id}

@app.post("/api/create-draft")
async def create_draft(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    user_id = payload.get("userId")
    device = payload.get("device", "desktop")
    pending = pending_listings.get(user_id)
    if not pending:
        return {"success": False, "error": "No analyzed listing found. Upload photos first."}
    existing = fill_jobs.get(user_id)
    if existing and existing.get("state") in {"queued", "running"}:
        return {"success": False, "error": "Facebook fill is already in progress."}
    details = pending.get("details") or {}
    session = session_manager.get_or_create_session(user_id, device)
    set_fill_job(user_id, "queued", "Queued Facebook fill.", 1, step="queued")
    background_tasks.add_task(
        run_create_draft_fill,
        user_id=user_id,
        session=session,
        pending=pending,
    )
    return {"success": True}

@app.post("/api/reveal-publish")
async def reveal_publish(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    user_id = payload.get("userId")
    device = payload.get("device", "desktop")
    if not user_id:
        return {"success": False, "error": "Missing user id."}
    if STRIPE_ENABLED:
        if get_user_credits(user_id) <= 0:
            return {"success": False, "error": "Out of posts! Please purchase more."}
        consume_credit(user_id)
    session = session_manager.get_or_create_session(user_id, device)
    background_tasks.add_task(
        reveal_publish_button,
        cdp_port=session["cdp_port"],
    )
    return {"success": True}

def cleanup_paths(paths: List[str]):
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass

def _as_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

def filter_lowball_used_comps(details: dict) -> dict:
    original_price = _as_float(details.get("original_price"))
    comps = details.get("used_comparables")
    if not original_price or not isinstance(comps, list):
        return details

    cutoff = original_price * 0.25
    filtered = []
    removed = 0
    for comp in comps:
        price = _as_float((comp or {}).get("price"))
        if price is not None and price < cutoff:
            removed += 1
            continue
        filtered.append(comp)

    if removed:
        details["used_comparables"] = filtered
        summary = (details.get("pricing_summary") or "").strip()
        note = f"Ignored {removed} low outlier used comp{'s' if removed != 1 else ''} below 25% of retail."
        details["pricing_summary"] = f"{summary} {note}".strip() if summary else note
    return details

def parse_model_json(text: str, provider_name: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"{provider_name} returned non-JSON output: {text[:500]}")
        return json.loads(match.group(0))


async def analyze_images_with_gemini(paths: List[str], prompt: str) -> dict:
    if client is None:
        raise RuntimeError("GEMINI_API_KEY is not configured or the Gemini client failed to initialize.")

    uploaded = [client.files.upload(path=p) for p in paths]
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[{"parts": [{"file_data": {"file_uri": f.uri, "mime_type": f.mime_type}} for f in uploaded] + [{"text": prompt}]}],
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearchRetrieval())])
        )
        details = parse_model_json(response.text or "", "Gemini")
        details = await canonicalize_listing_links(details)
        return filter_lowball_used_comps(details)
    finally:
        for f in uploaded:
            try: client.files.delete(name=f.name)
            except: pass


def image_data_url(path: str) -> str:
    mime_type = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def extract_openai_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return err.get("message") or json.dumps(err, ensure_ascii=True)[:500]
    return json.dumps(payload, ensure_ascii=True)[:500]


def analyze_images_with_openai_sync(paths: List[str], prompt: str) -> dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    openai_prompt = prompt + """

OpenAI fallback note:
- If you cannot verify current web sources, do not invent URLs.
- Use null for product_url and an empty used_comparables array when sources are not confidently known.
- Still identify the item from the photos and recommend a practical used Marketplace price."""
    content = [{"type": "text", "text": openai_prompt}]
    for path in paths:
        content.append({"type": "image_url", "image_url": {"url": image_data_url(path)}})

    response = requests.post(
        OPENAI_CHAT_COMPLETIONS_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        },
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI analysis failed ({response.status_code}): {extract_openai_error(response)}")
    payload = response.json()
    text = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    details = parse_model_json(text, "OpenAI")
    details["analysis_provider"] = "openai"
    return filter_lowball_used_comps(details)


async def analyze_images_with_openai(paths: List[str], prompt: str) -> dict:
    return await asyncio.to_thread(analyze_images_with_openai_sync, paths, prompt)


async def analyze_images(paths: List[str], correction: Optional[str] = None, prior_details: Optional[dict] = None):
    prompt = """You are helping someone sell items on Facebook Marketplace.

Use Google Search to identify the exact product, current original retail listing, and comparable used listings.
Price the item using both the original retail price and the used comps. Do not guess wildly if the match is weak.

Return only valid JSON with these fields:
- title: A short, specific, searchable title. Include brand AND model.
- price: A realistic used price in USD. Aim for roughly 75 percent of original retail, but adjust using used market comps when they clearly justify a higher or lower number.
- condition: One of: New, Like New, Good, Fair, Poor
- category: The most fitting category.
- description: Write 3-5 sentences in a very natural, low-key tone. No hype.
- original_price: Original retail price in USD if found, otherwise null.
- product_url: Original retail product link if found, otherwise null.
- used_comparables: Array of up to 3 objects with fields source, title, price, url. `price` must be a number in USD or null, never a string.
- pricing_summary: One short sentence explaining how original retail and used comps influenced the final price.
- confidence: 0-100 score.
- confidence_reason: Why that score.

Rules:
- Prefer manufacturer or major retailer product pages for product_url.
- Prefer real used marketplace comps for used_comparables, and prefer comps with visible prices.
- Ignore any used comparable priced below 25 percent of original retail. Treat those as low outliers and do not let them pull the price down.
- Never return Google grounding redirect URLs or `vertexaisearch.cloud.google.com` URLs. Return the final direct URL only.
- If the exact model is uncertain, lower confidence and say so.
- If sources conflict, explain that in confidence_reason or pricing_summary."""
    if prior_details:
        prompt += f"\n\nPrevious analysis JSON:\n{json.dumps(prior_details, ensure_ascii=True)}"
    if correction:
        prompt += f"\n\nUser correction to apply:\n{correction}\n\nUse this correction to revise the identification, pricing, and product link. If the correction conflicts with your prior guess, favor the correction unless the photos clearly disprove it."

    gemini_error = None
    try:
        return await analyze_images_with_gemini(paths, prompt)
    except Exception as e:
        gemini_error = e
        if not OPENAI_API_KEY:
            raise
        print(f"Gemini analysis failed; trying OpenAI fallback: {type(e).__name__}: {e}")

    try:
        return await analyze_images_with_openai(paths, prompt)
    except Exception as openai_error:
        raise RuntimeError(f"Gemini failed ({gemini_error}); OpenAI fallback failed ({openai_error})") from openai_error

def is_redirect_url(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return "vertexaisearch.cloud.google.com" in host or "grounding-api-redirect" in url

async def canonicalize_listing_links(details: dict) -> dict:
    product_url = details.get("product_url")
    comps = details.get("used_comparables")
    needs_fix = is_redirect_url(product_url) or any(is_redirect_url(comp.get("url")) for comp in comps or [])
    if not needs_fix:
        return details

    comp_payload = []
    for comp in comps or []:
        comp_payload.append({
            "source": comp.get("source"),
            "title": comp.get("title"),
            "price": comp.get("price"),
            "url": comp.get("url"),
        })

    prompt = f"""Return only valid JSON.

You are cleaning up listing source links for a Facebook Marketplace assistant.
Find direct canonical URLs for the product page and comparable used listings.

Rules:
- Never return Google grounding redirect URLs.
- Never return `vertexaisearch.cloud.google.com` URLs.
- Prefer the manufacturer page first for `product_url`. If unavailable, use a major retailer page.
- For used comparables, prefer the actual listing page URL from the source marketplace.
- Keep the existing title and sources aligned with the original analysis.

Current listing JSON:
{json.dumps({
    "title": details.get("title"),
    "original_price": details.get("original_price"),
    "product_url": product_url,
    "used_comparables": comp_payload,
}, ensure_ascii=True)}

Return JSON with exactly these fields:
- product_url
- used_comparables: array of objects with source, title, price, url
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearchRetrieval())])
    )
    text = (response.text or "").strip()
    try:
        cleaned = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return details
        cleaned = json.loads(match.group(0))

    cleaned_product_url = cleaned.get("product_url")
    if cleaned_product_url and not is_redirect_url(cleaned_product_url):
        details["product_url"] = cleaned_product_url
    elif is_redirect_url(details.get("product_url")):
        details["product_url"] = None

    cleaned_comps = cleaned.get("used_comparables")
    if isinstance(cleaned_comps, list):
        normalized = []
        for idx, comp in enumerate(cleaned_comps):
            original = comp_payload[idx] if idx < len(comp_payload) else {}
            url = comp.get("url")
            normalized.append({
                "source": comp.get("source") or original.get("source"),
                "title": comp.get("title") or original.get("title"),
                "price": comp.get("price", original.get("price")),
                "url": None if is_redirect_url(url) else url,
            })
        details["used_comparables"] = normalized

    return details

if os.path.isdir(NOVNC_DIR):
    app.mount("/novnc", StaticFiles(directory=NOVNC_DIR), name="novnc")
else:
    print(f"Warning: noVNC directory not found at {NOVNC_DIR}. Run install.sh or use the Docker image.")

@app.websocket("/vnc/{userId}")
async def vnc_proxy_desktop(websocket: WebSocket, userId: str):
    await _vnc_proxy(websocket, userId, "desktop")

@app.websocket("/vnc/{userId}/{device}")
async def vnc_proxy_device(websocket: WebSocket, userId: str, device: str):
    await _vnc_proxy(websocket, userId, device)

async def _safe_close_websocket(websocket: WebSocket):
    if websocket.application_state == WebSocketState.DISCONNECTED:
        return
    try:
        await websocket.close()
    except RuntimeError:
        pass

async def _vnc_proxy(websocket: WebSocket, userId: str, device: str):
    await websocket.accept()
    session = session_manager.get_or_create_session(userId, device)
    session_manager.mark_connected(userId, device)
    backend_url = f"ws://localhost:{session['ws_port']}"
    # Wait for backend to be ready (up to 5 seconds)
    backend_ws = None
    for i in range(10):
        try:
            backend_ws = await websockets.connect(backend_url)
            break
        except Exception as e:
            if i == 9: 
                print(f'VNC Proxy Backend Connection Failed after 10 attempts: {e}')
                session_manager.mark_disconnected(userId, device)
                await _safe_close_websocket(websocket)
                return
            await asyncio.sleep(0.5)

    try:
        async with backend_ws:
            async def forward_to_backend():
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        await backend_ws.send(data)
                except: pass
            async def forward_to_client():
                try:
                    while True:
                        data = await backend_ws.recv()
                        await websocket.send_bytes(data)
                except: pass
            await asyncio.gather(forward_to_backend(), forward_to_client())
    except Exception as e: print(f"VNC Proxy Error: {e}")
    finally:
        session_manager.mark_disconnected(userId, device)
        await _safe_close_websocket(websocket)
