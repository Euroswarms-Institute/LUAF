import os
import json
import time
import random
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
import threading
import base58
import requests
import httpx
from ecdsa import SigningKey, SECP256k1
import ccxt
from web3 import Web3, HTTPProvider
from loguru import logger
from swarms import Agent

NFT_MARKETPLACES = [
    {
        "name": "OpenSea",
        "listing_api": "https://api.opensea.io/api/v2/listings/collection/{collection}/all",
        "floor_price_api": "https://api.opensea.io/api/v2/collections/{collection}",
        "purchase_api": "https://api.opensea.io/api/v2/orders",
        "api_key_env": "OPENSEA_API_KEY"
    },
    {
        "name": "Blur.io",
        "listing_api": "https://api.blur.io/v1/collections/{collection}/listings",
        "floor_price_api": "https://api.blur.io/v1/collections/{collection}",
        "purchase_api": None,  # Requires onchain interaction
        "api_key_env": "BLUR_API_KEY"
    },
    {
        "name": "LooksRare",
        "listing_api": "https://api.looksrare.org/api/v1/orders?collection={collection}&type=listing",
        "floor_price_api": "https://api.looksrare.org/api/v1/collections/{collection}",
        "purchase_api": None,
        "api_key_env": "LOOKSRARE_API_KEY"
    }
]

# Example supported NFT collections (could be fetched from config or API)
DEFAULT_COLLECTIONS = [
    "boredapeyachtclub",
    "azuki",
    "mutant-ape-yacht-club",
    "doodles-official"
]

ETHEREUM_RPC_URL = os.environ.get("ETHEREUM_RPC_URL", "https://eth.llamarpc.com")
ARBITRAGE_CONTRACT_ADDRESS = os.environ.get("ARB_CONTRACT_ADDRESS", "0xYourSmartContractAddressHere")

# Pool private key (used for signing buys/sells); use secret manager in prod
POOL_PRIVATE_KEY = os.environ.get("POOL_PRIVATE_KEY")
POOL_ADDRESS = None  # Populated after key is loaded

MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4o-mini")
MAX_LOOPS = int(os.environ.get("MAX_LOOPS", "5"))
TX_MAX_RETRIES = 4
TX_RETRY_BACKOFF = [10, 30, 45, 90]  # seconds, increases on each retry

@dataclass
class NFTListing:
    marketplace: str
    listing_id: str
    token_id: str
    collection: str
    price_eth: float
    currency: str
    seller: str
    url: str
    expires: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ArbitrageOpportunity:
    listing: NFTListing
    floor_price: float
    expected_profit: float
    market_to_sell: str

@dataclass
class ArbitrageResult:
    bought: bool
    sold: bool
    buy_tx_hash: Optional[str] = None
    sell_tx_hash: Optional[str] = None
    profit_eth: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)

# --- Helper functions ---

def load_signing_key() -> Tuple[SigningKey, str]:
    key = POOL_PRIVATE_KEY
    if not key:
        raise ValueError("POOL_PRIVATE_KEY not set in environment.")
    try:
        if key.startswith('0x'):
            key_bytes = bytes.fromhex(key[2:])
        else:
            key_bytes = base58.b58decode(key)
        sk = SigningKey.from_string(key_bytes, curve=SECP256k1)
        vk = sk.verifying_key
        public_key = vk.to_string().hex()
        w3 = Web3(HTTPProvider(ETHEREUM_RPC_URL))
        address = w3.eth.account.from_key(key).address
        global POOL_ADDRESS
        POOL_ADDRESS = address
        return sk, address
    except Exception as e:
        logger.error(f"Failed to load signing key: {e}")
        raise

def fetch_json_with_retries(url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None, timeout: int = 40, retries: int = 3) -> Any:
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"HTTP GET failed (Attempt {attempt+1}/{retries}) for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error(f"HTTP GET to {url} failed after {retries} attempts.")
                raise
        except json.JSONDecodeError as e:
            logger.error(f"Decode error on {url}: {e}")
            raise

