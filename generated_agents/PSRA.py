import os
import sys
import argparse
import time
from typing import List, Dict, Optional, Tuple, Any
import json
import requests
from dataclasses import dataclass, field
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from loguru import logger
from swarms import Agent
import tempfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- CONFIGURATION ---
DEFAULT_MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4o-mini")
MAX_LOOPS = int(os.environ.get("MAX_LOOPS", "5"))
EBAY_APP_ID = os.environ.get("EBAY_APP_ID")  # eBay public API key
AMAZON_PAAPI_KEY = os.environ.get("AMAZON_PAAPI_KEY")  # Amazon PA/API key (if available)
AMAZON_PAAPI_SECRET = os.environ.get("AMAZON_PAAPI_SECRET")
AMAZON_PAAPI_ASSOC_TAG = os.environ.get("AMAZON_PAAPI_ASSOC_TAG")
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_TO = os.environ.get("EMAIL_TO")
SMTP_SERVER = os.environ.get("SMTP_SERVER")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
DAILY_MAX_RESULTS = int(os.environ.get("DAILY_MAX_RESULTS", "30"))
MIN_PROFIT_USD = float(os.environ.get("MIN_PROFIT_USD", "10.0"))
MIN_PROFIT_MARGIN = float(os.environ.get("MIN_PROFIT_MARGIN", "0.18"))  # 18% default

# --- DATA CLASSES ---
@dataclass
class ProductListing:
    title: str
    source: str  # 'ebay' or 'amazon'
    item_id: str
    url: str
    price: float
    currency: str
    shipping: float
    total_cost: float
    category: str
    image_url: Optional[str]
    sales_history: Optional[int]
    seller_rating: Optional[float]
    comparable_avg_price: Optional[float]
    profit_estimate: Optional[float]
    profit_margin: Optional[float]
    fees_estimation: Optional[float]
    recommendation_reason: Optional[str]
    fetched_at: str

@dataclass
class ArbitrageRecommendation:
    listing: ProductListing
    target_market: str  # e.g., 'Amazon', 'eBay', 'Other'
    projected_sale_price: float
    estimated_profit: float
    profit_margin: float
    resale_velocity: Optional[str]  # e.g., 'Fast', 'Medium', 'Slow'
    notes: Optional[str]=None

# --- HTTP SESSION SETUP (Retries + Timeouts) ---
def new_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=4, backoff_factor=3, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

# --- UTILITY HELPERS ---
def safe_float(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except (ValueError, TypeError):
        return None

def current_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def validate_listing(listing: ProductListing) -> bool:
    if not listing.title or not listing.url or listing.price is None or listing.total_cost is None:
        return False
    if listing.profit_estimate is None or listing.profit_estimate < MIN_PROFIT_USD:
        return False
    if listing.profit_margin is None or listing.profit_margin < MIN_PROFIT_MARGIN:
        return False
    if listing.category.lower() in ["gift card", "voucher"]:
        return False
    return True

# --- EBAY API HELPERS ---
def ebay_find_underpriced(keywords: str, category_id: Optional[str]=None, max_results: int=20) -> List[ProductListing]:
    if not EBAY_APP_ID:
        logger.error("EBAY_APP_ID missing in environment; cannot fetch eBay listings.")
        return []
    endpoint = "https://svcs.ebay.com/services/search/FindingService/v1"
    session = new_http_session()
    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": EBAY_APP_ID,
        "RESPONSE-DATA-FORMAT": "JSON",
        "keywords": keywords,
        "paginationInput.entriesPerPage": max_results,
        "outputSelector": "SellerInfo",
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true"
    }
    if category_id:
        params["categoryId"] = category_id
    try:
        logger.info(f"Querying eBay completed sales for {keywords}")
        resp = session.get(endpoint, params=params, timeout=45)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"eBay API error: {e}")
        return []
    try:
        content = resp.json()
        items = (
            content["findCompletedItemsResponse"][0]["searchResult"][0].get("item", [])
            if content.get("findCompletedItemsResponse") else []
        )
        results = []
        for item in items:
            selling_state = item.get("sellingStatus", [{}])[0].get("sellingState", [""])[0]
            if selling_state != "EndedWithSales":
                continue
            price_data = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
            price = safe_float(price_data.get("__value__"))
            shipping = safe_float(
                item.get("shippingInfo", [{}])[0].get("shippingServiceCost", [{}])[0].get("__value__")
            ) or 0.0
            results.append(ProductListing(
                title=item.get("title", [""])[0][:200],
                source="ebay",
                item_id=item.get("itemId", [""])[0],
                url=item.get("viewItemURL", [""])[0],
                price=price,
                currency=price_data.get("@currencyId", "USD"),
                shipping=shipping,
                total_cost=(price or 0) + (shipping or 0),
                category=item.get("primaryCategory", [{}])[0].get("categoryName", [""])[0],
                image_url=item.get("galleryURL", [None])[0],
                sales_history=1,
                seller_rating=safe_float(item.get("sellerInfo", [{}])[0].get("positiveFeedbackPercent", [None])[0]),
                comparable_avg_price=None,
                profit_estimate=None,
                profit_margin=None,
                fees_estimation=None,
                recommendation_reason=None,
                fetched_at=current_timestamp()
            ))
        logger.info(f"eBay completed results: {len(results)}")
        return results
    except Exception as e:
        logger.error(f"eBay result parse error: {e}")
        return []

