import os
import json
import re
import time
import random
import sys
import argparse
import threading
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict, field
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from requests.sessions import Session
from loguru import logger
from swarms import Agent
from ddgs import DDGS
import requests
import tempfile

def get_env(key: str, default: Any = None) -> Any:
    v = os.environ.get(key)
    if v is None:
        return default
    return v

MODEL_NAME = get_env("SVAI_MODEL", "gpt-4o-mini")
MAX_LOOPS = int(get_env("SVAI_MAX_LOOPS", 5))
PROPERTY_PORTALS = [
    "https://www.realtor.ca/",
    "https://trulia.com/",
    "https://www.domain.com.au/",
    "https://www.propertyfinder.ae/",
    "https://www.zillow.com/",
]
SUPPORTED_COUNTRIES = [
    "US", "CA", "AE", "AU"
]
HTTP_TIMEOUT = 45
SCRAPE_RETRIES = 3
INVESTOR_EMAIL = get_env("SVAI_INVESTOR_EMAIL", None)

@dataclass
class PropertyListing:
    portal: str
    title: str
    url: str
    address: str
    price: float
    currency: str
    bedrooms: Optional[int]
    bathrooms: Optional[int]
    size_sqft: Optional[float]
    property_type: str
    description: str
    country: str
    images: List[str] = field(default_factory=list)

@dataclass
class AnalysisResult:
    property: PropertyListing
    market_value: float
    market_rent: Optional[float]
    price_delta_pct: float
    estimated_yield: Optional[float]
    risk_score: float
    commentary: str

@dataclass
class LeadList:
    properties: List[AnalysisResult]
    generated_at: float
    search_query: str
    target_country: str
    num_underpriced: int
    mean_risk_score: float
    mean_expected_yield: Optional[float]

class PropertyScraper:
    def __init__(self, session: Session, user_agent: str = None):
        self.session = session
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )

    def fetch_listings(self, portal_url: str, query: str, max_results: int = 25) -> List[PropertyListing]:
        logger.info(f"Fetching listings from {portal_url} for '{query}'")
        search_url = self._make_search_url(portal_url, query)
        try:
            resp = self.session.get(
                search_url, 
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": self.user_agent},
                allow_redirects=True
            )
            resp.raise_for_status()
            return self._parse_listings_html(portal_url, resp.text, max_results)
        except requests.RequestException as e:
            logger.error(f"Failed fetch from {portal_url}: {e}")
            return []

    def _make_search_url(self, portal_url: str, query: str) -> str:
        if "realtor.ca" in portal_url:
            return f"https://www.realtor.ca/real-estate/{query.replace(' ', '-') }"
        elif "trulia.com" in portal_url:
            return f"https://www.trulia.com/{query.replace(' ', '_')}/"
        elif "domain.com.au" in portal_url:
            return f"https://www.domain.com.au/sale/?suburb={query.replace(' ', '-') }"
        elif "propertyfinder.ae" in portal_url:
            return f"https://www.propertyfinder.ae/en/search?c=1&l={query.replace(' ', '-') }"
        elif "zillow.com" in portal_url:
            return f"https://www.zillow.com/homes/{query.replace(' ', '-') }_rb/"
        else:
            return portal_url

    def _parse_price(self, price_str: str) -> Tuple[Optional[float], str]:
        price_str = price_str.replace(",", "").replace("$", "").strip()
        match = re.search(r"([0-9]+\.?[0-9]*)", price_str)
        if not match:
            return None, ""
        price = float(match.group(1))
        currency = "USD" if "$" in price_str else "AED" if "د.إ" in price_str else "AUD" if "AU" in price_str else "CAD" if "C$" in price_str else "USD"
        return price, currency

    def _parse_listings_html(self, portal: str, html: str, max_results: int) -> List[PropertyListing]:
        # For simplicity, use regex patterns to extract key information
        # In production, one would use BeautifulSoup or specialized APIs/feeds
        results: List[PropertyListing] = []
        try:
            card_pattern = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
            price_pattern = re.compile(r"(\$|C\$|د\.إ|AU\$)?\s*([0-9]+)")
            # Very rough; can be extended for full parsing per portal.
            matches = card_pattern.findall(html)
            for i, (href, inner) in enumerate(matches):
                if i >= max_results:
                    break
                title = re.sub(r'<[^>]+>', '', inner).strip() or "Property"
                url = href if href.startswith("http") else f"{portal.rstrip('/')}/{href.lstrip('/')}"
                price_match = price_pattern.search(inner)
                price, currency = self._parse_price(price_match.group(0)) if price_match else (None, "USD")
                address = ""
                address_match = re.search(r'([0-9]+\s+[A-Za-z\s]+)', inner)
                if address_match:
                    address = address_match.group(1)
                prop_type = "Apartment" if "apt" in inner.lower() or "condo" in inner.lower() else "House"
                bedrooms_match = re.search(r'(\d+)\s*(bed|BR|Bedroom)', inner, re.IGNORECASE)
                bathrooms_match = re.search(r'(\d+)\s*(bath|BA|Bathroom)', inner, re.IGNORECASE)
                size_match = re.search(r'(\d{3,5})\s*(sqft|m2|sqm)', inner, re.IGNORECASE)
                bedrooms = int(bedrooms_match.group(1)) if bedrooms_match else None
                bathrooms = int(bathrooms_match.group(1)) if bathrooms_match else None
                size_sqft = float(size_match.group(1)) if size_match else None
                description = re.sub(r'<[^>]+>', '', inner)[:300]
                country = self._infer_country(portal)
                images = []
                results.append(PropertyListing(
                    portal=portal,
                    title=title,
                    url=url,
                    address=address,
                    price=price or 0.0,
                    currency=currency,
                    bedrooms=bedrooms,
                    bathrooms=bathrooms,
                    size_sqft=size_sqft,
                    property_type=prop_type,
                    description=description,
                    country=country
                ))
            return results
        except Exception as e:
            logger.error(f"Parsing failed for portal {portal}: {e}")
            return []

    def _infer_country(self, portal: str) -> str:
        if "ca" in portal:
            return "CA"
        elif "ae" in portal:
            return "AE"
        elif "au" in portal:
            return "AU"
        elif "zillow" in portal or "trulia" in portal:
            return "US"
        return "US"

