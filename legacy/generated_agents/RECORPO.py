import os
import sys
import time
import json
import random
import argparse
from typing import List, Dict, Optional, Tuple, Any, Set
from dataclasses import dataclass, field
from urllib.parse import urljoin
import threading
import tempfile

from loguru import logger
import requests
import httpx
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
from lxml import etree, html
from swarms import Agent

# Configuration and env setup
def get_env(key: str, default: Optional[str] = None) -> str:
    val = os.environ.get(key, default)
    if val is None:
        raise ValueError(f"Missing required environment variable: {key}")
    return val

DEFAULT_MODEL = os.environ.get("SWARMS_MODEL", "gpt-4o-mini")
DEFAULT_MAX_LOOPS = int(os.environ.get("SWARMS_LOOPS", "5"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "60"))
HTTP_RETRIES = int(os.environ.get("HTTP_RETRIES", "3"))
USER_AGENT = os.environ.get("SCRAPER_USER_AGENT", "ReCorporateBot/1.2 (+https://recorpo.example.com/bot)")

# ---- Data Models ----
@dataclass
class ProductInfo:
    sku: str
    name: str
    url: str
    price: float
    currency: str
    in_stock: bool
    competitor: str
    last_checked: float

@dataclass
class RepricingRecommendation:
    sku: str
    current_price: float
    competitor_prices: Dict[str, float]
    recommended_price: float
    rationale: str
    timestamp: float

@dataclass
class ScrapeTarget:
    competitor: str
    base_url: str
    product_paths: List[str]
    price_selector: str
    stock_selector: str
    name_selector: str
    currency: str

# ---- Helper Functions ----
def retry_request_requests(
    session: requests.Session,
    method: str,
    url: str,
    **kwargs
) -> requests.Response:
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            logger.debug(f"[{method}] Attempt {attempt} for {url}")
            headers = kwargs.get("headers", {})
            headers.setdefault("User-Agent", USER_AGENT)
            kwargs["headers"] = headers
            resp = session.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            logger.warning(f"HTTP error {e} on {url}, attempt {attempt}")
            if attempt == HTTP_RETRIES:
                logger.error(f"Failed after {HTTP_RETRIES} attempts: {url}")
                raise
            time.sleep(2 ** attempt)

async def retry_request_httpx(
    method: str,
    url: str,
    client: httpx.AsyncClient,
    **kwargs
) -> httpx.Response:
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            logger.debug(f"[async-{method}] Attempt {attempt} for {url}")
            headers = kwargs.get("headers", {})
            headers.setdefault("User-Agent", USER_AGENT)
            kwargs["headers"] = headers
            resp = await client.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.RequestError as e:
            logger.warning(f"HTTPX error {e} on {url}, attempt {attempt}")
            if attempt == HTTP_RETRIES:
                logger.error(f"Failed after {HTTP_RETRIES} attempts: {url}")
                raise
            await asyncio.sleep(2 ** attempt)

# ---- Scraping Logic ----
def fetch_and_parse_product(
    target: ScrapeTarget,
    product_path: str
) -> Optional[ProductInfo]:
    session = requests.Session()
    url = urljoin(target.base_url, product_path)
    try:
        resp = retry_request_requests(session, "GET", url)
        soup = BeautifulSoup(resp.text, "lxml")
        # Defensive parsing – selectors can change; log and skip if parsing fails
        try:
            price_raw = soup.select_one(target.price_selector)
            price = float(price_raw.text.replace("$", "").replace(",", "").strip())
        except Exception as e:
            logger.warning(f"Failed to parse price on {url}: {e}")
            return None
        try:
            name_raw = soup.select_one(target.name_selector)
            name = name_raw.text.strip()
        except Exception as e:
            logger.warning(f"Failed to parse name on {url}: {e}")
            name = "Unknown"
        try:
            stock_raw = soup.select_one(target.stock_selector)
            in_stock = not any(bad in stock_raw.text.lower() for bad in ["out of stock", "sold out", "unavailable"])
        except Exception as e:
            logger.warning(f"Failed to parse stock on {url}: {e}")
            in_stock = False
        sku = product_path.split("/")[-1].split("?")[0]
        return ProductInfo(
            sku=sku,
            name=name,
            url=url,
            price=price,
            currency=target.currency,
            in_stock=in_stock,
            competitor=target.competitor,
            last_checked=time.time()
        )
    except Exception as ex:
        logger.error(f"Critical error fetching/parsing {url}: {ex}")
        return None

