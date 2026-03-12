import os
import sys
import argparse
import time
import json
import random
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
import threading
import tempfile
from functools import partial

import requests
from loguru import logger
from swarms import Agent
from ddgs import DDGS

# === Config ===
ECOMMERCE_APIS = [
    # Sample product search APIs (replace or expand as needed for real sources)
    # Structures expected: /search?q=product_name, /products/{id}
    'https://api.producthunt.com/v1/posts',
    'https://api.bestbuy.com/v1/products',
    # Add more trusted endpoints as appropriate
]
SECONDARY_MARKETPLACE_APIS = [
    # Example of secondary market search (must provide product title/identifier)
    'https://api.ebay.com/buy/browse/v1/item_summary/search',
    # Add more resale platforms if available
]
PRODUCT_MONITOR_INTERVAL = int(os.environ.get('PRODUCT_MONITOR_INTERVAL', '1800'))  # in seconds
PROFIT_MARGIN_THRESHOLD = float(os.environ.get('PROFIT_MARGIN_THRESHOLD', '0.15'))  # e.g. 15%
MAX_RESULTS = int(os.environ.get('MAX_RESULTS', '10'))  # per search
MODEL_NAME = os.environ.get('MODEL_NAME', 'gpt-4o-mini')
MAX_LOOPS = int(os.environ.get('MAX_LOOPS', '5'))
RETRY_LIMIT = int(os.environ.get('RETRY_LIMIT', '3'))
RETRY_BACKOFF_BASE = float(os.environ.get('RETRY_BACKOFF_BASE', '2.0')) # seconds
REQUEST_TIMEOUT = int(os.environ.get('REQUEST_TIMEOUT', '30'))
API_KEYS = {
    'producthunt': os.environ.get('PRODUCTHUNT_API_KEY'),
    'bestbuy': os.environ.get('BESTBUY_API_KEY'),
    'ebay': os.environ.get('EBAY_API_KEY')
}

# === Data Structures ===
@dataclass
class Product:
    title: str
    price: float
    currency: str
    product_id: str
    url: str
    source: str
    image_url: Optional[str] = None

@dataclass
class ArbitrageOpportunity:
    product: Product
    expected_resell_price: float
    expected_profit: float
    resale_platform: str
    resell_url: str

@dataclass
class Config:
    monitor_interval: int = PRODUCT_MONITOR_INTERVAL
    profit_margin_threshold: float = PROFIT_MARGIN_THRESHOLD
    max_results: int = MAX_RESULTS
    model_name: str = MODEL_NAME
    max_loops: int = MAX_LOOPS
    retry_limit: int = RETRY_LIMIT
    request_timeout: int = REQUEST_TIMEOUT
    api_keys: Dict[str, Optional[str]] = field(default_factory=lambda: API_KEYS)

# === Helpers: Networking, Retrying, Parsing ===
def retry_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Any] = None,
    timeout: int = REQUEST_TIMEOUT,
    max_retries: int = RETRY_LIMIT,
    backoff_base: float = RETRY_BACKOFF_BASE
) -> Any:
    """
    HTTP(S) request with retries and exponential backoff.
    Returns parsed JSON or raises after max_retries.
    """
    for attempt in range(1, max_retries + 1):
        try:
            logger.debug(f"HTTP {method} {url} Attempt {attempt}")
            response = requests.request(method=method, url=url, headers=headers, params=params, data=data, timeout=timeout)
            response.raise_for_status()
            try:
                return response.json()
            except json.JSONDecodeError as e:
                logger.error(f"Non-JSON response from {url}: {e}")
                raise
        except requests.RequestException as e:
            logger.warning(f"HTTP request to {url} failed: {e}")
            if attempt == max_retries:
                logger.error(f"Max retries reached for {url}")
                raise
            sleep_sec = backoff_base ** attempt + random.uniform(0, 1)
            logger.info(f"Retrying in {sleep_sec:.2f} seconds...")
            time.sleep(sleep_sec)


