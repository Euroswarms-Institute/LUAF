"""
LUAF X (Twitter) social layer: batch 2–3 agents per 2-tweet thread to stay under 500 tweets/month.
Uses x_pending.json, x_posted_agents.json, and optional x_post_queue.json for retries.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# Writable JSON state lives next to LUAF.py (project / install entry), not inside site-packages/luaf/.
_STATE_ROOT = Path(__file__).resolve().parents[1]
X_PENDING_PATH = _STATE_ROOT / "x_pending.json"
X_POSTED_PATH = _STATE_ROOT / "x_posted_agents.json"
X_QUEUE_PATH = _STATE_ROOT / "x_post_queue.json"
TWEET_MAX_LEN = 280
_DEFAULT_BATCH_SIZE = 2
_SLEEP_BETWEEN_TWEETS = 2.5
_MAX_TWEETS_PER_15MIN = 20


def _env_bool(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() in ("1", "true", "yes")


def _env_int(name: str, default: int, lo: int = 1, hi: int = 10) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _x_credentials() -> Optional[tuple[str, str, str, str]]:
    """Return (api_key, api_secret, access_token, access_secret) or None if not configured."""
    key = (os.environ.get("X_API_KEY") or "").strip()
    secret = (os.environ.get("X_API_SECRET") or "").strip()
    token = (os.environ.get("X_ACCESS_TOKEN") or "").strip()
    token_secret = (os.environ.get("X_ACCESS_SECRET") or "").strip()
    if key and secret and token and token_secret:
        return (key, secret, token, token_secret)
    return None


def is_x_post_enabled() -> bool:
    """True if LUAF_POST_TO_X=1 and X API credentials are set."""
    if not _env_bool("LUAF_POST_TO_X", "0"):
        return False
    return _x_credentials() is not None


def _load_json_list(path: Path) -> list[Any]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _load_posted_ids() -> set[str]:
    data = _load_json_list(X_POSTED_PATH)
    if not data:
        return set()
    if isinstance(data[0], dict):
        return {str(e.get("id", e.get("listing_url", ""))) for e in data if e.get("id") or e.get("listing_url")}
    return {str(x) for x in data}


def _save_json_list(path: Path, items: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")


def build_batch_thread(agents: list[dict[str, Any]]) -> list[str]:
    """
    Build a 2-tweet thread for 2–3 agents.
    Tweet 1: short combined description (name + one line each).
    Tweet 2: links and CAs (if applicable).
    Each tweet ≤ TWEET_MAX_LEN chars.
    """
    if not agents:
        return []
    lines1: list[str] = []
    for a in agents:
        name = (a.get("name") or a.get("ticker") or "Agent").strip()
        desc = (a.get("description_snippet") or "").strip() or "Autonomous unit."
        if len(desc) > 70:
            desc = desc[:67] + "..."
        lines1.append(f"{name}: {desc}")
    tweet1 = " ".join(lines1)
    if len(tweet1) > TWEET_MAX_LEN:
        tweet1 = tweet1[: TWEET_MAX_LEN - 3] + "..."
    links: list[str] = []
    cas: list[str] = []
    for a in agents:
        url = (a.get("listing_url") or "").strip()
        if url:
            links.append(url)
        ca = (a.get("token_address") or "").strip()
        if ca and len(ca) >= 32:
            cas.append(ca[:16] + "…" if len(ca) > 20 else ca)
    parts2: list[str] = []
    if links:
        link_str = " ".join(links[:3])
        if len(link_str) > 200:
            link_str = link_str[:197] + "..."
        parts2.append("Links: " + link_str)
    if cas:
        ca_str = " ".join(cas[:3])
        if len(ca_str) > 150:
            ca_str = ca_str[:147] + "..."
        parts2.append("CAs: " + ca_str)
    tweet2 = " ".join(parts2) if parts2 else (links[0] if links else "")
    if len(tweet2) > TWEET_MAX_LEN:
        tweet2 = tweet2[: TWEET_MAX_LEN - 3] + "..."
    return [tweet1, tweet2]


def post_tweet(text: str, in_reply_to_id: Optional[str] = None) -> Optional[str]:
    """
    Post a single tweet. Returns the new tweet id on success, None on failure.
    Uses OAuth 1.0a with X API v2 POST /2/tweets.
    """
    creds = _x_credentials()
    if not creds:
        logger.warning("X post skipped: credentials not set")
        return None
    try:
        from requests_oauthlib import OAuth1Session
    except ImportError:
        logger.warning("requests_oauthlib not installed; pip install requests_oauthlib")
        return None
    api_key, api_secret, access_token, access_secret = creds
    oauth = OAuth1Session(
        client_key=api_key,
        client_secret=api_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_secret,
    )
    url = "https://api.x.com/2/tweets"
    payload: dict[str, Any] = {"text": text[:TWEET_MAX_LEN]}
    if in_reply_to_id:
        payload["reply"] = {"in_reply_to_tweet_id": in_reply_to_id}
    try:
        resp = oauth.post(url, json=payload, timeout=15)
        if not resp.ok:
            logger.warning("X API post failed: {} {}", resp.status_code, resp.text[:300])
            return None
        data = resp.json()
        tweet_id = (data.get("data") or {}).get("id")
        return str(tweet_id) if tweet_id else None
    except Exception as e:
        logger.warning("X post error: {}", e)
        return None


def post_thread(tweet_texts: list[str]) -> Optional[str]:
    """
    Post a 2-tweet thread (tweet 2 as reply to tweet 1).
    Returns the first tweet id on success, None otherwise.
    """
    if not tweet_texts:
        return None
    first_id = post_tweet(tweet_texts[0])
    if not first_id:
        return None
    time.sleep(_SLEEP_BETWEEN_TWEETS)
    if len(tweet_texts) > 1:
        post_tweet(tweet_texts[1], in_reply_to_id=first_id)
    return first_id


def add_agent_to_x_pending(payload: dict[str, Any], result: dict[str, Any]) -> None:
    """
    Add a published agent to x_pending for batched posting.
    Skips if agent id is already in x_posted_agents.
    """
    if not is_x_post_enabled():
        return
    agent_id = (result.get("id") or result.get("listing_url") or "").strip()
    if not agent_id:
        return
    posted = _load_posted_ids()
    if agent_id in posted:
        return
    description_snippet = (payload.get("description") or "")[:100].strip()
    entry: dict[str, Any] = {
        "id": agent_id,
        "name": (payload.get("name") or "").strip(),
        "ticker": (payload.get("ticker") or "").strip(),
        "listing_url": (result.get("listing_url") or "").strip(),
        "token_address": (result.get("token_address") or "").strip(),
        "description_snippet": description_snippet,
    }
    pending = _load_json_list(X_PENDING_PATH)
    pending.append(entry)
    _save_json_list(X_PENDING_PATH, pending)
    logger.debug("Added agent to X pending: {}", entry.get("name", agent_id[:16]))


def _post_batch_and_mark(batch: list[dict[str, Any]]) -> bool:
    """Build thread, post, mark agents as posted, remove from pending. Returns True on success."""
    tweets = build_batch_thread(batch)
    if len(tweets) < 2:
        return False
    first_id = post_thread(tweets)
    if not first_id:
        return False
    posted = _load_posted_ids()
    for a in batch:
        aid = a.get("id") or ""
        if aid:
            posted.add(aid)
    _save_json_list(X_POSTED_PATH, sorted(posted))
    pending = _load_json_list(X_PENDING_PATH)
    batch_ids = {a.get("id") for a in batch}
    pending = [p for p in pending if (p.get("id") if isinstance(p, dict) else p) not in batch_ids]
    _save_json_list(X_PENDING_PATH, pending)
    logger.info("X thread posted (batch of {} agents)", len(batch))
    return True


def maybe_post_x_batch() -> None:
    """
    If x_pending has at least batch_size (2 or 3) agents, build a 2-tweet thread,
    post it, mark those agents as posted, and remove them from pending.
    On failure, enqueue the batch for retry if LUAF_X_QUEUE_ENABLED=1.
    """
    if not is_x_post_enabled():
        return
    batch_size = _env_int("LUAF_X_BATCH_SIZE", _DEFAULT_BATCH_SIZE, lo=2, hi=3)
    pending = _load_json_list(X_PENDING_PATH)
    if len(pending) < batch_size:
        return
    batch = pending[:batch_size]
    if _post_batch_and_mark(batch):
        return
    if _env_bool("LUAF_X_QUEUE_ENABLED", "1"):
        queue_list = _load_json_list(X_QUEUE_PATH)
        queue_list.append({"batch": batch, "enqueued_at": time.time()})
        _save_json_list(X_QUEUE_PATH, queue_list)
        logger.warning("X post failed; batch enqueued for retry ({} agents)", len(batch))


def drain_x_queue() -> None:
    """
    Process x_post_queue: post batched threads with rate limiting (sleep between tweets).
    Removes successfully posted batches from the queue.
    """
    if not is_x_post_enabled():
        return
    queue_list = _load_json_list(X_QUEUE_PATH)
    if not queue_list:
        return
    max_per_run = min(5, _env_int("LUAF_X_MAX_TWEETS_PER_15MIN", 20) // 2)
    processed = 0
    remaining: list[Any] = []
    for item in queue_list:
        if processed >= max_per_run:
            remaining.append(item)
            continue
        batch = item.get("batch") if isinstance(item, dict) else item
        if not isinstance(batch, list) or not batch:
            continue
        if _post_batch_and_mark(batch):
            processed += 1
        else:
            remaining.append(item)
        time.sleep(_SLEEP_BETWEEN_TWEETS * 2)
    _save_json_list(X_QUEUE_PATH, remaining)
    if processed:
        logger.info("X queue drained: {} batches posted", processed)
