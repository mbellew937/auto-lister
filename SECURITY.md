# Security Policy

## Supported Releases

The current public self-host package is the only supported release channel.
Update to the latest package before reporting an issue.

## Secret Handling

Keep API keys and payment credentials server-side only. Do not commit `.env`,
browser profiles, draft data, uploaded photos, session data, Stripe keys,
Gemini keys, OpenAI keys, OAuth client secrets, or generated `secret.key`
files.

Self-hosted instances should run behind HTTPS when exposed outside a local
network. Back up the configured data directory if users rely on saved drafts,
photo sets, or browser sessions.

## Reporting Security Issues

Report private security issues to MRB Technologies instead of posting them
publicly. Include the affected version, install method, and the smallest
reproducible description you can share without exposing credentials.
