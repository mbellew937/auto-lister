# Changelog

## 2026.05.24

- Published the source as a public GitHub repository.
- Updated self-host setup docs and defaults to include the public clone URL.
- Expanded Matomo coverage from pageviews and marketing CTAs to auth, setup,
  dashboard workflows, photo storage, AI analysis, Facebook fill status,
  embedded browser connection events, and self-host guide command copies.
- Added a public `/support` page with email, helpdesk, and GitHub issue links,
  plus support links from the marketing page, self-host guide, and dashboard.
- Tightened hosted credit defaults to first 25 users receiving 3 free publish
  credits, later users starting at 0, and Stripe Checkout requiring webhook
  configuration before billing turns on.
- Changed Photo Storage from click-to-analyze photo sets into a selectable photo
  library. Stored photos now stay saved until the user explicitly picks photos
  and runs Analyze Selected.

## 2026.05.16

- Added a public self-host package download path.
- Added release documentation: license, security policy, and changelog.
- Added metadata-stripped demo photos under `examples/demo-photos/`.
- Updated the self-host setup page to use a public tarball install flow.
- Added an optional OpenAI image-analysis fallback when Gemini is unavailable.
- Improved mobile browser/VNC sizing and keyboard controls.
- Improved Facebook posting reliability with CDP retry, login detection, and
  desktop-mode listing creation.
- Preserved the saved-photo workflow: Store Photos saves images, and Analyze
  runs later from the saved photo set.