def ebay_find_active_deals(keywords: str, category_id: Optional[str]=None, max_results: int=20) -> List[ProductListing]:
    if not EBAY_APP_ID:
        logger.error("EBAY_APP_ID missing in environment; cannot fetch eBay listings.")
        return []
    endpoint = "https://svcs.ebay.com/services/search/FindingService/v1"
    session = new_http_session()
    params = {
        "OPERATION-NAME": "findItemsAdvanced",
        "SERVICE-VERSION": "1.0.0",
        "SECURITY-APPNAME": EBAY_APP_ID,
        "RESPONSE-DATA-FORMAT": "JSON",
        "keywords": keywords,
        "paginationInput.entriesPerPage": max_results,
        "outputSelector": "SellerInfo",
        "itemFilter(0).name": "ListingType",
        "itemFilter(0).value": ["FixedPrice", "AuctionWithBIN"]
    }
    if category_id:
        params["categoryId"] = category_id
    try:
        logger.info(f"Querying eBay active deals for {keywords}")
        resp = session.get(endpoint, params=params, timeout=50)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"eBay API error: {e}")
        return []
    try:
        content = resp.json()
        items = (
            content["findItemsAdvancedResponse"][0]["searchResult"][0].get("item", [])
            if content.get("findItemsAdvancedResponse") else []
        )
        results = []
        for item in items:
            price_data = item.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0]
            price = safe_float(price_data.get("__value__"))
            shipping = safe_float(
                item.get("shippingInfo", [{}])[0].get("shippingServiceCost", [{}])[0].get("__value__")
            ) or 0.0
            results.append(ProductListing(
                title=item.get("title", [""])[0][:200],
                source="ebay",
                item_id=item.get("itemId", [""])[0],
                url=item.get("viewItemURL", [""])[0],
                price=price,
                currency=price_data.get("@currencyId", "USD"),
                shipping=shipping,
                total_cost=(price or 0) + (shipping or 0),
                category=item.get("primaryCategory", [{}])[0].get("categoryName", [""])[0],
                image_url=item.get("galleryURL", [None])[0],
                sales_history=None,
                seller_rating=safe_float(item.get("sellerInfo", [{}])[0].get("positiveFeedbackPercent", [None])[0]),
                comparable_avg_price=None,
                profit_estimate=None,
                profit_margin=None,
                fees_estimation=None,
                recommendation_reason=None,
                fetched_at=current_timestamp()
            ))
        logger.info(f"eBay active deals found: {len(results)}")
        return results
    except Exception as e:
        logger.error(f"eBay active results parse error: {e}")
        return []

