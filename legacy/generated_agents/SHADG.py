import os
import sys
import argparse
import json
import re
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict
import requests
from loguru import logger
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float, Text, select, inspect
from sqlalchemy.orm import sessionmaker, declarative_base
from tinydb import TinyDB, Query
from swarms import Agent
import tempfile

# =========== CONFIGURATION & CONSTANTS =========== #
SHOPIFY_API_KEY = os.environ.get("SHOPIFY_API_KEY")
SHOPIFY_PASSWORD = os.environ.get("SHOPIFY_PASSWORD")
SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE")  # e.g. 'mystore.myshopify.com'
GEN_IMAGE_API_URL = os.environ.get("GEN_IMAGE_API_URL", "https://api.ideogram.ai/generate")
GEN_IMAGE_API_KEY = os.environ.get("GEN_IMAGE_API_KEY")
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4o-mini")
MAX_LOOPS = int(os.environ.get("MAX_LOOPS", 5))
CAMPAIGN_DB_URL = os.environ.get("CAMPAIGN_DB_URL", "sqlite:///shopiadgen_campaigns.db")
CAMPAIGN_DB_TINY = os.environ.get("CAMPAIGN_DB_TINY", "shopiadgen_campaigns_tinydb.json")
ANALYSIS_DAYS = int(os.environ.get("ANALYSIS_DAYS", 7))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", 60))
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2023-10")
RETRY_BACKOFF = [2, 5, 10]

# =========== SQLALCHEMY BASE =========== #
Base = declarative_base()

@dataclass
class CampaignRecord:
    id: Optional[int]
    created_at: datetime
    title: str
    description: str
    ad_copy: str
    image_path: str
    target_segment: str
    projected_roi: float
    channel: str
    store_name: str

class Campaign(Base):
    __tablename__ = 'campaigns'
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    title = Column(String(256), nullable=False)
    description = Column(Text, nullable=False)
    ad_copy = Column(Text, nullable=False)
    image_path = Column(String(512), nullable=False)
    target_segment = Column(String(128), nullable=False)
    projected_roi = Column(Float, nullable=False)
    channel = Column(String(64), nullable=False)
    store_name = Column(String(128), nullable=False)

# =========== DATABASE HELPERS =========== #

def setup_sqlalchemy_engine(db_url: str) -> Any:
    logger.info(f"Initializing SQLAlchemy engine at {db_url}")
    engine = create_engine(db_url, echo=False, future=True)
    Base.metadata.create_all(engine)
    return engine

def get_sqlalchemy_session(engine: Any) -> Any:
    Session = sessionmaker(bind=engine)
    return Session()

def save_campaign_record_sqlalchemy(record: CampaignRecord, session: Any) -> None:
    logger.info(f"Saving campaign record: {record.title}")
    campaign = Campaign(
        created_at=record.created_at,
        title=record.title,
        description=record.description,
        ad_copy=record.ad_copy,
        image_path=record.image_path,
        target_segment=record.target_segment,
        projected_roi=record.projected_roi,
        channel=record.channel,
        store_name=record.store_name
    )
    session.add(campaign)
    session.commit()

# =========== TINYDB HELPERS =========== #

def get_tinydb_instance(db_file: str) -> TinyDB:
    db = TinyDB(db_file)
    return db

def save_campaign_record_tinydb(record: CampaignRecord, db: TinyDB) -> None:
    logger.info(f"Saving campaign to TinyDB: {record.title}")
    db.insert(asdict(record))

def get_recent_campaigns_tinydb(db: TinyDB, store_name: str, days: int = 14) -> List[Dict]:
    Q = Query()
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    return db.search((Q.store_name == store_name) & (Q.created_at >= since))

# =========== SHOPIFY API HELPERS =========== #

