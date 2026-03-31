"""Marketplace and RapidAPI publishing adapters."""

from luaf.publishing.dispatch import publish_for_target
from luaf.publishing.model import PUBLISH_TARGET_REGISTRY, PublishTarget, get_publish_target

__all__ = [
    "PUBLISH_TARGET_REGISTRY",
    "PublishTarget",
    "get_publish_target",
    "publish_for_target",
]
