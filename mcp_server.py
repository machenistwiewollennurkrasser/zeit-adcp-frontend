"""
ZEIT AdCP MCP Server - v1.7.0

MCP-konformer Server fuer ZEIT Advise Werbeinventar-Discovery.
Implementiert Model Context Protocol (MCP) ueber JSON-RPC 2.0.

Railway-Deployment: Laeuft auf Railway.app, oeffentlich erreichbar
Lokal: uvicorn mcp_server:app --reload --port 8000

v1.1: GitHub-Produktladen fuer Cloud-Deployment
v1.2: Rate Limiting, CORS Whitelist, Input-Laengenbegrenzung
v1.3: Dynamischer GitHub-Tree-Loader fuer v3-Bestand
      (84 JSONs + 2 Definitions in 7 Kategorie-Ordnern)
      Health-Endpoint zeigt products_by_type und definitions
v1.4: Definitions werden an match_products durchgereicht.
      Kompatibel mit konsolidierter matching.py v3.0 (Router-Architektur,
      Print/Newsletter/Podcast in einer Engine).
v1.4.6: format_options mit individuellem Discount pro Format,
      Newsletter list_price/discount_pct, Podcast example_pricing,
      assumptions durchreichen, Branchenpreise MVP-deaktiviert.
v1.5.5: Preisbezeichnung vereinheitlicht: alle Preise als
      "Listenpreis, netto zzgl. MwSt." gemaess Abstimmung Lars/Udo.
      format_candidates Unterstuetzung fuer DIE ZEIT Format-Uebersicht.
      Hilfsfunktionen fmt_price() und price_line() fuer konsistente
      Preisformatierung in allen Endpoints.
v1.6.0: DIE ZEIT Wochenzeitung Detail-Endpoint: 56 Issues mit
      booking_deadline_label/premium_deadline_label, linked_speziale,
      special_theme_clusters, regional_editions, all_speziale.
v1.7.0: Sonderveroeffentlichungen-Umbau: _build_svoe_clusters() liefert
      9 Cluster-Karten im products_list. Neuer Endpoint
      GET /clusters/sonderveroeffentlichungen/{id} mit allen Terminen.
      product_detail-Zweig fuer sonderveroeffentlichung mit Terminen,
      Reichweite und Ad-Formaten.
"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional, Any, Literal, List, Dict, Union
from pathlib import Path
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import json
import logging
import os
import urllib.request
import tempfile
from datetime import datetime, date as _date, timedelta

from matching import (
    match_products, ProductIndex, parse_brief, check_industry_discount,
    get_reach, get_audience, get_matching_metadata,
    get_print_specifics, get_print_ad_formats,
    get_newsletter_specifics, get_newsletter_formats, get_newsletter_pricing,
    get_newsletter_pricing_model, get_newsletter_parent_relationship,
    get_podcast_specifics, get_podcast_pricing_model,
    get_podcast_fixed_placement_pricing, get_podcast_tkp_pricing,
    get_issues, format_newsletter_schedule,
)

# =====================================================
# Logging
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("zeit_adcp_mcp")

# =====================================================
# Konfiguration
# =====================================================

BASE_DIR = Path(__file__).parent
PRODUCTS_DIR = Path(os.getenv("PRODUCTS_DIR", str(BASE_DIR / "products")))
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", None)
GITHUB_REPO = os.getenv("GITHUB_REPO", None)
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

PRODUCT_CATEGORIES = [
    "beilegendes_magazin",
    "die_zeit",
    "magazin",
    "newsletter",
    "podcast",
    "regional",
    "sonderveroeffentlichung",
]

# Einheitlicher Preis-Suffix fuer alle Ausgaben
PREIS_SUFFIX = "EUR (Listenpreis, netto zzgl. MwSt.)"


# =====================================================
# Hilfsfunktionen Preisformatierung
# =====================================================

def fmt_price(price) -> str:
    """Formatiert einen Preis als deutschen Tausender-String."""
    if price is None:
        return "k.A."
    return f"{price:,.0f}".replace(",", ".")


def price_line(label: str, price) -> str:
    """Erzeugt eine fertige Preiszeile: 'Label | 12.345 EUR (Listenpreis, netto zzgl. MwSt.)'"""
    return f"{label} | {fmt_price(price)} {PREIS_SUFFIX}"


# =====================================================
# GitHub-Loader
# =====================================================

def load_products_from_github_v3():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.warning("Kein GITHUB_TOKEN oder GITHUB_REPO konfiguriert")
        return None

    tree_url = (
        f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/"
        f"{GITHUB_BRANCH}?recursive=1"
    )
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ZEIT-AdCP-Server",
    }

    try:
        req = urllib.request.Request(tree_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            tree = json.loads(resp.read())
    except Exception as e:
        logger.error(f"GitHub Tree-API Fehler: {e}")
        return None

    if tree.get("truncated"):
        logger.warning("GitHub Tree-API: Antwort ist truncated")

    json_files = []
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        if not path.endswith(".json"):
            continue
        if path.split("/")[0] not in PRODUCT_CATEGORIES:
            continue
        json_files.append(path)

    logger.info(f"GitHub Tree: {len(json_files)} JSONs gefunden")

    tmp_dir = Path(tempfile.mkdtemp(prefix="zeit_adcp_v3_"))
    loaded = 0
    failed = []

    for path in json_files:
        raw_url = (
            f"https://raw.githubusercontent.com/{GITHUB_REPO}/"
            f"{GITHUB_BRANCH}/{path}"
        )
        target = tmp_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(raw_url, headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "User-Agent": "ZEIT-AdCP-Server",
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                target.write_bytes(r.read())
            loaded += 1
        except Exception as e:
            failed.append((path, str(e)))
            logger.warning(f"Konnte {path} nicht laden: {e}")

    if failed:
        logger.warning(f"{len(failed)} Files fehlgeschlagen, {loaded} OK")
    else:
        logger.info(f"GitHub: alle {loaded} JSONs geladen")

    return tmp_dir if loaded > 0 else None


def load_product_index(load_dir: Path):
    products = []
    definitions = {}

    for path in sorted(load_dir.rglob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"JSON-Parse-Fehler in {path}: {e}")
            continue

        if "definitions" in path.name or "product_id" not in data:
            category = path.parent.name
            definitions[category] = data
            logger.info(f"  Definitions: {category} aus {path.name}")
            continue

        data["_category"] = path.parent.name
        products.append(data)

    return ProductIndex(products=products), definitions


# =====================================================
# FastAPI App
# =====================================================

app = FastAPI(
    title="ZEIT AdCP MCP Server",
    description=(
        "MCP-konformer Server fuer ZEIT Advise Werbeinventar-Discovery. "
        "Holtzbrinck AI Exploration Program, San Francisco 2026."
    ),
    version="1.7.0",
    docs_url="/docs" if ENVIRONMENT == "development" else None,
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://claude.ai",
        "https://zeit-adcp-frontend-production.up.railway.app",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://langdock.com",
        "https://*.langdock.com",
        "https://chat.openai.com",
        "https://*.openai.com",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================================================
# Produktdaten laden
# =====================================================

logger.info(f"GITHUB_TOKEN gesetzt: {bool(GITHUB_TOKEN)}")
logger.info(f"GITHUB_REPO: {GITHUB_REPO}")
logger.info(f"GITHUB_BRANCH: {GITHUB_BRANCH}")
logger.info(f"PRODUCTS_DIR: {PRODUCTS_DIR}")

_load_dir = PRODUCTS_DIR

if GITHUB_TOKEN and GITHUB_REPO:
    logger.info("Lade v3-Bestand von GitHub...")
    _github_tmp = load_products_from_github_v3()
    if _github_tmp:
        _load_dir = _github_tmp
    else:
        logger.error("GitHub-Laden fehlgeschlagen, versuche lokal...")

try:
    product_index, definitions = load_product_index(_load_dir)
    logger.info(f"{len(product_index.products)} Produkte, {len(definitions)} Definitions geladen")
    by_type: Dict[str, int] = {}
    for p in product_index.products:
        pt = p.get("product_type", "unknown")
        by_type[pt] = by_type.get(pt, 0) + 1
    for pt, n in sorted(by_type.items()):
        logger.info(f"  {pt}: {n}")
except Exception as e:
    logger.error(f"Fehler beim Laden: {e}")
    raise

# =====================================================
# Pydantic Models
# =====================================================

class JSONRPCRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: Optional[Dict[str, Any]] = None
    id: Optional[Union[str, int]] = None

    class Config:
        json_schema_extra = {
            "example": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "get_products",
                    "arguments": {"brief": "Luxus-Uhren-Anzeige, Maenner 40-60"}
                },
                "id": 1
            }
        }


class JSONRPCResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None
    id: Optional[Union[str, int]] = None


class MCPError(BaseModel):
    code: int
    message: str
    data: Optional[Dict[str, Any]] = None


# =====================================================
# Tool Definitions
# =====================================================

MCP_TOOLS = [
    {
        "name": "get_products",
        "description": (
            "Findet passende ZEIT-Werbeprodukte (Magazine, Newsletter, Podcasts) "
            "fuer eine Werbekampagne. Nimmt einen natuerlichsprachigen "
            "Kampagnen-Brief entgegen und gibt Produktvorschlaege mit Preisen, "
            "Reichweiten und Match-Scores zurueck."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "brief": {
                    "type": "string",
                    "description": (
                        "Natuerlichsprachiger Kampagnen-Brief mit Zielgruppe, "
                        "Branche, Budget (optional), Laufzeit (optional)."
                    )
                },
                "brand_domain": {"type": "string", "description": "Domain der Marke (optional)"},
                "brand_name": {"type": "string", "description": "Name der Marke (optional)"},
                "max_results": {
                    "type": "integer",
                    "description": "Max. Produkte in der Antwort (default: 5, max: 10)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10
                }
            },
            "required": ["brief"]
        }
    }
]

# =====================================================
# MCP Routing
# =====================================================

@app.get("/api")
async def root():
    return {
        "name": "ZEIT AdCP MCP Server",
        "version": "1.7.0",
        "protocol": "mcp",
        "protocol_version": "2024-11-05",
        "endpoints": {
            "mcp_jsonrpc": "POST /mcp",
            "health": "GET /health",
            "adagents_discovery": "GET /.well-known/adagents.json",
            "legacy_adcp": "POST /mcp/get_products"
        },
        "status": {
            "environment": ENVIRONMENT,
            "products_loaded": len(product_index.products),
            "definitions_loaded": list(definitions.keys()),
            "schema_version": "3.0",
            "pilot_phase": "phase_3_full_inventory"
        }
    }


@app.post("/mcp")
@limiter.limit("30/minute")
async def mcp_jsonrpc_endpoint(request: Request):
    try:
        body = await request.json()
        rpc_request = JSONRPCRequest(**body)
        logger.info(f"MCP: method={rpc_request.method}, id={rpc_request.id}")
    except Exception as e:
        logger.error(f"Parse Error: {e}")
        return JSONRPCResponse(
            error=MCPError(code=-32700, message="Parse error").dict(),
            id=None
        ).dict()

    if rpc_request.method == "initialize":
        return handle_initialize(rpc_request)
    elif rpc_request.method == "tools/list":
        return handle_tools_list(rpc_request)
    elif rpc_request.method == "tools/call":
        return await handle_tools_call(rpc_request)
    else:
        return JSONRPCResponse(
            error=MCPError(code=-32601, message=f"Method not found: {rpc_request.method}").dict(),
            id=rpc_request.id
        ).dict()


def handle_initialize(rpc_request):
    return JSONRPCResponse(
        result={
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "ZEIT AdCP MCP Server", "version": "1.7.0"},
            "capabilities": {"tools": {}}
        },
        id=rpc_request.id
    ).dict()


def handle_tools_list(rpc_request):
    return JSONRPCResponse(
        result={"tools": MCP_TOOLS},
        id=rpc_request.id
    ).dict()


async def handle_tools_call(rpc_request):
    params = rpc_request.params or {}
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if tool_name == "get_products":
        return await execute_get_products(arguments, rpc_request.id)
    else:
        return JSONRPCResponse(
            error=MCPError(
                code=-32602,
                message=f"Unknown tool: {tool_name}",
                data={"available_tools": [t["name"] for t in MCP_TOOLS]}
            ).dict(),
            id=rpc_request.id
        ).dict()


# =====================================================
# Core: execute_get_products (MCP-Endpoint)
# =====================================================

async def execute_get_products(
    arguments: Dict[str, Any],
    request_id: Optional[Union[str, int]]
):
    try:
        brief = arguments.get("brief")
        if not brief:
            return JSONRPCResponse(
                error=MCPError(code=-32602, message="Missing required parameter: brief").dict(),
                id=request_id
            ).dict()

        if len(brief) > 2000:
            return JSONRPCResponse(
                error=MCPError(
                    code=-32602,
                    message="Parameter brief too long (max 2000 characters)"
                ).dict(),
                id=request_id
            ).dict()

        max_results = min(arguments.get("max_results", 5), 10)
        logger.info(f"get_products: brief_length={len(brief)}, max_results={max_results}")

        matches = match_products(
            brief=brief,
            products=product_index.products,
            definitions=definitions,
            max_results=max_results
        )

        if not matches:
            result_text = {
                "status": "no_match",
                "message": "Keine passenden Produkte gefunden. Kontakt: advise@zeit.de",
                "products": [],
                "pilot_note": "Phase 3: Voller Bestand (Print, Newsletter, Podcast). Schema v3.0."
            }
        else:
            products_list = []
            for m in matches:
                product = m["product"]
                best_format = m.get("best_format")

                product_data = {
                    "product_id": product["product_id"],
                    "name": product["product_name"],
                    "match_score": round(m["score"], 1),
                    "match_type": m["match_type"],
                    "match_reasoning": m["reasoning"],
                }

                summary_lines = []

                # --- PRINT ---
                if best_format and best_format.get("price_net_eur"):
                    product_data["pricing"] = {
                        "price_net_eur": best_format["price_net_eur"],
                        "format": best_format["format_name"],
                        "currency": "EUR"
                    }

                    fc = m.get("format_candidates") or []
                    if len(fc) > 1:
                        parsed_for_discount = parse_brief(brief)
                        format_options_list = []
                        for c in fc:
                            fmt_entry = {
                                "format": c.get("format_name"),
                                "price_net_eur": c.get("price_net_eur"),
                                "currency": "EUR"
                            }
                            disc = check_industry_discount(parsed_for_discount, product, c)
                            if disc and disc.get("discount_pct"):
                                fmt_entry["discount"] = disc
                            format_options_list.append(fmt_entry)
                        product_data["format_options"] = format_options_list

                        for fo in format_options_list:
                            summary_lines.append(
                                price_line(fo.get("format", ""), fo.get("price_net_eur"))
                            )
                    else:
                        summary_lines.append(
                            price_line(
                                best_format.get("format_name", ""),
                                best_format.get("price_net_eur")
                            )
                        )

                    if m.get("assumptions"):
                        product_data["assumptions"] = m["assumptions"]

                # --- NEWSLETTER + PODCAST ---
                else:
                    pr = m.get("pricing")
                    if isinstance(pr, dict):
                        nl_price = pr.get("price_eur_net")
                        pod_total = pr.get("total_price_eur_net")

                        # Newsletter
                        if nl_price is not None:
                            product_data["pricing"] = {
                                "price_net_eur": nl_price,
                                "price_unit": pr.get("price_unit"),
                                "format_id": pr.get("format_id"),
                                "format_display_name": pr.get("format_display_name"),
                                "applied_pricing_model": pr.get("applied_pricing_model"),
                                "applied_cluster": pr.get("applied_cluster"),
                                "applied_kulturpreis": pr.get("applied_kulturpreis"),
                                "currency": "EUR"
                            }
                            if pr.get("list_price_eur_net"):
                                product_data["pricing"]["list_price_eur_net"] = pr["list_price_eur_net"]
                            if pr.get("discount_pct"):
                                product_data["pricing"]["discount_pct"] = pr["discount_pct"]

                            fmt_name = pr.get("format_display_name") or pr.get("format_id") or ""
                            if pr.get("discount_pct") and pr["discount_pct"] > 0:
                                lp = pr.get("list_price_eur_net")
                                cluster = pr.get("applied_cluster") or ""
                                summary_lines.append(price_line(fmt_name, lp))
                                summary_lines.append(
                                    f"{fmt_name} | Branchenpreis ({cluster}): "
                                    f"{fmt_price(nl_price)} {PREIS_SUFFIX} "
                                    f"(Vorteil {pr['discount_pct']}%)"
                                )
                            elif pr.get("applied_kulturpreis"):
                                summary_lines.append(
                                    f"{fmt_name} | {fmt_price(nl_price)} {PREIS_SUFFIX} "
                                    f"(Kulturpreis angewendet)"
                                )
                            else:
                                summary_lines.append(price_line(fmt_name, nl_price))

                        # Podcast konkreter Preis
                        elif pod_total is not None:
                            slot = pr.get("format_slot", "")
                            ad_type = pr.get("ad_type_length", "")
                            tkp = pr.get("tkp_eur_net")
                            ai = pr.get("booked_audio_impressions")
                            pk = pr.get("performance_class", "")
                            cluster = pr.get("cluster", "")
                            product_data["pricing"] = {
                                "price_net_eur": pod_total,
                                "total_price_net_eur": pod_total,
                                "format": f"{ad_type} {slot}".strip(),
                                "tkp_eur_net": tkp,
                                "performance_class": pk,
                                "cluster": cluster,
                                "format_slot": slot,
                                "ad_type_length": ad_type,
                                "booked_audio_impressions": ai,
                                "billing_unit": pr.get("billing_unit"),
                                "mbv_satisfied": pr.get("mbv_satisfied"),
                                "pricing_model": pr.get("pricing_model"),
                                "currency": "EUR"
                            }
                            summary_lines.append(
                                price_line(f"{ad_type} {slot}".strip(), pod_total)
                            )
                            if tkp and ai:
                                summary_lines.append(
                                    f"TKP {tkp} EUR x {fmt_price(ai)} Audio-Impressions"
                                )
                            summary_lines.append(
                                f"Performance-Klasse: {pk}, Cluster: {cluster}"
                            )

                        # Podcast Beispielrechnung
                        elif pr.get("is_example") and pr.get("example_price_eur_net"):
                            ep = pr["example_price_eur_net"]
                            product_data["pricing"] = {
                                "price_net_eur": ep,
                                "format": "Beispielrechnung (Standard-Setup)",
                                "is_example": True,
                                "example_basis": pr.get("example_basis"),
                                "pricing_model": pr.get("pricing_model"),
                                "currency": "EUR"
                            }
                            summary_lines.append(
                                f"Beispielrechnung bei Standard-Setup: "
                                f"{fmt_price(ep)} {PREIS_SUFFIX}"
                            )
                            if pr.get("example_basis"):
                                summary_lines.append(f"(Basis: {pr['example_basis']})")
                            summary_lines.append(
                                "Der tatsaechliche Preis haengt von Slot, Format, "
                                "Targeting und Branche ab."
                            )

                        # Nur Hinweis
                        elif pr.get("hint"):
                            product_data["pricing_hint"] = pr["hint"]
                            summary_lines.append(pr["hint"])

                    if m.get("assumptions"):
                        product_data["assumptions"] = m["assumptions"]

                # Reichweite und Versand in summary_lines einbauen (zuverlässiger als separate Felder)
                reach = product.get("reach", {})
                nl_schedule = m.get("newsletter_schedule", "")
                if nl_schedule or reach:
                    meta_lines = []
                    if nl_schedule:
                        meta_lines.append(f"Versand: {nl_schedule}")
                    subs = reach.get("subscribers_total")
                    readers = reach.get("reader_total")
                    circ = reach.get("circulation_total")
                    reach_val = subs or readers or circ
                    reach_src = reach.get("source", "")
                    if reach_val:
                        reach_label = "Abonnenten" if subs else ("Leser" if readers else "Auflage")
                        reach_line = f"Reichweite: {fmt_price(reach_val)} {reach_label}"
                        if reach_src:
                            reach_line += f" (Quelle: {reach_src})"
                        meta_lines.append(reach_line)
                    if meta_lines:
                        summary_lines = meta_lines + (["---"] if summary_lines else []) + summary_lines

                if summary_lines:
                    product_data["pricing_summary"] = "\n".join(summary_lines)

                # Erscheinungstermine wenn angefragt
                issue_dates = m.get("issue_dates", "")
                if issue_dates:
                    product_data["issue_dates"] = issue_dates

                # Strukturierte Felder zusaetzlich fuer maschinelle Auswertung
                if nl_schedule:
                    product_data["newsletter_schedule"] = nl_schedule
                if reach:
                    product_data["reach"] = {
                        "circulation": reach.get("circulation_total"),
                        "subscribers": reach.get("subscribers_total"),
                        "readers": reach.get("reader_total"),
                        "primary_metric": reach.get("primary_metric_name"),
                        "source": reach.get("source")
                    }

                product_data["next_step"] = (
                    "Kontakt ZEIT Advise fuer Buchung: advise@zeit.de"
                    if m["match_type"] == "exact_match"
                    else "Alternative verfuegbar. Beratung: advise@zeit.de"
                )

                products_list.append(product_data)

            result_text = {
                "status": "success",
                "message": f"{len(products_list)} passende Produkte gefunden",
                "products": products_list,
                "data_status_date": "2026-01-01",
                "pilot_note": (
                    "ZEIT AdCP Exploration Pilot. Preise basieren auf "
                    "Preisliste 2026 (iq media Nr. 20). Demo-Daten."
                )
            }

        logger.info(
            f"get_products: {result_text['status']}, "
            f"{len(result_text['products'])} products"
        )

        return JSONRPCResponse(
            result={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result_text, indent=2, ensure_ascii=False)
                    }
                ]
            },
            id=request_id
        ).dict()

    except Exception as e:
        logger.error(f"Error in get_products: {e}", exc_info=True)
        return JSONRPCResponse(
            error=MCPError(code=-32603, message="Internal server error").dict(),
            id=request_id
        ).dict()


# =====================================================
# Legacy AdCP Endpoint
# =====================================================

@app.post("/mcp/get_products")
async def legacy_adcp_endpoint(request: Request):
    """Legacy AdCP-Endpoint. Gleicher Output wie MCP-Endpoint."""
    try:
        body = await request.json()
        brief = body.get("brief")
        max_results = body.get("max_results", 10)

        logger.info(f"Legacy: brief_length={len(brief) if brief else 0}")

        if not brief:
            raise HTTPException(400, "Missing required field: brief")

        matches = match_products(
            brief=brief,
            products=product_index.products,
            definitions=definitions,
            max_results=max_results
        )

        if not matches:
            return {
                "products": [],
                "status": "no_match",
                "message": "Keine passenden Produkte gefunden",
                "data_status_date": "2026-01-01"
            }

        products = []
        for m in matches:
            p = m["product"]
            best_format = m.get("best_format")

            product_response = {
                "product_id": p["product_id"],
                "name": p["product_name"],
                "match_type": m["match_type"],
                "match_reasoning": m["reasoning"],
                "match_score": m["score"],
            }

            summary_lines = []

            # --- PRINT ---
            if best_format and best_format.get("price_net_eur"):
                product_response["pricing"] = {
                    "price_net_eur": best_format["price_net_eur"],
                    "format": best_format.get("format_name"),
                    "currency": "EUR"
                }

                fc = m.get("format_candidates") or []
                if len(fc) > 1:
                    for c in fc:
                        summary_lines.append(
                            price_line(c.get("format_name", ""), c.get("price_net_eur"))
                        )
                else:
                    summary_lines.append(
                        price_line(
                            best_format.get("format_name", ""),
                            best_format.get("price_net_eur")
                        )
                    )

            # --- NEWSLETTER + PODCAST ---
            else:
                pr = m.get("pricing")
                if isinstance(pr, dict):
                    nl_price = pr.get("price_eur_net")
                    pod_total = pr.get("total_price_eur_net")

                    if nl_price is not None:
                        product_response["pricing"] = {
                            "price_net_eur": nl_price,
                            "format": pr.get("format_display_name") or pr.get("format_id"),
                            "applied_pricing_model": pr.get("applied_pricing_model"),
                            "applied_cluster": pr.get("applied_cluster"),
                            "applied_kulturpreis": pr.get("applied_kulturpreis"),
                            "currency": "EUR"
                        }
                        fmt_name = pr.get("format_display_name") or pr.get("format_id") or ""
                        if pr.get("applied_kulturpreis"):
                            summary_lines.append(
                                f"{fmt_name} | {fmt_price(nl_price)} {PREIS_SUFFIX} (Kulturpreis)"
                            )
                        else:
                            summary_lines.append(price_line(fmt_name, nl_price))

                    elif pod_total is not None:
                        slot = pr.get("format_slot", "")
                        ad_type = pr.get("ad_type_length", "")
                        tkp = pr.get("tkp_eur_net")
                        ai = pr.get("booked_audio_impressions")
                        pk = pr.get("performance_class", "")
                        cluster = pr.get("cluster", "")
                        product_response["pricing"] = {
                            "price_net_eur": pod_total,
                            "format": f"{ad_type} {slot}".strip(),
                            "tkp_eur_net": tkp,
                            "performance_class": pk,
                            "cluster": cluster,
                            "booked_audio_impressions": ai,
                            "pricing_model": "tkp_based",
                            "currency": "EUR"
                        }
                        summary_lines.append(
                            price_line(f"{ad_type} {slot}".strip(), pod_total)
                        )
                        if tkp and ai:
                            summary_lines.append(
                                f"TKP {tkp} EUR x {fmt_price(ai)} Audio-Impressions"
                            )
                        summary_lines.append(f"Performance-Klasse: {pk}, Cluster: {cluster}")

                    elif pr.get("is_example") and pr.get("example_price_eur_net"):
                        ep = pr["example_price_eur_net"]
                        product_response["pricing"] = {
                            "price_net_eur": ep,
                            "format": "Beispielrechnung (Standard-Setup)",
                            "is_example": True,
                            "currency": "EUR"
                        }
                        summary_lines.append(
                            f"Beispielrechnung bei Standard-Setup: "
                            f"{fmt_price(ep)} {PREIS_SUFFIX}"
                        )
                        if pr.get("example_basis"):
                            summary_lines.append(f"(Basis: {pr['example_basis']})")

                    elif pr.get("hint"):
                        product_response["pricing_hint"] = pr["hint"]
                        summary_lines.append(pr["hint"])

            # Reichweite und Versand in summary_lines einbauen
            reach = p.get("reach", {})
            nl_schedule = m.get("newsletter_schedule", "")
            if nl_schedule or reach:
                meta_lines = []
                if nl_schedule:
                    meta_lines.append(f"Versand: {nl_schedule}")
                subs = reach.get("subscribers_total")
                readers = reach.get("reader_total")
                circ = reach.get("circulation_total")
                reach_val = subs or readers or circ
                reach_src = reach.get("source", "")
                if reach_val:
                    reach_label = "Abonnenten" if subs else ("Leser" if readers else "Auflage")
                    reach_line = f"Reichweite: {fmt_price(reach_val)} {reach_label}"
                    if reach_src:
                        reach_line += f" (Quelle: {reach_src})"
                    meta_lines.append(reach_line)
                if meta_lines:
                    summary_lines = meta_lines + (["---"] if summary_lines else []) + summary_lines

            if summary_lines:
                product_response["pricing_summary"] = "\n".join(summary_lines)

            if m.get("assumptions"):
                product_response["assumptions"] = m["assumptions"]

            # Erscheinungstermine wenn angefragt
            issue_dates = m.get("issue_dates", "")
            if issue_dates:
                product_response["issue_dates"] = issue_dates

            # Strukturierte Felder zusaetzlich
            if nl_schedule:
                product_response["newsletter_schedule"] = nl_schedule
            if reach:
                product_response["reach"] = {
                    "circulation": reach.get("circulation_total"),
                    "subscribers": reach.get("subscribers_total"),
                    "readers": reach.get("reader_total"),
                    "primary_metric": reach.get("primary_metric_name"),
                    "source": reach.get("source")
                }

            products.append(product_response)

        return {
            "products": products,
            "status": "completed",
            "data_status_date": "2026-01-01"
        }

    except Exception as e:
        logger.error(f"Error in legacy endpoint: {e}")
        raise HTTPException(500, "Internal server error")


# =====================================================
# Browse: /products/list
# =====================================================

ZEIT_REGIONAL_IDS = {
    "zeit_hamburg_2026", "zeit_schweiz_2026", "zeit_oesterreich_2026",
    "zeit_im_osten_2026", "zeit_alpen_2026", "christ_und_welt_2026",
}

DIE_ZEIT_BEILAGEN_IDS = {
    "agenda_kultur_2026",
    "entdecken_2026",
    "was_tun_2026",
    "zeit_reisetraeume_2026",
}

DIE_ZEIT_SONDERVEROEFFENTLICHUNGEN_IDS = {
    "zeit_was_tun_themen_2026",
    "zeit_fuer_unternehmer_speziale_2026",
    "zeit_geld_2026",
    "zeit_gesundheit_speziale_2026",
    "zeit_green_2026",
    "zeit_immobilien_speziale_2026",
    "zeit_kunst_kultur_speziale_2026",
    "zeit_literatur_2026",
    "zeit_mobilitaet_technologie_speziale_2026",
    "zeit_nachhaltigkeit_speziale_2026",
    "zeit_reisen_speziale_2026",
    "zeit_schule_bildung_2026",
    "zeit_wissen_speziale_2026",
}

PODCAST_GENRE_MAP = {
    "true_crime": "True-Crime-Podcast", "crime": "True-Crime-Podcast",
    "wirtschaft": "Wirtschafts-Podcast", "politik": "Politik-Podcast",
    "wissen": "Wissens-Podcast", "kultur": "Kultur-Podcast",
}


def _product_subtitle(p: dict) -> str:
    pt  = p.get("product_type", "")
    pid = p.get("product_id", "")
    cat = p.get("_category", "")
    if pid in ZEIT_REGIONAL_IDS:
        return "Regionalausgabe DIE ZEIT"
    if pt == "wochenzeitung":
        return "Wochenzeitung"
    if pt == "magazin":
        freq = p.get("publication_frequency", "")
        return "Wochenmagazin" if freq == "weekly" else "Magazin"
    if pt == "b2b_magazin":
        return "B2B-Magazin"
    if pt == "kindermagazin":
        return "Kindermagazin"
    if pt == "submagazin":
        return "Sub-Magazin"
    if pt == "sonderheft":
        if pid in DIE_ZEIT_BEILAGEN_IDS:
            return "Beilage in DIE ZEIT"
        if pid in DIE_ZEIT_SONDERVEROEFFENTLICHUNGEN_IDS:
            return "Sonderveroeffentlichung in DIE ZEIT"
        return "Magazin"
    if pt == "beilage" or cat == "beilegendes_magazin":
        return "Beilage in DIE ZEIT"
    if pt == "newsletter":
        pricing_models = p.get("pricing_models", [])
        if isinstance(pricing_models, dict):
            for pm in pricing_models.values():
                if isinstance(pm, dict) and pm.get("basis") == "magazine_companion":
                    return "Magazin-Newsletter"
        return "Newsletter"
    if pt == "podcast":
        tags = p.get("matching_metadata", {}).get("topical_tags", [])
        for tag in tags:
            for key, label in PODCAST_GENRE_MAP.items():
                if key in tag.lower():
                    return label
        return "Podcast"
    return pt.replace("_", " ").title() if pt else "Produkt"


def _build_svoe_clusters() -> list:
    """Gruppiert alle Sonderveroeffentlichungen nach topical_cluster und liefert 9 Cluster-Karten."""
    clusters: dict[str, dict] = {}
    for p in product_index.products:
        if p.get("_category") != "sonderveroeffentlichung":
            continue
        tc = p.get("topical_cluster", {})
        cid = tc.get("cluster_id", "")
        cname = tc.get("cluster_display_name", "")
        if not cid:
            continue
        issues = p.get("print_specifics", {}).get("issues", [])
        termine = len(issues)
        if cid not in clusters:
            clusters[cid] = {
                "cluster_id":           cid,
                "cluster_display_name": cname,
                "product_type":         "cluster",
                "name":                 cname,
                "speziale_count":       0,
                "termine_count":        0,
            }
        clusters[cid]["speziale_count"] += 1
        clusters[cid]["termine_count"]  += termine

    result = []
    for c in clusters.values():
        sc = c["speziale_count"]
        tc = c["termine_count"]
        speziale_label = "Speziale" if sc == 1 else "Speziale"
        c["subtitle"] = f"{sc} {speziale_label}, {tc} Termine in 2026"
        result.append(c)
    return sorted(result, key=lambda x: x["name"].lower())


@app.get("/products/list")
async def products_list():
    wochenzeitung, regional, beilagen = [], [], []
    magazine, podcasts, newsletter = [], [], []

    for p in product_index.products:
        pt  = p.get("product_type", "")
        pid = p.get("product_id", "")
        cat = p.get("_category", "")
        item = {
            "product_id":   pid,
            "name":         p.get("product_name", ""),
            "product_type": pt,
            "subtitle":     _product_subtitle(p),
        }
        if pid in ZEIT_REGIONAL_IDS or cat == "regional":
            regional.append(item)
        elif cat == "die_zeit" and pt == "wochenzeitung":
            wochenzeitung.append(item)
        elif pid in DIE_ZEIT_BEILAGEN_IDS:
            beilagen.append(item)
        elif cat == "sonderveroeffentlichung":
            pass  # wird durch _build_svoe_clusters() abgedeckt
        elif pt in ("magazin", "b2b_magazin", "kindermagazin", "submagazin", "sonderheft"):
            magazine.append(item)
        elif pt == "podcast":
            podcasts.append(item)
        elif pt == "newsletter":
            newsletter.append(item)

    s = lambda lst: sorted(lst, key=lambda x: x["name"].lower())
    return {
        "die_zeit": {
            "wochenzeitung":             s(wochenzeitung),
            "regional":                  s(regional),
            "sonderveroeffentlichungen": _build_svoe_clusters(),
            "beilagen":                  s(beilagen),
        },
        "magazine":   s(magazine),
        "podcasts":   s(podcasts),
        "newsletter": s(newsletter),
    }


# =====================================================
# Detail: /products/detail/{product_id}
# =====================================================

_BP_LEVELS = {
    "listenpreis":    "active",
    "branchenpreis_1": "disabled",
    "branchenpreis_2": "disabled",
    "branchenpreis_3": "disabled",
    "branchenpreis_4": "disabled",
}

_SPOT_LENGTH = {
    "audio_ad_20s":         20,
    "native_audio_ad_30s":  30,
    "native_audio_ad_60s":  60,
    "native_audio_ad_240s": 240,
}

# Nur bei PK II Brand-Buchungen anwendbar, nicht bei PK I
VOLUMEN_RABATTE = [
    {"min_audio_impressions":  250000, "rabatt_pct": 20},
    {"min_audio_impressions":  500000, "rabatt_pct": 30},
    {"min_audio_impressions": 1000000, "rabatt_pct": 40},
    {"min_audio_impressions": 2000000, "rabatt_pct": 50},
]

# Verbrechen: AdSlot fuer Eigenwerbung reserviert. Was jetzt: Podcastlaenge zu kurz.
PODCASTS_OHNE_MIDROLL = {
    "verbrechen_podcast_2026",
    "was_jetzt_podcast_2026",
}


def _find_product(product_id: str) -> Optional[dict]:
    for p in product_index.products:
        if p.get("product_id") == product_id:
            return p
    return None


def _top_level_type(p: dict) -> str:
    pt  = p.get("product_type", "")
    cat = p.get("_category", "")
    pid = p.get("product_id", "")
    if pt == "newsletter":
        return "newsletter"
    if pt == "podcast":
        return "podcast"
    if (cat in ("die_zeit", "regional", "sonderveroeffentlichung", "beilegendes_magazin")
            or pid in ZEIT_REGIONAL_IDS
            or pid in DIE_ZEIT_BEILAGEN_IDS
            or pid in DIE_ZEIT_SONDERVEROEFFENTLICHUNGEN_IDS):
        return "die_zeit"
    return "magazin"


def _die_zeit_subtype(p: dict) -> Optional[str]:
    pid = p.get("product_id", "")
    pt  = p.get("product_type", "")
    cat = p.get("_category", "")
    if pid in ZEIT_REGIONAL_IDS or cat == "regional":
        return "regional"
    if cat == "die_zeit" and pt == "wochenzeitung":
        return "wochenzeitung"
    if pid in DIE_ZEIT_BEILAGEN_IDS or pt == "beilage":
        return "beilage"
    if pid in DIE_ZEIT_SONDERVEROEFFENTLICHUNGEN_IDS or pt == "sonderheft":
        return "sonderveroeffentlichung"
    return None


def _build_common(p: dict) -> dict:
    aud = get_audience(p)
    mm  = get_matching_metadata(p)
    return {
        "editorial_focus": p.get("editorial_focus") or p.get("description_long"),
        "themenwelten":    mm.get("topical_tags") or [],
        "zielgruppen":     aud.get("primary") or aud.get("primary_segments") or [],
    }


def _build_reach_magazin(p: dict) -> dict:
    r = get_reach(p)
    return {
        "circulation_total":      r.get("circulation_total"),
        "reader_total":           r.get("reader_total"),
        "subscription_share_pct": r.get("subscription_share_pct"),
        "source":                 r.get("source"),
        "warning":                r.get("reader_data_age_warning"),
    }


def _build_reach_newsletter(p: dict) -> dict:
    r   = get_reach(p)
    aud = get_audience(p)
    return {
        "subscribers_total": r.get("subscribers_total") or aud.get("subscribers_total"),
        "open_rate_pct":     r.get("open_rate_pct"),
        "source":            r.get("source"),
    }


def _build_reach_podcast(p: dict) -> dict:
    r = get_reach(p)
    return {
        "downloads_per_episode":      r.get("downloads_per_episode"),
        "audio_impressions_typical":  r.get("audio_impressions_typical"),
        "engagement_metrics": {
            "completion_rate_pct":     r.get("completion_rate_pct"),
            "attentive_listening_pct": r.get("attentive_listening_pct"),
            "ad_recall_pct":           r.get("ad_recall_pct"),
        },
        "source": r.get("source"),
    }


def _build_pricing_magazin(p: dict) -> dict:
    formats = []
    for f in get_print_ad_formats(p):
        if f.get("mvp_in_scope") is False:
            continue
        formats.append({
            "format_name":  f.get("format_name") or f.get("name", ""),
            "price_net_eur": f.get("price_net_eur"),
            "booking_unit": f.get("booking_unit"),
            "auf_anfrage":  bool(f.get("auf_anfrage", False)),
        })
    return {"currency": "EUR", "price_basis": "listenpreis_netto",
            "formats": formats, "bp_levels": dict(_BP_LEVELS)}


def _build_pricing_newsletter(p: dict) -> dict:
    ns  = get_newsletter_specifics(p)
    npr = ns.get("pricing") or {}
    formats = []
    for f in get_newsletter_formats(p):
        cp         = f.get("cluster_prices") or {}
        flat_price = f.get("price_eur_net") or f.get("kulturpreis_eur_net")
        formats.append({
            "format_name":    f.get("format_display_name") or f.get("format_id", ""),
            "price_net_eur":  flat_price if flat_price is not None else next(iter(cp.values()), None),
            "cluster_prices": cp if cp else None,
            "booking_unit":   f.get("price_unit"),
            "auf_anfrage":    bool(f.get("auf_anfrage", False)),
        })
    return {
        "currency":            "EUR",
        "price_basis":         "listenpreis_netto",
        "pricing_model":       get_newsletter_pricing_model(p),
        "booking_unit":        npr.get("booking_unit"),
        "applicable_clusters": npr.get("applicable_clusters"),
        "formats":             formats,
        "bp_levels":           dict(_BP_LEVELS),
    }


def _build_pricing_podcast(p: dict) -> dict:
    pid = p.get("product_id", "")
    fpp = get_podcast_fixed_placement_pricing(p)

    if fpp and fpp.get("formats"):
        fixed_slots = []
        for f in (fpp.get("formats") or []):
            slot = f.get("slot") or f.get("ad_type_id") or f.get("format_id", "")
            cp   = f.get("cluster_prices") or f.get("cluster_prices_eur_net") or {}
            price = next(iter(cp.values()), None) if cp else None
            fixed_slots.append({
                "slot":                slot,
                "cluster_prices":      cp,
                "price_per_episode_eur": price,
                "min_episodes":        f.get("min_episodes"),
            })
        return {
            "currency":        "EUR",
            "pricing_model":   "fixed_slot",
            "laufzeit":        fpp.get("laufzeit"),
            "fixed_slots":     fixed_slots,
            "midroll_excluded": pid in PODCASTS_OHNE_MIDROLL,
            "volume_discounts": None,
            "bp_levels":       dict(_BP_LEVELS),
        }

    # TKP: Default-Raten aus podcast_definitions, Produkt-Level hat Vorrang
    pod_defs = definitions.get("podcast") or {}
    table = dict(pod_defs.get("tkp_pricing_table") or {})
    product_override = (get_podcast_tkp_pricing(p) or {}).get("tkp_pricing_table") or {}
    if product_override:
        table.update(product_override)

    tkp_rows = []
    for ad_type_length, slots in table.items():
        if not isinstance(slots, dict):
            continue
        for slot_key, pks in slots.items():
            if not isinstance(pks, dict):
                continue
            for pk, cluster_prices in pks.items():
                if not isinstance(cluster_prices, dict):
                    continue
                tkp_rows.append({
                    "ad_type_length":      ad_type_length,
                    "spot_length_seconds": _SPOT_LENGTH.get(ad_type_length),
                    "slot":                slot_key,
                    "performance_class":   pk,
                    "cluster_prices":      cluster_prices,
                })

    mbv = pod_defs.get("minimum_booking_value_eur_net") or {}
    return {
        "currency":        "EUR",
        "pricing_model":   "tkp",
        "tkp_table":       tkp_rows,
        "mbv_eur_net":     mbv if isinstance(mbv, dict) else {},
        "midroll_excluded": pid in PODCASTS_OHNE_MIDROLL,
        "volume_discounts": VOLUMEN_RABATTE,
        "bp_levels":       dict(_BP_LEVELS),
    }


def _build_die_zeit_wochenzeitung(p: dict) -> dict:
    """Reichert die 56 Issues mit Labels auf und baut Regional- und Speziale-Listen."""
    ps = p.get("print_specifics", {})
    r  = get_reach(p)

    reach = {
        "circulation_total": r.get("circulation_total"),
        "subscribers_total": r.get("subscribers_total"),
        "reader_total":      r.get("reader_total"),
        "source":            r.get("source"),
    }

    issues_out = []
    for iss in ps.get("issues", []):
        bd    = iss.get("booking_deadline")
        bt    = iss.get("booking_deadline_time_local", "10:00")
        ppd   = iss.get("premium_placement_booking_deadline")
        ppt   = iss.get("premium_placement_booking_deadline_time_local", "10:00")
        bd_d  = _date.fromisoformat(bd)  if bd  else None
        ppd_d = _date.fromisoformat(ppd) if ppd else None
        issues_out.append({
            "issue_number":               iss.get("issue_number_sequential"),
            "publication_date":           iss.get("publication_date"),
            "publication_weekday":        iss.get("publication_weekday"),
            "is_special_publication_date": bool(iss.get("is_special_publication_date")),
            "booking_deadline":           f"{bd}T{bt}:00" if bd else None,
            "booking_deadline_label":     _fmt_ad_close_label(bd_d, bt)   if bd_d  else None,
            "premium_deadline":           f"{ppd}T{ppt}:00" if ppd else None,
            "premium_deadline_label":     _fmt_ad_close_label(ppd_d, ppt) if ppd_d else None,
            "has_special_themes":         bool(iss.get("has_special_themes")),
            "linked_speziale":            iss.get("linked_speziale") or [],
            "issue_themes":               iss.get("issue_themes") or [],
        })

    schedule = {
        "issues_per_year":      ps.get("issue_count_per_year"),
        "issues_regular":       ps.get("issue_count_regular"),
        "issues_special_dates": sum(1 for i in issues_out if i["is_special_publication_date"]),
        "issues":               issues_out,
    }

    # Regionalausgaben alphabetisch sortiert nach ID
    regional_editions = []
    for rid in sorted(ZEIT_REGIONAL_IDS):
        rp = _find_product(rid)
        if rp:
            regional_editions.append({
                "product_id":   rid,
                "product_name": rp.get("product_name", rid),
            })

    # Alle Speziale aus linked_speziale aller Issues dedupliziert
    seen: dict = {}
    for iss in issues_out:
        for sp in iss.get("linked_speziale", []):
            sid = sp.get("speziale_product_id")
            if sid and sid not in seen:
                seen[sid] = {
                    "speziale_product_id":  sid,
                    "product_name":         sp.get("theme_display_name", sid),
                    "cluster_id":           sp.get("cluster_id"),
                    "cluster_display_name": sp.get("cluster_display_name"),
                }
    all_speziale = list(seen.values())

    return {
        "reach":                  reach,
        "schedule":               schedule,
        "regional_editions":      regional_editions,
        "all_speziale":           all_speziale,
        "special_theme_clusters": ps.get("special_theme_clusters") or [],
    }


def _build_schedule_magazin(p: dict) -> Optional[dict]:
    issues = []
    for iss in get_issues(p):
        themes = iss.get("issue_themes") or []
        issues.append({
            "issue_number":        iss.get("issue_id") or iss.get("issue_number"),
            "publication_date":    iss.get("publication_date"),
            "ad_close_date":       iss.get("booking_deadline"),
            "material_close_date": iss.get("material_deadline"),
            "thematic_focus":      themes[0] if themes else iss.get("special_theme"),
        })
    return {"issues": issues} if issues else None


# -------------------------------------------------------
# Ad-Close-Berechnung fuer Newsletter-Kalender
# -------------------------------------------------------

_DOW_MAP = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
_DOW_KEY = {0: 'mon', 1: 'tue', 2: 'wed', 3: 'thu', 4: 'fri', 5: 'sat', 6: 'sun'}
_DOW_ABBR_DE = {0: 'Mo', 1: 'Di', 2: 'Mi', 3: 'Do', 4: 'Fr', 5: 'Sa', 6: 'So'}


def _fmt_ad_close_label(d: _date, time_str: str) -> str:
    return f"{_DOW_ABBR_DE[d.weekday()]} {d.strftime('%d.%m.%Y')}, {time_str}"


def _ad_close_iso(d: _date, time_str: str) -> str:
    h, m = (int(x) for x in time_str.split(":"))
    return datetime(d.year, d.month, d.day, h, m).isoformat()


def _iso_week(d: _date) -> int:
    return d.isocalendar()[1]


def calc_ad_close_date(issue_date: _date, logic: str, reference, time_str: str = "12:00") -> Optional[_date]:
    if not issue_date or not logic:
        return None
    if logic == "weekday_previous_week":
        target_dow = _DOW_MAP.get(str(reference), 0)
        mon_of_week = issue_date - timedelta(days=issue_date.weekday())
        mon_of_prev = mon_of_week - timedelta(weeks=1)
        return mon_of_prev + timedelta(days=target_dow)
    if logic == "weekday_before_issue":
        target_dow = _DOW_MAP.get(str(reference), 0)
        d = issue_date - timedelta(days=1)
        for _ in range(14):
            if d.weekday() == target_dow:
                return d
            d -= timedelta(days=1)
    if logic == "workdays_before_issue":
        n = int(reference)
        d = issue_date - timedelta(days=1)
        counted = 0
        for _ in range(n * 3 + 14):
            if d.weekday() < 5:
                counted += 1
                if counted >= n:
                    return d
            d -= timedelta(days=1)
    return None


def _calc_next_issues(pub_days: list, dl: dict, ns: dict, is_kw: bool) -> list:
    logic   = dl.get("ad_close_logic")
    ref     = dl.get("ad_close_reference")
    t       = dl.get("ad_close_time") or "12:00"
    fmts    = (ns.get("pricing") or {}).get("formats") or []
    today   = _date.today()
    horizon = today + timedelta(weeks=16)
    result  = []

    if is_kw:
        days_to_mon = (7 - today.weekday()) % 7 or 7
        kw_start = today + timedelta(days=days_to_mon)
        for _ in range(16):
            kw_end  = kw_start + timedelta(days=6)
            kw_num  = _iso_week(kw_start)
            acd     = calc_ad_close_date(kw_start, logic, ref, t)
            entry   = {
                "kw_number": kw_num,
                "kw_start":  kw_start.isoformat(),
                "kw_end":    kw_end.isoformat(),
            }
            if acd:
                entry["ad_close"]       = _ad_close_iso(acd, t)
                entry["ad_close_label"] = _fmt_ad_close_label(acd, t)
            result.append(entry)
            kw_start += timedelta(weeks=1)
        return result

    if not pub_days:
        return result

    dow_set = {_DOW_MAP[d] for d in pub_days if d in _DOW_MAP}

    pub_day_overrides: dict = {}
    if logic == "pub_day_specific":
        for ov in (dl.get("pub_day_overrides") or []):
            pd = ov.get("publication_day")
            if pd:
                pub_day_overrides[pd] = ov

    max_entries = 100
    d = today + timedelta(days=1)
    while d <= horizon and len(result) < max_entries:
        if d.weekday() in dow_set:
            day_key = _DOW_KEY[d.weekday()]
            entry   = {"issue_date": d.isoformat(), "weekday": day_key}
            if logic == "format_specific":
                fmt_closes = []
                for fmt in fmts:
                    fo  = fmt.get("ad_close_overrides") or {}
                    fl  = fo.get("ad_close_logic")
                    fr  = fo.get("ad_close_reference")
                    ft  = fo.get("ad_close_time") or t
                    acd = calc_ad_close_date(d, fl, fr, ft) if fl else None
                    fmt_closes.append({
                        "format_name":    fmt.get("format_display_name") or fmt.get("format_id", ""),
                        "ad_close":       _ad_close_iso(acd, ft) if acd else None,
                        "ad_close_label": _fmt_ad_close_label(acd, ft) if acd else None,
                    })
                entry["format_ad_closes"] = fmt_closes
            elif logic == "pub_day_specific":
                ov = pub_day_overrides.get(day_key)
                if ov:
                    ov_t = ov.get("ad_close_time") or t
                    acd  = calc_ad_close_date(d, ov.get("ad_close_logic"), ov.get("ad_close_reference"), ov_t)
                    if acd:
                        entry["ad_close"]       = _ad_close_iso(acd, ov_t)
                        entry["ad_close_label"] = _fmt_ad_close_label(acd, ov_t)
            elif logic:
                acd = calc_ad_close_date(d, logic, ref, t)
                if acd:
                    entry["ad_close"]       = _ad_close_iso(acd, t)
                    entry["ad_close_label"] = _fmt_ad_close_label(acd, t)
            result.append(entry)
        d += timedelta(days=1)
    return result


def _build_schedule_newsletter(p: dict) -> Optional[dict]:
    ns  = get_newsletter_specifics(p)
    ch  = ns.get("channel") or {}
    dl  = ns.get("deadlines") or {}
    freq_label      = format_newsletter_schedule(p)
    publication_days = ch.get("publication_days") or []
    issues_per_year  = ch.get("issues_per_year")
    anzeigenschluss  = dl.get("anzeigenschluss")
    booking_unit     = (ns.get("pricing") or {}).get("booking_unit")
    is_kw            = booking_unit == "kalenderwoche"
    if not (freq_label or publication_days or issues_per_year or anzeigenschluss):
        return None
    next_issues = _calc_next_issues(publication_days, dl, ns, is_kw)
    return {
        "frequency_label":   freq_label or None,
        "publication_days":  publication_days,
        "issues_per_year":   issues_per_year,
        "anzeigenschluss":   anzeigenschluss,
        "next_issues":       next_issues,
    }


def _check_completeness(p: dict, top_type: str, pricing: dict,
                        schedule: Optional[dict]) -> dict:
    missing = []
    if not (p.get("editorial_focus") or p.get("description_long")):
        missing.append("editorial_focus")
    fmts = pricing.get("formats") or pricing.get("fixed_slots") or pricing.get("tkp_table") or []
    if not fmts:
        missing.append("pricing.formats")
    elif not any(f.get("price_net_eur") or f.get("price_per_episode_eur") or f.get("tkp_eur")
                 for f in fmts):
        missing.append("pricing.formats[].price")
    if top_type in ("magazin", "die_zeit") and not (schedule and schedule.get("issues")):
        missing.append("schedule.issues")
    if top_type == "newsletter" and not (schedule and schedule.get("frequency_label")):
        missing.append("schedule.frequency")
    if not missing:
        level = "full"
    elif len(missing) <= 2:
        level = "partial"
    else:
        level = "minimal"
    return {"data_completeness": level, "missing_fields": missing}


def _build_dz_speziale_lookup() -> dict:
    """Baut Lookup (speziale_id, pub_date) -> DZ-Issue-Daten fuer Kreuzreferenz."""
    dz = _find_product("die_zeit_2026")
    lookup: dict = {}
    if not dz:
        return lookup
    for dz_iss in dz.get("print_specifics", {}).get("issues", []):
        for ls in (dz_iss.get("linked_speziale") or []):
            key = (ls.get("speziale_product_id"), dz_iss.get("publication_date"))
            lookup[key] = {
                "in_zeit_issue_number": dz_iss.get("issue_number_sequential"),
                "theme_display_name":   ls.get("theme_display_name"),
                "ad_close_date":        ls.get("theme_specific_ad_close_date"),
            }
    return lookup


def _speziale_iss_to_termin(iss: dict, pid: str, dz_data: dict) -> dict:
    pub_date    = iss.get("publication_date")
    ad_close_str = dz_data.get("ad_close_date") or iss.get("booking_deadline")
    pub_d        = _date.fromisoformat(pub_date)    if pub_date    else None
    ad_close_d   = _date.fromisoformat(ad_close_str) if ad_close_str else None
    themes       = iss.get("issue_themes") or []
    return {
        "issue_id":              iss.get("issue_id"),
        "publication_date":      pub_date,
        "publication_date_label": (
            f"{_DOW_ABBR_DE[pub_d.weekday()]} {pub_d.strftime('%d.%m.%Y')}" if pub_d else None
        ),
        "theme_display_name":    dz_data.get("theme_display_name") or (themes[0] if themes else None),
        "in_zeit_issue_number":  dz_data.get("in_zeit_issue_number"),
        "ad_close_date":         ad_close_str,
        "ad_close_label":        _fmt_ad_close_label(ad_close_d, "10:00") if ad_close_d else None,
    }


def _build_cluster_termine(cluster_id: str) -> Optional[dict]:
    """Alle Termine aller Speziale in einem Cluster, chronologisch sortiert."""
    dz_lookup = _build_dz_speziale_lookup()
    cluster_display_name = None
    termine = []
    for p in product_index.products:
        if p.get("_category") != "sonderveroeffentlichung":
            continue
        tc = p.get("topical_cluster", {})
        if tc.get("cluster_id") != cluster_id:
            continue
        if cluster_display_name is None:
            cluster_display_name = tc.get("cluster_display_name")
        pid   = p.get("product_id", "")
        pname = p.get("product_name", "")
        for iss in p.get("print_specifics", {}).get("issues", []):
            pub_date = iss.get("publication_date")
            dz_data  = dz_lookup.get((pid, pub_date), {})
            t = _speziale_iss_to_termin(iss, pid, dz_data)
            t["speziale_product_id"]   = pid
            t["speziale_product_name"] = pname
            termine.append(t)
    if not termine:
        return None
    termine.sort(key=lambda x: x.get("publication_date") or "")
    return {
        "cluster_id":           cluster_id,
        "cluster_display_name": cluster_display_name,
        "termine_count":        len(termine),
        "termine":              termine,
    }


def _build_speziale_detail(p: dict) -> dict:
    """Terminkliste, Reichweite und Ad-Formate fuer ein Speziale-Produkt."""
    pid      = p.get("product_id", "")
    dz_lookup = _build_dz_speziale_lookup()
    termine  = []
    for iss in p.get("print_specifics", {}).get("issues", []):
        pub_date = iss.get("publication_date")
        dz_data  = dz_lookup.get((pid, pub_date), {})
        termine.append(_speziale_iss_to_termin(iss, pid, dz_data))
    termine.sort(key=lambda x: x.get("publication_date") or "")

    r = get_reach(p)
    reach = {
        "circulation_total": r.get("circulation_total"),
        "reader_total":      r.get("reader_total"),
        "source":            r.get("source"),
        "inherited_from":    r.get("inheritance", {}).get("source_product_id"),
    }

    ad_formats = []
    for f in p.get("print_specifics", {}).get("ad_formats", []):
        if f.get("mvp_in_scope") is False:
            continue
        ad_formats.append({
            "format_name":   f.get("format_name"),
            "price_net_eur": f.get("price_net_eur"),
            "auf_anfrage":   bool(f.get("auf_anfrage", False)),
        })

    tc = p.get("topical_cluster", {})
    return {
        "termine":    termine,
        "reach":      reach,
        "ad_formats": ad_formats,
        "cluster": {
            "cluster_id":           tc.get("cluster_id"),
            "cluster_display_name": tc.get("cluster_display_name"),
        },
    }


# =====================================================
# Cluster: /clusters/sonderveroeffentlichungen/{id}
# =====================================================

@app.get("/clusters/sonderveroeffentlichungen/{cluster_id}")
async def cluster_detail(cluster_id: str):
    result = _build_cluster_termine(cluster_id)
    if result is None:
        raise HTTPException(404, detail={"error": "Cluster nicht gefunden",
                                         "cluster_id": cluster_id})
    return result


@app.get("/products/detail/{product_id}")
async def product_detail(product_id: str):
    p = _find_product(product_id)
    if p is None:
        raise HTTPException(404, detail={"error": "Product not found",
                                         "product_id": product_id})
    try:
        top_type = _top_level_type(p)
        subtype  = _die_zeit_subtype(p) if top_type == "die_zeit" else None
        common   = _build_common(p)

        # Wochenzeitung: eigener Zweig mit 56-Issues-Struktur
        if top_type == "die_zeit" and subtype == "wochenzeitung":
            wz = _build_die_zeit_wochenzeitung(p)
            return {
                "product_id":             p.get("product_id"),
                "name":                   p.get("product_name"),
                "subtitle":               _product_subtitle(p),
                "product_type":           "wochenzeitung",
                "die_zeit_subtype":       "wochenzeitung",
                "common":                 common,
                "reach":                  wz["reach"],
                "schedule":               wz["schedule"],
                "pricing":                None,
                "regional_editions":      wz["regional_editions"],
                "all_speziale":           wz["all_speziale"],
                "special_theme_clusters": wz["special_theme_clusters"],
            }

        # Sonderveroeffentlichung: Terminkliste + Reichweite + Formate
        if p.get("_category") == "sonderveroeffentlichung":
            spez = _build_speziale_detail(p)
            return {
                "product_id":       p.get("product_id"),
                "name":             p.get("product_name"),
                "subtitle":         _product_subtitle(p),
                "product_type":     "sonderveroeffentlichung",
                "die_zeit_subtype": "sonderveroeffentlichung",
                "common":           common,
                "reach":            spez["reach"],
                "ad_formats":       spez["ad_formats"],
                "termine":          spez["termine"],
                "cluster":          spez["cluster"],
            }

        if top_type in ("magazin", "die_zeit"):
            reach    = _build_reach_magazin(p)
            pricing  = _build_pricing_magazin(p)
            schedule = _build_schedule_magazin(p)
        elif top_type == "newsletter":
            reach    = _build_reach_newsletter(p)
            pricing  = _build_pricing_newsletter(p)
            schedule = _build_schedule_newsletter(p)
        else:  # podcast
            reach    = _build_reach_podcast(p)
            pricing  = _build_pricing_podcast(p)
            schedule = None

        return {
            "product_id":       p.get("product_id"),
            "name":             p.get("product_name"),
            "subtitle":         _product_subtitle(p),
            "product_type":     top_type,
            "die_zeit_subtype": subtype,
            "common":           common,
            "reach":            reach,
            "pricing":          pricing,
            "schedule":         schedule,
            "_meta":            _check_completeness(p, top_type, pricing, schedule),
        }
    except Exception as e:
        logger.error(f"product_detail error for {product_id}: {e}", exc_info=True)
        raise HTTPException(500, detail={"error": "Internal server error",
                                         "product_id": product_id})


# =====================================================
# Health & Discovery
# =====================================================

@app.get("/health")
async def health():
    by_type: Dict[str, int] = {}
    for p in product_index.products:
        pt = p.get("product_type", "unknown")
        by_type[pt] = by_type.get(pt, 0) + 1

    return {
        "status": "ok",
        "version": "1.7.0",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "environment": ENVIRONMENT,
        "schema_version": "3.0",
        "products_total": len(product_index.products),
        "products_by_type": by_type,
        "definitions_loaded": sorted(definitions.keys()),
        "protocol": "mcp",
        "protocol_version": "2024-11-05"
    }


@app.get("/.well-known/adagents.json")
async def adagents_discovery():
    adagents_path = BASE_DIR / "adagents.json"
    if not adagents_path.exists():
        raise HTTPException(404, "adagents.json not found")
    with open(adagents_path, encoding='utf-8') as f:
        return JSONResponse(json.load(f))


# =====================================================
# Startup
# =====================================================

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("ZEIT AdCP MCP Server gestartet")
    logger.info("Version: 1.6.0")
    logger.info(f"Environment: {ENVIRONMENT}")
    logger.info(f"Schema-Version: 3.0")
    logger.info(f"Produkte geladen: {len(product_index.products)}")
    logger.info(f"Definitions geladen: {sorted(definitions.keys())}")
    logger.info(f"MCP Tools: {len(MCP_TOOLS)}")
    logger.info("=" * 60)


# =====================================================
# Frontend ausliefern
# =====================================================
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