def create_requests_session() -> Session:
    session = requests.Session()
    retries = Retry(
        total=SCRAPE_RETRIES,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def ddgs_search(query: str, num_results: int = 8) -> List[Dict[str, Any]]:
    logger.info(f"Running web search for '{query}' via ddgs")
    try:
        return [
            {
                "title": r["title"],
                "body": r["body"],
                "href": r["href"]
            } for r in DDGS().text(query, max_results=num_results)
        ]
    except Exception as e:
        logger.error(f"DDGS search failed: {e}")
        return []

def fetch_market_estimate(address: str, country: str, session: Session) -> Tuple[Optional[float], Optional[float]]:
    # Use web search and parse for price/rent estimate
    results = ddgs_search(f"market value and fair rent for {address} {country}", 3)
    value, rent = None, None
    for res in results:
        val_match = re.search(r'(\$|د\.إ|C\$|AU\$)?\s*([0-9]+)', res["body"])
        if val_match and value is None:
            v, _ = PropertyScraper(session)._parse_price(val_match.group(0))
            value = v
        rent_match = re.search(r'(\$|د\.إ|C\$|AU\$)?\s*([0-9]+)/mo', res["body"])
        if rent_match and rent is None:
            r, _ = PropertyScraper(session)._parse_price(rent_match.group(0))
            rent = r
    return (value, rent)

def investment_score(price: float, market_value: Optional[float], rent: Optional[float], size: Optional[float]) -> Tuple[float, Optional[float]]:
    # Lower risk score is better; yield is annualized gross yield if data available
    if not market_value or market_value == 0:
        return 0.0, None
    delta = (market_value - price) / market_value
    yield_ratio = None
    if rent and price > 0:
        yield_ratio = ((rent * 12) / price) * 100
    # Simple risk: properties bought >8% below market get 0.1, else scale up risk
    risk = max(0.1, 1 - min(delta, 1)) + (0.1 if not yield_ratio or yield_ratio < 4 else 0)
    return risk, yield_ratio

def analyze_listing(listing: PropertyListing, session: Session) -> AnalysisResult:
    # Try to estimate market value, rent; compute risk and commentary
    market_value, market_rent = fetch_market_estimate(listing.address or listing.title, listing.country, session)
    if not market_value:
        market_value = listing.price * 1.05  # Conservative estimate
    price_delta_pct = 100 * (market_value - listing.price) / market_value if market_value else 0
    risk, expected_yield = investment_score(listing.price, market_value, market_rent, listing.size_sqft)
    commentary = (
        f"Listed at {listing.currency} {listing.price:,.0f}, market value approx. {listing.currency} {market_value:,.0f}. "
        f"Delta: {price_delta_pct:.1f}%. "
        f"{f'Estimated annualized yield {expected_yield:.1f}%.' if expected_yield else ''} "
        f"Risk score: {risk:.2f} (1=high risk, 0=low risk)."
    )
    return AnalysisResult(
        property=listing,
        market_value=market_value,
        market_rent=market_rent,
        price_delta_pct=price_delta_pct,
        estimated_yield=expected_yield,
        risk_score=risk,
        commentary=commentary
    )

def generate_lead_list(
    analyses: List[AnalysisResult],
    search_query: str,
    target_country: str
) -> LeadList:
    underpriced = [a for a in analyses if a.price_delta_pct > 6 and a.risk_score <= 0.3]
    mean_risk = float(sum(a.risk_score for a in underpriced) / len(underpriced)) if underpriced else 0.0
    mean_yield = (
        sum(a.estimated_yield or 0 for a in underpriced) / len(underpriced)
        if underpriced and any(a.estimated_yield for a in underpriced) else None
    )
    return LeadList(
        properties=underpriced,
        generated_at=time.time(),
        search_query=search_query,
        target_country=target_country,
        num_underpriced=len(underpriced),
        mean_risk_score=mean_risk,
        mean_expected_yield=mean_yield
    )

def export_lead_list_to_json(lead_list: LeadList, path: str) -> None:
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        json.dump({
            **asdict(lead_list),
            "properties": [
                {
                    **asdict(a.property),
                    "market_value": a.market_value,
                    "market_rent": a.market_rent,
                    "price_delta_pct": a.price_delta_pct,
                    "estimated_yield": a.estimated_yield,
                    "risk_score": a.risk_score,
                    "commentary": a.commentary
                } for a in lead_list.properties
            ]
        }, f, indent=2, ensure_ascii=False)

def send_lead_list_to_investor(json_path: str, investor_email: Optional[str]) -> bool:
    # Placeholder for email or API integration to sell list -- here, log only
    if investor_email:
        logger.info(f"Lead list would be sent to {investor_email} (simulated)")
    else:
        logger.info("Investor email not configured. Skipping external send.")
    return True

def property_investment_agent(task: str) -> str:
    logger.info(f"Starting ScoutValuator investment lead generation task: '{task}'")
    query, country = parse_task_query(task)
    session = create_requests_session()
    scraper = PropertyScraper(session)
    all_analyses: List[AnalysisResult] = []
    # Gather from all portals supported in the target country
    portals = [p for p in PROPERTY_PORTALS if infer_portal_country(p) == country]
    if not portals:
        logger.warning(f"No supported portals for country {country}. Falling back to all portals.")
        portals = PROPERTY_PORTALS
    for portal in portals:
        listings = scraper.fetch_listings(portal, query, max_results=20)
        for listing in listings:
            try:
                analysis = analyze_listing(listing, session)
                all_analyses.append(analysis)
            except Exception as e:
                logger.error(f"Analysis failed for {listing.title} at {listing.url}: {e}")
    lead_list = generate_lead_list(all_analyses, query, country)
    if lead_list.num_underpriced == 0:
        logger.info("No underpriced properties found in analysis.")
    # Export to temp file for monetization
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json", encoding="utf-8", mode="w") as tf:
        export_lead_list_to_json(lead_list, tf.name)
        logger.info(f"Lead list exported to: {tf.name}")
        send_lead_list_to_investor(tf.name, INVESTOR_EMAIL)
        return f"Lead list generated at {time.ctime(lead_list.generated_at)} with {lead_list.num_underpriced} opportunities. File: {tf.name}"

def parse_task_query(task: str) -> Tuple[str, str]:
    task = task.strip().lower()
    # Detect country
    for c in SUPPORTED_COUNTRIES:
        if c.lower() in task:
            country = c
            break
    else:
        country = SUPPORTED_COUNTRIES[0]
    # Extract main query (remove country tokens)
    for c in SUPPORTED_COUNTRIES:
        task = task.replace(c.lower(), "")
    task = re.sub(r'[^a-z0-9\s]', ' ', task)
    task = re.sub(r'\s+', ' ', task)
    query = task.strip() or "apartment"
    return query, country

def infer_portal_country(portal_url: str) -> str:
    if "ca" in portal_url:
        return "CA"
    elif "ae" in portal_url:
        return "AE"
    elif "au" in portal_url:
        return "AU"
    elif "zillow" in portal_url or "trulia" in portal_url:
        return "US"
    return "US"

SYSTEM_PROMPT = (
    "You are ScoutValuator, an AI agent specializing in scanning global real estate portals for potentially underpriced properties, "
    "performing investment analysis (market value, rent, yield, risk score), and generating tailored, actionable lead lists for property investors. "
    "Given a free-form investor task (e.g., 'scan Toronto condos CA'), output the most promising underpriced properties using these steps: "
    "1) Search leading portals and web for listings fitting the query; 2) Estimate fair market value/rent for each; 3) Score for investment and risk; "
    "4) Output a JSON list of property leads, each with key attributes, market analysis, and risk. Only include properties with >6% gap to market value and low-medium risk. "
    "Never hallucinate data; if no opportunities, return an informative summary. All outputs must be factual, recent, and actionable for an investor seeking real ROI."
)

sv_agent = Agent(
    agent_name="ScoutValuator Property Investment AI",
    agent_description=(
        "Scans real estate portals, performs valuation/risk analysis, exports lists of underpriced investment properties for monetization and lead resale."
    ),
    system_prompt=SYSTEM_PROMPT,
    model_name=MODEL_NAME,
    max_loops=MAX_LOOPS
)

def main():
    parser = argparse.ArgumentParser(description="ScoutValuator: Scan & analyze underpriced property investment opportunities.")
    parser.add_argument("--task", type=str, default="condo Toronto CA", help="Task/query for property scan [default: 'condo Toronto CA']")
    parser.add_argument("--investor-email", type=str, default=INVESTOR_EMAIL, help="Investor email to send the lead list (simulated)")
    args = parser.parse_args()
    global INVESTOR_EMAIL
    if args.investor_email:
        INVESTOR_EMAIL = args.investor_email
    try:
        result = sv_agent.run(property_investment_agent, args.task)
        print(result)
    except Exception as e:
        logger.error(f"ScoutValuator agent failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