def search_web_products_ddgs(query: str, max_results: int = 10) -> List[Product]:
    logger.info(f"Web search for discounted products: {query}")
    products = []
    try:
        ddgs_results = DDGS().text(query, max_results=max_results)
        for item in ddgs_results:
            try:
                price = extract_price_from_snippet(item.get('body', '') + ' ' + item.get('title', ''))
                if price is not None:
                    product = Product(
                        title=item.get('title', 'Untitled'),
                        price=price,
                        currency='USD',  # heuristic; real APIs should specify
                        product_id=item.get('href', ''),
                        url=item.get('href', ''),
                        source='ddgs',
                        image_url=None # ddgs does not provide
                    )
                    products.append(product)
            except Exception as e:
                logger.debug(f"Failed to parse DDGS result: {e}")
    except Exception as e:
        logger.error(f"Web search failed: {e}")
    return products

def extract_price_from_snippet(snippet: str) -> Optional[float]:
    import re
    prices = re.findall(r'\$([0-9]+(?:\.[0-9]{1,2})?)', snippet)
    if not prices:
        return None
    try:
        price = float(min(prices, key=lambda x: float(x)))
        return price
    except Exception:
        return None

def fetch_products_from_producthunt(keyword: str, api_key: Optional[str], max_results: int = 10) -> List[Product]:
    """
    - ProductHunt's public API for trending tech; limited product pricing, but illustrative.
    - Replace or expand with more eCommerce APIs as needed.
    """
    if not api_key:
        logger.warning("No ProductHunt API key set, skipping.")
        return []
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {api_key}'}
    params = {'search[query]': keyword}
    endpoint = 'https://api.producthunt.com/v2/api/graphql'  # Per docs
    payload = {
        'query': f'{{ posts (search: "{keyword}") {{ edges {{ node {{ id name url thumbnail {{ url }} reviewsCount }} }} }} }}'
    }
    products = []
    try:
        data = retry_request(
            method='POST',
            url=endpoint,
            headers=headers,
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT
        )
        posts = data.get('data', {}).get('posts', {}).get('edges', [])
        for post in posts[:max_results]:
            node = post.get('node', {})
            title = node.get('name', 'Untitled')
            url = node.get('url', '')
            thumbnail = node.get('thumbnail', {}).get('url', None)
            product = Product(
                title=title,
                price=0.0,  # ProductHunt usually lacks pricing; best effort
                currency='USD',
                product_id=node.get('id', ''),
                url=url,
                source='ProductHunt',
                image_url=thumbnail
            )
            products.append(product)
    except Exception as e:
        logger.error(f"ProductHunt API error: {e}")
    return products

def fetch_products_from_bestbuy(keyword: str, api_key: Optional[str], max_results: int = 10) -> List[Product]:
    if not api_key:
        logger.warning("No BestBuy API key set, skipping.")
        return []
    endpoint = 'https://api.bestbuy.com/v1/products'
    # BestBuy's API uses a proprietary filter syntax
    params = {
        'apiKey': api_key,
        'format': 'json',
        'show': 'name,salePrice,sku,url,image',
        'pageSize': max_results,
        'sort': 'salePrice.asc',
        '(search)': keyword
    }
    products = []
    try:
        data = retry_request(
            method='GET',
            url=endpoint,
            params=params,
            timeout=REQUEST_TIMEOUT
        )
        items = data.get('products', [])
        for item in items:
            try:
                title = item.get('name', 'Untitled')
                price = float(item.get('salePrice', 0))
                product = Product(
                    title=title,
                    price=price,
                    currency='USD',
                    product_id=str(item.get('sku', '')),
                    url=item.get('url', ''),
                    source='BestBuy',
                    image_url=item.get('image', None)
                )
                products.append(product)
            except Exception as e:
                logger.debug(f"BestBuy product parse error: {e}")
    except Exception as e:
        logger.error(f"BestBuy API error: {e}")
    return products