# --- AMAZON (PAAPI5) HELPER (Optional: skips if not configured) ---
def amazon_search_offers(keywords: str, max_results: int=10) -> List[ProductListing]:
    # Amazon PAAPI is complex and requires access keys + associate tag.
    if not AMAZON_PAAPI_KEY or not AMAZON_PAAPI_SECRET or not AMAZON_PAAPI_ASSOC_TAG:
        logger.warning("Amazon PAAPI keys not configured; skipping Amazon search.")
        return []
    endpoint = "https://webservices.amazon.com/paapi5/searchitems"
    session = new_http_session()
    # Due to Amazon API's required signed requests, users must set up signature logic to use this function in production.
    # For now, skip implementation or use a third-party library if available.
    return []

# --- ARBITRAGE LOGIC ---
def estimate_selling_fees(source: str, price: float) -> float:
    # Estimate rough eBay/Amazon fee. Defaults: eBay 13%, Amazon 15%.
    if source == "amazon":
        return price * 0.15 + 2.49  # $2.49 shipped item
    return price * 0.13

def get_comparable_price(listing: ProductListing, completed_sales: List[ProductListing]) -> Optional[float]:
    # Use recent completed sales as comparable value
    rel = [li.total_cost for li in completed_sales if li.title and listing.title[:20].lower() in li.title.lower() and li.category == listing.category]
    if not rel:
        rel = [li.total_cost for li in completed_sales if li.category == listing.category]
    if not rel:
        return None
    avg = sum(rel) / len(rel)
    return round(avg, 2)

def enrich_listings_with_arbitrage(active_listings: List[ProductListing], completed_sales: List[ProductListing]) -> List[ProductListing]:
    """
    Add comparable sales, fees, and profit estimates to listings.
    """
    results = []
    for listing in active_listings:
        avg_sale = get_comparable_price(listing, completed_sales)
        if not avg_sale:
            continue
        fees = estimate_selling_fees(listing.source, avg_sale)
        expected_profit = avg_sale - listing.total_cost - fees
        profit_margin = expected_profit / listing.total_cost if listing.total_cost else 0
        rec_reason = f"Market avg ${avg_sale:.2f}, buy for ${listing.total_cost:.2f}."
        listing.comparable_avg_price = avg_sale
        listing.fees_estimation = fees
        listing.profit_estimate = expected_profit
        listing.profit_margin = profit_margin
        listing.recommendation_reason = rec_reason
        if validate_listing(listing):
            results.append(listing)
    # Sort by profit descending
    return sorted(results, key=lambda l: l.profit_estimate or 0, reverse=True)

# --- DAILY REPORT GENERATION ---
def generate_recommendations(keywords: str, max_results: int=DAILY_MAX_RESULTS) -> List[ProductListing]:
    logger.info(f"Generating arbitrage recommendations for: {keywords}")
    completed_sales = ebay_find_underpriced(keywords, max_results=24)
    time.sleep(1)
    actives = ebay_find_active_deals(keywords, max_results=max_results)
    if not completed_sales or not actives:
        logger.error("Insufficient data from eBay for recommendations.")
        return []
    enriched = enrich_listings_with_arbitrage(actives, completed_sales)
    final = enriched[:max_results]
    logger.info(f"Final recommendations: {len(final)}")
    return final

def recommendations_as_text(listings: List[ProductListing]) -> str:
    lines = [
        f"{li.title}\n  Link: {li.url}\n  Price: ${li.total_cost:.2f} ({li.currency})\n  Market avg: ${li.comparable_avg_price:.2f}\n  Est. profit: ${li.profit_estimate:.2f} | Margin: {li.profit_margin:.1%}\n  Seller rating: {li.seller_rating if li.seller_rating is not None else 'N/A'}\n  Reason: {li.recommendation_reason}\n"
        for li in listings
    ]
    return "\n----------------------------\n".join(lines)

def write_report_to_file(listings: List[ProductListing]) -> str:
    fd, path = tempfile.mkstemp(suffix="_arbitrage_report.txt")
    os.close(fd)
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(f"PriceScout Arbitrage Report -- {current_timestamp()}\n\n")
        f.write(recommendations_as_text(listings))
    logger.info(f"Report written to: {path}")
    return path

