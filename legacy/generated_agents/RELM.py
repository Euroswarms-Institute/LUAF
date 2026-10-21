import os
import sys
import argparse
import time
import random
import math
import json
import threading
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, asdict
import tempfile
import requests
from loguru import logger
from swarms import Agent

#
# ---------------------------
# Configuration & Environment
# ---------------------------

DEFAULT_MODEL = os.environ.get("RELM_MODEL", "gpt-4o-mini")
MAX_LOOPS = int(os.environ.get("RELM_MAX_LOOPS", "5"))
PROPERTY_MARKETS = os.environ.get("RELM_MARKETS", "us,uk").split(",")
MAX_LISTINGS_PER_MARKET = int(os.environ.get("RELM_MAX_LISTINGS", "20"))
DDGS_TIMEOUT = float(os.environ.get("RELM_DDGS_TIMEOUT", "90"))
API_TIMEOUT = float(os.environ.get("RELM_API_TIMEOUT", "80"))
MAX_API_RETRIES = int(os.environ.get("RELM_API_RETRIES", "3"))
RETRY_BACKOFF = float(os.environ.get("RELM_RETRY_BACKOFF", "3.0"))
SUBSCRIPTION_LEAD_LIMIT = int(os.environ.get("RELM_LEADS_PER_RUN", "6"))

# Real listing API endpoints: (demo uses public portals for search)
MARKET_SEARCH_URLS = {
    "us": [
        "https://www.realtor.com/realestateandhomes-search/", # Query appended
        "https://www.zillow.com/homes-for-sale/", # Query appended
    ],
    "uk": [
        "https://www.zoopla.co.uk/for-sale/property/", # Query appended
    ],
    "ca": [
        "https://www.realtor.ca/map#", # Query appended
    ],
    "uae": [
        "https://www.propertyfinder.ae/en/search?c=1&s=2&ob=mr", # Query params
    ],
}

# -----------------------------
# Data Models
# -----------------------------

@dataclass
class Listing:
    url: str
    price: float
    address: str
    beds: Optional[int]
    baths: Optional[float]
    sqft: Optional[float]
    description: Optional[str]
    market: str
    images: Optional[List[str]]
    timestamp: float
    source: str

@dataclass
class Comp:
    address: str
    price: float
    beds: Optional[int]
    baths: Optional[float]
    sqft: Optional[float]
    days_ago: Optional[int]
    source: str
    url: Optional[str]

@dataclass
class Lead:
    listing: Listing
    valuation: float
    comps: List[Comp]
    discount_percent: float
    market_trend: str
    reason: str

# -----------------------------
# Helper Functions
# -----------------------------


def http_get_with_retries(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: float = API_TIMEOUT, max_retries: int = MAX_API_RETRIES) -> Optional[requests.Response]:
    """HTTP GET with retries and exponential backoff, logs errors."""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            logger.warning(f"GET failed ({attempt + 1}/{max_retries}) for {url}: {e}")
            if attempt < max_retries - 1:
                backoff_time = RETRY_BACKOFF * (2 ** attempt)
                logger.info(f"Retrying in {backoff_time:.1f} seconds...")
                time.sleep(backoff_time)
            else:
                logger.error(f"GET failed permanently for {url}: {e}")
    return None

# Safe float parser
def parse_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(",", "").replace("$", "").replace("£", "").strip())
    except Exception:
        return None

# Safe int parser
def parse_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).replace(",", "").strip())
    except Exception:
        return None

# Validate listing structure
def validate_listing(dct: Dict[str, Any], market: str, source: str) -> Optional[Listing]:
    price = parse_float(dct.get("price"))
    address = dct.get("address")
    if not price or not address:
        return None
    beds = parse_int(dct.get("beds")) if dct.get("beds") is not None else None
    baths = parse_float(dct.get("baths")) if dct.get("baths") is not None else None
    sqft = parse_float(dct.get("sqft")) if dct.get("sqft") is not None else None
    desc = dct.get("description")
    images = dct.get("images")
    ts = time.time()
    return Listing(
        url=dct.get("url"), price=price, address=address, beds=beds, baths=baths,
        sqft=sqft, description=desc, market=market, images=images, timestamp=ts, source=source)

# Validate comp structure
def validate_comp(dct: Dict[str, Any], source: str) -> Optional[Comp]:
    price = parse_float(dct.get("price"))
    address = dct.get("address")
    if not price or not address:
        return None
    beds = parse_int(dct.get("beds")) if dct.get("beds") is not None else None
    baths = parse_float(dct.get("baths")) if dct.get("baths") is not None else None
    sqft = parse_float(dct.get("sqft")) if dct.get("sqft") is not None else None
    days_ago = parse_int(dct.get("days_ago")) if dct.get("days_ago") is not None else None
    url = dct.get("url")
    return Comp(
        address=address, price=price, beds=beds, baths=baths,
        sqft=sqft, days_ago=days_ago, source=source, url=url)

