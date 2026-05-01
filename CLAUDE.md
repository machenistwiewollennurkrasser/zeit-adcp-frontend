# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo at a glance

Single-process FastAPI app that serves both:
- An MCP-conformant ad inventory discovery server (JSON-RPC 2.0 over `POST /mcp`) for ZEIT Advise.
- A static `index.html` sales UI mounted at `/` that calls the same server's legacy AdCP endpoint.

Despite the name `zeit-adcp-frontend`, this repo is now a full backend + frontend bundle. The "frontend repo" name is historical (see commit `8952b49`).

Three top-level Python files do all the work:

- `mcp_server.py` — FastAPI app. Routes (`/`, `/api`, `/mcp`, `/mcp/get_products`, `/health`, `/.well-known/adagents.json`), MCP JSON-RPC dispatcher, response shaping for both MCP and legacy AdCP, GitHub product loader, CORS + rate limiting.
- `matching.py` — All scoring/pricing logic. Single entry point `match_products()` parses the brief and routes by `product_type` to one of three engines (Print / Newsletter / Podcast). Rich keyword catalogs at the top (audience, industry, goals, values, channel hints, podcast slots) drive `parse_brief()`.
- `index.html` — Self-contained HTML/CSS/JS sales UI. No build step. Talks to `/health` and `/mcp/get_products` at `API_BASE = ""` (same-origin).

## Commands

```bash
# Run locally (default dev port the UI's error message references is 8001; Procfile uses $PORT)
python3 -m uvicorn mcp_server:app --reload --port 8001

# Install deps
pip install -r requirements.txt

# Quick smoke test of the MCP endpoint
curl -X POST http://localhost:8001/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_products","arguments":{"brief":"Luxus-Uhren-Anzeige, Maenner 40-60"}},"id":1}'

# Health / inventory summary
curl http://localhost:8001/health
```

There are no tests, no linter config, and no build step. `Procfile` is for Railway deployment (`web: python -m uvicorn mcp_server:app --host 0.0.0.0 --port $PORT`).

## Product data loading

Products are NOT in this repo. At startup `mcp_server.py` either:
1. Reads `PRODUCTS_DIR` (default `./products`) from disk, OR
2. If `GITHUB_TOKEN` and `GITHUB_REPO` are set, downloads JSONs from a separate GitHub repo (the v3 inventory) into a tempdir using the GitHub Tree API + raw URLs.

`load_product_index()` walks the load dir; files containing `"definitions"` in the name (or no `product_id`) become `definitions[<category>]`, everything else is a product. Definitions are passed through to `match_products(..., definitions=...)` and used by Newsletter/Podcast pricing resolvers.

Recognized category folders are hard-coded in `PRODUCT_CATEGORIES` (`mcp_server.py`): `beilegendes_magazin`, `die_zeit`, `magazin`, `newsletter`, `podcast`, `regional`, `sonderveroeffentlichung`.

If you need to test product-loading paths locally, either point `PRODUCTS_DIR` at a directory of v3 JSONs or set the GitHub env vars.

## Architecture: matching engine

`matching.py` is large (~1900 lines) but follows a strict pattern. Read it as four layers:

1. **Keyword catalogs** (top of file): `AUDIENCE_KEYWORDS`, `INDUSTRY_KEYWORDS`, `GOAL_KEYWORDS`, `VALUE_KEYWORDS`, `CHANNEL_HINTS`, `KULTURKUNDE_HINTS`, `PODCAST_SLOT_HINTS`, `PODCAST_AD_TYPE_HINTS`, `PREMIUM_TARGETING_HINTS`. These are the surface area for "the engine doesn't pick up X" bugs — usually a missing keyword.
2. **`parse_brief(brief: str) -> ParsedBrief`**: regex/keyword scan that produces a single channel-agnostic struct. One brief can simultaneously trigger Print + Newsletter + Podcast signals.
3. **Schema adapters** (`get_audience`, `get_print_specifics`, `get_newsletter_pricing`, ...): isolate the v3 Hybrid-Block JSON shape. Always go through these accessors rather than reaching into raw product dicts — the schema is layered (`print_specifics`, `newsletter_specifics`, `podcast_specifics`, plus shared `audience`, `reach`, `matching_metadata`, `pricing_models`).
4. **Score functions + router**: `score_print` / `score_newsletter` / `score_podcast` each return `(score, reasoning, assumptions, ...)`. `match_products()` is the only public entry point — it parses, dispatches by `product_type`, applies a Print bonus (+15) and Channel-Hint bonus/malus (+10/-15, or hard-filter for single-channel briefs), then sorts and applies a **score-spread** filter: top-1 always survives; siblings only if `score >= 0.7 * top_score`.

