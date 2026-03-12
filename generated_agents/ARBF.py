import os
import sys
import time
import json
import random
import argparse
from typing import List, Dict, Optional, Any, Tuple, Generator
from dataclasses import dataclass, field, asdict
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from loguru import logger
from ddgs import DDGS
from swarms import Agent
import tempfile

def getenv(key: str, default: Optional[str] = None) -> str:
    val = os.environ.get(key)
    if val is None:
        if default is not None:
            return default
        raise ValueError(f"Missing required environment variable: {key}")
    return val

MODEL_NAME = getenv("SWARMS_MODEL", "gpt-4o-mini")
MAX_LOOPS = int(os.environ.get("SWARMS_MAX_LOOPS", "5"))
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "")
AMAZON_ACCESS_KEY = os.environ.get("AMAZON_ACCESS_KEY", "")
AMAZON_SECRET_KEY = os.environ.get("AMAZON_SECRET_KEY", "")
AMAZON_ASSOC_TAG = os.environ.get("AMAZON_ASSOC_TAG", "")
PROFIT_MARGIN = float(os.environ.get("MIN_PROFIT_MARGIN", "0.20"))  # 20% by default
MAX_DAILY_PURCHASES = int(os.environ.get("MAX_DAILY_PURCHASES", "2"))  # daily safety limit
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in ("true", "1", "yes")

# --- Structured Data Types ---
@dataclass
class EbayListing:
    item_id: str
    title: str
    price: float
    currency: str
    url: str
    image: str
    condition: str
    shipping: float
    seller_username: str
    seller_feedback: float
    available: bool

@dataclass
class AmazonListing:
    asin: str
    title: str
    price: float
    currency: str
    url: str
    image: str
    prime: bool
    fba: bool

@dataclass
class ArbitrageOpportunity:
    ebay: EbayListing
    amazon: AmazonListing
    estimated_profit: float
    profit_margin: float

# --- HTTP Session with Retries ---
def get_retry_session() -> requests.Session:
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# --- EBAY SEARCH ---
def search_ebay(keyword: str, max_results: int = 20) -> List[EbayListing]:
    """
    Search eBay for listings using the Finding API.
    Requires EBAY_APP_ID env var (application key).
    """
    results = []
    if not EBAY_APP_ID:
        logger.warning("No EBAY_APP_ID supplied; skipping live eBay API. Using DDGS fallback.")
        # Fallback: DuckDuckGo scrape
        with DDGS() as ddgs:
            for r in ddgs.text(f"site:ebay.com {keyword}", max_results=max_results):
                price = extract_price_from_text(r.get("body", ""))
                if price is None:
                    continue
                listing = EbayListing(
                    item_id = r["href"].split("/itm/")[-1].split("?")[0],
                    title = r["title"],
                    price = price,
                    currency = "USD",
                    url = r["href"],
                    image = r.get("image", ""),
                    condition = "Unknown",
                    shipping = 0.0,
                    seller_username = "",
                    seller_feedback = 0.0,
                    available = True
                )
                results.append(listing)
    else:
        endpoint = "https://svcs.ebay.com/services/search/FindingService/v1"
        headers = {"X-EBAY-SOA-SECURITY-APPNAME": EBAY_APP_ID}
        params = {
            "OPERATION-NAME": "findItemsByKeywords",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": EBAY_APP_ID,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "",
            "keywords": keyword,
            "paginationInput.entriesPerPage": str(max_results),
            "outputSelector": "SellerInfo"
        }
        session = get_retry_session()
        try:
            resp = session.get(endpoint, params=params, headers=headers, timeout=45)
            resp.raise_for_status()
            data = resp.json()
            items = data["findItemsByKeywordsResponse"][0]["searchResult"][0].get("item", [])
            for item in items:
                try:
                    id_ = item["itemId"][0]
                    price = float(item["sellingStatus"][0]["convertedCurrentPrice"][0]["__value__"])
                    currency = item["sellingStatus"][0]["convertedCurrentPrice"][0]["@currencyId"]
                    title = item.get("title", [""])[0]
                    url = item.get("viewItemURL", [""])[0]
                    image = item.get("galleryURL", [""])[0]
                    cond = item.get("condition", [{}])[0].get("conditionDisplayName", "Unknown")
                    ship = float(item.get("shippingInfo", [{}])[0].get("shippingServiceCost", [{}])[0].get("__value__", 0.0))
                    seller = item.get("sellerInfo", [{}])[0].get("sellerUserName", "")
                    fb = float(item.get("sellerInfo", [{}])[0].get("positiveFeedbackPercent", 0.0))
                    available = True
                    listing = EbayListing(
                        item_id=id_,
                        title=title,
                        price=price,
                        currency=currency,
                        url=url,
                        image=image,
                        condition=cond,
                        shipping=ship,
                        seller_username=seller,
                        seller_feedback=fb,
                        available=available
                    )
                    results.append(listing)
                except Exception as ex:
                    logger.error(f"Error parsing eBay result: {ex}")
        except requests.RequestException as ex:
            logger.error(f"eBay API failure: {ex}")
    return results

