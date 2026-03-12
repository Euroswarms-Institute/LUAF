import os
import sys
import json
import time
import random
import argparse
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import requests
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float, Text, MetaData, Table
from sqlalchemy.orm import sessionmaker
from tinydb import TinyDB, Query
from loguru import logger
from swarms import Agent
import tempfile

def get_env_var(key: str, default: Optional[str] = None) -> str:
    value = os.environ.get(key, default)
    if value is None:
        raise EnvironmentError(f"Missing required environment variable: {key}")
    return value

@dataclass
class CampaignIdea:
    title: str
    description: str
    ad_copy: str
    image_path: str
    created_at: str

@dataclass
class ProductInfo:
    id: int
    title: str
    vendor: str
    price: float
    total_sales: int
    subscription: bool

def get_shopify_api_credentials() -> Tuple[str, str, str]:
    api_key = get_env_var('SHOPIFY_API_KEY')
    password = get_env_var('SHOPIFY_PASSWORD')
    shop_name = get_env_var('SHOPIFY_SHOP_NAME')
    return api_key, password, shop_name

def get_openai_api_key() -> str:
    return get_env_var("OPENAI_API_KEY")

# --- Data Fetching and Storage Helpers ---
def create_sqlite_engine(db_path: str = 'shopigen_campaigns.db'):
    return create_engine(f'sqlite:///{db_path}')

def init_campaign_sql_table(engine) -> Table:
    metadata = MetaData(bind=engine)
    campaigns = Table(
        'campaigns', metadata,
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('title', String(256)),
        Column('description', Text),
        Column('ad_copy', Text),
        Column('image_path', String(512)),
        Column('created_at', DateTime)
    )
    metadata.create_all(checkfirst=True)
    return campaigns

def store_campaign_idea_sql(engine, campaign_idea: CampaignIdea) -> None:
    conn = engine.connect()
    campaigns = init_campaign_sql_table(engine)
    try:
        stmt = campaigns.insert().values(
            title=campaign_idea.title,
            description=campaign_idea.description,
            ad_copy=campaign_idea.ad_copy,
            image_path=campaign_idea.image_path,
            created_at=datetime.fromisoformat(campaign_idea.created_at)
        )
        conn.execute(stmt)
        logger.info(f"Stored campaign '{campaign_idea.title}' in SQLite (campaigns table)")
    except Exception as e:
        logger.error(f"Error storing campaign in SQL: {e}")
        raise
    finally:
        conn.close()

def store_campaign_idea_tinydb(db: TinyDB, campaign_idea: CampaignIdea) -> None:
    try:
        db.insert(asdict(campaign_idea))
        logger.info(f"Stored campaign '{campaign_idea.title}' in TinyDB")
    except Exception as e:
        logger.error(f"Error storing campaign in TinyDB: {e}")
        raise

def fetch_shopify_store_data(api_key: str, password: str, shop_name: str) -> Dict[str, Any]:
    session = requests.Session()
    session.auth = (api_key, password)
    base_url = f"https://{shop_name}.myshopify.com/admin/api/2022-10/"
    endpoints = {
        "products": "products.json",
        "orders": "orders.json?status=any&limit=250",
        "customers": "customers.json?limit=250"
    }
    data = {}
    for k, endpoint in endpoints.items():
        for attempt in range(3):
            try:
                logger.info(f"Fetching Shopify {k} (attempt {attempt+1})...")
                resp = session.get(base_url + endpoint, timeout=60)
                resp.raise_for_status()
                data[k] = resp.json()
                break
            except requests.RequestException as e:
                logger.warning(f"Fetch attempt {attempt+1} failed: {e}")
                time.sleep(2**attempt)
                if attempt == 2:
                    logger.error(f"Failed to fetch {k} after 3 attempts.")
                    raise
    logger.info("Fetched Shopify store data successfully.")
    return data

# --- Data Analysis Helpers ---
def extract_subscription_products(products_json: Dict[str, Any]) -> List[ProductInfo]:
    products = products_json.get('products', [])
    filtered = []
    for prod in products:
        tags = prod.get('tags', '')
        if 'subscription' in tags.lower() or any(t in tags.lower() for t in ['subscribe', 'recurring']):
            filtered.append(ProductInfo(
                id=prod.get('id'),
                title=prod.get('title'),
                vendor=prod.get('vendor'),
                price=float(prod.get('variants', [{}])[0].get('price', 0.0)),
                total_sales=0,
                subscription=True
            ))
    return filtered