def fetch_products(keyword: str, config: Config) -> List[Product]:
    """
    Aggregate products from all configured APIs and web search, deduplicating by title+source.
    """
    all_products = []
    seen = set()
    if config.api_keys.get('producthunt'):
        ph = fetch_products_from_producthunt(keyword, config.api_keys['producthunt'], config.max_results)
        for p in ph:
            k = (p.title.lower(), p.source)
            if k not in seen:
                seen.add(k)
                all_products.append(p)
    if config.api_keys.get('bestbuy'):
        bb = fetch_products_from_bestbuy(keyword, config.api_keys['bestbuy'], config.max_results)
        for p in bb:
            k = (p.title.lower(), p.source)
            if k not in seen:
                seen.add(k)
                all_products.append(p)
    # Supplement with DDGS web search
    ddgs_prods = search_web_products_ddgs(f"{keyword} cheap sale discount", max_results=config.max_results)
    for p in ddgs_prods:
        k = (p.title.lower(), p.source)
        if k not in seen:
            seen.add(k)
            all_products.append(p)
    logger.info(f"Found {len(all_products)} unique products for keyword '{keyword}'")
    return all_products

def fetch_secondary_market_price(
    product: Product,
    api_key: Optional[str],
    timeout: int = REQUEST_TIMEOUT
) -> Optional[Tuple[float, str]]:
    """
    Fetch expected resale price from secondary marketplaces.
    Returns (price, url) of top resale listing, or None.
    """
    # We'll use eBay for illustration, searching by title
    if not api_key:
        logger.warning("No eBay API key set, skipping resale check.")
        return None
    endpoint = 'https://api.ebay.com/buy/browse/v1/item_summary/search'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Accept': 'application/json'
    }
    params = {
        'q': product.title,
        'limit': 5
    }
    try:
        data = retry_request(
            method='GET',
            url=endpoint,
            headers=headers,
            params=params,
            timeout=timeout
        )
        items = data.get('itemSummaries', [])
        if not items:
            return None
        # Find the median or best price and url
        prices = [float(offer.get('price', {}).get('value', product.price)) for offer in items if offer.get('price')]
        urls = [offer.get('itemWebUrl', '') for offer in items if offer.get('itemWebUrl')]
        if not prices or not urls:
            return None
        price = max(prices)
        url = urls[prices.index(price)]
        return price, url
    except Exception as e:
        logger.error(f"eBay API error: {e}")
        return None

def find_arbitrage_opportunities(
    products: List[Product],
    config: Config
) -> List[ArbitrageOpportunity]:
    """
    Finds arbitrage (resale) opportunities where potential profit > threshold.
    """
    opportunities = []
    for prod in products:
        resale = fetch_secondary_market_price(prod, config.api_keys.get('ebay'), config.request_timeout)
        if resale is None:
            continue
        resell_price, resell_url = resale
        if prod.price <= 0 or resell_price <= 0:
            continue
        profit = resell_price - prod.price
        margin = profit / prod.price
        logger.debug(f"Evaluating arbitrage: {prod.title} | Buy: ${prod.price:.2f} | Resell: ${resell_price:.2f} | Margin: {margin:.2%}")
        if margin >= config.profit_margin_threshold:
            opportunity = ArbitrageOpportunity(
                product=prod,
                expected_resell_price=resell_price,
                expected_profit=profit,
                resale_platform='eBay',
                resell_url=resell_url
            )
            opportunities.append(opportunity)
    logger.info(f"Found {len(opportunities)} arbitrage opportunities.")
    return opportunities

def safe_execute_purchase(
    opportunity: ArbitrageOpportunity,
    config: Config
) -> bool:
    """
    Simulates or executes a purchase on the source platform.
    In production, this would require automation or direct integration.
    """
    # For safety, print out the opportunity details and simulate the purchase.
    logger.warning(f"[SIM] Would purchase: {opportunity.product.title} @ {opportunity.product.url} for ${opportunity.product.price:.2f}")
    logger.warning(f"[SIM] Would list for resale on {opportunity.resale_platform}: {opportunity.resell_url} @ ${opportunity.expected_resell_price:.2f}")
    # To automate, integrate source site API with authentication and order logic.
    return True

def opportunity_report(
    opportunities: List[ArbitrageOpportunity]
) -> str:
    """
    Produces a report in plain text for found arbitrage opportunities.
    """
    if not opportunities:
        return "No arbitrage opportunities found."
    report_lines = [
        f"Total opportunities: {len(opportunities)}\n"
    ]
    for op in opportunities:
        l = (
            f"{op.product.title}\n"
            f"  Buy: {op.product.url} @ ${op.product.price:.2f} ({op.product.source})\n"
            f"  Resell: {op.resale_platform} {op.resell_url} @ ${op.expected_resell_price:.2f}\n"
            f"  Expected profit: ${op.expected_profit:.2f} | Margin: {op.expected_profit / op.product.price:.2%}\n"
        )
        report_lines.append(l)
    return "\n".join(report_lines)