# Basic market identification from address
def infer_market_from_address(address: str) -> str:
    if address:
        addr_low = address.lower()
        if "london" in addr_low or ".uk" in addr_low:
            return "uk"
        if "toronto" in addr_low or ".ca" in addr_low:
            return "ca"
        if "dubai" in addr_low or "uae" in addr_low:
            return "uae"
        if "new york" in addr_low or "chicago" in addr_low or ".com" in addr_low:
            return "us"
    return "us"

# -----------------------------
# Listing & Comp Sourcing
# -----------------------------

# Lightweight web search for listings
from ddgs import DDGS

def find_recent_listings(market: str, max_results: int = MAX_LISTINGS_PER_MARKET) -> List[Listing]:
    logger.info(f"Searching for recent listings in {market}")
    ddgs = DDGS()
    query = f"site:{market} real estate houses for sale recent"
    results = []
    try:
        for entry in ddgs.text(query, max_results=max_results):
            # Try to parse price/address from snippet
            snippet = entry.get("body", "")
            price, beds, baths, sqft = None, None, None, None
            # Naive extraction heuristics
            lines = snippet.split("\n")
            for line in lines:
                l = line.lower()
                if ("$" in l or "£" in l) and any(x in l for x in ["price", "list", "asking"]):
                    price = parse_float(line)
                if "bed" in l and not beds:
                    beds = parse_int(line.split("bed")[0])
                if "bath" in l and not baths:
                    baths = parse_float(line.split("bath")[0])
                if "sqft" in l and not sqft:
                    sqft = parse_float(line)
            listing = validate_listing({
                "url": entry.get("href"),
                "price": price if price else None,
                "address": entry.get("title"),
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "description": snippet,
                "images": None,
            }, market, "ddgs")
            if listing:
                results.append(listing)
            if len(results) >= max_results:
                break
    except Exception as ex:
        logger.error(f"Failed listing fetch for {market}: {ex}")
    logger.info(f"Found {len(results)} listings for market {market}")
    return results


def find_comps(listing: Listing, max_results: int = 6) -> List[Comp]:
    """Find recent comparable sales from web search."""
    ddgs = DDGS()
    results = []
    try:
        city = listing.address.split(",")[-1].strip() if "," in listing.address else listing.address
        query = f"recent home sales {city}"
        logger.info(f"Searching for comps: {query}")
        for entry in ddgs.text(query, max_results=max_results):
            snippet = entry.get("body", "")
            price = None
            beds = None
            baths = None
            sqft = None
            lines = snippet.split("\n")
            for line in lines:
                l = line.lower()
                if ("$" in l or "£" in l) and ("sold" in l or "sale" in l):
                    price = parse_float(line)
                if "bed" in l and not beds:
                    beds = parse_int(line.split("bed")[0])
                if "bath" in l and not baths:
                    baths = parse_float(line.split("bath")[0])
                if "sqft" in l and not sqft:
                    sqft = parse_float(line)
            # Very rough time estimation
            days_ago = None
            for token in lines:
                if "days ago" in token.lower():
                    days_ago = parse_int(token.lower().split("days ago")[0].strip())
            comp = validate_comp({
                "address": entry.get("title"),
                "price": price,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "days_ago": days_ago,
                "url": entry.get("href"),
            }, "ddgs")
            if comp:
                results.append(comp)
            if len(results) >= max_results:
                break
    except Exception as ex:
        logger.error(f"Failed comp fetch for {listing.address}: {ex}")
    logger.info(f"Comps found for {listing.address}: {len(results)}")
    return results


def fetch_market_trend(listing: Listing) -> str:
    """Use web search to summarize the price trend for the market of a listing."""
    ddgs = DDGS()
    city = listing.address.split(",")[-1].strip() if "," in listing.address else listing.address
    query = f"real estate price trend {city}"
    trend_summary = "Unknown"
    try:
        for entry in ddgs.text(query, max_results=3):
            snippet = entry.get("body", "")
            if snippet:
                # Use the first snippet found
                trend_summary = snippet[:160]
                break
    except Exception as ex:
        logger.error(f"Failed to fetch trend for {city}: {ex}")
    return trend_summary

# -----------------------------
# Valuation & Undervalue Filter
# -----------------------------