def analyze_top_products(df_orders: pd.DataFrame, products: List[ProductInfo]) -> List[Tuple[ProductInfo, int]]:
    if df_orders.empty:
        logger.warning("No orders data to analyze.")
        return []
    product_sales = {}
    for _, row in df_orders.iterrows():
        try:
            for line in row['line_items']:
                pid = line.get('product_id')
                quantity = int(line.get('quantity', 0))
                if pid in product_sales:
                    product_sales[pid] += quantity
                else:
                    product_sales[pid] = quantity
        except Exception as e:
            logger.warning(f"Failed to process line item: {e}")
    ranked = []
    for prod in products:
        sales = product_sales.get(prod.id, 0)
        ranked.append((prod, sales))
    ranked.sort(key=lambda x: x[1], reverse=True)
    logger.info(f"Top subscription products: {[p.title for p,_ in ranked[:3]]}")
    return ranked

def weekly_growth_metrics(df_orders: pd.DataFrame) -> Dict[str, float]:
    df_orders['created_at'] = pd.to_datetime(df_orders['created_at'], errors='coerce')
    last_week = datetime.utcnow() - timedelta(days=7)
    weekly_orders = df_orders[df_orders['created_at'] > last_week]
    gross = weekly_orders['total_price'].astype(float).sum()
    n_orders = weekly_orders.shape[0]
    average = (gross / n_orders) if n_orders else 0.0
    return {'weekly_gross': gross, 'num_orders': n_orders, 'average_order': average}

def segment_customers(df_customers: pd.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
    df_customers['created_at'] = pd.to_datetime(df_customers['created_at'], errors='coerce')
    last_month = datetime.utcnow() - timedelta(days=30)
    new_customers = df_customers[df_customers['created_at'] > last_month]
    repeat_customers = df_customers[df_customers['orders_count'].astype(int) > 1]
    return {
        'new_customers': new_customers.to_dict('records'),
        'repeat_customers': repeat_customers.to_dict('records')
    }

# --- LLM-powered Generation Helpers ---
def generate_ad_copy(product: ProductInfo, audience: str, metrics: Dict[str, float], api_key: str) -> str:
    prompt = f"""
    Write a persuasive, high-converting Facebook ad copy for the following subscription product:
    Product: {product.title}
    Vendor: {product.vendor}
    Price: ${product.price:.2f}
    Audience segment: {audience}
    Weekly average order: ${metrics['average_order']:.2f}
    Current trends: Weekly gross ${metrics['weekly_gross']:.2f}, {metrics['num_orders']} orders last week.
    The copy must be less than 60 words, have a clear CTA, and emphasize recurring value.
    """
    for attempt in range(3):
        try:
            logger.info(f"Generating ad copy for product {product.title} for audience {audience}")
            response = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'gpt-4o',
                    'messages': [{'role': 'system', 'content': prompt}],
                    'max_tokens': 120,
                    'temperature': 0.8
                },
                timeout=60
            )
            response.raise_for_status()
            choices = response.json()['choices']
            if choices and choices[0]['message']['content']:
                return choices[0]['message']['content'].strip()
        except requests.RequestException as e:
            logger.warning(f"Attempt {attempt+1} to generate ad copy failed: {e}")
            time.sleep(2**attempt)
    logger.error(f"Failed to generate ad copy for {product.title} after 3 tries.")
    return "Upgrade your routine with our subscription! Join today for exclusive value."

def generate_ad_image(product: ProductInfo, api_key: str) -> str:
    prompt = (
        f"Studio photo of '{product.title}' with attractive background, vibrant color palette, clear subscription/recurring theme, high detail, e-commerce promotion, 1024x1024"
    )
    for attempt in range(3):
        try:
            logger.info(f"Generating ad image for {product.title}")
            response = requests.post(
                'https://api.openai.com/v1/images/generations',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={"prompt": prompt, "n": 1, "size": "1024x1024"},
                timeout=120
            )
            response.raise_for_status()
            url = response.json()['data'][0]['url']
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tf:
                img_resp = requests.get(url, timeout=60)
                img_resp.raise_for_status()
                tf.write(img_resp.content)
                image_path = tf.name
            logger.info(f"Saved ad image to {image_path}")
            return image_path
        except requests.RequestException as e:
            logger.warning(f"Attempt {attempt+1} to generate ad image failed: {e}")
            time.sleep(2**attempt)
    logger.error(f"Failed to generate ad image for {product.title} after 3 tries.")
    return ""