# Multi-threaded scraping for speed
def scrape_competitor_products(
    targets: List[ScrapeTarget]
) -> List[ProductInfo]:
    results: List[ProductInfo] = []
    lock = threading.Lock()
    def worker(target: ScrapeTarget, product_path: str):
        prod = fetch_and_parse_product(target, product_path)
        if prod is not None:
            with lock:
                results.append(prod)
    threads = []
    for tgt in targets:
        for path in tgt.product_paths:
            t = threading.Thread(target=worker, args=(tgt, path), daemon=True)
            threads.append(t)
            t.start()
    for t in threads:
        t.join(timeout=HTTP_TIMEOUT + 8)
    return results

# ---- Data Processing/Analytics ----
def to_dataframe(products: List[ProductInfo]) -> pd.DataFrame:
    if not products:
        return pd.DataFrame([])
    df = pd.DataFrame([vars(p) for p in products])
    df['price'] = df['price'].astype(float)
    df['last_checked'] = pd.to_datetime(df['last_checked'], unit='s')
    return df

# Example repricing rule: price just below the lowest competitor in stock
# with floor/ceiling logic
def make_repricing_recommendations(
    retailer_prices: pd.DataFrame,
    competitor_prices: pd.DataFrame,
    price_floor: float = 1.0,
    price_ceiling: Optional[float] = None,
    markdown_pct: float = 0.01
) -> List[RepricingRecommendation]:
    recs: List[RepricingRecommendation] = []
    retailer_skus = retailer_prices['sku'].unique()
    for sku in retailer_skus:
        this_row = retailer_prices[retailer_prices['sku'] == sku].iloc[0]
        sk_comp = competitor_prices[(competitor_prices['sku'] == sku) & (competitor_prices['in_stock'])]
        if not sk_comp.empty:
            min_row = sk_comp.loc[sk_comp['price'].idxmin()]
            recommended = max(price_floor, min_row['price'] - markdown_pct * min_row['price'])
            if price_ceiling is not None:
                recommended = min(recommended, price_ceiling)
            rationale = f"Price set just below {min_row['competitor']} (in stock at {min_row['price']:.2f} {min_row['currency']})"
            competitor_dict = dict(zip(sk_comp['competitor'], sk_comp['price']))
        else:
            recommended = this_row['price']
            rationale = "No competitors in stock; hold price."
            competitor_dict = {}
        recs.append(RepricingRecommendation(
            sku=sku,
            current_price=this_row['price'],
            competitor_prices=competitor_dict,
            recommended_price=round(recommended, 2),
            rationale=rationale,
            timestamp=time.time(),
        ))
    return recs

# ---- API Serialization ----
def recommendations_to_json(recs: List[RepricingRecommendation]) -> str:
    payload = [
        {
            "sku": r.sku,
            "current_price": r.current_price,
            "competitor_prices": r.competitor_prices,
            "recommended_price": r.recommended_price,
            "rationale": r.rationale,
            "timestamp": r.timestamp
        } for r in recs
    ]
    return json.dumps(payload, ensure_ascii=False)

# ---- Main Agent System Prompt ----
RECORPORATE_SYSTEM_PROMPT = """
You are ReCorporate, an expert e-commerce data intelligence and repricing strategist. For every retailer product SKU and its associated competitor pricing/inventory data, you:
- Analyze all in-stock competitor prices and suggest an aggressive but sustainable price just below the lowest in-stock competitor (with optional floor/ceiling limits).
- Provide actionable repricing recommendations formatted as a JSON array, specifying for each SKU: (1) SKU, (2) retailer current price, (3) dict of in-stock competitor prices, (4) recommended new price, (5) concise rationale.
- Never hallucinate data; only act on provided inputs. If competitor data is missing, recommend holding price.
- Always output strict machine-readable JSON format as described, for API delivery.
- Never include non-machine-readable content or markdown; all output must be raw JSON.
"""

# ---- Agent Entrypoint and Orchestration ----
def run_recorporate_agent(
    retailer_catalog: List[Dict[str, Any]],
    competitor_targets: List[ScrapeTarget],
    price_floor: float = 1.0,
    price_ceiling: Optional[float] = None
) -> str:
    logger.info("[RECORPORATE] Starting competitor scraping...")
    competitor_products = scrape_competitor_products(competitor_targets)
    logger.info(f"Scraped {len(competitor_products)} competitor products.")
    retailer_df = pd.DataFrame(retailer_catalog)
    competitor_df = to_dataframe(competitor_products)
    if retailer_df.empty:
        raise ValueError("Retailer catalog empty, cannot generate recommendations.")
    recs = make_repricing_recommendations(
        retailer_prices=retailer_df,
        competitor_prices=competitor_df,
        price_floor=price_floor,
        price_ceiling=price_ceiling
    )
    return recommendations_to_json(recs)