def estimate_property_value(comps: List[Comp], listing: Listing) -> Optional[float]:
    """Estimating a fair value from comps for the given listing."""
    if not comps:
        return None
    prices = [c.price for c in comps if c.price]
    if not prices:
        return None
    # Simple price/sqft normalization if possible
    sqft_listing = listing.sqft
    if sqft_listing:
        price_per_sqft = [c.price/c.sqft for c in comps if c.price and c.sqft]
        if price_per_sqft:
            value = sum(price_per_sqft) / len(price_per_sqft) * sqft_listing
            return value
    # Else use median of comp prices
    prices.sort()
    n = len(prices)
    if n % 2 == 1:
        median = prices[n // 2]
    else:
        median = (prices[n // 2 - 1] + prices[n // 2]) / 2
    return median

def identify_undervalued(listings: List[Listing], discount_threshold: float = 0.09) -> List[Lead]:
    """For each listing, fetch comps, compute fair value, and select if undervalued."""
    leads = []
    for listing in listings:
        comps = find_comps(listing, max_results=8)
        if not comps:
            logger.info(f"No comps found for {listing.address}")
            continue
        fair_value = estimate_property_value(comps, listing)
        if not fair_value:
            logger.info(f"No fair value found for {listing.address}")
            continue
        if fair_value <= 0.01:
            continue
        discount = (fair_value - listing.price) / fair_value
        if discount > discount_threshold:
            trend = fetch_market_trend(listing)
            reason = (
                f"Listing price ${listing.price:,.0f} is {discount*100:.1f}% below estimated value ${fair_value:,.0f} "
                f"(based on {len(comps)} comps). Market trend: {trend[:80]}")
            leads.append(Lead(
                listing=listing,
                valuation=fair_value,
                comps=comps,
                discount_percent=discount*100,
                market_trend=trend,
                reason=reason
            ))
    leads.sort(key=lambda ld: ld.discount_percent, reverse=True)
    return leads

# -----------------------------
# LLM Agent System Prompt
# -----------------------------
SYSTEM_PROMPT = (
    "You are RealEstate LeadMiner, an expert AI analyst for property investors. Given a set of property listings, "
    "recent comparable sales data, and price trends for a market, your job is to:
"
    " 1. For each listing, review the address, price, size, and condition details.
"
    " 2. Compare to recent local comps and market trends, estimate a fair market value, and decide if the listing is undervalued.
"
    " 3. Output a concise actionable lead report with: listing address and price, estimated value, percent under value, reasoning, and relevant comps (address, price, sale recency).
"
    "Only include listings where the discount to market value is justified, referencing at least two nearby comps and local price trends. Format output as a JSON array of qualified investment leads with fields: address, price, estimated_value, discount_percent, comps (array), trend_summary, reasoning. Ensure all numbers are well formatted. Never guess information not in the data."
)

# -----------------------------
# Core Agent Logic
# -----------------------------

def run_lead_miner(task: Optional[str] = None) -> Dict[str, Any]:
    """Run the full lead miner discovery and reporting process."""
    start_time = time.time()
    output: Dict[str, Any] = {"leads": []}
    batch_listings = []
    for market in PROPERTY_MARKETS:
        listings = find_recent_listings(market, max_results=MAX_LISTINGS_PER_MARKET)
        batch_listings.extend(listings)
    logger.info(f"Found {len(batch_listings)} listings across {PROPERTY_MARKETS}")
    leads = identify_undervalued(batch_listings)
    if not leads:
        logger.warning("No undervalued leads found.")
        return {"leads": [], "result": "No undervalued properties found.", "took_seconds": time.time() - start_time}
    # Limit to subscription quota
    delivered = leads[:SUBSCRIPTION_LEAD_LIMIT]
    # Format for LLM report
    llm_input = [
        {
            "address": ld.listing.address,
            "price": ld.listing.price,
            "estimated_value": ld.valuation,
            "discount_percent": round(ld.discount_percent, 2),
            "comps": [
                {
                    "address": c.address,
                    "price": c.price,
                    "days_ago": c.days_ago
                } for c in ld.comps[:3]
            ],
            "trend_summary": ld.market_trend[:120],
            "reasoning": ld.reason
        } for ld in delivered
    ]
    # LLM agent to produce final lead sheet
    agent = Agent(
        agent_name="RealEstate LeadMiner",
        agent_description="Scans listings, evaluates comps/trends, and delivers qualified undervalued property investment leads.",
        system_prompt=SYSTEM_PROMPT,
        model_name=DEFAULT_MODEL,
        max_loops=MAX_LOOPS
    )
    report = agent.run({
        "leads": llm_input,
        "investor_notes": task or "Identify the top undervalued real estate investment leads in my target markets."
    })
    output["leads"] = llm_input
    output["llm_report"] = report
    output["took_seconds"] = time.time() - start_time
    return output

# -----------------------------
# Entrypoint
# -----------------------------

def main():
    parser = argparse.ArgumentParser(description="RealEstate LeadMiner agent: scan, value, and deliver property investment leads.")
    parser.add_argument("--task", type=str, default=None, help="Custom task prompt for the LLM report.")
    args = parser.parse_args()
    logger.info("Starting RealEstate LeadMiner...")
    result = run_lead_miner(args.task)
    print(json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