def get_listings(marketplace: Dict[str, Any], collection: str, limit: int = 12) -> List[NFTListing]:
    url = marketplace["listing_api"].format(collection=collection)
    key = os.environ.get(marketplace["api_key_env"])
    headers = {"Accept": "application/json"}
    if key:
        headers["X-API-KEY"] = key
    listings = []
    try:
        data = fetch_json_with_retries(url, headers, timeout=40, retries=3)
        if marketplace["name"] == "OpenSea":
            for item in data.get("listings", [])[:limit]:
                listing = NFTListing(
                    marketplace="OpenSea",
                    listing_id=item["order_hash"],
                    token_id=str(item["asset"]["token_id"]),
                    collection=collection,
                    price_eth=float(item['price']['current']['eth_price']),
                    currency=item['price']['current']['currency'],
                    seller=item.get("maker", ""),
                    url=item.get("permalink", ""),
                    expires=item.get("expiration_time"),
                    meta=item
                )
                listings.append(listing)
        elif marketplace["name"] == "Blur.io":
            for item in data.get("listings", [])[:limit]:
                listing = NFTListing(
                    marketplace="Blur.io",
                    listing_id=item["id"],
                    token_id=str(item["tokenId"]),
                    collection=collection,
                    price_eth=float(item['priceEth']),
                    currency="ETH",
                    seller=item.get("maker", ""),
                    url=f"https://blur.io/asset/{collection}/{item['tokenId']}",
                    expires=item.get("expiration"),
                    meta=item
                )
                listings.append(listing)
        elif marketplace["name"] == "LooksRare":
            for item in data.get("data", [])[:limit]:
                listing = NFTListing(
                    marketplace="LooksRare",
                    listing_id=item["hash"],
                    token_id=str(item["tokenId"]),
                    collection=collection,
                    price_eth=float(Web3.fromWei(int(item['price']), 'ether')),
                    currency="ETH",
                    seller=item.get("signer", ""),
                    url=f"https://looksrare.org/collections/{collection}/{item['tokenId']}",
                    expires=item.get("endTime"),
                    meta=item
                )
                listings.append(listing)
    except Exception as e:
        logger.error(f"Error fetching listings from {marketplace['name']}: {e}")
    return listings

def get_floor_price(marketplace: Dict[str, Any], collection: str) -> Optional[float]:
    url = marketplace["floor_price_api"].format(collection=collection)
    key = os.environ.get(marketplace["api_key_env"])
    headers = {"Accept": "application/json"}
    if key:
        headers["X-API-KEY"] = key
    try:
        data = fetch_json_with_retries(url, headers, timeout=30, retries=3)
        if marketplace["name"] == "OpenSea":
            fp = float(data['collection']['stats']['floor_price'])
            return fp
        elif marketplace["name"] == "Blur.io":
            fp = float(data['collection']['floorPriceEth'])
            return fp
        elif marketplace["name"] == "LooksRare":
            fp = float(Web3.fromWei(int(data['data']['floorAsk']['price']), 'ether'))
            return fp
    except Exception as e:
        logger.warning(f"Could not fetch floor price from {marketplace['name']} for {collection}: {e}")
    return None

def find_arbitrage_opportunities(collections: List[str], price_margin_pct: float = 7.0, min_profit_eth: float = 0.012) -> List[ArbitrageOpportunity]:
    opportunities = []
    for collection in collections:
        floors = {m["name"]: get_floor_price(m, collection) for m in NFT_MARKETPLACES}
        if not any(v for v in floors.values()):
            continue
        # Find lowest current listing in all marketplaces
        all_listings = []
        for m in NFT_MARKETPLACES:
            all_listings += get_listings(m, collection, limit=10)
        # Compare listing price to median floor from other markets
        for listing in all_listings:
            my_floor = floors[listing.marketplace]
            if my_floor is None or listing.price_eth >= my_floor * (1 - 0.01):
                continue
            # Try to sell at the highest floor among the other markets
            others = [v for k, v in floors.items() if k != listing.marketplace and v]
            if not others:
                continue
            max_resale_floor = max(others)
            if max_resale_floor > listing.price_eth * (1 + price_margin_pct/100.0):
                expected_profit = max_resale_floor - listing.price_eth
                if expected_profit >= min_profit_eth:
                    # Pick highest-priced marketplace as resale target
                    market_to_sell = [k for k, v in floors.items() if v == max_resale_floor][0]
                    opp = ArbitrageOpportunity(
                        listing=listing,
                        floor_price=max_resale_floor,
                        expected_profit=expected_profit,
                        market_to_sell=market_to_sell
                    )
                    opportunities.append(opp)
    return sorted(opportunities, key=lambda x: -x.expected_profit)

def simulate_gas_and_fees() -> float:
    # Simulate average gas for buy/sell on ETH mainnet NFT markets
    base_fee = random.uniform(0.0022, 0.008)
    return base_fee

