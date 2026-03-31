"""Route publish to Swarms marketplace or RapidAPI-assisted bundle."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from luaf.publishing.model import get_publish_target
from luaf.publishing.rapid import publish_rapid_assisted
from luaf.publishing.swarms import publish_agent


def publish_for_target(
    payload: dict[str, Any],
    *,
    swarms_key: str,
    pkey: str,
    dry_run: bool,
    image_url: Optional[str] = None,
    creator_wallet: Optional[str] = None,
    rapid_registry_path: Path,
) -> Optional[dict[str, Any]]:
    """
    Dispatch by LUAF_PUBLISH_TARGET (swarms | rapidapi).

    Swarms path preserves existing luaf_publish.publish_agent behavior.
    """
    target = get_publish_target()
    if target == "rapidapi":
        return publish_rapid_assisted(payload, dry_run=dry_run, registry_path=rapid_registry_path)
    return publish_agent(
        payload,
        swarms_key,
        pkey,
        dry_run,
        image_url=image_url,
        creator_wallet=creator_wallet,
    )
