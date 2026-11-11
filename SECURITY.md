# Security

Auditarr moves files in your media library and holds API keys for your other apps, so please report anything security-relevant privately rather than in a public issue: use GitHub's "Report a vulnerability" button on the Security tab of this repository.

What is in place today: a built-in sign-in (Setup tab, step 4) with hashed passwords and signed session cookies; API keys stored in Auditarr's own database and never echoed back; a token-protected webhook; a read-only mode used for every staged update; nothing deleted directly (trash with a countdown). The full test harness is described in `docs/TESTING.md`.