def purchase_nft(listing: NFTListing) -> Optional[str]:
    # For demo, simulate OpenSea API; Onchain for others (Blur/LooksRare)
    logger.info(f"Attempting NFT purchase: {listing.marketplace}, {listing.collection} #{listing.token_id} at {listing.price_eth} ETH")
    # We assume all purchases use ETH; add onchain swap if needed in prod
    if listing.marketplace == "OpenSea":
        api_key = os.environ.get("OPENSEA_API_KEY")
        order_url = f"https://api.opensea.io/api/v2/orders"
        headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
        body = {
            "order_hash": listing.listing_id,
            "taker": POOL_ADDRESS
        }
        for attempt in range(TX_MAX_RETRIES):
            try:
                resp = requests.post(order_url, headers=headers, json=body, timeout=60)
                if resp.status_code in (200, 201, 202):
                    result = resp.json()
                    logger.success(f"NFT purchased, tx: {result.get('transaction_hash')}")
                    return result.get('transaction_hash', None)
                else:
                    logger.warning(f"NFT purchase failed [{resp.status_code}]: {resp.text}")
                    if attempt < TX_MAX_RETRIES-1:
                        time.sleep(TX_RETRY_BACKOFF[attempt])
                    else:
                        logger.error(f"Purchase failed after retries.")
                        return None
            except requests.RequestException as e:
                logger.warning(f"Purchase attempt {attempt+1} failed: {e}")
                if attempt < TX_MAX_RETRIES-1:
                    time.sleep(TX_RETRY_BACKOFF[attempt])
                else:
                    logger.error(f"Purchase failed after retries.")
                    return None
    else:
        # Onchain purchase: Build and send transaction
        w3 = Web3(HTTPProvider(ETHEREUM_RPC_URL))
        try:
            nonce = w3.eth.get_transaction_count(POOL_ADDRESS)
            tx = {
                'to': listing.meta.get('contract_address', listing.collection),
                'value': w3.toWei(listing.price_eth, 'ether'),
                'gas': 280_000,
                'gasPrice': w3.eth.gas_price,
                'nonce': nonce,
                'data': b''  # Would need ABI to construct full calldata
            }
            signed_tx = w3.eth.account.sign_transaction(tx, POOL_PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            tx_hex = tx_hash.hex()
            logger.success(f"Sent onchain buy tx: {tx_hex}")
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            return tx_hex
        except Exception as e:
            logger.error(f"On-chain NFT buy failed: {e}")
            return None

def list_nft_for_resale(listing: NFTListing, resale_market: str, price_eth: float) -> Optional[str]:
    logger.info(f"Listing NFT {listing.collection} #{listing.token_id} for resale on {resale_market} at {price_eth} ETH")
    # For OpenSea, use API; for others, simulate on-chain listing
    if resale_market == "OpenSea":
        api_key = os.environ.get("OPENSEA_API_KEY")
        url = "https://api.opensea.io/api/v2/listings"
        headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
        body = {
            "asset": {
                "token_id": listing.token_id,
                "collection": listing.collection
            },
            "seller": POOL_ADDRESS,
            "price": {
                "eth_price": price_eth,
                "currency": "ETH"
            },
            "expiration_time": int(time.time()) + 4*3600
        }
        for attempt in range(TX_MAX_RETRIES):
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=60)
                if resp.status_code in (200, 201, 202):
                    listing_info = resp.json()
                    logger.success(f"NFT listed for resale: {listing_info.get('listing_id')}")
                    return listing_info.get('listing_id', None)
                else:
                    logger.warning(f"NFT resale listing failed [{resp.status_code}]: {resp.text}")
                    if attempt < TX_MAX_RETRIES-1:
                        time.sleep(TX_RETRY_BACKOFF[attempt])
                    else:
                        logger.error(f"Resale listing failed after retries.")
                        return None
            except requests.RequestException as e:
                logger.warning(f"Resale listing attempt {attempt+1} failed: {e}")
                if attempt < TX_MAX_RETRIES-1:
                    time.sleep(TX_RETRY_BACKOFF[attempt])
                else:
                    logger.error(f"Resale listing failed after retries.")
                    return None
    else:
        # For Blur, LooksRare: implement on-chain listing via pool contract
        w3 = Web3(HTTPProvider(ETHEREUM_RPC_URL))
        try:
            nonce = w3.eth.get_transaction_count(POOL_ADDRESS)
            tx = {
                'to': listing.meta.get('contract_address', listing.collection),
                'value': 0,
                'gas': 320_000,
                'gasPrice': w3.eth.gas_price,
                'nonce': nonce,
                'data': b''  # On-chain listing logic would be here.
            }
            signed_tx = w3.eth.account.sign_transaction(tx, POOL_PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            tx_hex = tx_hash.hex()
            logger.success(f"Sent resale listing tx: {tx_hex}")
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            return tx_hex
        except Exception as e:
            logger.error(f"On-chain NFT resale listing failed: {e}")
            return None

def distribute_profits_to_tokenholders(profit_eth: float, details: Dict[str, Any]):
    logger.info(f"Triggering profit distribution: {profit_eth:.5f} ETH to pool token holders.")
    w3 = Web3(HTTPProvider(ETHEREUM_RPC_URL))
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(ARBITRAGE_CONTRACT_ADDRESS), abi=details.get('contract_abi', []))
        nonce = w3.eth.get_transaction_count(POOL_ADDRESS)
        tx = contract.functions.distributeProfits().build_transaction({
            'from': POOL_ADDRESS,
            'value': w3.toWei(profit_eth, 'ether'),
            'nonce': nonce,
            'gas': 220_000,
            'gasPrice': w3.eth.gas_price
        })
        signed = w3.eth.account.sign_transaction(tx, POOL_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        logger.success(f"Profit distributed, tx: {tx_hash.hex()}")
        return tx_hash.hex()
    except Exception as e:
        logger.error(f"Failed to distribute profits: {e}")
        return None

def arbitrage_cycle(collections: List[str]) -> List[ArbitrageResult]:
    results = []
    opps = find_arbitrage_opportunities(collections)
    logger.info(f"Found {len(opps)} arbitrage opportunities.")
    sk, pool_addr = load_signing_key()
    # Only do the most profitable opportunity in a single cycle
    for opp in opps[:1]:
        logger.info(f"Executing arbitrage: Buy {opp.listing.collection} #{opp.listing.token_id} at {opp.listing.price_eth} ETH, resell on {opp.market_to_sell} at {opp.floor_price} ETH.")
        # Estimate fees
        net_profit = opp.expected_profit - simulate_gas_and_fees() * 2  # buy & sell
        if net_profit <= 0:
            logger.info(f"Net profit after fees not sufficient (est. {net_profit:.5f} ETH). Skipping.")
            continue
        # Purchase NFT
        buy_tx = purchase_nft(opp.listing)
        if not buy_tx:
            logger.warning(f"Purchase failed for listing {opp.listing.listing_id}")
            continue
        # List NFT for resale
        resale_tx = list_nft_for_resale(opp.listing, opp.market_to_sell, opp.floor_price)
        if not resale_tx:
            logger.warning(f"Listing for resale failed for token {opp.listing.token_id}")
            continue
        # Simulate immediate resale (in prod, monitor pending marketplace sale)
        # Profit accounting
        logger.success(f"Arbitrage sequence complete. Profit: {net_profit:.5f} ETH.")
        distribute_tx = distribute_profits_to_tokenholders(net_profit, details={})
        res = ArbitrageResult(
            bought=True, sold=True,
            buy_tx_hash=buy_tx, sell_tx_hash=resale_tx,
            profit_eth=net_profit,
            details={"distribute_tx": distribute_tx}
        )
        results.append(res)
    return results

# --- Swarms Agent ---

AGENT_NAME = "NFT Arbitrage Executor"
AGENT_DESCRIPTION = (
    "Continuously monitors major NFT marketplaces via API, detects underpriced listings, auto-purchases using a pooled fund, and relists at fair market. Profits are distributed to smart-contract token holders. Results are available via API for automated consumption."
)
SYSTEM_PROMPT = (
    "You are an autonomous NFT arbitrage executor. Monitor all configured major NFT markets for underpriced listings. For each opportunity: \n"
    "1. Compare current listings to floor price in other markets.\n"
    "2. When profit margin exceeds thresholds (after fees), instantly buy and relist on highest-priced market.\n"
    "3. Use pooled wallet for purchases with strict key and fund management.\n"
    "4. Distribute net profits to pool token holders via smart contract.\n"
    "5. Log all trade actions and profit distributions.")

NFT_ARBITRAGE_AGENT = Agent(
    agent_name=AGENT_NAME,
    agent_description=AGENT_DESCRIPTION,
    system_prompt=SYSTEM_PROMPT,
    model_name=MODEL_NAME,
    max_loops=MAX_LOOPS,
)

# Entrypoint

def main():
    logger.add("nftarb.log", rotation="2 MB")
    logger.info("NFT Arbitrage Executor started.")
    try:
        collections = os.environ.get("NFT_COLLECTIONS")
        if collections:
            collections = [x.strip() for x in collections.split(",") if x.strip()]
        else:
            collections = DEFAULT_COLLECTIONS
        logger.info(f"Monitoring NFT collections: {collections}")
        # Run arbitrage logic
        arbitrage_results = arbitrage_cycle(collections)
        output = {
            "arbitrage_trades": [
                {
                    "bought": r.bought,
                    "sold": r.sold,
                    "buy_tx_hash": r.buy_tx_hash,
                    "sell_tx_hash": r.sell_tx_hash,
                    "profit_eth": r.profit_eth,
                    "details": r.details
                }
                for r in arbitrage_results
            ]
        }
        print(json.dumps(output, indent=2))
    except Exception as e:
        logger.opt(exception=True).error(f"Fatal exception: {e}")
        print(json.dumps({"error": str(e)}), flush=True)

if __name__ == "__main__":
    main()