# ---- Swarms Agent Setup ----
recorpo_agent = Agent(
    agent_name="ReCorporate",
    agent_description=(
        "AI-powered e-commerce competitor intelligence and API-driven repricing recommendations. Scrapes competitor pricing/stock in real-time, analyzes, and outputs actionable pricing changes for maximum profit."
    ),
    system_prompt=RECORPORATE_SYSTEM_PROMPT,
    model_name=DEFAULT_MODEL,
    max_loops=DEFAULT_MAX_LOOPS
)

def default_catalog() -> List[Dict[str, Any]]:
    # Example data with three products for the retailer
    return [
        {"sku": "12345", "name": "Blue Widget", "url": "https://myshop.com/prod/12345", "price": 19.99, "currency": "USD", "in_stock": True, "competitor": "self", "last_checked": time.time()},
        {"sku": "67890", "name": "Red Widget", "url": "https://myshop.com/prod/67890", "price": 29.99, "currency": "USD", "in_stock": True, "competitor": "self", "last_checked": time.time()},
        {"sku": "ABC12", "name": "Green Widget", "url": "https://myshop.com/prod/ABC12", "price": 24.99, "currency": "USD", "in_stock": True, "competitor": "self", "last_checked": time.time()}
    ]

def default_scrape_targets() -> List[ScrapeTarget]:
    # Example competitor web targets (selectors would need to be real and current)
    return [
        ScrapeTarget(
            competitor="AcmeStore",
            base_url="https://acmestore.example.com/",
            product_paths=["products/12345", "products/67890", "products/ABC12"],
            price_selector="span.price",
            stock_selector="div#stock-status",
            name_selector="h1.product-title",
            currency="USD"
        ),
        ScrapeTarget(
            competitor="ShopMax",
            base_url="https://shopmax.example.com/",
            product_paths=["item/12345", "item/67890", "item/ABC12"],
            price_selector="p.product-price",
            stock_selector="span.availability",
            name_selector="div.product-name > h2",
            currency="USD"
        ),
    ]


def api_payload_to_catalog(payload: str) -> List[Dict[str, Any]]:
    try:
        arr = json.loads(payload)
        assert isinstance(arr, list)
        for prod in arr:
            assert all(k in prod for k in ["sku", "name", "url", "price", "currency", "in_stock", "competitor", "last_checked"])
        return arr
    except Exception as ex:
        logger.error(f"Malformed catalog input: {ex}")
        raise ValueError("Catalog payload must be a list of product dicts.")

def api_payload_to_targets(payload: str) -> List[ScrapeTarget]:
    try:
        arr = json.loads(payload)
        assert isinstance(arr, list)
        result = []
        for target in arr:
            required = ["competitor", "base_url", "product_paths", "price_selector", "stock_selector", "name_selector", "currency"]
            if not all(k in target for k in required):
                raise ValueError(f"Missing keys in target {target}")
            result.append(ScrapeTarget(
                competitor=target["competitor"],
                base_url=target["base_url"],
                product_paths=target["product_paths"],
                price_selector=target["price_selector"],
                stock_selector=target["stock_selector"],
                name_selector=target["name_selector"],
                currency=target["currency"]
            ))
        return result
    except Exception as ex:
        logger.error(f"Malformed targets input: {ex}")
        raise ValueError("Targets payload must be a list of ScrapeTarget dicts.")

# ---- Entrypoint ----
def main():
    parser = argparse.ArgumentParser(description="Run ReCorporate competitor pricing agent.")
    parser.add_argument("--retailer_catalog", type=str, default=None, help="Retailer catalog as JSON array string (optional, else uses default)")
    parser.add_argument("--competitor_targets", type=str, default=None, help="Scrape targets as JSON array string (optional, else uses default)")
    parser.add_argument("--price_floor", type=float, default=1.0, help="Minimum price allowed (default=1.0)")
    parser.add_argument("--price_ceiling", type=float, default=None, help="Maximum price allowed (optional)")
    args = parser.parse_args()
    try:
        logger.info("[RECORPORATE] Initializing...")
        if args.retailer_catalog:
            catalog = api_payload_to_catalog(args.retailer_catalog)
        else:
            catalog = default_catalog()
        if args.competitor_targets:
            targets = api_payload_to_targets(args.competitor_targets)
        else:
            targets = default_scrape_targets()
        logger.info(f"Loaded {len(catalog)} retailer products and {len(targets)} competitor targets.")
        result_json = run_recorporate_agent(
            retailer_catalog=catalog,
            competitor_targets=targets,
            price_floor=args.price_floor,
            price_ceiling=args.price_ceiling
        )
        # LLM call for audit trail (optional, required per Swarms agent contract)
        llm_output = recorpo_agent.run(result_json)
        print(llm_output)
    except Exception as e:
        logger.error(f"ReCorporate agent failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