def shopify_request(resource: str, params: Optional[Dict] = None, limit: int = 250, retry: int = 0) -> List[Dict]:
    if not (SHOPIFY_API_KEY and SHOPIFY_PASSWORD and SHOPIFY_STORE):
        logger.error("Missing Shopify API configuration.")
        raise RuntimeError("Shopify API keys and store must be set in environment.")
    url = f"https://{SHOPIFY_API_KEY}:{SHOPIFY_PASSWORD}@{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/{resource}.json"
    params = params or {}
    params['limit'] = limit
    try:
        logger.info(f"Fetching Shopify resource: {resource}")
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        key = resource.split("/")[0]
        result = data.get(key + 's') or data.get(key)
        if result is None:
            logger.warning(f"No data found for {resource}")
            return []
        return result
    except requests.RequestException as ex:
        if retry < len(RETRY_BACKOFF):
            wait = RETRY_BACKOFF[retry]
            logger.warning(f"Shopify API error ({ex}), retrying in {wait}s...")
            time.sleep(wait)
            return shopify_request(resource, params, limit, retry + 1)
        logger.error(f"Shopify API error after retries: {ex}")
        raise
    except ValueError:
        logger.error(f"Failed to decode JSON for {resource}")
        raise


def get_shopify_orders(days: int = 7) -> List[Dict]:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    orders = shopify_request("orders", {"created_at_min": since, "status": "any"}, limit=250)
    return orders

def get_shopify_customers() -> List[Dict]:
    customers = shopify_request("customers", {}, limit=250)
    return customers

def get_shopify_products() -> List[Dict]:
    products = shopify_request("products", {}, limit=250)
    return products

# =========== DATA AGGREGATION & ANALYSIS =========== #

def extract_subscription_orders(orders: List[Dict]) -> pd.DataFrame:
    logger.info("Extracting subscription orders from Shopify data...")
    records = []
    for order in orders:
        is_sub = False
        if order.get("tags") and re.search(r"subscription", order["tags"], re.I):
            is_sub = True
        elif any(re.search(r"subscription", (item.get("title", "") + str(item.get("variant_title", ""))), re.I) for item in order.get("line_items", [])):
            is_sub = True
        if is_sub:
            records.append({
                "customer_id": order.get("customer", {}).get("id"),
                "created_at": order.get("created_at"),
                "total_price": float(order.get("total_price", 0.0)),
                "currency": order.get("currency"),
                "tags": order.get("tags", ""),
            })
    if not records:
        logger.warning("No subscription orders found.")
        return pd.DataFrame()
    return pd.DataFrame(records)


def compute_key_metrics(sub_orders: pd.DataFrame, customers: List[Dict]) -> Dict[str, Any]:
    logger.info("Computing key metrics for campaign ideation...")
    metrics = {}
    if sub_orders.empty:
        return metrics
    metrics['subscriptions'] = len(sub_orders)
    metrics['sub_total'] = float(sub_orders['total_price'].sum())
    metrics['avg_sub_value'] = float(sub_orders['total_price'].mean())
    metrics['currency'] = sub_orders['currency'].iloc[0]
    # Example: Churn calculation
    last_14 = sub_orders[sub_orders['created_at'] >= (datetime.utcnow() - timedelta(days=14)).isoformat()]
    prev_14 = sub_orders[(sub_orders['created_at'] < (datetime.utcnow() - timedelta(days=14)).isoformat()) & (sub_orders['created_at'] >= (datetime.utcnow() - timedelta(days=28)).isoformat())]
    metrics['churn_rate'] = 1 - (len(last_14) / len(prev_14)) if len(prev_14) > 0 else 0.0
    c_df = pd.DataFrame(customers)
    metrics['total_customers'] = len(c_df) if not c_df.empty else 0
    return metrics


def suggest_campaign_segments(customers: List[Dict], orders: pd.DataFrame) -> List[str]:
    logger.info("Suggesting target segments based on customer/order features...")
    segments = set()
    if not orders.empty:
        # Example: New vs. Churned subscribers
        recent = orders[orders['created_at'] >= (datetime.utcnow() - timedelta(days=14)).isoformat()]
        old = orders[orders['created_at'] < (datetime.utcnow() - timedelta(days=14)).isoformat()]
        if len(recent) > 0:
            segments.add("New subscribers")
        if len(old) > 0:
            segments.add("Churned/inactive subscribers")
    segments.add("All subscribers")
    return list(segments)