# --- EMAIL REPORTING ---
def send_email_report(subject: str, text_body: str, attachment_path: Optional[str] = None) -> None:
    if not (EMAIL_FROM and EMAIL_TO and SMTP_SERVER and SMTP_USER and SMTP_PASS):
        logger.warning("Email credentials not properly set, skipping email send.")
        return
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            att = MIMEText(f.read(), _subtype="plain", _charset="utf-8")
            att.add_header("Content-Disposition", f"attachment; filename={os.path.basename(attachment_path)}")
            msg.attach(att)
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=60) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        logger.info(f"Email sent to {EMAIL_TO}")
    except Exception as e:
        logger.error(f"Error sending email report: {e}")

# --- SYSTEM PROMPT FOR LLM (swarms agent) ---
SYSTEM_PROMPT = (
    """
    You are PriceScout Resale Agent, an autonomous e-commerce arbitrage analyst for professional resellers. For a given product search term, you:
    - Query multiple e-commerce marketplaces (eBay, Amazon), analyzing both active listings and recent completed sales using their APIs
    - Identify underpriced listings by comparing current offers with verified recent sales (true market value)
    - Calculate projected net profit and profit margin for each candidate listing, accounting for shipping, platform fees, and typical transaction costs
    - Filter out listings with low demand (few/no sales history), poor seller feedback, or low profit margin
    - Present the highest-potential opportunities as a report: each row includes title, link, price paid, expected resale value, est. net profit, margin %, seller reputation, and a short reason for inclusion
    - Only recommend listings with minimum $10 profit and 18% margin. Prioritize high-sale-velocity, well-rated sellers, and tangible, shippable goods.
    - Output a concise, tabular summary and detailed list. If no qualifying deals are found, state so clearly.
    - You must be precise, explain any assumptions, and output clear instructions for follow-up actions.
    """
)

# --- SWARMS AGENT SETUP ---
agent = Agent(
    agent_name="PriceScout Resale Agent",
    agent_description=(
        "Autonomously analyzes e-commerce marketplaces to find underpriced goods for resale, estimating net profit, filtering by demand, and producing actionable daily buying recommendations."
    ),
    system_prompt=SYSTEM_PROMPT,
    model_name=DEFAULT_MODEL_NAME,
    max_loops=MAX_LOOPS
)

def run_daily_scouting(task: Optional[str]=None, keywords: Optional[str]=None) -> str:
    if not keywords and not task:
        keywords = os.environ.get("DAILY_KEYWORDS", "used electronics, sneakers, collectibles, vintage watch, iphone")
    if not keywords:
        keywords = "iphone"
    all_recommendations = []
    for kw in [k.strip() for k in keywords.split(",") if k.strip()]:
        logger.info(f"Processing search: {kw}")
        recs = generate_recommendations(kw, max_results=DAILY_MAX_RESULTS)
        all_recommendations.extend(recs)
        time.sleep(2)
    if not all_recommendations:
        report_str = f"No profitable underpriced listings identified for keywords: {keywords} at {current_timestamp()}"
    else:
        report_str = recommendations_as_text(all_recommendations)
    report_path = write_report_to_file(all_recommendations)
    send_email_report(
        subject=f"PriceScout Arbitrage Report {current_timestamp()}",
        text_body=report_str,
        attachment_path=report_path,
    )
    # Compose summary for LLM postprocessing (optionally included)
    prompt_task = f"Summarize and prioritize {len(all_recommendations)} arbitrage listings for: {keywords}. Extract top daily opportunities suitable for fast resale."
    final_output = agent.run(prompt_task + "\n" + report_str)
    print(final_output)
    return final_output

# --- ENTRYPOINT: CLI SETUP ---
def main() -> None:
    parser = argparse.ArgumentParser(description="PriceScout: AI-driven e-commerce resale arbitrage agent.")
    parser.add_argument("--keywords", type=str, default=None, help="Comma-separated product search terms (e.g. 'iPhone, sneaker, vintage watch')")
    parser.add_argument("--max_results", type=int, default=DAILY_MAX_RESULTS, help="Maximum recommendations per search term.")
    args = parser.parse_args()
    try:
        run_daily_scouting(keywords=args.keywords)
    except Exception as e:
        logger.error(f"Error running PriceScout: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