def extract_price_from_text(text: str) -> Optional[float]:
    """Best-effort extraction of price from string."""
    import re
    m = re.search(r'\$([0-9,.]+)', text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None

# --- AMAZON SEARCH ---
def search_amazon(keyword: str, max_results: int = 10) -> List[AmazonListing]:
    """
    Search Amazon for the keyword. Use DDGS as public fallback for demo purposes; real deployments should use PA API with keys.
    """
    results: List[AmazonListing] = []
    if not AMAZON_ACCESS_KEY or not AMAZON_SECRET_KEY or not AMAZON_ASSOC_TAG:
        logger.warning("No Amazon API keys; using DDGS Amazon scrape fallback.")
        with DDGS() as ddgs:
            for r in ddgs.text(f"site:amazon.com {keyword}", max_results=max_results):
                price = extract_price_from_text(r.get("body", ""))
                if price is None:
                    continue
                asin = extract_asin(r.get("href", ""))
                if asin is None:
                    continue
                listing = AmazonListing(
                    asin=asin,
                    title=r["title"],
                    price=price,
                    currency="USD",
                    url=r["href"],
                    image=r.get("image", ""),
                    prime="prime" in r["body"].lower(),
                    fba="fulfilled by amazon" in r["body"].lower() or "fba" in r["body"].lower()
                )
                results.append(listing)
    else:
        # Place for real PA API implementation with keys. For demo, fallback is DDGS only.
        logger.info("Amazon Product Advertising API not implemented in this demo; using DDGS fallback.")
    return results

def extract_asin(url: str) -> Optional[str]:
    """Extract Amazon ASIN from url."""
    import re
    m = re.search(r'/dp/([A-Z0-9]{10})', url)
    if m:
        return m.group(1)
    m = re.search(r'/gp/product/([A-Z0-9]{10})', url)
    if m:
        return m.group(1)
    return None

# --- PROFIT ESTIMATION ---
def estimate_profit(ebay: EbayListing, amazon: AmazonListing) -> Tuple[float, float]:
    """
    Calculate estimated profit and margin after fees/sales tax/ship.
    Conservative: Amazon takes ~15% cut; eBay shipping variable.
    """
    amazon_price = amazon.price
    ebay_cost = ebay.price + (ebay.shipping or 0.0)
    amazon_fees = 0.15 * amazon_price
    # Assume shipping to Amazon for FBA costs $2.5 per item
    misc_cost = 2.5
    gross_profit = amazon_price - amazon_fees - misc_cost - ebay_cost
    profit_margin = 0.0
    try:
        if ebay_cost > 0:
            profit_margin = gross_profit / ebay_cost
    except Exception:
        profit_margin = 0.0
    return gross_profit, profit_margin

# --- SCAN MARKET AND FIND OPPORTUNITIES ---
def find_arbitrage_opportunities(
    keywords: List[str],
    max_per_keyword: int = 5,
    max_total: int = 15
) -> List[ArbitrageOpportunity]:
    """Finds opportunities given a list of keywords."""
    opportunities: List[ArbitrageOpportunity] = []
    for kw in keywords:
        ebay_listings = search_ebay(kw, max_results=max_per_keyword)
        logger.info(f"Found {len(ebay_listings)} eBay listings for '{kw}'")
        if not ebay_listings:
            continue
        amazon_listings = search_amazon(kw, max_results=max_per_keyword)
        logger.info(f"Found {len(amazon_listings)} Amazon listings for '{kw}'")
        if not amazon_listings:
            continue
        for el in ebay_listings:
            for al in amazon_listings:
                # Require Amazon price > eBay price + 20% margin
                if al.price <= 0 or el.price <= 0:
                    continue
                profit, margin = estimate_profit(el, al)
                if profit > 10 and margin > PROFIT_MARGIN:
                    opp = ArbitrageOpportunity(
                        ebay=el,
                        amazon=al,
                        estimated_profit=round(profit,2),
                        profit_margin=round(margin,3)
                    )
                    opportunities.append(opp)
                    logger.success(f"Profitable flip: eBay '{el.title}' @${el.price} → Amazon '{al.title}' @${al.price} | Est. profit: ${profit:.2f} ({margin*100:.1f}%)")
                if len(opportunities) >= max_total:
                    return opportunities
    return opportunities

# --- EBAY PURCHASE (Dry-run by default) ---
def purchase_ebay_item(listing: EbayListing) -> Dict[str, Any]:
    """
    Make a purchase (buy it now) on eBay. For public releases, always dry-run unless authorized.
    """
    if DRY_RUN or not EBAY_APP_ID:
        logger.info(f"DRY RUN: Would purchase eBay item '{listing.title}' ({listing.url}) for ${listing.price}")
        return {"status": "dry-run", "item_id": listing.item_id, "url": listing.url}
    # Place for real eBay order API logic. For live, must use OAuth+buyer account API.
    logger.warning("Live eBay order automation requires additional implementation and authorization.")
    return {"status": "not-implemented", "item_id": listing.item_id, "url": listing.url}

# --- AMAZON RELISTING (Dry-run) ---
def relist_on_amazon(listing: EbayListing, price: float) -> Dict[str, Any]:
    """
    Prepare Amazon listing details for relisting. Outputs relist plan; requires additional manual or API step to list.
    """
    if DRY_RUN or not (AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY and AMAZON_ASSOC_TAG):
        logger.info(f"DRY RUN: Would create Amazon listing for '{listing.title}' at ${price}")
        return {"status": "dry-run", "title": listing.title, "price": price}
    # Place for real Amazon MWS/SP-API call.
    logger.warning("Live Amazon listing not implemented for public safety.")
    return {"status": "not-implemented", "title": listing.title, "price": price}

# --- EXPORT TO TEMP FILE ---
def export_opportunities(opportunities: List[ArbitrageOpportunity]) -> str:
    """
    Write opportunities as jsonl to a temp file, return path.
    """
    if not opportunities:
        logger.warning("No arbitrage opportunities to export.")
        return ""
    with tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8", errors="replace", suffix=".jsonl") as f:
        for opp in opportunities:
            f.write(json.dumps({
                "ebay": asdict(opp.ebay),
                "amazon": asdict(opp.amazon),
                "estimated_profit": opp.estimated_profit,
                "profit_margin": opp.profit_margin
            }) + "\n")
        logger.info(f"Exported opportunities to {f.name}")
        return f.name

# --- SYSTEM PROMPT FOR SWARMS ---
SYSTEM_PROMPT = (
    "You are ArbiFlip, an autonomous e-commerce arbitrage agent.\n"
    "Your job is to continuously find underpriced products on eBay,\n"
    "assess resale potential by searching Amazon listings, estimate after-fee profits,\n"
    "and identify only those where the expected profit and margin are both attractive and above threshold.\n"
    "You must export a list of arbitrage leads (eBay url, Amazon url, profit, margin),\n"
    "and for each, provide a summary of the opportunity.\n"
    "All input and output is in JSON. Always validate all listings and prices.\n"
    "DO NOT attempt to interactively prompt or ask for user credentials.\n"
    "For each opportunity, output a JSON object with eBay/amazon/full links, prices, est_profit, margin,\n"
    "and relist instruction. Dry run is always enabled unless API keys are present.\n"
)

# --- AGENT CONSTRUCTION ---
agent = Agent(
    agent_name="ArbiFlip Agent",
    agent_description="Autonomously discovers eBay-to-Amazon arbitrage leads and prepares full opportunity exports, with dry-run safety.",
    system_prompt=SYSTEM_PROMPT,
    model_name=MODEL_NAME,
    max_loops=MAX_LOOPS,
)

# --- ENTRYPOINT ---
def main() -> None:
    logger.info("Starting ArbiFlip arbitrage opportunity search...")
    parser = argparse.ArgumentParser(description="ArbiFlip: eBay-Amazon Arbitrage AI")
    parser.add_argument("--keywords", type=str, default="electronics,office gadgets,headphones,bluetooth speakers,lego", help="Comma-separated list of product keywords (default: electronics,...)")
    parser.add_argument("--max-per-keyword", type=int, default=5, help="Max eBay/Amazon results per keyword (default: 5)")
    parser.add_argument("--max-total", type=int, default=12, help="Max total arbitrage leads to return (default: 12)")
    parser.add_argument("--dry-run", action="store_true", default=DRY_RUN, help="Enable dry-run mode (default: true)")
    args = parser.parse_args() if hasattr(sys, 'argv') else None

    keywords = [k.strip() for k in (args.keywords if args else "electronics,office gadgets,headphones,bluetooth speakers,lego").split(",") if k.strip()]
    opps = find_arbitrage_opportunities(
        keywords,
        max_per_keyword=args.max_per_keyword if args else 5,
        max_total=args.max_total if args else 12
    )
    logger.info(f"Discovered {len(opps)} arbitrage opportunities.")

    # For each, prepare relist plan
    results: List[Dict[str, Any]] = []
    num_buys = 0
    for opp in opps:
        result = {
            "ebay": asdict(opp.ebay),
            "amazon": asdict(opp.amazon),
            "profit": opp.estimated_profit,
            "margin": opp.profit_margin,
        }
        try:
            if num_buys < MAX_DAILY_PURCHASES:
                purchase_resp = purchase_ebay_item(opp.ebay)
                if purchase_resp.get("status") == "dry-run":
                    result["purchase_status"] = "dry-run"
                elif purchase_resp.get("status") == "not-implemented":
                    result["purchase_status"] = "not-implemented"
                else:
                    result["purchase_status"] = "purchased"
                num_buys += 1
            else:
                result["purchase_status"] = "skipped:quota"
        except Exception as ex:
            logger.error(f"Error during eBay purchase: {ex}")
            result["purchase_status"] = f"error:{ex}"
        try:
            relist_price = round(opp.amazon.price - 1.00, 2)
            relist_resp = relist_on_amazon(opp.ebay, relist_price)
            result["relist_status"] = relist_resp.get("status")
        except Exception as ex:
            logger.error(f"Error during Amazon relist: {ex}")
            result["relist_status"] = f"error:{ex}"
        results.append(result)
    out_path = export_opportunities(opps)
    print(json.dumps({"status": "ok", "opportunity_count": len(opps), "results": results, "export_path": out_path}, indent=2))

if __name__ == "__main__":
    main()
