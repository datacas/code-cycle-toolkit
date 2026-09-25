# Captured result contracts

These redacted fixtures come from live Codex and Claude Code skill dispatches
recorded on 2026-09-23 and 2026-09-24. They retain machine-readable result
keys and status tokens, with identifiers replaced by stable placeholders.
URLs, titles, prose, and provider-specific identifiers have been removed. The
capture headers identify GitHub as the host; the source records retained no
model or skill revision, so those fields are explicitly marked unknown rather
than inferred.

`codex-rereview.json` predates the additive `severity` and `blocks_approval`
fields for `verified_findings`; it exercises compatibility with real earlier
output. The skill example tests exercise the current documented contract.
