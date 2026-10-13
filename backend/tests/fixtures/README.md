# Test fixtures

* `sec_*.json` — **recorded** real SEC EDGAR responses (2026-10-12), trimmed to the XBRL
  concepts EventAlpha uses (companyfacts) / the latest 150 filings (submissions).
* `massive_*.json` — hand-written responses that follow the documented Massive
  (Polygon) REST response shapes. Values are illustrative, not real market data.

Tests never call external APIs.