# =========== IMAGE GENERATION HELPER =========== #
def generate_campaign_image(prompt: str) -> str:
    if not (GEN_IMAGE_API_URL and GEN_IMAGE_API_KEY):
        logger.warning("No campaign image API configured; skipping image generation.")
        return ""
    headers = {"Authorization": f"Bearer {GEN_IMAGE_API_KEY}", "Content-Type": "application/json"}
    data = {"prompt": prompt, "style": "ad", "width": 1024, "height": 1024}
    try:
        logger.info(f"Requesting campaign image for prompt: {prompt}")
        resp = requests.post(GEN_IMAGE_API_URL, headers=headers, json=data, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        image_url = resp.json().get("image_url")
        if not image_url:
            logger.error(f"Failed to fetch campaign image URL for prompt: {prompt}")
            return ""
        # Download image to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg", mode="wb") as f:
            iresp = requests.get(image_url, timeout=HTTP_TIMEOUT)
            iresp.raise_for_status()
            f.write(iresp.content)
            tmp_path = f.name
        logger.info(f"Campaign image saved at: {tmp_path}")
        return tmp_path
    except requests.RequestException as ex:
        logger.error(f"Image generation failed: {ex}")
        return ""

# =========== MARKETING COPY GENERATION =========== #
def generate_ad_copy(segment: str, key_metrics: Dict[str, Any], product_names: List[str]) -> str:
    stats = f"{key_metrics.get('subscriptions', 0)} active subscriptions, $ {key_metrics.get('avg_sub_value', 0):.2f}/subscriber avg value. "
    if product_names:
        stats += f"Featured: {', '.join(product_names[:3])}"
    headline = f"Unlock Exclusive Value for {segment}!"
    body = f"Join our thriving community of subscribers and never miss out. Enjoy premium products and peace of mind—risk-free. Try now and experience the difference."
    cta = "Subscribe today and save!"
    return f"{headline}\n\n{body}\n\n{stats}\n{cta}"

# =========== ROI PROJECTION =========== #
def project_campaign_roi(key_metrics: Dict[str, Any], segment: str) -> float:
    base = float(key_metrics.get("avg_sub_value", 20.0))
    mult = 1.2 if 'new' in segment.lower() else 1.05
    roi = base * mult * np.random.uniform(1.0, 1.2)
    logger.info(f"Projected ROI for segment {segment}: {roi}")
    return roi

# =========== SYSTEM PROMPT FOR SWARMS AGENT =========== #
SYSTEM_PROMPT = """You are ShopiAdGen, an AI agent for Shopify subscription e-commerce stores. Each week, you:
- Analyze store orders, customers, and product data to derive actionable subscriber metrics
- Identify the most valuable or promising customer segments (e.g., new signups, churned subscribers)
- For each segment, propose a unique, data-driven marketing campaign idea for the coming week
- For each campaign idea, generate:
   - A compelling campaign title and 2-3 sentence description
   - High-converting ad copy (headline, body, CTA)
   - An image prompt for ad creative, matching the product and segment
   - Projected ROI based on current metrics
   - Suggested marketing channel (email, Facebook, Instagram, etc.)
- Output a structured JSON containing campaign elements per segment
- No placeholder text: all proposals must be specific to the input metrics and segment
- Campaigns must be immediately usable by marketing and growth teams

Inputs: Store name, product names, key metrics (as JSON), segments (as JSON)
Outputs: Array of campaign JSONs: {title, description, ad_copy, image_prompt, target_segment, projected_roi, channel}
Constraints: No generic ideas. Proposals must reference store context, segment, and actual numbers where possible.\n"
"""

# =========== MAIN AGENT RUNNER =========== #
def main():
    parser = argparse.ArgumentParser(description="ShopiAdGen: AI Shopify campaign generator")
    parser.add_argument("--store_name", type=str, default=SHOPIFY_STORE or "demo-store.myshopify.com", help="Shopify store name")
    parser.add_argument("--analysis_days", type=int, default=ANALYSIS_DAYS, help="Days of data to analyze (default 7)")
    parser.add_argument("--num_campaigns", type=int, default=3, help="Number of campaigns to propose per run")
    parser.add_argument("--print_campaigns", action="store_true", help="Print generated campaigns to stdout")
    parser.add_argument("--list_recent", action="store_true", help="List recent campaigns in database")
    args = parser.parse_args()

    logger.info("===== ShopiAdGen Starting =====")
    # 1. Data Fetch
    try:
        orders = get_shopify_orders(args.analysis_days)
        customers = get_shopify_customers()
        products = get_shopify_products()
        product_names = [p.get("title", "") for p in products if p.get("title")] 
    except Exception as ex:
        logger.error(f"Failed to fetch Shopify data: {ex}")
        sys.exit(1)

    # 2. Data Analysis
    sub_orders_df = extract_subscription_orders(orders)
    key_metrics = compute_key_metrics(sub_orders_df, customers)
    segments = suggest_campaign_segments(customers, sub_orders_df)
    if not segments:
        logger.warning("No segments found for campaign; using default.")
        segments = ["All subscribers"]

    # 3. Campaign Proposal via LLM
    agent = Agent(
        agent_name="ShopiAdGen",
        agent_description="Generates Shopify subscription store marketing campaign ideas, ad copy, and creatives, based on live store data.",
        system_prompt=SYSTEM_PROMPT,
        model_name=MODEL_NAME,
        max_loops=MAX_LOOPS
    )
    input_payload = {
        "store_name": args.store_name,
        "product_names": product_names,
        "key_metrics": key_metrics,
        "segments": segments
    }
    try:
        logger.info("Querying swarms agent for weekly campaign proposals...")
        llm_result = agent.run(json.dumps(input_payload))
        campaigns = json.loads(llm_result)
        if not isinstance(campaigns, list):
            logger.error(f"Invalid campaign JSON: {campaigns}")
            campaigns = []
    except Exception as ex:
        logger.error(f"Failed to generate campaigns: {ex}")
        campaigns = []

    if not campaigns:
        logger.warning("No campaign ideas generated; falling back to rule-based proposals.")
        campaigns = []
        for seg in segments[:args.num_campaigns]:
            ad_copy = generate_ad_copy(seg, key_metrics, product_names)
            image_prompt = f"A modern, high-conversion ad creative for a {seg} Shopify subscription business, focusing on: {', '.join(product_names[:2])}."
            roi_proj = project_campaign_roi(key_metrics, seg)
            campaigns.append({
                "title": f"{seg} Flash Offer",
                "description": f"Drive {seg.lower()} with a limited time, value-packed subscription offer.",
                "ad_copy": ad_copy,
                "image_prompt": image_prompt,
                "target_segment": seg,
                "projected_roi": roi_proj,
                "channel": "Email + Paid Social"
            })

    # 4. Image Generation, Storage, and DB Save
    sql_engine = setup_sqlalchemy_engine(CAMPAIGN_DB_URL)
    sql_session = get_sqlalchemy_session(sql_engine)
    tinydb_db = get_tinydb_instance(CAMPAIGN_DB_TINY)
    now = datetime.utcnow()
    stored_records = []
    for camp in campaigns:
        try:
            img_path = generate_campaign_image(camp.get("image_prompt"))
            rec = CampaignRecord(
                id=None,
                created_at=now,
                title=camp.get("title", "Untitled Campaign"),
                description=camp.get("description", ""),
                ad_copy=camp.get("ad_copy", ""),
                image_path=img_path,
                target_segment=camp.get("target_segment", "All"),
                projected_roi=float(camp.get("projected_roi", 0.0)),
                channel=camp.get("channel", "Email"),
                store_name=args.store_name
            )
            save_campaign_record_sqlalchemy(rec, sql_session)
            save_campaign_record_tinydb(rec, tinydb_db)
            stored_records.append(rec)
        except Exception as ex:
            logger.error(f"Failed to store campaign: {ex}")

    if args.print_campaigns:
        for rec in stored_records:
            print(json.dumps(asdict(rec), indent=2, default=str))

    if args.list_recent:
        logger.info(f"Listing recent campaigns from TinyDB for {args.store_name}...")
        recent_camps = get_recent_campaigns_tinydb(tinydb_db, args.store_name)
        for camp in recent_camps:
            print(json.dumps(camp, indent=2, default=str))

    logger.info(f"{len(stored_records)} campaigns successfully generated and saved.")

if __name__ == "__main__":
    main()
