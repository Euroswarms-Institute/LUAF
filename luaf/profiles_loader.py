#!/usr/bin/env python3
"""
LUAF profile loading. Each profile is a directory under luaf/profiles/ (package) with three
plain-text files: system_prompt.txt (required), topic_prompt.txt (optional),
product_focus.txt (optional). No Markdown styling in the files.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_SYSTEM_PROMPT_FILE = 'system_prompt.txt'
_TOPIC_PROMPT_FILE = 'topic_prompt.txt'
_PRODUCT_FOCUS_FILE = 'product_focus.txt'
_DISPLAY_NAME_FILE = 'display_name.txt'


def _read_file(path: Path) -> str:
    """Read file content; return stripped string or empty."""
    if not path.is_file():
        return ''
    try:
        return path.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return ''


def _display_name_from_system_prompt(content: str, profile_id: str) -> str:
    """First non-empty line of system prompt is the display name; else profile_id."""
    for line in (content or '').strip().splitlines():
        line = line.strip()
        if line:
            return line[:80]
    return profile_id


def list_profiles(profiles_dir: Path) -> list[dict[str, Any]]:
    """
    List profiles: each subdirectory of profiles_dir is a profile. Required file
    is system_prompt.txt. Optional: topic_prompt.txt, product_focus.txt.
    Returns list of dicts: id, display_name, system_prompt, topic_prompt?, product_focus?.
    """
    out: list[dict[str, Any]] = []
    if not profiles_dir.is_dir():
        return out
    for subdir in sorted(profiles_dir.iterdir()):
        if not subdir.is_dir():
            continue
        system_path = subdir / _SYSTEM_PROMPT_FILE
        system = _read_file(system_path)
        if not system:
            continue
        profile_id = subdir.name
        topic = _read_file(subdir / _TOPIC_PROMPT_FILE) or None
        focus = _read_file(subdir / _PRODUCT_FOCUS_FILE) or None
        display = _read_file(subdir / _DISPLAY_NAME_FILE)
        if not display:
            display = _display_name_from_system_prompt(system, profile_id)
        else:
            display = display.splitlines()[0].strip()[:80] if display else profile_id
        out.append({
            'id': profile_id,
            'display_name': display or profile_id,
            'system_prompt': system,
            'topic_prompt': topic if topic else None,
            'product_focus': focus if focus else None,
        })
    return out


def get_default_profile(
    designer_prompt_path: Path,
    default_topic_prompt: str,
    default_product_focus: str,
) -> dict[str, Any]:
    """
    Build the default profile (current behavior): system prompt from
    designer_system_prompt.txt, topic/focus from provided strings.
    """
    system = _read_file(designer_prompt_path)
    return {
        'id': 'default',
        'display_name': 'default',
        'system_prompt': system,
        'topic_prompt': (default_topic_prompt or '').strip() or None,
        'product_focus': (default_product_focus or '').strip() or None,
    }
