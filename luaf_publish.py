from __future__ import annotations
import json, os, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import requests
from loguru import logger

_BASE_URL = (os.environ.get('LUAF_SWARMS_BASE_URL') or 'https://swarms.world').strip()
_SOLANA_RPC_URL = (os.environ.get('LUAF_SOLANA_RPC_URL') or 'https://api.mainnet-beta.solana.com').strip()
_HTTP_PUBLISH_TIMEOUT = 350
_HTTP_CLAIM_FEES_TIMEOUT = 3099
_balance_cache: tuple[float, float] = (0.0, 0.0)
_BALANCE_CACHE_TTL_SECONDS = 60.0

def _resp_json(resp: Any) -> dict:
    try:
        return resp.json() if resp.text.strip() else {}
    except json.JSONDecodeError:
        return {'_raw': resp.text[:500] if resp.text else ''}

def get_private_key_from_env() -> Optional[str]:
    k = os.environ.get('SOLANA_PRIVATE_KEY', '').strip()
    if k:
        return k
    p = os.environ.get('SOLANA_PRIVATE_KEY_FILE', '').strip()
    if not p:
        return None
    pp = Path(p)
    if not pp.exists():
        logger.warning('Key file not found: {}', p)
        return None
    try:
        return pp.read_text(encoding='utf-8').strip()
    except Exception as e:
        logger.warning('Key file read error: {}', e)
        return None

def get_creator_pubkey() -> Optional[str]:
    pubkey = (os.environ.get('SOLANA_PUBKEY') or os.environ.get('CREATOR_WALLET') or '').strip()
    if pubkey:
        return pubkey
    pkey = get_private_key_from_env()
    if not pkey:
        return None
    try:
        from solders.keypair import Keypair
        kp = Keypair.from_base58_string(pkey)
        return str(kp.pubkey())
    except ImportError:
        logger.debug('solders not installed; set CREATOR_WALLET or SOLANA_PUBKEY for balance check')
        return None
    except Exception as e:
        logger.warning('Could not derive pubkey from private key: {}', e)
        return None

def get_solana_balance(pubkey: Optional[str], rpc_url: str = _SOLANA_RPC_URL) -> float:
    global _balance_cache
    if not pubkey or not rpc_url:
        return 0.0
    now = time.monotonic()
    cached_at, cached_bal = _balance_cache
    if cached_at > 0 and now - cached_at < _BALANCE_CACHE_TTL_SECONDS:
        return cached_bal
    try:
        resp = requests.post(rpc_url, json={'jsonrpc': '2.0', 'id': 1, 'method': 'getBalance', 'params': [pubkey]}, timeout=10)
        if not resp.ok:
            logger.warning('Solana RPC getBalance failed: {} (use LUAF_SOLANA_RPC_URL for a dedicated RPC to avoid rate limits)', resp.status_code)
            if cached_at > 0:
                return cached_bal
            return 0.0
        data = resp.json()
        result = data.get('result')
        if result is None and 'error' in data:
            logger.warning('Solana RPC error: {}', data.get('error'))
            if cached_at > 0:
                return cached_bal
            return 0.0
        if isinstance(result, dict):
            lamports = result.get('value', 0) or 0
        else:
            lamports = result if isinstance(result, (int, float)) else 0
        balance = float(lamports) / 1000000000.0
        _balance_cache = (now, balance)
        return balance
    except Exception as e:
        logger.warning('get_solana_balance failed: {}', e)
        if cached_at > 0:
            return cached_bal
        return 0.0

def load_agents_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
        return d if isinstance(d, list) else []
    except (json.JSONDecodeError, OSError):
        return []

def append_agent_to_registry(path: Path, name: str, ticker: str, listing_url: Optional[str] = None, id_: Optional[str] = None, token_address: Optional[str] = None, published_at: Optional[str] = None) -> None:
    reg = load_agents_registry(path)
    entry: dict[str, Any] = {'name': name, 'ticker': ticker, 'listing_url': listing_url, 'id': id_, 'token_address': token_address}
    if published_at is not None:
        entry['published_at'] = published_at
    reg.append(entry)
    fd, tmp = tempfile.mkstemp(prefix='luaf_reg_', suffix='.json', dir=path.parent, text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(reg, indent=2))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def claim_fees(ca: str, private_key: str, api_key: Optional[str] = None) -> Optional[dict[str, Any]]:
    headers: dict[str, str] = {'Content-Type': 'application/json'}
    if api_key and api_key.strip():
        headers['Authorization'] = f'Bearer {api_key.strip()}'
    try:
        resp = requests.post(f"{_BASE_URL.rstrip('/')}/api/product/claimfees", headers=headers, json={'ca': ca, 'privateKey': private_key}, timeout=_HTTP_CLAIM_FEES_TIMEOUT)
        status_code, data = (resp.status_code, _resp_json(resp))
    except requests.RequestException as e:
        logger.error('Claim fees failed: {}', e)
        return None
    if status_code >= 400:
        logger.error('Claim fees failed: {} {}', status_code, data)
    return data

def run_delayed_claim_pass(registry_path: Path, private_key: str, api_key: Optional[str], delay_hours: float) -> None:
    if not (private_key or '').strip():
        return
    reg = load_agents_registry(registry_path)
    now = datetime.now(timezone.utc)
    for e in reg:
        ca = e.get('token_address') or e.get('ca')
        if not ca or len(ca) < 32:
            continue
        published_at_str = e.get('published_at')
        if published_at_str:
            try:
                pub_dt = datetime.fromisoformat(published_at_str.replace('Z', '+00:00'))
                if (now - pub_dt).total_seconds() < delay_hours * 3600:
                    logger.debug('Skip claim for {} (published {}h ago)', ca[:16], round((now - pub_dt).total_seconds() / 3600, 1))
                    continue
            except (ValueError, TypeError):
                pass
        logger.info('Claiming fees for {}', ca[:16])
        cr = claim_fees(ca, private_key.strip(), api_key=api_key)
        if cr and cr.get('success'):
            logger.info('Claimed: sig={} sol={}', cr.get('signature'), cr.get('amountClaimedSol'))
        elif cr:
            logger.warning('Claim: {}', cr)

def publish_agent(payload: dict[str, Any], api_key: str, private_key: str, dry_run: bool, image_url: Optional[str] = None, creator_wallet: Optional[str] = None) -> Optional[dict[str, Any]]:
    tokenized = not dry_run
    if payload.get('tokenized_on') is False or payload.get('is_tokenized') is False:
        tokenized = False
    if tokenized and (not (private_key or '').strip()):
        logger.warning('Tokenized publish requires private_key; skipping.')
        return None
    out = dict(payload)
    out['tokenized_on'] = tokenized
    if tokenized:
        out['private_key'] = private_key.strip()
        cw = (creator_wallet or '').strip()
        if cw:
            out['creator_wallet'] = cw
    else:
        for k in ('ticker', 'creator_wallet', 'private_key'):
            out.pop(k, None)
    if dry_run:
        logger.info('Dry run: tokenized_on=False')
    if image_url:
        out['image_url'] = image_url
    try:
        resp = requests.post(f"{_BASE_URL.rstrip('/')}/api/add-agent", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json=out, timeout=_HTTP_PUBLISH_TIMEOUT)
        status_code, data = (resp.status_code, _resp_json(resp))
    except requests.RequestException as e:
        logger.error('Publish failed: {}', e)
        return None
    if status_code >= 400:
        logger.error('Publish failed: {} {}', status_code, data)
        return data
    logger.info('Success: {} id={} ca={}', data.get('listing_url'), data.get('id'), data.get('token_address', 'N/A'))
    return data
