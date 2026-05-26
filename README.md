# Auto-Lister

Auto-Lister is a self-hosted Facebook Marketplace assistant. It uses Gemini to identify items from photos, estimate a used-sale price, write a listing, and fill a Facebook Marketplace draft through a browser session you can review before publishing.

## What You Need

- A Gemini API key
- A server that can run Docker, or an Ubuntu/Debian host with Python, Chromium, Xvfb, x11vnc, and openbox
- A Facebook account for the browser session

Gemini and Stripe keys are server-side only. Do not put them in frontend JavaScript, screenshots, support messages, or committed files. A fresh self-hosted install uses local first-admin setup by default.

## Quick Start With Docker

Clone the public repository:

```bash
git clone https://github.com/mbellew937/auto-lister.git
cd auto-lister
```

Then configure and start it:

```bash
cp .env.example .env
nano .env
docker compose up -d --build
```

Open:

```text
http://localhost:8000
```

For a public install, put the container behind HTTPS.

You can also download the packaged tarball from the hosted setup guide:

```bash
curl -L -o auto-lister-self-host.tar.gz https://marketplace.mrbtechnologies.com/downloads/auto-lister-self-host.tar.gz
mkdir -p auto-lister
tar -xzf auto-lister-self-host.tar.gz -C auto-lister --strip-components=1
cd auto-lister
```

## Required Environment

```bash
GEMINI_API_KEY=your_gemini_key
AUTO_MARKETPLACE_AUTH_PROVIDER=local
```

On first launch, open `/setup` and create the first local admin. After setup, users sign in at `/login`.

Optional OpenAI fallback:

```bash
OPENAI_API_KEY=your_openai_key
OPENAI_MODEL=gpt-4o-mini
```

Optional hosted-offer and attribution settings:

```bash
AUTO_MARKETPLACE_CREDIT_LABEL=MRB Technologies
AUTO_MARKETPLACE_CREDIT_URL=https://mrbtechnologies.com
AUTO_MARKETPLACE_HOSTED_URL=https://marketplace.mrbtechnologies.com
AUTO_MARKETPLACE_HOSTED_COMPARE_AT_PRICE='$5'
AUTO_MARKETPLACE_HOSTED_PRICE='$1'
AUTO_MARKETPLACE_FREE_SIGNUP_LIMIT=25
AUTO_MARKETPLACE_FREE_POSTS=3
AUTO_MARKETPLACE_PACKAGE_DOWNLOAD_URL=https://marketplace.mrbtechnologies.com/downloads/auto-lister-self-host.tar.gz
AUTO_MARKETPLACE_PUBLIC_REPO_URL=https://github.com/mbellew937/auto-lister.git
AUTO_MARKETPLACE_SUPPORT_EMAIL=support@mrbtechnologies.com
AUTO_MARKETPLACE_SUPPORT_HELPDESK_URL=https://helpdesk.mrbtechnologies.com
AUTO_MARKETPLACE_SUPPORT_GITHUB_ISSUES_URL=https://github.com/mbellew937/auto-lister/issues
AUTO_MARKETPLACE_ZAMMAD_URL=https://helpdesk.mrbtechnologies.com
AUTO_MARKETPLACE_ZAMMAD_GROUP=MRB Technologies
AUTO_MARKETPLACE_ZAMMAD_API_TOKEN=
```

The login page shows a hosted option by default: "Run it on mine" with `$5` struck through, `$1` per post for a limited time, and the first 25 sign-ups receiving 3 free posts. New hosted users after that start with 0 free posts. Regenerations do not count; only clicking Publish should consume a paid post in a hosted billing flow.

## Support

The app exposes `/support` with email, helpdesk, and GitHub issue links. Configure these for your own install:

```bash
AUTO_MARKETPLACE_SUPPORT_EMAIL=support@example.com
AUTO_MARKETPLACE_SUPPORT_URL=mailto:support@example.com
AUTO_MARKETPLACE_SUPPORT_HELPDESK_URL=https://helpdesk.example.com
AUTO_MARKETPLACE_SUPPORT_GITHUB_ISSUES_URL=https://github.com/you/auto-lister/issues
```

Set `AUTO_MARKETPLACE_ZAMMAD_API_TOKEN`, `AUTO_MARKETPLACE_ZAMMAD_URL`, and
`AUTO_MARKETPLACE_ZAMMAD_GROUP` to enable the no-sign-in guest helpdesk form on
`/support`. The token stays server-side and creates Zammad tickets with guest
customer email addresses.

## Optional Matomo Analytics

Set these to enable pageviews plus non-PII workflow events for setup, auth, photo uploads, AI analysis, draft handling, browser sessions, and Facebook fill progress:

```bash
MATOMO_URL=https://analytics.example.com
MATOMO_SITE_ID=1
```

## Auth Modes

Default self-host mode is local auth:

```bash
AUTO_MARKETPLACE_AUTH_PROVIDER=local
```

Local auth stores users in the configured data directory as `users.json`; keep that directory backed up and private.

## Optional Stripe Monetization

Self-hosters can add their own Stripe account if they want to sell access to their hosted copy. Keep this as BYO Stripe, disabled by default.

Recommended shape:

- Use Stripe Checkout Sessions for one-time per-post purchases.
- Store the self-hoster's `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and `STRIPE_PRICE_ID` only on the server.
- Configure the Stripe webhook endpoint at `https://your-domain.example.com/api/webhook` and send `checkout.session.completed` plus `checkout.session.async_payment_succeeded`.
- Credit or charge only when the user actually clicks Publish.
- Do not charge for re-analysis, regeneration, photo upload, draft creation, or edits.
- Use Stripe Connect only if you are running a central platform and need to onboard sellers or take an application fee.

Placeholder env vars are included in `.env.example`, but the core app does not require Stripe to run.

## Manual Install

On an Ubuntu/Debian host:

```bash
sudo ./install.sh
cp .env.example .env
nano .env
./start.sh
```

For systemd:

```bash
sudo mkdir -p /opt/auto-marketplace /var/lib/auto-marketplace
sudo cp -a . /opt/auto-marketplace/
sudo cp marketplace.service /etc/systemd/system/marketplace.service
sudo cp .env /etc/auto-marketplace.env
sudo systemctl daemon-reload
sudo systemctl enable --now marketplace.service
```

## Data Storage

By default, Docker stores persistent app data in the `auto_lister_data` volume mounted at `/data`.

The app stores:

- browser profiles
- uploaded photos
- saved drafts
- stored photo library items

Back up the `/data` volume if users rely on saved drafts or browser sessions.

## Demo Photos

Photo Storage saves photos without creating or analyzing a listing. Pick one or
more stored photos later and choose Analyze Selected to create a listing draft.

The release includes metadata-stripped demo photos in `examples/demo-photos/`.
They are safe to use for checking the photo upload and analysis flow, but users
should upload their own item photos for real listings.

## Secret Safety

The package ignores `.env`, credential files, local browser profiles, drafts, and generated caches. Before sharing a zip or publishing a repo, verify:

```bash
grep -RIn "GEMINI_API_KEY\\|OPENAI_API_KEY\\|private_key\\|client_secret\\|STRIPE_SECRET_KEY" . --exclude-dir=.git --exclude-dir=venv --exclude=.env
```

Only `.env.example` should contain placeholder names.

## License

Auto-Lister is released under the MIT License. See `LICENSE`.