Match types are derived from raw score: `>=70` exact_match, `>=30` counter_proposal, else suggestion.

`product_type` groups (matching.py:54): `PRINT_TYPES = {magazin, sonderheft, beilage, wochenzeitung, submagazin, b2b_magazin, kindermagazin}`, `NEWSLETTER_TYPES = {newsletter}`, `PODCAST_TYPES = {podcast}`. Anything else is silently skipped by the router.

## Architecture: response shaping

The same `matches` list from `match_products()` is rendered twice with similar but **not identical** logic in `mcp_server.py`:

- `execute_get_products()` — wraps results in MCP JSON-RPC `content[0].text` (a stringified JSON blob). Used by the `/mcp` JSON-RPC endpoint.
- `legacy_adcp_endpoint()` — returns plain JSON at `POST /mcp/get_products`. This is what `index.html` calls.

When changing pricing/summary output, **update both branches**. They share three pricing shapes you'll need to cover:
- Print: `best_format.price_net_eur` (+ optional `format_candidates` with per-format industry discounts).
- Newsletter: `pricing.price_eur_net` (+ optional `list_price_eur_net` / `discount_pct` / `applied_kulturpreis` / `applied_cluster`).
- Podcast: either a concrete `total_price_eur_net` (TKP × audio impressions, with cluster + performance class) or an `is_example` "Beispielrechnung" path, or a `hint`-only fallback.

Pricing helpers `fmt_price()` / `price_line()` and the constant `PREIS_SUFFIX = "EUR (Listenpreis, netto zzgl. MwSt.)"` exist so both endpoints emit the same pricing strings — keep using them rather than reformatting inline. The unified suffix is an explicit product decision (v1.5.5: "alle Preise als Listenpreis, netto zzgl. MwSt." — abgestimmt Lars/Udo).

Note the unit mismatch between server and UI: the server returns **net** prices and labels them as such; `index.html` applies `VAT = 1.19` (`gross()`) and shows brutto, and the footer reads "Preise brutto inkl. 19% MwSt." Keep this in mind when changing currency formatting on either side.

## Conventions

- All comments and user-facing strings in this codebase are in **German** (with ASCII transliterations like `ae/oe/ue/ss` rather than umlauts — this is intentional, follow the same style in new code/strings).
- Versioning: `mcp_server.py` carries a `version` string in the FastAPI constructor and `/health`, and `matching.py` documents changes in the module docstring `Changelog`. Bump them when you ship behavior changes.
- CORS allowlist is explicit (no wildcards for production) — add new origins to the list in `mcp_server.py` rather than relaxing it.
- Rate limiting: only `/mcp` is limited (`30/minute`). The legacy endpoint isn't.

## Branch Policy

Solo-Entwicklung, kein PR-Workflow. Immer direkt auf `main` committen und pushen.

- Keine Feature-Branches anlegen
- Keine Pull Requests eroeffnen
- `git push origin main` nach jedem Commit
- Live-Test auf Railway ist der Sicherheits-Mechanismus

## Sister repo and architecture boundary

There is a parallel repo `zeit-adcp-pilot` that backs the Custom GPTs **Klara** and **PriceAdvise** at OpenAI (live at `zeit-adcp-pilot-production.up.railway.app`). Both repos read the same product data from `zeit-adcp-products`, but each has its **own** `matching.py` and `mcp_server.py`. Changes made in this repo must never touch the pilot repo. If a fix needs to land in both, it has to be **manually ported** — there is no shared package or import path between them.
