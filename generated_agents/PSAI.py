import os
import sys
import json
import time
import re
import csv
import tempfile
import argparse
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, asdict
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from requests import Session, RequestException
from loguru import logger
from swarms import Agent
from ddgs import DDGS

def get_env_str(var: str, fallback: str = "") -> str:
    val = os.environ.get(var)
    if val is not None:
        return val.strip()
    return fallback

def get_env_int(var: str, fallback: int = 5) -> int:
    val = os.environ.get(var)
    try:
        return int(val)
    except (TypeError, ValueError):
        return fallback

@dataclass
class Listing:
    title: str
    price: float
    currency: str
    location: str
    url: str
    beds: Optional[int]
    baths: Optional[int]
    area: Optional[float]
    description: str
    images: List[str]
    listed_on: str
    raw: Dict[str, Any]

@dataclass
class InvestmentAnalysis:
    price: float
    estimated_value: float
    delta_percent: float
    location: str
    potential_roi: float
    summary: str
    listing_url: str

@dataclass
class Lead:
    title: str
    location: str
    price: float
    estimated_value: float
    delta_percent: float
    potential_roi: float
    url: str
    summary: str

def create_session(max_retries: int = 3, backoff: float = 0.3, timeout: int = 60) -> Session:
    session = Session()
    retry = Retry(
        total=max_retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_redirect=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.request = with_timeout(session.request, timeout)
    return session

def with_timeout(fn, timeout: int):
    def wrapped(*args, **kwargs):
        kwargs["timeout"] = timeout
        return fn(*args, **kwargs)
    return wrapped

# ---- Search & Scraping Helpers ----
def search_portals(query: str, max_results: int = 50) -> List[Dict[str, Any]]:
    logger.info(f"Searching DDGS for: '{query}', max_results={max_results}")
    ddgs = DDGS()
    try:
        results = [r for r in ddgs.text(query, max_results=max_results)]
        logger.info(f"Got {len(results)} results from DDGS")
        return results
    except Exception as e:
        logger.error(f"DDGS search failed: {e}")
        return []

def extract_listings_from_ddgs(ddgs_results: List[Dict[str, Any]]) -> List[Listing]:
    listings: List[Listing] = []
    for r in ddgs_results:
        url = r.get("href") or r.get("url") or ""
        title = r.get("title") or r.get("body") or ""
        snippet = r.get("body") or ""
        if not url or not title:
            continue
        price, currency = extract_price(title + " " + snippet)
        beds = extract_beds(title + " " + snippet)
        baths = extract_baths(title + " " + snippet)
        area = extract_area(title + " " + snippet)
        images = extract_images(snippet)
        location = extract_location(title + " " + snippet)
        listed_on = r.get("date") or ""
        l = Listing(
            title=title,
            price=price,
            currency=currency,
            location=location,
            url=url,
            beds=beds,
            baths=baths,
            area=area,
            description=snippet,
            images=images,
            listed_on=listed_on,
            raw=r,
        )
        if l.price > 0:
            listings.append(l)
    logger.info(f"Extracted {len(listings)} listings from DDGS results")
    return listings

def extract_price(text: str) -> Tuple[float, str]:
    # Looks for patterns like "$1,200,000", "USD 2,000,000", etc.
    price_pattern = re.compile(r"(USD|CAD|AED|GBP|EUR|\$|£|€)?\s*[\$£€]?([\d]+\.?\d*)", re.IGNORECASE)
    match = price_pattern.search(text.replace('\xa0', ' '))
    if match:
        currency = match.group(1) or "$"
        price_str = match.group(2).replace(",", "")
        try:
            price = float(price_str)
            return price, currency
        except Exception:
            return 0.0, ""
    return 0.0, ""

def extract_beds(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s?bed", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None

def extract_baths(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s?bath", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None

def extract_area(text: str) -> Optional[float]:
    m = re.search(r"([\d]+)\s?(sqft|square feet|sqm|sq m)", text, re.IGNORECASE)
    if m:
        try:
            area = float(m.group(1).replace(",", ""))
            return area
        except Exception:
            return None
    return None

def extract_images(text: str) -> List[str]:
    urls = re.findall(r"https?://\S+\.(?:jpg|jpeg|png|webp)", text, re.IGNORECASE)
    return list(set(urls))

def extract_location(text: str) -> str:
    # Tries to find a city or neighborhood in text (crude)
    matches = re.findall(r'(Dubai|Abu Dhabi|Sharjah|London|Manhattan|Sydney|Toronto|Vancouver|Paris|Berlin|Los Angeles|Miami|New York)', text, re.IGNORECASE)
    if matches:
        return matches[0]
    # fallback: empty
    return ""

# ---- Valuation & Analysis ----
def get_market_valuation(listing: Listing, session: Optional[Session]) -> Optional[float]:
    
    # Try Endeksa or similar public site for value estimate
    # Alternatively, use average price per sqft/sqm in location for crude estimate
    logger.info(f"Estimating value for '{listing.title}' at {listing.location}")
    avg_psqft = get_average_price_per_area(listing.location, session)
    if avg_psqft and listing.area:
        est_val = avg_psqft * listing.area
        logger.debug(f"Estimated value based on area: {est_val}")
        return est_val
    # fallback to price
    return None

def get_average_price_per_area(location: str, session: Optional[Session]) -> Optional[float]:
    # Example: search "average price per square foot in Dubai Marina" and parse result numerically
    query = f"average price per square foot in {location}"
    r = search_portals(query, max_results=5)
    prices: List[float] = []
    for result in r:
        price, _ = extract_price(result.get("body", "") + " " + result.get("title", ""))
        area_val = extract_area(result.get("body", "") + " " + result.get("title", ""))
        if price and area_val:
            try:
                psqft = price / area_val
                if 20 < psqft < 10000: # Ignore wild outliers
                    prices.append(psqft)
            except Exception:
                continue
    if prices:
        avg = sum(prices) / len(prices)
        logger.debug(f"Average price per sqft for {location}: {avg}")
        return avg
    return None

def analyze_listing(listing: Listing, session: Optional[Session]) -> Optional[InvestmentAnalysis]:
    est_val = get_market_valuation(listing, session)
    if est_val is None or est_val == 0.0:
        logger.debug(f"Skipping analysis for '{listing.title}' due to missing market value estimate.")
        return None
    price = listing.price
    delta_percent = 100.0 * (est_val - price) / est_val if est_val else 0.0
    # Simple heuristics: ROI = annual rent as % of price (simulate average for region/property type)
    avg_annual_rent = get_avg_rent(listing.location, listing.beds, session)
    if avg_annual_rent and price > 0:
        roi = 100.0 * avg_annual_rent / price
    else:
        roi = 0.0
    summary = (f"Price is {delta_percent:.1f}% below estimated value. Potential gross ROI: {roi:.1f}% based on region average.")
    return InvestmentAnalysis(
        price=price,
        estimated_value=est_val,
        delta_percent=delta_percent,
        location=listing.location,
        potential_roi=roi,
        summary=summary,
        listing_url=listing.url,
    )

def get_avg_rent(location: str, beds: Optional[int], session: Optional[Session]) -> Optional[float]:
    # Search "average annual rent for 2 bed apartment in {location}"
    desc = f"{beds} bed" if beds else "apartment"
    query = f"average annual rent for {desc} in {location}"
    r = search_portals(query, max_results=5)
    rents: List[float] = []
    for result in r:
        val, _ = extract_price(result.get("body", "") + " " + result.get("title", ""))
        if val > 1000 and val < 500000:
            rents.append(val)
    if rents:
        avg = sum(rents) / len(rents)
        logger.debug(f"Average rent for {desc} in {location}: {avg}")
        return avg
    return None

# ---- Lead Generation ----
def filter_underpriced_analyses(analyses: List[Tuple[Listing, InvestmentAnalysis]], threshold_pct: float = 10.0) -> List[Lead]:
    leads: List[Lead] = []
    for listing, analysis in analyses:
        if analysis.delta_percent >= threshold_pct and analysis.price > 0:
            leads.append(Lead(
                title=listing.title,
                location=analysis.location,
                price=analysis.price,
                estimated_value=analysis.estimated_value,
                delta_percent=analysis.delta_percent,
                potential_roi=analysis.potential_roi,
                url=analysis.listing_url,
                summary=analysis.summary
            ))
    logger.info(f"Filtered {len(leads)} underpriced leads (threshold={threshold_pct}%)")
    return leads

def export_leads_to_csv(leads: List[Lead], out_path: Optional[str] = None) -> str:
    fieldnames = ["title", "location", "price", "estimated_value", "delta_percent", "potential_roi", "url", "summary"]
    if not out_path:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w", encoding="utf-8", newline="")
        path = tmp.name
    else:
        path = out_path
    try:
        with open(path, "w", encoding="utf-8", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for lead in leads:
                d = asdict(lead)
                d["price"] = f"{d['price']:.2f}"
                d["estimated_value"] = f"{d['estimated_value']:.2f}"
                d["delta_percent"] = f"{d['delta_percent']:.1f}"
                d["potential_roi"] = f"{d['potential_roi']:.1f}"
                writer.writerow(d)
        logger.info(f"Exported leads to CSV: {path}")
        return path
    except Exception as e:
        logger.error(f"CSV export failed: {e}")
        raise

# ---- Main Agent Logic ----
def core_lead_generation(
    search_keywords: str,
    max_listings: int = 50,
    underprice_pct: float = 15.0,
    top_n: int = 20
) -> Dict[str, Any]:
    session = create_session()
    logger.info(f"Starting lead generation for keywords: '{search_keywords}'")
    ddgs_results = search_portals(search_keywords, max_results=max_listings)
    listings = extract_listings_from_ddgs(ddgs_results)
    analyses: List[Tuple[Listing, InvestmentAnalysis]] = []
    for listing in listings:
        try:
            analysis = analyze_listing(listing, session)
            if analysis is not None:
                analyses.append((listing, analysis))
        except Exception as e:
            logger.warning(f"Analysis failed for {listing.url}: {e}")
            continue
    leads = filter_underpriced_analyses(analyses, threshold_pct=underprice_pct)
    leads = sorted(leads, key=lambda l: l.delta_percent, reverse=True)
    leads = leads[:top_n] if top_n and len(leads) > top_n else leads
    csv_path = export_leads_to_csv(leads)
    return {
        "lead_count": len(leads),
        "csv_path": csv_path,
        "sample": [asdict(l) for l in leads[:3]],
    }

# ---- Swarms Agent Setup ----
AI_SYSTEM_PROMPT = (
    "You are PropScout AI – an autonomous agent that scans real estate listings from the public web, "
    "identifies underpriced investment properties using market heuristics, compiles concise investment-grade analyses (including estimated value and ROI), "
    "and outputs ready-to-sell lead lists (as CSV) for use by property investors and deal sourcers.\n"
    "Instructions:\n"
    "1. Use public search (DDGS) to pull active real estate listings in the user-specified location and property type.\n"
    "2. For each listing, estimate value using local price-per-area estimates or public property valuation where available.\n"
    "3. Flag as underpriced if the listing price is >10% below the estimated market value.\n"
    "4. For flagged listings, calculate gross rental ROI using region averages, and produce a short summary.\n"
    "5. Output a CSV with one row per lead: title, location, price, estimated_value, delta_percent, potential_roi, url, summary.\n"
    "6. Never include listings with unclear price or value. Only output leads you judge as solid investment opportunities.\n"
    "7. Never hallucinate data; base all analyses on observable numbers.\n"
    "8. Never include or process personal data.\n"
)

MODEL_NAME = get_env_str("SWARMS_MODEL", "gpt-4o-mini")
MAX_LOOPS = get_env_int("SWARMS_MAX_LOOPS", 5)
AGENT_NAME = "PropScout AI"
AGENT_DESCRIPTION = (
    "Autonomous agent for scanning, analyzing, and lead-list generation of underpriced real estate investment opportunities. "
    "Delivers investor-ready CSV lead lists for monetization by deal sourcers or property data entrepreneurs."
)

ps_agent = Agent(
    agent_name=AGENT_NAME,
    agent_description=AGENT_DESCRIPTION,
    system_prompt=AI_SYSTEM_PROMPT,
    model_name=MODEL_NAME,
    max_loops=MAX_LOOPS
)

# ---- Entrypoint ----
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PropScout AI: Underpriced Property Lead Agent")
    parser.add_argument("--keywords", type=str, default="Dubai investment property", help="Search keywords for real estate listings")
    parser.add_argument("--max_listings", type=int, default=50, help="Max number of listings to scan per run")
    parser.add_argument("--underprice_pct", type=float, default=15.0, help="Discount to consider as underpriced (%)")
    parser.add_argument("--top_n", type=int, default=20, help="Max leads to include in output list")
    parser.add_argument("--dest", type=str, default="", help="Destination CSV path (optional)")
    parser.add_argument("--investor_email", type=str, default="", help="Test: Send lead CSV to this email (optional; prints not sends)")
    return parser.parse_args()

# ---- Monetization Demo Step ----
def sell_lead_list_to_investor(csv_path: str, investor_email: Optional[str] = None) -> None:
    if not os.path.isfile(csv_path):
        logger.error(f"Lead file not found: {csv_path}")
        return
    if investor_email:
        logger.info(f"[DEMO] Would send lead list {csv_path} to investor email {investor_email}. ")
        print(f"Lead list ready to send: {csv_path} (simulate delivery to {investor_email})")
    else:
        logger.info(f"Investor email not supplied; not sending. CSV at {csv_path}")
        print(f"Lead list file ready for sale/distribution: {csv_path}")

if __name__ == "__main__":
    args = parse_args()
    logger.add(sys.stderr, level="INFO")
    try:
        output = core_lead_generation(
            search_keywords=args.keywords,
            max_listings=args.max_listings,
            underprice_pct=args.underprice_pct,
            top_n=args.top_n)
        print(json.dumps({
            "lead_count": output["lead_count"],
            "csv_path": output["csv_path"],
            "sample": output["sample"]
        }, indent=2))
        if args.investor_email:
            sell_lead_list_to_investor(output["csv_path"], investor_email=args.investor_email)
        else:
            sell_lead_list_to_investor(output["csv_path"], investor_email=None)
    except Exception as exc:
        logger.exception(f"PropScout AI run failed: {exc}")
        sys.exit(1)