# --- Main Agent Logic ---
def generate_campaign_ideas(api_key: str, password: str, shop_name: str, openai_key: str) -> List[CampaignIdea]:
    store_data = fetch_shopify_store_data(api_key, password, shop_name)
    products = extract_subscription_products(store_data['products'])
    if not products:
        logger.warning("No subscription products detected.")
        return []
    orders = store_data['orders'].get('orders', [])
    df_orders = pd.DataFrame(orders) if orders else pd.DataFrame(columns=['line_items', 'total_price', 'created_at'])
    customers = store_data['customers'].get('customers', [])
    df_customers = pd.DataFrame(customers) if customers else pd.DataFrame(columns=['created_at', 'orders_count'])
    # Data wrangling
    top_products = analyze_top_products(df_orders, products)[:2]  # limit to 2 per week
    metrics = weekly_growth_metrics(df_orders)
    customer_segments = segment_customers(df_customers)
    campaign_ideas = []
    for prod, _ in top_products:
        for seg, custs in customer_segments.items():
            ad_copy = generate_ad_copy(prod, seg.replace('_', ' '), metrics, openai_key)
            img_path = generate_ad_image(prod, openai_key)
            idea = CampaignIdea(
                title=f"{prod.title}: Targeting {seg.replace('_', ' ').title()}",
                description=f"Promote {prod.title} to {seg.replace('_', ' ')} based on recent order and customer data.",
                ad_copy=ad_copy,
                image_path=img_path,
                created_at=datetime.utcnow().isoformat()
            )
            campaign_ideas.append(idea)
    logger.info(f"Generated {len(campaign_ideas)} campaign ideas.")
    return campaign_ideas

# --- Swarms Agent Configuration ---
def build_swarms_agent(model_name: Optional[str] = None, max_loops: Optional[int] = None) -> Agent:
    agent_name = "ShopiGen Campaign Agent"
    agent_description = (
        "Automatically analyzes your Shopify subscription store data, discovers high-impact campaign opportunities, and generates ready-to-use ad copy and images. Data-driven, tailored for your store's weekly growth. Stores all campaigns for future reference."
    )
    system_prompt = (
        "You are a professional e-commerce marketing assistant. Each week, you analyze detailed store sales, customer, and product data (esp. subscriptions) from Shopify. "
        "You surface actionable marketing campaign ideas tailored to audience/customer segments, generate 1-2 concrete campaign briefs with high-converting ad copy (60 words or less) and suggest image creative. All output must be practical for paid social/email ads. "
        "Summarize the opportunity, provide the campaign title, ad copy, image file path (already saved), and campaign rationale. Only output structured JSON in the field format: title, description, ad_copy, image_path, created_at."
    )
    if model_name is None:
        model_name = os.environ.get('SWARMS_MODEL_NAME', 'gpt-4o-mini')
    if max_loops is None:
        ml = os.environ.get('SWARMS_MAX_LOOPS')
        max_loops = int(ml) if ml else 5
    return Agent(
        agent_name=agent_name,
        agent_description=agent_description,
        system_prompt=system_prompt,
        model_name=model_name,
        max_loops=max_loops
    )

def main() -> None:
    parser = argparse.ArgumentParser(description="ShopiGen Campaign Generator")
    parser.add_argument('--sqlite-path', type=str, default='shopigen_campaigns.db', help='Path to SQLite DB file')
    parser.add_argument('--tinydb-path', type=str, default='shopigen_campaigns.json', help='Path to TinyDB file')
    parser.add_argument('--dry-run', action='store_true', default=False, help='If set, does not persist campaigns')
    args = parser.parse_args()
    # Credentials
    try:
        shopify_api_key, shopify_password, shopify_shop_name = get_shopify_api_credentials()
        openai_api_key = get_openai_api_key()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)
    # Storage setup
    engine = create_sqlite_engine(args.sqlite_path)
    tinydb = TinyDB(args.tinydb_path)
    # Generate weekly campaign ideas
    campaign_ideas = generate_campaign_ideas(shopify_api_key, shopify_password, shopify_shop_name, openai_api_key)
    if not campaign_ideas:
        logger.error("No campaign ideas generated. Exiting.")
        sys.exit(1)
    # Store campaigns unless dry-run
    if not args.dry_run:
        for idea in campaign_ideas:
            store_campaign_idea_sql(engine, idea)
            store_campaign_idea_tinydb(tinydb, idea)
    # Prepare summary for LLM agent
    llm_in = json.dumps([asdict(idea) for idea in campaign_ideas], ensure_ascii=False)
    agent = build_swarms_agent()
    task = f"Here are campaign ideas for this week: {llm_in}"
    try:
        output = agent.run(task)
        print(output)
    except Exception as e:
        logger.error(f"Swarms agent failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
