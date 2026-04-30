"""
ZEIT AdCP MCP Server - v1.5.5

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
from datetime import datetime

from matching import match_products, ProductIndex, parse_brief, check_industry_discount

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
    version="1.5.5",
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

@app.get("/")
async def root():
    return {
        "name": "ZEIT AdCP MCP Server",
        "version": "1.5.5",
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
            "serverInfo": {"name": "ZEIT AdCP MCP Server", "version": "1.5.5"},
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
        "version": "1.5.5",
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
    logger.info("Version: 1.5.5")
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