# === SWARMS Agent Setup ===
AGENT_NAME = os.environ.get('AGENT_NAME', 'RepriceResell Agent')
AGENT_DESCRIPTION = (
    "Monitors eCommerce product listings for price drops, identifies arbitrage opportunities via price comparison with secondary marketplaces (eBay etc), and automatically executes profitable trades above a configurable profit margin. Aggregates multiple APIs, applies dynamic pricing logic, and produces actionable reports for resale."
)
SYSTEM_PROMPT = (
    "You are an autonomous eCommerce arbitrage agent."
    " You will:"
    " 1. Find discounted products on monitored platforms (BestBuy, ProductHunt, etc) relevant to KEYWORDS."
    " 2. For each, check the secondary market resale value (e.g. eBay) and calculate expected margin."
    f" 3. Only act on opportunities above the profit threshold of {PROFIT_MARGIN_THRESHOLD:.0%}."
    " 4. For each opportunity, purchase the item if possible and create a resale draft on the secondary platform."
    " 5. Generate detailed, actionable reports with links and margin calculations."
    "Never invent prices or products. Only act on verifiable data from trusted APIs."
    " Do not proceed with a purchase unless all risk checks (availability, margin, platform stability) pass."
    " Always log decisions and errors."
    "Report output must be clear and structured."
)
agent = Agent(
    agent_name=AGENT_NAME,
    agent_description=AGENT_DESCRIPTION,
    system_prompt=SYSTEM_PROMPT,
    model_name=MODEL_NAME,
    max_loops=MAX_LOOPS
)

# === Entrypoint ===
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="RepriceResell Agent: Monitor for eCommerce price drops and resell for profit."
    )
    parser.add_argument('--keywords', type=str, default="graphics card", help="Product keyword(s) to monitor. Default: 'graphics card'")
    parser.add_argument('--profit-margin', type=float, default=PROFIT_MARGIN_THRESHOLD, help="Minimum profit margin (0.15 for 15%)")
    parser.add_argument('--monitor-interval', type=int, default=PRODUCT_MONITOR_INTERVAL, help="How often to poll for new products (seconds)")
    parser.add_argument('--max-results', type=int, default=MAX_RESULTS, help="Max search results per source")
    parser.add_argument('--runs', type=int, default=1, help="Number of monitoring cycles (default 1 - set >1 for long running)")
    parser.add_argument('--dry-run', action='store_true', default=True, help="Simulate purchases (default: True)")
    args = parser.parse_args()

    config = Config(
        monitor_interval=args.monitor_interval,
        profit_margin_threshold=args.profit_margin,
        max_results=args.max_results,
        model_name=MODEL_NAME,
        max_loops=MAX_LOOPS,
        retry_limit=RETRY_LIMIT,
        request_timeout=REQUEST_TIMEOUT,
        api_keys=API_KEYS
    )
    total_found = 0
    for i in range(args.runs):
        logger.info(f"\n=== Monitoring cycle {i+1}/{args.runs} ===")
        products = fetch_products(args.keywords, config)
        if not products:
            logger.warning("No products found.")
            continue
        opportunities = find_arbitrage_opportunities(products, config)
        total_found += len(opportunities)
        report = opportunity_report(opportunities)
        print(report)
        for opportunity in opportunities:
            if not args.dry_run:
                safe_execute_purchase(opportunity, config)
            else:
                logger.info("Dry run: Skipping real purchase.")
        if i < args.runs - 1:
            logger.info(f"Waiting {config.monitor_interval} seconds for next cycle...")
            time.sleep(config.monitor_interval)
    if total_found == 0:
        logger.info("No arbitrage opportunities detected.")
    else:
        logger.success(f"Total {total_found} arbitrage opportunities detected in {args.runs} cycles.")

if __name__ == "__main__":
    main()
