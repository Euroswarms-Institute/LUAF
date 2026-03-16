#!/usr/bin/env python3
from __future__ import annotations
import functools, hashlib, importlib.resources, json, os, pickle, queue, random, re, subprocess, sys, tempfile, time, urllib.parse, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
import requests
from dotenv import load_dotenv
from loguru import logger
try:
    from luaf_tui import create_luaf_app
except ImportError:
    create_luaf_app = None
try:
    from toolbox.templates import get_template as _get_template
except ImportError:
    _get_template = None
try:
    from planner import plan_from_topic_and_search as _plan_from_topic_and_search
    from executor import execute_plan as _execute_plan
except ImportError:
    _plan_from_topic_and_search = None
    _execute_plan = None
try:
    from swarms import Agent as SwarmsAgent
except ImportError:
    try:
        from swarms.structs.agent import Agent as SwarmsAgent
    except ImportError:
        SwarmsAgent = None
try:
    from swarms_client import SwarmsClient as _SwarmsClient
except ImportError:
    _SwarmsClient = None
_ReactAgent = None
try:
    from swarms.agents.react_agent import ReactAgent as _ReactAgent
except ImportError:
    try:
        from swarms.agents import ReactAgent as _ReactAgent
    except ImportError:
        pass
try:
    from openclaw_controller import run_social_autonomy as _run_social_autonomy
except ImportError:
    _run_social_autonomy = None
from luaf_publish import publish_agent, get_private_key_from_env, get_creator_pubkey, get_solana_balance, load_agents_registry as _load_agents_registry, append_agent_to_registry, claim_fees, run_delayed_claim_pass
try:
    from luaf_x_post import add_agent_to_x_pending as _add_agent_to_x_pending, maybe_post_x_batch as _maybe_post_x_batch, drain_x_queue as _drain_x_queue, is_x_post_enabled as _is_x_post_enabled
except ImportError:
    _add_agent_to_x_pending = None
    _maybe_post_x_batch = None
    _drain_x_queue = None
    _is_x_post_enabled = None
try:
    from luaf_profiles import list_profiles as _list_profiles, get_default_profile as _get_default_profile_impl
except ImportError:
    _list_profiles = None
    _get_default_profile_impl = None
_REPO_ROOT = Path(__file__).resolve().parents[1]
_LUAF_DIR = Path(__file__).resolve().parent
# Cwd first so user .env overrides repo/defaults (doctor and run see user keys)
load_dotenv(Path.cwd() / '.env')
load_dotenv(_REPO_ROOT / '.env', override=False)
if (os.environ.get('LUAF_LOG_FILE', '1') or '').strip().lower() not in ('0', 'false', 'no'):
    _log_dir = _LUAF_DIR / 'logs'
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = _log_dir / 'luaf.log'
    try:
        logger.add(_log_file, format='{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}', level=0, rotation='10 MB', retention='7 days', encoding='utf-8')
    except Exception:
        pass

def _env_bool(name: str, default: str='0') -> bool:
    return (os.environ.get(name, default) or '').strip().lower() in ('1', 'true', 'yes')

def _env_int(name: str, default: int, lo: int=1, hi: int=999999) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default

def _env_float(name: str, default: float, lo: float=0.0, hi: float=2.0) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default

# CLI theme (OpenClaw palette + Codex clarity). Respect NO_COLOR and --no-color.
_cli_no_color: bool = False

def _cli_theme_no_color() -> bool:
    """Return True if CLI styling should be disabled (NO_COLOR or --no-color)."""
    if _cli_no_color:
        return True
    return (os.environ.get('NO_COLOR') or '').strip() != ''

def set_cli_no_color(value: bool) -> None:
    global _cli_no_color
    _cli_no_color = bool(value)

def _style_heading(s: str) -> str:
    """Accent style for headings, labels, command names."""
    if _cli_theme_no_color():
        return s
    return f'\033[1;38;5;202m{s}\033[0m'

def _style_success(s: str) -> str:
    if _cli_theme_no_color():
        return s
    return f'\033[32m{s}\033[0m'

def _style_warn(s: str) -> str:
    if _cli_theme_no_color():
        return s
    return f'\033[33m{s}\033[0m'

def _style_error(s: str) -> str:
    if _cli_theme_no_color():
        return s
    return f'\033[31m{s}\033[0m'

def _style_muted(s: str) -> str:
    if _cli_theme_no_color():
        return s
    return f'\033[2;37m{s}\033[0m'

def _style_info(s: str) -> str:
    if _cli_theme_no_color():
        return s
    return f'\033[36m{s}\033[0m'

def _style_accent(s: str) -> str:
    """Primary highlight (accentBright)."""
    if _cli_theme_no_color():
        return s
    return f'\033[1;38;5;208m{s}\033[0m'

def _str_from_result(r: Any) -> str:
    if isinstance(r, str):
        return r.strip()
    if isinstance(r, dict):
        return (r.get('output') or r.get('content') or r.get('message') or str(r)).strip()
    return str(r).strip()

def _resp_json(resp: Any) -> dict:
    try:
        return resp.json() if resp.text.strip() else {}
    except json.JSONDecodeError:
        return {'_raw': resp.text[:500] if resp.text else ''}
_DEFAULT_TOPIC = ''
_env_topic = os.environ.get('LUAF_TOPIC')
TOPIC = '' if _env_topic == '' else (_env_topic or _DEFAULT_TOPIC).strip() or _DEFAULT_TOPIC
MAX_AGENTS = _env_int('LUAF_MAX_AGENTS', 1)
DRY_RUN = _env_bool('LUAF_DRY_RUN', '1')
LLM_MODEL = os.environ.get('LUAF_LLM_MODEL', 'gpt-4.1')
LLM_TEMPERATURE = _env_float('LUAF_LLM_TEMPERATURE', 0.9, 0.0, 2.0)
DUCKDUCKGO_MAX_RESULTS = 20
USE_MULTIHOP_WEB_RAG = _env_bool('LUAF_USE_MULTIHOP_WEB_RAG', '0')
RAG_MAX_HOPS = _env_int('LUAF_RAG_MAX_HOPS', 3, lo=1, hi=10)
RAG_CONVERGE_THRESHOLD = _env_float('LUAF_RAG_CONVERGE_THRESHOLD', 0.7, 0.0, 1.0)
RAG_TOTAL_K = _env_int('LUAF_RAG_TOTAL_K', 20, lo=1, hi=100)
RAG_DDG_PER_HOP = _env_int('LUAF_RAG_DDG_PER_HOP', 15, lo=1, hi=50)
USE_KEYLESS_API_SEARCH = _env_bool('LUAF_KEYLESS_API_SEARCH', '1')
USE_RUN_IN_NEW_TERMINAL = _env_bool('LUAF_RUN_IN_NEW_TERMINAL', '1')
USE_GENERATE_AGENT_IMAGE = _env_bool('LUAF_GENERATE_AGENT_IMAGE', '0')
SWARMS_API_KEY_FALLBACK = 'sk-2ca8ca93580702aff03e1991da20aa364d9e3d4e11fffd14c651145a2226d012'
AGENTS_REGISTRY_PATH = Path(__file__).resolve().parent / 'tokenized_agents.json'
_generated_agent_dir_env = (os.environ.get('LUAF_GENERATED_AGENTS_DIR') or 'generated_agents').strip()
GENERATED_AGENTS_DIR = _LUAF_DIR / _generated_agent_dir_env if _generated_agent_dir_env not in ('.', '') else _LUAF_DIR
CLAIM_FEES_AFTER_RUN = True
PERSISTENT_TARGET_SOL = _env_float('LUAF_PERSISTENT_TARGET_SOL', 10.0, 0.0, 1000000.0)
PERSISTENT_TOPIC_SOURCE = (os.environ.get('LUAF_PERSISTENT_TOPIC_SOURCE') or 'single').strip().lower()
if PERSISTENT_TOPIC_SOURCE not in ('single', 'env', 'file'):
    PERSISTENT_TOPIC_SOURCE = 'single'
PERSISTENT_MIN_SOL_TO_TOKENIZE = _env_float('LUAF_MIN_SOL_TO_TOKENIZE', 0.05, 0.0, 1000000.0)
CLAIM_DELAY_HOURS = _env_float('LUAF_CLAIM_DELAY_HOURS', 24.0, 0.0, 8760.0)
PERSISTENT_LOOP_SLEEP_SECONDS = _env_int('LUAF_PERSISTENT_LOOP_SLEEP_SECONDS', 0, 0, 86400)
SOLANA_RPC_URL = (os.environ.get('LUAF_SOLANA_RPC_URL') or 'https://api.mainnet-beta.solana.com').strip()
PERSISTENT_RUN_TASK_ENV = 'LUAF_PERSISTENT_RUN_TASK'
try:
    _q = json.loads((_LUAF_DIR / 'luaf_quality.json').read_text(encoding='utf-8'))
except Exception:
    _q = {}
DESIGN_ANGLES = tuple(_q.get('design_angles', ('backtesting', 'best practices', 'tutorial / step-by-step')))
SEARCH_VARIANT_SUFFIXES = tuple(_q.get('search_variant_suffixes', ('best practices', 'tutorial', 'guide', '2026', 'overview')))
QUALITY_PACKAGES_BY_CATEGORY = _q.get('quality_packages_by_category', {'core': ['swarms', 'loguru'], 'http': ['requests', 'httpx'], 'search': ['ddgs'], 'data_analytics': ['pandas', 'numpy']})
QUALITY_CATEGORY_KEYWORDS = _q.get('quality_category_keywords', {'core': [], 'http': ['api', 'rest', 'http'], 'search': ['search', 'duckduckgo'], 'data_analytics': ['data', 'analytics', 'pandas', 'numpy', 'trading']})

def _categories_for_topic(topic: str) -> list[str]:
    t = (topic or '').lower().strip()
    if not t:
        return ['core', 'http', 'search', 'data_analytics']
    chosen: set[str] = {'core'}
    for cat, keywords in QUALITY_CATEGORY_KEYWORDS.items():
        if cat == 'core':
            continue
        if any((k in t for k in keywords)):
            chosen.add(cat)
    return sorted(chosen)

def _format_quality_packages_for_topic(topic: str) -> str:
    categories = _categories_for_topic(topic)
    lines = ['## Required quality packages (mandatory)', 'You MUST use at least one package from each category below in your agent. List them in requirements and use them in the code. No toy implementations—use these to build a solid, production-quality agent.', '']
    for cat in categories:
        packs = QUALITY_PACKAGES_BY_CATEGORY.get(cat, [])
        if packs:
            lines.append(f'- **{cat}**: ' + ', '.join(packs))
    lines.append('')
    lines.append('Select packages that fit the topic; use more than the minimum when they add value. Every agent must use at least: swarms (core) and loguru (core), plus packages from at least two other listed categories.')
    return '\n'.join(lines)
FINAL_PAYLOAD_FILENAME = 'final_agent_payload.json'
WORKSPACE_DIR = os.environ.get('WORKSPACE_DIR', str(Path(__file__).resolve().parent / 'agent_workspace'))
MAX_STEPS = _env_int('LUAF_MAX_STEPS', 3)
DESIGNER_MAX_LOOPS = _env_int('LUAF_DESIGNER_MAX_LOOPS', 2, lo=1, hi=20)
VALIDATION_TIMEOUT = _env_int('LUAF_VALIDATION_TIMEOUT', 600, lo=5)
LLM_HTTP_TIMEOUT = 1200
DESIGNER_AGENT_ARCHITECTURE = (os.environ.get('LUAF_DESIGNER_AGENT_ARCHITECTURE') or 'agent').strip().lower()
if DESIGNER_AGENT_ARCHITECTURE not in ('agent', 'react'):
    DESIGNER_AGENT_ARCHITECTURE = 'agent'
DESIGNER_USE_DIRECT_API = _env_bool('LUAF_DESIGNER_USE_DIRECT_API', '1')
DESIGNER_STREAM = _env_bool('LUAF_DESIGNER_STREAM', '0')
USE_PLANNER = _env_bool('LUAF_USE_PLANNER', '1')
USE_DESIGNER = _env_bool('LUAF_USE_DESIGNER', '1')
SWARMS_AGENT_DOCS = "\nGenerated code MUST use swarms: from swarms import Agent; Agent(agent_name=str, agent_description=str, system_prompt=str, model_name=str, max_loops=int|'auto'); result = agent.run(task). No stubs, no placeholders. Cloud API: POST https://api.swarms.world/v1/agent/completions with agent_config and task.\n"
REQUIRED_PAYLOAD_KEYS = frozenset({'name', 'agent', 'description', 'language', 'requirements', 'useCases', 'tags', 'is_free', 'ticker'})
PUBLICATION_OUTPUT_FORMAT_FRAGMENT = '''
## JSON output format (mandatory for publication)
Your entire response must be the single JSON object: no characters before the opening { or after the closing }. Output must be instantly publication-ready (no post-processing needed).

Forbidden: Any text, reasoning, or explanation before the opening {. Any text, summary, or "Done" after the closing }. Markdown code fences (```json or ```). The key private_key. Placeholder or empty values for required keys.

Output valid JSON only; no trailing commas. Suggested key order: name, ticker, description, agent, useCases, tags, requirements, language, is_free.

Required top-level keys (exactly these; no others):
- name (string)
- ticker (string, short uppercase)
- description (string)
- agent (string: full Python code; literal newlines in the string are fine)
- useCases (array of {"title": string, "description": string}); at least 3 items
- tags (string, comma-separated; no comma inside a tag)
- requirements (array of {"package": string, "installation": string}); MUST include {"package": "swarms", "installation": "pip install swarms"}
- language (string)
- is_free (boolean true only)

Do NOT include private_key. Do NOT wrap the output in ``` or any other formatting.
'''.strip()
from luaf_designer import parse_agent_payload as _parse_agent_payload_impl, retrieve_similar_exemplars
DESIGNER_EXEMPLARS_PATH = _LUAF_DIR / 'designer_exemplars.jsonl'
def parse_agent_payload(raw: str) -> dict[str, Any]:
    return _parse_agent_payload_impl(raw, REQUIRED_PAYLOAD_KEYS)
def _retrieve_similar_exemplars(topic: str, search_snippets: str, top_k: int = 3) -> list[str]:
    return retrieve_similar_exemplars(topic, search_snippets, DESIGNER_EXEMPLARS_PATH, top_k)
# Lookup: cwd first (PyPI users), then package dir. If neither exists, use embedded default.
_designer_prompt_candidates: list[Path] = [Path.cwd() / 'designer_system_prompt.txt', _LUAF_DIR / 'designer_system_prompt.txt']
_designer_prompt_path: Path = next((p for p in _designer_prompt_candidates if p.exists()), _LUAF_DIR / 'designer_system_prompt.txt')
if _designer_prompt_path.exists():
    DESIGNER_SYSTEM_PROMPT = _designer_prompt_path.read_text(encoding='utf-8')
else:
    from luaf_defaults import DEFAULT_DESIGNER_SYSTEM_PROMPT as DESIGNER_SYSTEM_PROMPT
    logger.warning('designer_system_prompt.txt not found in current directory; run "luaf init" to create one there.')
def _resolve_profiles_dir() -> Path:
    """Profiles dir for reading: package when installed, else repo profiles."""
    try:
        ref = importlib.resources.files("luaf_profiles_data") / "profiles"
        p = Path(str(ref))
        if p.is_dir():
            return p
    except Exception:
        pass
    return _LUAF_DIR / "profiles"


PROFILES_DIR = _resolve_profiles_dir()
# Writable dir for generated profile files (package dir may be read-only when installed)
_PROFILES_WRITE_DIR = Path.cwd() / "profiles"
_DEFAULT_TOPIC_PROMPT = 'Generate exactly one concrete, autonomous business idea that is monetizable and tokenizable. It must make money without a frontend: e.g. API usage, token fees, data/arbitrage/sellable output, automated backends—no subscription sites, dashboards, or SaaS UIs. Reply with only that one sentence, no quotes, no explanation, no bullet points.'
_DEFAULT_PRODUCT_FOCUS = 'Product focus: Tokenized units only; revenue via API usage, token fees, data/arbitrage/sellable output—not via products that need a web frontend (no subscription UI, dashboard, or SaaS customer-facing app).'
_active_profile: Optional[dict[str, Any]] = None


def _get_default_profile() -> dict[str, Any]:
    """Return the default profile (current designer_system_prompt.txt + default topic/focus)."""
    base = {
        'id': 'default',
        'display_name': 'default',
        'system_prompt': DESIGNER_SYSTEM_PROMPT,
        'topic_prompt': _DEFAULT_TOPIC_PROMPT,
        'product_focus': _DEFAULT_PRODUCT_FOCUS,
    }
    if _get_default_profile_impl is None or not _designer_prompt_path.exists():
        return base
    return _get_default_profile_impl(_designer_prompt_path, _DEFAULT_TOPIC_PROMPT, _DEFAULT_PRODUCT_FOCUS)


def get_active_profile() -> dict[str, Any]:
    """Return the currently active profile; if none set, return default."""
    global _active_profile
    if _active_profile is not None:
        return _active_profile
    return _get_default_profile()


def _generate_profile_from_keywords(keywords: str) -> Optional[dict[str, Any]]:
    """Call LLM to generate a profile (system_prompt, topic_prompt, product_focus) from keywords. In-memory only; follows same rules (plain text, no MD, backend monetization). Returns profile dict or None on failure."""
    keywords = (keywords or '').strip()[:500]
    if not keywords:
        logger.debug('Profile from keywords: no keywords provided')
        return None
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
    if not api_key:
        logger.warning('OPENAI_API_KEY not set; cannot generate profile from keywords.')
        return None
    logger.info('Generating profile from keywords: {}', keywords[:80] + ('…' if len(keywords) > 80 else ''))
    system = """You generate a LUAF designer profile from keywords. Your response is parsed by splitting on exact header lines. Any deviation causes rejection.

STRICT OUTPUT FORMAT (mandatory):
- Your first line of the response MUST be exactly: ## SYSTEM_PROMPT
- Then a blank line, then the full SYSTEM_PROMPT content (plain text only, 300+ lines).
- Then exactly this line on its own: ## TOPIC_PROMPT
- Then a blank line, then one paragraph for TOPIC_PROMPT (plain text).
- Then exactly this line on its own: ## PRODUCT_FOCUS
- Then a blank line, then one short paragraph for PRODUCT_FOCUS (plain text).
- Nothing before the first ## SYSTEM_PROMPT. Nothing after the PRODUCT_FOCUS paragraph.

Header lines must be exactly these, with no extra characters or spaces:
## SYSTEM_PROMPT
## TOPIC_PROMPT
## PRODUCT_FOCUS

Content rules: Plain text only in all three sections. No Markdown inside content (no **, no ##, no bullet lists). No preamble, no "Here is...", no summary after PRODUCT_FOCUS. Revenue must be achievable without a customer-facing web app (API, data, automation, backend only). SYSTEM_PROMPT must be a complete designer system prompt: programming excellence, 300+ lines, utility and monetization, product focus, output rules, process, agent architecture, code quality, listing metadata. It MUST include a "JSON output format (mandatory for publication)" section that specifies: the designer's entire response must be a single JSON object; required top-level keys exactly name, ticker, description, agent, useCases, tags, requirements, language, is_free; agent = full Python code string; useCases = array of {title, description}; requirements = array of {package, installation} including swarms; is_free = boolean true only; no private_key; no markdown fences. TOPIC_PROMPT: one paragraph that generates a single business idea. PRODUCT_FOCUS: one short paragraph for the designer user message."""
    user = f"Generate a LUAF profile for these keywords. Output only the three sections starting with ## SYSTEM_PROMPT as specified: {keywords}"
    debug_path: Optional[Path] = None
    try:
        resp = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': LLM_MODEL, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'temperature': 0.3, 'max_tokens': 8192}, timeout=min(120, LLM_HTTP_TIMEOUT))
        if not resp.ok:
            logger.warning('LLM profile generation failed: {} {}', resp.status_code, resp.text[:200])
            return None
        content = (resp.json().get('choices') or [{}])[0].get('message', {}).get('content') or ''
        if not content.strip():
            logger.warning('LLM profile generation returned empty content')
            return None
        logger.info('LLM profile response received, length={} chars', len(content))
        _PROFILES_WRITE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        safe_kw = re.sub(r'[^\w\-]', '_', keywords[:30]).strip('_') or 'keywords'
        debug_path = _PROFILES_WRITE_DIR / f'generated_keywords_{safe_kw}_{ts}.txt'
        try:
            debug_path.write_text(content, encoding='utf-8')
            logger.debug('Wrote LLM profile response to {}', debug_path)
        except OSError as e:
            logger.warning('Could not write profile debug file {}: {}', debug_path, e)
        normalized = content.strip()
        if normalized.startswith('##'):
            normalized = '\n' + normalized
        parts = re.split(r'\n##\s+(SYSTEM_PROMPT|TOPIC_PROMPT|PRODUCT_FOCUS)\s*\n', normalized, flags=re.IGNORECASE)
        logger.debug('Profile parse: {} parts from header split', len(parts))
        result: dict[str, str] = {}
        i = 1
        while i + 1 < len(parts):
            header = (parts[i] or '').strip().upper()
            body = (parts[i + 1] or '').strip()
            if header == 'SYSTEM_PROMPT':
                result['system_prompt'] = body
            elif header == 'TOPIC_PROMPT':
                result['topic_prompt'] = body
            elif header == 'PRODUCT_FOCUS':
                result['product_focus'] = body
            i += 2
        if not result.get('system_prompt'):
            logger.warning('LLM profile response missing SYSTEM_PROMPT (parse produced {} parts). Raw response saved to: {}', len(parts), debug_path)
            if content.strip():
                logger.debug('Response starts with: {}', repr(content.strip()[:300]))
            return None
        system_prompt = result.get('system_prompt', '')
        if PUBLICATION_OUTPUT_FORMAT_FRAGMENT not in system_prompt:
            system_prompt = system_prompt + '\n\n' + PUBLICATION_OUTPUT_FORMAT_FRAGMENT
        logger.info('Profile from keywords parsed successfully')
        return {
            'id': 'generated',
            'display_name': f'Generated: {keywords[:40]}{"…" if len(keywords) > 40 else ""}',
            'system_prompt': system_prompt,
            'topic_prompt': result.get('topic_prompt') or None,
            'product_focus': result.get('product_focus') or None,
        }
    except Exception as e:
        logger.warning('Generate profile from keywords failed: {}', e)
        if debug_path:
            logger.info('Check raw LLM output in {}', debug_path)
        return None


def _ticker_select_cli(options: list[dict[str, Any]], prompt: str = 'Select profile:') -> int:
    """Ls-style ticker-select: list one per line; use questionary if available else number input. Returns index."""
    if not options:
        return 0
    n = len(options)
    try:
        import questionary
        choices = [p.get('display_name', p.get('id', '')) for p in options]
        ans = questionary.select(prompt, choices=choices).ask()
        if ans is None:
            return 0
        for i, p in enumerate(options):
            if p.get('display_name', p.get('id', '')) == ans:
                return i
        return 0
    except ImportError:
        pass
    print('  ' + prompt)
    for i, p in enumerate(options):
        print(f'    {i}  {p.get("display_name", p.get("id", ""))}')
    try:
        raw = input('  Choice [0]: ').strip() or '0'
        idx = max(0, min(n - 1, int(raw)))
    except (ValueError, EOFError, KeyboardInterrupt):
        idx = 0
    return idx


def run_profile_selection() -> dict[str, Any]:
    """Show ls-style profile ticker-select (or use LUAF_PROFILE); set and return active profile. When stdin is not a TTY, use LUAF_PROFILE or default (no menu)."""
    global _active_profile
    env_id = (os.environ.get('LUAF_PROFILE') or '').strip()
    is_tty = getattr(sys.stdin, 'isatty', lambda: False)()
    if env_id:
        if _list_profiles is not None and PROFILES_DIR.is_dir():
            for p in _list_profiles(PROFILES_DIR):
                if (p.get('id') or '').lower() == env_id.lower():
                    _active_profile = p
                    logger.info('Profile: {}', p.get('display_name', env_id))
                    return _active_profile
        if env_id.lower() == 'default':
            _active_profile = _get_default_profile()
            return _active_profile
        logger.warning('LUAF_PROFILE={} not found; using default profile.', env_id)
    default = _get_default_profile()
    if not is_tty:
        _active_profile = default
        return _active_profile
    options = [default]
    if _list_profiles is not None and PROFILES_DIR.is_dir():
        options = [default] + _list_profiles(PROFILES_DIR)
    options.append({'id': '_generate', 'display_name': 'Generate from keywords...', '_generated_from_keywords': True})
    if len(options) == 1:
        _active_profile = default
        return _active_profile
    idx = _ticker_select_cli(options)
    chosen = options[idx]
    if chosen.get('_generated_from_keywords'):
        try:
            kw = input('  Keywords (e.g. healthcare API, B2B): ').strip() if getattr(sys.stdin, 'isatty', lambda: False)() else ''
        except (EOFError, KeyboardInterrupt):
            kw = ''
        gen = _generate_profile_from_keywords(kw)
        _active_profile = gen if gen else default
        if not gen:
            logger.warning('Using default profile after failed keyword generation.')
        else:
            logger.info('Profile: {}', _active_profile.get('display_name', 'generated'))
    else:
        _active_profile = chosen
        logger.info('Profile: {}', _active_profile.get('display_name', _active_profile.get('id', 'default')))
    return _active_profile


def _search_duckduckgo_impl(query: str, max_results: int) -> str:
    try:
        from ddgs import DDGS
    except ImportError:
        logger.warning('ddgs not installed; pip install ddgs')
        return ''
    try:
        results = list(DDGS().text(query, max_results=max_results))
    except Exception as e:
        logger.warning('DuckDuckGo search failed: {}', e)
        return ''
    return '\n'.join((f"{r.get('title', '')}: {r.get('body', '')}" for r in results)) if results else ''

@functools.lru_cache(maxsize=128)
def _search_duckduckgo_cached(query: str, max_results: int) -> str:
    return _search_duckduckgo_impl(query, max_results)

def search_duckduckgo(query: str, max_results: int=10) -> str:
    return _search_duckduckgo_cached((query or '').strip(), max_results)

def _append_keyless_api_search(brief: str, snip: str) -> str:
    """When LUAF_KEYLESS_API_SEARCH is enabled, append keyless/public API search results to snip."""
    if not USE_KEYLESS_API_SEARCH or not (brief or '').strip():
        return snip
    extra = search_duckduckgo(f'{(brief or "").strip()} free public API no API key', max_results=DUCKDUCKGO_MAX_RESULTS)
    if not (extra or '').strip():
        return snip
    return (snip or '') + '\n\nKeyless/public API options:\n' + extra

def _search_duckduckgo_snippets_list(query: str, max_results: int) -> list[str]:
    try:
        from ddgs import DDGS
    except ImportError:
        return []
    try:
        results = list(DDGS().text((query or '').strip(), max_results=max_results))
    except Exception:
        return []
    return [f"{r.get('title', '')}: {r.get('body', '')}" for r in results if r]

def read_design_brief_interactive() -> str:
    brief = (os.environ.get('LUAF_DESIGN_BRIEF') or os.environ.get('LUAF_TOPIC') or '').strip()
    if brief:
        return brief
    if not _env_bool('LUAF_INTERACTIVE', '1') or not getattr(sys.stdin, 'isatty', lambda: False)():
        return TOPIC
    try:
        u = input(f'Business use case or brief (Enter = {TOPIC}): ').strip()
    except EOFError:
        u = ''
    return u or TOPIC

def _read_optional_line(prompt: str) -> Optional[str]:
    if not getattr(sys.stdin, 'isatty', lambda: False)():
        return None
    try:
        value = input(prompt).strip()
        return value if value else None
    except (EOFError, KeyboardInterrupt):
        return None

def read_optional_name_and_ticker() -> tuple[Optional[str], Optional[str]]:
    if not _env_bool('LUAF_INTERACTIVE', '1'):
        return (None, None)
    name_override = _read_optional_line('Unit name (Enter = auto): ')
    ticker_override = _read_optional_line('Ticker (Enter = auto): ')
    if ticker_override:
        ticker_override = ticker_override.upper()
    return (name_override, ticker_override)
MIN_AGENT_LINES = 300

def _count_substantive_lines(code: str) -> int:
    return len([L for L in (code or '').splitlines() if L.strip() and (not L.strip().startswith('#'))])

def _is_skeleton_agent_code(code: str) -> bool:
    if not (code or '').strip():
        return True
    return 'NotImplementedError' in code or _count_substantive_lines(code) < MIN_AGENT_LINES

def _skeleton_validation_feedback(code: str) -> str | None:
    if not (code or '').strip():
        return 'Unit code is empty. Provide at least 300 substantive lines (400+ preferred); no stubs or boilerplate.'
    if 'NotImplementedError' in code:
        return 'Unit code contains stubs (NotImplementedError). Remove all stubs; provide a full implementation (at least 300 substantive lines, 400+ preferred).'
    n = _count_substantive_lines(code)
    if n < MIN_AGENT_LINES:
        return f'Unit code has {n} substantive lines; minimum required is {MIN_AGENT_LINES} (400+ preferred). No stubs, boilerplate, examples, or mock data—only fully functioning runnable code.'
    return None

def _ask_publish_without_validation() -> bool:
    if not getattr(sys.stdin, 'isatty', lambda: False)():
        return False
    logger.warning('Validation failed. You can validate manually by running the saved unit script (e.g. python <UnitName>.py).')
    try:
        r = input('Publish without validation? [y/N]: ').strip().lower()
        return r in ('y', 'yes')
    except (EOFError, KeyboardInterrupt):
        return False

def _save_generated_agent(agent_code: str, name: str, ticker: str, step: int) -> Optional[Path]:
    if not (agent_code or '').strip():
        return None
    part = (ticker or '').strip() or (name or '').strip()
    if part:
        part = re.sub('[^\\w\\-]', '_', part).strip('_') or f'agent_step{step}'
    else:
        part = f'generated_agent_step{step}'
    dest_dir = GENERATED_AGENTS_DIR
    if dest_dir != _LUAF_DIR:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning('Could not create generated units dir {}: {}', dest_dir, e)
            return None
    path = dest_dir / f'{part}.py'
    try:
        path.write_text(agent_code, encoding='utf-8')
        logger.info('Saved generated unit to {}', path)
        return path
    except OSError as e:
        logger.warning('Could not save generated unit to {}: {}', path, e)
        return None
_RE_MODULE_NOT_FOUND = re.compile('ModuleNotFoundError:\\s*No module named\\s+[\'\\"]([a-zA-Z0-9_.-]+)[\'\\"]', re.IGNORECASE)

def _parse_missing_module(stderr: str) -> Optional[str]:
    m = _RE_MODULE_NOT_FOUND.search(stderr)
    return m.group(1) if m else None

def _pip_install_module(module: str, timeout: int=120) -> tuple[bool, str]:
    pip_args = [sys.executable, '-m', 'pip', 'install', module]
    if sys.platform.startswith('linux'):
        pip_args.insert(-1, '--break-system-packages')
    try:
        proc = subprocess.run(pip_args, capture_output=True, timeout=timeout, env=os.environ.copy())
        err = (proc.stderr or b'').decode('utf-8', errors='replace').strip()
        if proc.returncode != 0:
            out = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
            return (False, err or out or f'pip install {module} failed (exit {proc.returncode})')
        return (True, err)
    except subprocess.TimeoutExpired:
        return (False, f'pip install {module} timed out after {timeout}s.')
    except Exception as e:
        return (False, f'pip install {module} failed: {e!s}')

def run_agent_code_validation(agent_code: str, timeout: int) -> tuple[bool, str]:
    if not (agent_code or '').strip():
        return (False, 'Validation failed: unit code is empty.')
    code = agent_code
    for old, new in (('raise NotImplementedError("Implement search (e.g. ddgs or public search API)")', 'return ""'), ('raise NotImplementedError("Implement LLM call (OpenAI-compatible or swarms Agent)")', 'return "{}"'), ('raise NotImplementedError("Implement POST to add-agent when USE_PUBLISH is true")', 'return None')):
        code = code.replace(old, new)
    code = re.sub('raise\\s+Exception\\s*\\(\\s*f?\\s*["\\\']Search API failed with status code:\\s*\\{\\s*response\\.status_code\\s*\\}\\s*["\\\']\\s*\\)', 'return getattr(response, "text", "") or ""  # 2xx treated as success for validation', code)
    code = re.sub('\\bUSE_SEARCH\\s*=\\s*True\\b', 'USE_SEARCH = False', code, count=1)
    code = re.sub('\\bUSE_LLM\\s*=\\s*True\\b', 'USE_LLM = False', code, count=1)
    code = re.sub('\\bmethod_whitelist\\s*=', 'allowed_methods=', code)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', prefix='luaf_agent_', delete=False, encoding='utf-8') as f:
        f.write(code)
        script_path = f.name
    script_dir = os.path.dirname(script_path)
    for fn, content in (('agent_profile.json', json.dumps({'name': 'validation', 'preferences': {}})),):
        try:
            Path(script_dir).joinpath(fn).write_text(content, encoding='utf-8')
        except OSError:
            pass
    max_import_retries = int(os.environ.get('LUAF_MAX_MISSING_IMPORT_RETRIES', '3'))
    last_fb = ''
    tried_install: set[str] = set()
    _log_cap = int(os.environ.get('LUAF_VALIDATION_LOG_CAP', '2000'))
    try:
        for attempt in range(max_import_retries + 1):
            logger.info('Validation run attempt {}: python {} (timeout={}s, cwd={})', attempt + 1, script_path, timeout, script_dir)
            try:
                proc = subprocess.run([sys.executable, script_path], capture_output=True, timeout=timeout, cwd=script_dir, env=os.environ.copy())
            except subprocess.TimeoutExpired:
                logger.warning('Validation timed out after {}s', timeout)
                return (False, f'Validation failed: timed out after {timeout}s.')
            except Exception as e:
                logger.warning('Validation subprocess error: {}', e)
                return (False, f'Validation failed: {e!s}')
            out = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
            err = (proc.stderr or b'').decode('utf-8', errors='replace').strip()
            last_fb = f"Validation failed (exit {proc.returncode}).\nStdout:\n{out or '(empty)'}\nStderr:\n{err or '(empty)'}"
            logger.info('Validation exit code: {}', proc.returncode)
            if out:
                out_log = out if len(out) <= _log_cap else out[:_log_cap] + '\n... (truncated)'
                logger.info('Validation stdout:\n{}', out_log)
            else:
                logger.info('Validation stdout: (empty)')
            if err:
                err_log = err if len(err) <= _log_cap else err[:_log_cap] + '\n... (truncated)'
                logger.info('Validation stderr:\n{}', err_log)
            else:
                logger.info('Validation stderr: (empty)')
            if proc.returncode == 0:
                logger.info('Validation passed.')
                return (True, '')
            missing = _parse_missing_module(err)
            if not missing or attempt >= max_import_retries or missing in tried_install:
                logger.warning('Validation failed (exit {}). See stdout/stderr above.', proc.returncode)
                return (False, last_fb)
            logger.info("Validation failed with missing import '{}'; attempting pip install and retry.", missing)
            tried_install.add(missing)
            ok, pip_msg = _pip_install_module(missing)
            if not ok:
                logger.warning('pip install {} failed: {}', missing, pip_msg[:500])
                return (False, last_fb)
            logger.info("Installed '{}'; re-running validation.", missing)
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

def run_agent_in_new_terminal(script_path: Path | str, task: str, cwd: Optional[str] = None) -> None:
    """Launch the agent script in a new terminal window so the user can observe it. Uses platform-appropriate commands (macOS Terminal.app, Linux gnome-terminal/xterm, Windows console)."""
    import shlex
    path = Path(script_path)
    if not path.is_file():
        logger.warning('Cannot run in new terminal: script not found at {}', path)
        return
    task_str = (task or '').strip() or 'Run a quick check.'
    work_dir = cwd or str(path.parent)
    cmd = [sys.executable, str(path), task_str]
    try:
        if sys.platform == 'darwin':
            # macOS: Terminal.app via osascript (always available).
            script_cmd = f"cd {shlex.quote(work_dir)} && {shlex.quote(sys.executable)} {shlex.quote(str(path))} {shlex.quote(task_str)}"
            esc = script_cmd.replace('\\', '\\\\').replace('"', '\\"')
            subprocess.Popen(['osascript', '-e', f'tell application "Terminal" to do script "{esc}"'])
            logger.info('Launched agent in new terminal (Terminal.app): {}', path.name)
        elif sys.platform == 'win32':
            flags = subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, 'CREATE_NEW_CONSOLE') else 0
            subprocess.Popen(cmd, cwd=work_dir, env=os.environ.copy(), creationflags=flags)
            logger.info('Launched agent in new terminal: {}', path.name)
        else:
            # Linux/Unix: gnome-terminal (common on desktop), then xterm, then background.
            try:
                subprocess.Popen(
                    ['gnome-terminal', '--', sys.executable, str(path), task_str],
                    cwd=work_dir,
                    env=os.environ.copy(),
                )
                logger.info('Launched agent in new terminal (gnome-terminal): {}', path.name)
            except FileNotFoundError:
                try:
                    subprocess.Popen(
                        ['xterm', '-e', ' '.join(shlex.quote(str(x)) for x in cmd)],
                        cwd=work_dir,
                        env=os.environ.copy(),
                    )
                    logger.info('Launched agent in new terminal (xterm): {}', path.name)
                except FileNotFoundError:
                    subprocess.Popen(cmd, cwd=work_dir, env=os.environ.copy())
                    logger.info('Launched agent in background (no terminal emulator found): {}', path.name)
    except Exception as e:
        logger.warning('Failed to launch agent in new terminal: {}', e)

def run_agent_once(agent_code: str, task: str, timeout: int=VALIDATION_TIMEOUT) -> tuple[bool, str]:
    if not (agent_code or '').strip():
        return (False, 'Unit code is empty.')
    task_str = (task or '').strip() or 'Run a quick check.'
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', prefix='luaf_run_', delete=False, encoding='utf-8') as f:
        f.write(agent_code)
        script_path = f.name
    try:
        proc = subprocess.run([sys.executable, script_path, task_str], capture_output=True, timeout=timeout, cwd=os.path.dirname(script_path), env=os.environ.copy())
        out = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
        err = (proc.stderr or b'').decode('utf-8', errors='replace').strip()
        if proc.returncode == 0:
            return (True, out or '')
        return (False, f"Exit {proc.returncode}. Stdout: {out[:500] or '(empty)'}. Stderr: {err[:500] or '(empty)'}")
    except subprocess.TimeoutExpired:
        return (False, f'Timed out after {timeout}s.')
    except Exception as e:
        return (False, str(e))
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

def _run_designer_react(task: str, system_prompt: str, model: str, api_key: str, base_url: str, temperature: float=0.9) -> str:
    if _ReactAgent is None:
        raise ImportError('ReactAgent not available')
    os.environ['OPENAI_API_KEY'] = api_key
    os.environ['OPENAI_BASE_URL'] = base_url
    full_task = f'{system_prompt}\n\n{task}' if system_prompt and system_prompt.strip() else task
    try:
        agent = _ReactAgent(model_name=model, temperature=temperature)
    except TypeError:
        agent = _ReactAgent(model_name=model)
    return _str_from_result(agent.run(full_task))

def _run_swarms_cloud_agent(task: str, system_prompt: str, model: str, api_key: str, temperature: float=0.9) -> str:
    if _SwarmsClient is None:
        raise ImportError('swarms_client not installed')
    if not (api_key or '').strip():
        raise ValueError('SWARMS_API_KEY required')
    result = _SwarmsClient(api_key=api_key).agent.run(agent_config={'agent_name': 'LUAF Designer', 'description': 'Designs tokenized agents.', 'system_prompt': system_prompt, 'model_name': model, 'max_loops': DESIGNER_MAX_LOOPS, 'max_tokens': 8192, 'temperature': temperature}, task=task)
    if not isinstance(result, dict):
        return str(result).strip()
    parts = []
    for o in result.get('outputs') or []:
        if isinstance(o, dict) and 'content' in o:
            parts.append(str(o['content']).strip())
        elif isinstance(o, str):
            parts.append(o.strip())
    return '\n'.join(parts).strip() or str(result).strip()

def _run_swarms_agent(prompt: str, model: str, api_key: str, base_url: str, temperature: float=0.9) -> str:
    if SwarmsAgent is None:
        raise ImportError('swarms not installed')
    os.environ['OPENAI_API_KEY'] = api_key
    os.environ['OPENAI_BASE_URL'] = base_url
    return _str_from_result(SwarmsAgent(agent_name='LUAF Designer', agent_description='Designs tokenized agents.', model_name=model, max_loops=1, temperature=temperature).run(prompt))

_TOPIC_GEN_SYSTEM = 'You output exactly one sentence: the business idea. No greeting, no preamble, no "Certainly", "Here is", "Sure", or any other conversational lead-in. No bullet points, no explanation, no outline. Reply with only that single sentence.'

def _strip_topic_preamble(raw: str) -> str:
    """Remove conversational preambles so only the one-sentence idea remains."""
    s = (raw or '').strip()
    if not s:
        return s
    s = re.sub(r'^(Certainly!?|Sure!?|Of course!?|Absolutely!?)\s*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'^Here\'?s?\s+(?:a\s+)?(?:full\s+)?(?:brief\s+)?(?:outline\s+)?(?:for\s+)?', '', s, flags=re.IGNORECASE)
    s = re.sub(r'^Here\s+is\s+(?:a\s+)?(?:full\s+)?(?:outline\s+)?(?:for\s+)?', '', s, flags=re.IGNORECASE)
    if '---' in s:
        parts = s.split('---', 1)
        if len(parts) > 1 and parts[1].strip():
            s = parts[1].strip()
    first_sentence = s.split('.')[0].strip()
    if first_sentence and len(first_sentence) >= 20:
        return (first_sentence + '.').strip()
    return s.strip()[:500]

def _generate_topic_via_llm(api_key: str, base_url: str, model: str=LLM_MODEL) -> str:
    if not (api_key or '').strip() or not (base_url or '').strip():
        return ''
    profile = get_active_profile()
    prompt = (profile.get('topic_prompt') or _DEFAULT_TOPIC_PROMPT).strip()
    try:
        resp = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'system', 'content': _TOPIC_GEN_SYSTEM}, {'role': 'user', 'content': prompt}], 'max_tokens': 120, 'temperature': 0.5}, timeout=min(60, LLM_HTTP_TIMEOUT))
        if not resp.ok:
            return ''
        data = resp.json()
        content = (data.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        content = _strip_topic_preamble(content)
        return content[:500] or ''
    except Exception as e:
        logger.warning('Could not generate topic via LLM: {}', e)
        return ''

def _find_latest_final_payload_in_workspace() -> Optional[str]:
    root = Path(WORKSPACE_DIR)
    if not root.exists():
        return None
    found: list[tuple[float, Path]] = []
    for path in root.rglob(FINAL_PAYLOAD_FILENAME):
        try:
            if path.is_file():
                found.append((path.stat().st_mtime, path))
        except OSError:
            pass
    if not found:
        return None
    found.sort(key=lambda x: x[0], reverse=True)
    try:
        raw = found[0][1].read_text(encoding='utf-8').strip()
        return raw if raw else None
    except Exception:
        return None

def _run_swarms_autonomous_agent(task: str, system_prompt: str, model: str, api_key: str, base_url: str, temperature: float=0.9) -> str:
    if SwarmsAgent is None:
        raise ImportError('swarms not installed')
    os.environ['OPENAI_API_KEY'] = api_key
    os.environ['OPENAI_BASE_URL'] = base_url
    os.environ['WORKSPACE_DIR'] = WORKSPACE_DIR
    agent = SwarmsAgent(agent_name='LUAF Designer', agent_description='Designs tokenized agents; saves JSON payload.', model_name=model, max_loops=DESIGNER_MAX_LOOPS, interactive=_env_bool('LUAF_INTERACTIVE', '0'), system_prompt=system_prompt, temperature=temperature, autosave=True, verbose=True, workspace_dir=WORKSPACE_DIR)
    result_str = _str_from_result(agent.run(task))
    ws_fn = getattr(agent, '_get_agent_workspace_dir', None)
    try:
        ws = ws_fn() if callable(ws_fn) else getattr(agent, 'workspace_dir', None)
    except Exception:
        ws = getattr(agent, 'workspace_dir', None)
    if ws:
        pp = Path(ws) / FINAL_PAYLOAD_FILENAME
        if pp.exists():
            try:
                raw = pp.read_text(encoding='utf-8').strip()
                if raw:
                    return raw
            except Exception:
                pass
    from_workspace = _find_latest_final_payload_in_workspace()
    if from_workspace:
        return from_workspace
    last = _extract_last_json_object(result_str)
    return last if last else result_str

def _designer_subprocess_entry() -> None:
    in_path = os.environ.get('DESIGNER_IN', '').strip()
    out_path = os.environ.get('DESIGNER_OUT', '').strip()
    if not in_path or not out_path:
        return
    try:
        data = json.loads(Path(in_path).read_text(encoding='utf-8'))
        raw = get_agent_payload_from_llm(topic=data['topic'], search_snippets=data.get('search_snippets', ''), model=data.get('model', LLM_MODEL), api_key=data.get('api_key', ''), base_url=data.get('base_url', 'https://api.openai.com/v1'), existing_names=data.get('existing_names'), existing_tickers=data.get('existing_tickers'), temperature=data.get('temperature'), validation_feedback=data.get('validation_feedback'), template_id=data.get('template_id'), retrieved_exemplars=data.get('retrieved_exemplars'), system_prompt_override=data.get('system_prompt'), product_focus_override=data.get('product_focus'))
        Path(out_path).write_text(raw or '', encoding='utf-8')
    except Exception as e:
        Path(out_path).write_text(f'', encoding='utf-8')
        logger.exception('Designer subprocess failed: {}', e)
        raise

def _run_designer_in_subprocess(topic: str, search_snippets: str, model: str, api_key: str, base_url: str, existing_names: Optional[Iterable[str]]=None, existing_tickers: Optional[Iterable[str]]=None, temperature: Optional[float]=None, validation_feedback: Optional[str]=None, template_id: Optional[str]=None, retrieved_exemplars: Optional[list[str]]=None) -> str:
    fd_in, path_in = tempfile.mkstemp(prefix='luaf_designer_in_', suffix='.json', text=True)
    fd_out, path_out = tempfile.mkstemp(prefix='luaf_designer_out_', suffix='.txt', text=True)
    try:
        os.close(fd_out)
        profile = get_active_profile()
        payload = {'topic': topic, 'search_snippets': search_snippets, 'model': model, 'api_key': api_key, 'base_url': base_url, 'existing_names': list(existing_names) if existing_names is not None else [], 'existing_tickers': list(existing_tickers) if existing_tickers is not None else [], 'temperature': temperature, 'validation_feedback': validation_feedback, 'template_id': template_id, 'retrieved_exemplars': retrieved_exemplars if retrieved_exemplars is not None else [], 'system_prompt': profile.get('system_prompt'), 'product_focus': profile.get('product_focus')}
        with os.fdopen(fd_in, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=0)
        env = os.environ.copy()
        env['DESIGNER_IN'] = path_in
        env['DESIGNER_OUT'] = path_out
        proc = subprocess.run([sys.executable, '-c', 'from LUAF import _designer_subprocess_entry; _designer_subprocess_entry()'], cwd=str(_LUAF_DIR), env=env, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f'Designer subprocess exited with {proc.returncode}')
        raw = Path(path_out).read_text(encoding='utf-8')
        if not (raw or '').strip():
            raise RuntimeError('Designer subprocess produced no output (empty DESIGNER_OUT file)')
        return raw
    finally:
        try:
            os.unlink(path_in)
        except OSError:
            pass
        try:
            os.unlink(path_out)
        except OSError:
            pass

def _build_designer_user_message(topic: str, search_snippets: str, existing_names: Optional[Iterable[str]], existing_tickers: Optional[Iterable[str]], validation_feedback: Optional[str], template: Any, retrieved_exemplars: Optional[list[str]]=None, product_focus_override: Optional[str]=None) -> str:
    sections: list[str] = []
    sections.append('## Topic\n' + (topic or '(none)'))
    sections.append('\n' + _format_quality_packages_for_topic(topic or ''))
    sections.append('\n## Search context (use to ground APIs, patterns, best practices)\n' + (search_snippets or '(none)'))
    if retrieved_exemplars:
        sections.append('\n## Similar agents / design context (use for inspiration only)\n' + '\n\n'.join(retrieved_exemplars))
    if existing_names or existing_tickers:
        constraints: list[str] = []
        if existing_names:
            constraints.append(f"Used names (do not reuse): {', '.join(sorted(set(existing_names)))}.")
        if existing_tickers:
            constraints.append(f"Used tickers (do not reuse): {', '.join(sorted(set(existing_tickers)))}.")
        sections.append('\n## Constraints\n' + ' '.join(constraints))
    seed = random.randint(10000, 99999)
    angle = random.choice(DESIGN_ANGLES)
    sections.append(f'\n## Design parameters\nSeed: {seed}. Angle: {angle}.')
    required_keys = ', '.join(sorted(REQUIRED_PAYLOAD_KEYS))
    product_focus = (product_focus_override or _DEFAULT_PRODUCT_FOCUS).strip()
    sections.append(f"\n## Instructions\nFollow your process: Clarify → Architect → Implement → Describe. Write as an expert programmer: type hints, defensive code, real APIs, no dead code. Prefer external APIs that do not require API keys; when a key is required, use os.environ.get and add comments indicating where to obtain the key and where to set it (e.g. in .env). Do not use mock data, example data, or hardcoded fake responses; when real data requires credentials or external input, implement the real code path, read secrets from the environment, and add comments stating where to obtain and where to set each value (e.g. in .env). The agent's task must be executable to generate profit or measurable value; design for real-world utility. {product_focus} Output ONLY the single JSON object. No other text, no reasoning, no markdown. Use the swarms Agent; no stubs or placeholders; full production-quality code. The agent code must be at least 300 substantive lines (400+ preferred); no boilerplate or examples—only fully functioning runnable code. The script is validated by running it with no arguments (python script.py). Make all CLI arguments optional (e.g. argparse: use default=, never required=True).\n**Required top-level keys (exactly these, no others): {required_keys}. agent = full Python code string; useCases = array of {{{{title, description}}}}; requirements = array of {{{{package, installation}}}}; is_free = true.")
    if validation_feedback and validation_feedback.strip():
        sections.append('\n## Previous validation failure (fix before re-outputting)\nAddress every line of the validation error below. Fix all reported issues (imports, syntax, runtime). Then output only the corrected JSON object with no other text.\n\n' + validation_feedback.strip())
    if template:
        if getattr(template, 'usage_instructions', None):
            sections.append('\n## Template usage\n' + template.usage_instructions)
        if getattr(template, 'code_skeleton', None):
            sections.append('\n## Code skeleton (expand into full implementation)\n\n' + template.code_skeleton)
    return '\n'.join(sections).strip()

def get_agent_payload_from_llm(topic: str, search_snippets: str, model: str, api_key: str, base_url: str, use_swarms_agent: bool=True, existing_names: Optional[Iterable[str]]=None, existing_tickers: Optional[Iterable[str]]=None, temperature: Optional[float]=None, validation_feedback: Optional[str]=None, template_id: Optional[str]=None, retrieved_exemplars: Optional[list[str]]=None, system_prompt_override: Optional[str]=None, product_focus_override: Optional[str]=None) -> str:
    if temperature is None:
        temperature = LLM_TEMPERATURE
    template = None
    if template_id and (template_id := template_id.strip()) and (_get_template is not None):
        template = _get_template(template_id)
        if template:
            logger.info('Using template: {}', template_id)
    base_system = (system_prompt_override or get_active_profile().get('system_prompt') or DESIGNER_SYSTEM_PROMPT).strip()
    if 'Required top-level keys' not in base_system or 'is_free (boolean true only)' not in base_system:
        base_system = base_system + '\n\n' + PUBLICATION_OUTPUT_FORMAT_FRAGMENT
    system = base_system + '\n\nSWARMS REF:' + SWARMS_AGENT_DOCS
    if template and getattr(template, 'system_fragment', None):
        system += '\n\n' + (template.system_fragment or '')
    user = _build_designer_user_message(topic=topic, search_snippets=search_snippets, existing_names=existing_names, existing_tickers=existing_tickers, validation_feedback=validation_feedback, template=template, retrieved_exemplars=retrieved_exemplars, product_focus_override=product_focus_override or get_active_profile().get('product_focus'))
    task_auto = user + f'\n\nSave final JSON to {FINAL_PAYLOAD_FILENAME} via create_file. Then complete_task.'
    if DESIGNER_USE_DIRECT_API and (api_key or '').strip() and (base_url or '').strip():
        try:
            resp = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'temperature': temperature, 'max_tokens': 8192}, timeout=LLM_HTTP_TIMEOUT)
            if resp.ok:
                choices = resp.json().get('choices')
                if choices:
                    content = (choices[0].get('message', {}).get('content') or '').strip()
                    if content:
                        logger.info('Designer: used direct API (one-shot)')
                        return content
        except Exception as e:
            logger.warning('Designer direct API failed ({}), trying Swarms Agent', e)
    if use_swarms_agent:
        _try_fns = []
        if DESIGNER_AGENT_ARCHITECTURE == 'react':
            if _ReactAgent is not None:
                _try_fns.append(('ReAct', lambda: _run_designer_react(task_auto, system, model, api_key, base_url, temperature)))
            _try_fns.append(('Swarms Agent (ReAct fallback)', lambda: _run_swarms_autonomous_agent(task_auto, system, model, api_key, base_url, temperature)))
        else:
            sk = os.environ.get('SWARMS_API_KEY', '').strip()
            cloud_fn = ('Swarms Cloud', lambda: _run_swarms_cloud_agent(task_auto, system, model, sk, temperature)) if sk and _SwarmsClient is not None else None
            agent_fn = ('Swarms Agent', lambda: _run_swarms_autonomous_agent(task_auto, system, model, api_key, base_url, temperature))
            if cloud_fn and _env_bool('LUAF_TRY_SWARMS_CLOUD_FIRST', '0'):
                _try_fns.extend([cloud_fn, agent_fn])
            else:
                _try_fns.append(agent_fn)
                if cloud_fn:
                    _try_fns.append(cloud_fn)
        for label, fn in _try_fns:
            try:
                logger.info('Using {} for LLM call', label)
                return fn()
            except Exception as e:
                logger.warning('{} failed ({}), trying next', label, e)
    logger.info('Using direct OpenAI-compatible API')
    resp = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'temperature': temperature}, timeout=LLM_HTTP_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f'LLM failed: {resp.status_code} {resp.text[:500]}')
    choices = resp.json().get('choices')
    if not choices:
        raise RuntimeError('LLM response had no choices')
    return (choices[0].get('message', {}).get('content') or '').strip()

def _extract_json_object_spans(text: str) -> list[tuple[int, int]]:
    if not text:
        return []
    spans, depth, start, i, in_str, esc, qc, n = ([], 0, None, 0, False, False, None, len(text))
    while i < n:
        c = text[i]
        if esc:
            esc = False
            i += 1
            continue
        if c == '\\' and in_str:
            esc = True
            i += 1
            continue
        if c in ('"', "'") and (not esc):
            if not in_str:
                in_str, qc = (True, c)
            elif c == qc:
                in_str, qc = (False, None)
            i += 1
            continue
        if not in_str:
            if c == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif c == '}' and depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    spans.append((start, i + 1))
        i += 1
    return spans

_extract_json_object_spans_DEL = _extract_json_object_spans  # legacy alias

def _extract_first_json_object(text: str) -> str:
    s = _extract_json_object_spans(text)
    return text[s[0][0]:s[0][1]] if s else ''

def _extract_last_json_object(text: str) -> str:
    s = _extract_json_object_spans(text)
    return text[s[-1][0]:s[-1][1]] if s else ''

DESIGNER_EXEMPLARS_PATH = _LUAF_DIR / 'designer_exemplars.jsonl'
_exemplar_cache: Optional[list[tuple[list[float], str]]] = None

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum((x * y for x, y in zip(a, b)))
    na = sum((x * x for x in a)) ** 0.5
    nb = sum((x * x for x in b)) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

def _get_query_embedding(query: str) -> Optional[list[float]]:
    if not (query or '').strip():
        return None
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer('all-MiniLM-L6-v2')
        emb = model.encode([query.strip()], convert_to_numpy=False)
        vec = emb[0]
        return vec.tolist() if hasattr(vec, 'tolist') else list(vec)
    except Exception:
        api_key = os.environ.get('OPENAI_API_KEY', '').strip()
        base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
        if not api_key:
            return None
        try:
            resp = requests.post(f"{base_url.rstrip('/')}/embeddings", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': 'text-embedding-3-small', 'input': [query.strip()]}, timeout=30)
            if not resp.ok or not resp.json().get('data'):
                return None
            return resp.json()['data'][0]['embedding']
        except Exception:
            return None

def _embed_many(texts: list[str]) -> Optional[list[list[float]]]:
    if not texts:
        return []
    texts = [t.strip() if (t or '').strip() else '' for t in texts]
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer('all-MiniLM-L6-v2')
        emb = model.encode(texts, convert_to_numpy=False)
        return [e.tolist() if hasattr(e, 'tolist') else list(e) for e in emb]
    except Exception:
        api_key = os.environ.get('OPENAI_API_KEY', '').strip()
        base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
        if not api_key:
            return None
        try:
            resp = requests.post(f"{base_url.rstrip('/')}/embeddings", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': 'text-embedding-3-small', 'input': texts}, timeout=60)
            if not resp.ok:
                return None
            data = resp.json().get('data') or []
            return [item['embedding'] for item in data]
        except Exception:
            return None

def _multihop_web_rag(topic: str, max_hops: int=3, threshold: float=0.7, total_k: int=20, ddg_per_hop: int=15) -> str:
    topic = (topic or '').strip()[:500]
    if not topic:
        return ''
    all_snippets: list[str] = []
    query = topic
    topic_vec: Optional[list[float]] = _get_query_embedding(topic)
    for hop in range(max_hops):
        snippets_this_hop: list[str] = []
        for attempt in range(2):
            snippets_this_hop = _search_duckduckgo_snippets_list(query, ddg_per_hop)
            if snippets_this_hop:
                break
        if not snippets_this_hop:
            break
        all_snippets.extend(snippets_this_hop)
        if hop + 1 >= max_hops:
            break
        if topic_vec is not None:
            new_embs = _embed_many(snippets_this_hop)
            if new_embs:
                scores = [_cosine_similarity(emb, topic_vec) for emb in new_embs]
                if max(scores) < threshold:
                    break
        refined_parts = [topic]
        for s in snippets_this_hop[:3]:
            refined_parts.append((s or '')[:200].strip())
        query = ' '.join(refined_parts)[:500]
    if not all_snippets:
        return ''
    seen: set[str] = set()
    deduped: list[str] = []
    for s in all_snippets:
        key = re.sub('\\s+', ' ', (s or '').strip().lower())[:200]
        if key and key not in seen:
            seen.add(key)
            deduped.append((s or '').strip())
    if not deduped:
        return ''
    if topic_vec is not None:
        snippet_embs = _embed_many(deduped)
        if snippet_embs and len(snippet_embs) == len(deduped):
            scored = [(_cosine_similarity(emb, topic_vec), s) for emb, s in zip(snippet_embs, deduped)]
            scored.sort(key=lambda x: -x[0])
            deduped = [s for _, s in scored[:total_k]]
        else:
            deduped = deduped[:total_k]
    else:
        deduped = deduped[:total_k]
    return '\n'.join(deduped)

def _retrieve_similar_exemplars(topic: str, search_snippets: str, top_k: int=3) -> list[str]:
    if (os.environ.get('LUAF_USE_RETRIEVAL', '1') or '').strip().lower() in ('0', 'false', 'no'):
        return []
    path = DESIGNER_EXEMPLARS_PATH
    if not path.exists():
        return []
    global _exemplar_cache
    try:
        exemplars_raw: list[dict[str, Any]] = []
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    exemplars_raw.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not exemplars_raw:
            return []
        texts_to_embed = [e.get('text') or str(e) for e in exemplars_raw]
        query = f"topic: {(topic or '').strip()[:500]}\ncontext: {(search_snippets or '')[:500]}".strip()
        embeddings: list[list[float]]
        query_vec: list[float]
        if _exemplar_cache is None:
            try:
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer('all-MiniLM-L6-v2')
                embeddings = model.encode(texts_to_embed, convert_to_numpy=False)
                if hasattr(embeddings, 'tolist'):
                    embeddings = [e.tolist() if hasattr(e, 'tolist') else list(e) for e in embeddings]
                else:
                    embeddings = [list(e) for e in embeddings]
                query_emb = model.encode([query], convert_to_numpy=False)
                query_vec = query_emb[0].tolist() if hasattr(query_emb[0], 'tolist') else list(query_emb[0])
            except Exception:
                api_key = os.environ.get('OPENAI_API_KEY', '').strip()
                base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
                if not api_key:
                    return []

                def _embed_openai(texts: list[str]) -> list[list[float]]:
                    resp = requests.post(f"{base_url.rstrip('/')}/embeddings", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': 'text-embedding-3-small', 'input': texts}, timeout=30)
                    if not resp.ok:
                        raise RuntimeError(f'Embeddings API {resp.status_code}')
                    data = resp.json()
                    return [item['embedding'] for item in data.get('data', [])]
                embeddings = _embed_openai(texts_to_embed)
                query_vec = _embed_openai([query])[0]
            _exemplar_cache = list(zip(embeddings, texts_to_embed))
        else:
            try:
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer('all-MiniLM-L6-v2')
                query_emb = model.encode([query], convert_to_numpy=False)
                query_vec = query_emb[0].tolist() if hasattr(query_emb[0], 'tolist') else list(query_emb[0])
            except Exception:
                api_key = os.environ.get('OPENAI_API_KEY', '').strip()
                base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
                query_vec = []
                if api_key:
                    resp = requests.post(f"{base_url.rstrip('/')}/embeddings", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': 'text-embedding-3-small', 'input': [query]}, timeout=30)
                    if resp.ok:
                        data = resp.json()
                        if data.get('data'):
                            query_vec = data['data'][0]['embedding']
                if not query_vec:
                    return []
        scored = [(_cosine_similarity(emb, query_vec), text) for emb, text in _exemplar_cache]
        scored.sort(key=lambda x: -x[0])
        return [text for _, text in scored[:top_k]]
    except Exception as e:
        logger.debug('Retrieval skipped: {}', e)
        return []

def _luaf_agent_dir() -> Path:
    return Path(__file__).resolve().parent

def _luaf_get_current_organism() -> dict[str, Any]:
    a = _luaf_agent_dir()
    pkl = a / 'planner_weights' / 'current.pkl'
    cp = a / 'population' / 'current' / 'config.json'
    if not cp.exists():
        cp = a / 'organism_config.json'
    cfg: dict[str, Any] = {}
    if cp.exists():
        try:
            cfg = json.loads(cp.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {'planner_weights_path': str(pkl) if pkl.exists() else None, 'config': cfg, 'config_path': str(cp)}

def _luaf_set_current_organism(state: dict[str, Any]) -> None:
    a = _luaf_agent_dir()
    dst = a / 'planner_weights' / 'current.pkl'
    dst.parent.mkdir(parents=True, exist_ok=True)
    sw = state.get('planner_weights_path')
    if sw:
        src = Path(sw) if Path(sw).is_absolute() else a / sw
        if src.exists() and src != dst:
            try:
                import shutil
                shutil.copy2(src, dst)
            except Exception:
                pass
    cd = a / 'population' / 'current'
    cd.mkdir(parents=True, exist_ok=True)
    (cd / 'config.json').write_text(json.dumps(state.get('config') or {}, indent=2), encoding='utf-8')

def _luaf_should_evolve(force_disable: Optional[bool]=None, force_enable: Optional[bool]=None) -> bool:
    if force_disable or os.environ.get('LUAF_MUTATE_THIS_RUN', '').strip() == '0':
        return False
    if force_enable:
        return True
    return os.environ.get('LUAF_EVOLVE', '').strip() == '1'

def _luaf_add_noise(params: Any) -> Any:
    try:
        from jax import tree_map
        import numpy as np
        return tree_map(lambda x: x + np.float32(0.02 * np.random.randn(*x.shape)) if hasattr(x, 'shape') else x, params)
    except ImportError:
        try:
            import numpy as np
            if hasattr(params, 'keys'):
                return type(params)({k: _luaf_add_noise(v) for k, v in params.items()})
            return params + np.float32(0.02 * np.random.randn(*params.shape)) if hasattr(params, 'shape') else params
        except ImportError:
            return params

def _luaf_mutate_planner(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    a = _luaf_agent_dir()
    wp = state.get('planner_weights_path')
    p = Path(wp) if wp else a / 'planner_weights' / 'current.pkl'
    if not p.is_absolute():
        p = a / p
    if not p.exists():
        return None
    try:
        with open(p, 'rb') as f:
            params = pickle.load(f)
    except Exception:
        return None
    cd = a / 'planner_weights' / 'candidates'
    cd.mkdir(parents=True, exist_ok=True)
    cid = uuid.uuid4().hex[:8]
    op = cd / f'{cid}.pkl'
    try:
        with open(op, 'wb') as f:
            pickle.dump(_luaf_add_noise(params), f)
    except Exception:
        return None
    return {**state, 'planner_weights_path': str(op)}
_EVOLVE_TESTS = [('DeFi analytics', 'DeFi context'), ('Trading bot', 'Trading context'), ('Research summariser', 'Summarisation context')]

def _luaf_evaluate(state: dict[str, Any], timeout: int=60) -> tuple[bool, float]:
    if not (_plan_from_topic_and_search and _execute_plan and _get_template):
        return (False, 0.0)
    wp = state.get('planner_weights_path') or os.environ.get('LUAF_PLANNER_WEIGHTS')
    prev = os.environ.get('LUAF_PLANNER_WEIGHTS')
    if wp:
        os.environ['LUAF_PLANNER_WEIGHTS'] = str(wp)
    try:
        val, hashes = (1.0, [])
        for t, s in _EVOLVE_TESTS:
            try:
                plan = _plan_from_topic_and_search(t, s, use_model=bool(wp))
                pl = _execute_plan(plan, _get_template, required_payload_keys=REQUIRED_PAYLOAD_KEYS)
            except Exception:
                val = 0.0
                break
            hashes.append(hashlib.sha256(str(sorted(plan.items())).encode()).hexdigest()[:16])
            code = pl.get('agent', '') if isinstance(pl.get('agent'), str) else str(pl.get('agent', ''))
            if not run_agent_code_validation(code, timeout)[0]:
                val = 0.0
        div = len(set(hashes)) / max(1, len(hashes)) if hashes else 0.0
        return (val >= 1.0, val + 0.1 * div)
    finally:
        if wp:
            if prev is not None:
                os.environ['LUAF_PLANNER_WEIGHTS'] = prev
            else:
                os.environ.pop('LUAF_PLANNER_WEIGHTS', None)

def _luaf_run_evolution() -> tuple[dict[str, Any], bool]:
    st = _luaf_get_current_organism()
    c = _luaf_mutate_planner(st)
    if c is None:
        return (st, False)
    ok_c, sc_c = _luaf_evaluate(c)
    if not ok_c:
        return (st, False)
    ok_s, sc_s = _luaf_evaluate(st)
    if sc_c <= (sc_s if ok_s else 0.0):
        return (st, False)
    _luaf_set_current_organism(c)
    return (c, True)

def _luaf_run_self_train(topic: str, use_search: bool=True) -> bool:
    a = _luaf_agent_dir()
    if str(a) not in sys.path:
        sys.path.insert(0, str(a))
    topics = [t for t in [topic.strip(), 'DeFi analytics', 'Trading bot', 'Research summariser', 'On-chain metrics', 'Backtesting strategy', 'Real-time alerts', 'Multi-DEX comparison', 'Risk metrics'] if t]
    if not topics:
        return False
    sid = uuid.uuid4().hex[:8]
    dd = a / 'planner_data'
    dd.mkdir(parents=True, exist_ok=True)
    jp = dd / f'self_train_{sid}.jsonl'
    cd = a / 'planner_weights' / 'candidates'
    cd.mkdir(parents=True, exist_ok=True)
    cp = cd / f'{sid}.pkl'
    try:
        from planner.data_pipeline import run_pipeline
    except ImportError:
        logger.error('planner.data_pipeline unavailable')
        return False
    try:
        run_pipeline(topics, jp, use_search=use_search, max_search_results=10)
    except Exception as e:
        logger.warning('self_train pipeline: {}', e)
        return False
    try:
        subprocess.run([sys.executable, '-m', 'planner.train', '--data', str(jp), '--out', str(cp)], cwd=str(a), env=os.environ.copy(), check=True, timeout=600)
    except Exception as e:
        logger.warning('self_train train: {}', e)
        return False
    cur = _luaf_get_current_organism()
    cand = {**cur, 'planner_weights_path': str(cp)}
    ok_c, sc_c = _luaf_evaluate(cand)
    if not ok_c:
        return False
    ok_s, sc_s = _luaf_evaluate(cur)
    if sc_c <= (sc_s if ok_s else 0.0):
        return False
    _luaf_set_current_organism(cand)
    logger.info('self_train: adopted candidate')
    return True

def _run_evolution_standalone() -> None:
    try:
        _, u = _luaf_run_evolution()
        logger.info('Evolution done. Updated: {}', u)
    except Exception as e:
        logger.warning('Evolution failed: {}', e)

def _keyless_agent_image_url(payload: dict[str, Any]) -> Optional[str]:
    """Build a keyless AI image URL for the agent (Pollinations.ai). No API key. Returns URL or None."""
    name = (payload.get('name') or '').strip() or 'Agent'
    desc = (payload.get('description') or '').strip()
    prompt = f'Professional icon for an AI agent named {name}. {desc[:80]}' if desc else f'Professional icon for an AI agent named {name}.'
    prompt = re.sub(r'[\s]+', ' ', prompt).strip()
    if not prompt:
        return None
    try:
        encoded = urllib.parse.quote(prompt, safe='')
        base = (os.environ.get('LUAF_AGENT_IMAGE_BASE_URL') or 'https://gen.pollinations.ai').strip().rstrip('/')
        url = f'{base}/image/{encoded}'
        return url
    except Exception as e:
        logger.debug('Keyless agent image URL failed: {}', e)
        return None

def _agent_image_url_for_publish(payload: dict[str, Any]) -> Optional[str]:
    """Resolve image_url for publish: payload, env override, or keyless generation when enabled."""
    url = (payload.get('image_url') or '').strip() or (os.environ.get('LUAF_AGENT_IMAGE_URL') or '').strip()
    if url:
        return url
    if _env_bool('LUAF_GENERATE_AGENT_IMAGE', '0'):
        url = _keyless_agent_image_url(payload)
        if url:
            logger.info('Using keyless agent image: {}', url[:80] + '...')
        return url
    return None

def _schedule_x_post_for_agent(payload: dict[str, Any], res: dict[str, Any]) -> None:
    """If X post is enabled, add agent to pending and maybe post a batch. Best-effort; never raises."""
    if _add_agent_to_x_pending is None or _maybe_post_x_batch is None or _is_x_post_enabled is None or not _is_x_post_enabled():
        return
    try:
        _add_agent_to_x_pending(payload, res)
        _maybe_post_x_batch()
    except Exception as e:
        logger.warning('X post scheduling failed: {}', e)

def _drain_x_queue_if_enabled() -> None:
    """Drain X post queue if the X layer is available and enabled. Best-effort; never raises."""
    if _drain_x_queue is None or _is_x_post_enabled is None or not _is_x_post_enabled():
        logger.debug('X queue drain skipped (disabled or module not loaded).')
        return
    try:
        _drain_x_queue()
    except Exception as e:
        logger.warning('X queue drain failed: {}', e)

def _run_social_standalone() -> None:
    if _run_social_autonomy is None:
        logger.error('OpenClaw not available.')
        return
    try:
        b = input(f'Brief (Enter={TOPIC}): ').strip() or TOPIC
        r = _run_social_autonomy(b)
        logger.info('Social: {}', 'sent' if r else 'no action')
    except EOFError:
        _run_social_autonomy(TOPIC)
    except Exception as e:
        logger.warning('Social failed: {}', e)

def _run_self_train_standalone() -> None:
    try:
        t = input(f'Topic (Enter={TOPIC}): ').strip() or TOPIC
        _luaf_run_self_train(t)
    except EOFError:
        _luaf_run_self_train(TOPIC)
    except Exception as e:
        logger.warning('Self-train failed: {}', e)

def _run_build_dataset_standalone() -> None:
    try:
        from planner.data_pipeline import run_pipeline
    except ImportError:
        logger.error('planner.data_pipeline unavailable')
        return
    try:
        ts = input('Topics (comma/path, Enter=TOPIC): ').strip() or TOPIC
        if ts.endswith('.txt'):
            with open(ts, encoding='utf-8') as f:
                topics = [l.strip() for l in f if l.strip()]
        else:
            topics = [t.strip() for t in ts.split(',') if t.strip()]
        if not topics:
            logger.warning('No topics.')
            return
        op = _luaf_agent_dir() / 'planner_data' / 'train.jsonl'
        op.parent.mkdir(parents=True, exist_ok=True)
        run_pipeline(topics, op, use_search=True)
        logger.info('Wrote {}', op)
    except EOFError:
        run_pipeline([TOPIC], _luaf_agent_dir() / 'planner_data' / 'train.jsonl', use_search=True)
    except Exception as e:
        logger.warning('Build dataset failed: {}', e)

def _run_train_planner_standalone() -> None:
    ad = _luaf_agent_dir()
    dd = ad / 'planner_data' / 'train.jsonl'
    do = ad / 'planner_weights' / 'current.pkl'
    try:
        ds = input(f'Data ({dd}): ').strip() or str(dd)
        os_ = input(f'Out ({do}): ').strip() or str(do)
    except EOFError:
        ds, os_ = (str(dd), str(do))
    if not Path(ds).exists():
        logger.error('Not found: {}', ds)
        return
    try:
        subprocess.run([sys.executable, '-m', 'planner.train', '--data', ds, '--out', os_], cwd=str(ad), check=True)
        logger.info('Saved {}', os_)
    except Exception as e:
        logger.warning('Train failed: {}', e)
_log_queue: 'queue.Queue[str]' = queue.Queue()
_tui_stop_requested: bool = False
_tui_current_topic: str = ''
_tui_session_published: int = 0
_tui_session_last_name: str = ''
_tui_stopped_reason: str = ''

def _run_pipeline_with_brief(brief: str) -> None:
    os.environ['LUAF_DESIGN_BRIEF'] = (brief or TOPIC).strip()
    main()

def _get_next_persistent_topic(state: list[int]) -> str:
    if PERSISTENT_TOPIC_SOURCE == 'single':
        suffix = f' {random.choice(SEARCH_VARIANT_SUFFIXES)}' if random.random() < 0.5 else ''
        return (TOPIC + suffix).strip()
    if PERSISTENT_TOPIC_SOURCE == 'env':
        raw = (os.environ.get('LUAF_TOPIC_LIST') or '').strip()
        topics = [t.strip() for t in raw.split(',') if t.strip()]
        if not topics:
            return TOPIC
        idx = state[0] % len(topics)
        state[0] += 1
        return topics[idx]
    if PERSISTENT_TOPIC_SOURCE == 'file':
        path_str = (os.environ.get('LUAF_TOPIC_FILE') or '').strip()
        if not path_str:
            return TOPIC
        p = Path(path_str)
        if not p.exists():
            logger.warning('LUAF_TOPIC_FILE not found: {}', path_str)
            return TOPIC
        try:
            lines = [ln.strip() for ln in p.read_text(encoding='utf-8').splitlines() if ln.strip()]
        except Exception as e:
            logger.warning('Could not read LUAF_TOPIC_FILE: {}', e)
            return TOPIC
        if not lines:
            return TOPIC
        idx = state[0] % len(lines)
        state[0] += 1
        return lines[idx]
    return TOPIC

def run_persistent() -> None:
    global _tui_current_topic, _tui_session_published, _tui_session_last_name, _tui_stopped_reason
    target_sol = _env_float('LUAF_PERSISTENT_TARGET_SOL', 10.0, 0.0, 1000000.0)
    min_sol_to_tokenize = _env_float('LUAF_MIN_SOL_TO_TOKENIZE', 0.05, 0.0, 1000000.0)
    claim_delay_hours = _env_float('LUAF_CLAIM_DELAY_HOURS', 24.0, 0.0, 8760.0)
    dry_run = _env_bool('LUAF_DRY_RUN', '1')
    use_run_in_new_terminal = _env_bool('LUAF_RUN_IN_NEW_TERMINAL', '1')
    validation_timeout = _env_int('LUAF_VALIDATION_TIMEOUT', 600, lo=5)
    loop_sleep_seconds = _env_int('LUAF_PERSISTENT_LOOP_SLEEP_SECONDS', 0, 0, 86400)
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
    swarms_key = (os.environ.get('SWARMS_API_KEY') or SWARMS_API_KEY_FALLBACK or '').strip()
    if not api_key:
        logger.error('OPENAI_API_KEY not set')
        return
    pkey = get_private_key_from_env()
    cwallet = (os.environ.get('SOLANA_PUBKEY') or os.environ.get('CREATOR_WALLET') or '').strip()
    pubkey = get_creator_pubkey()
    run_task_override = (os.environ.get(PERSISTENT_RUN_TASK_ENV) or '').strip()
    topic_state: list[int] = [0]
    used_n: set[str] = set()
    used_t: set[str] = set()
    vfb: Optional[str] = None
    tmpl = (os.environ.get('LUAF_TEMPLATE') or '').strip() or None
    _drain_x_queue_if_enabled()
    while True:
        if _tui_stop_requested:
            _tui_stopped_reason = 'stop'
            logger.info('Stop requested; exiting persistent loop.')
            return
        balance = get_solana_balance(pubkey, SOLANA_RPC_URL)
        logger.info('Persistent: balance={:.4f} SOL, target={} SOL', balance, target_sol)
        if balance >= target_sol:
            _tui_stopped_reason = 'target'
            logger.info('Target SOL reached ({} >= {}). Exiting.', balance, target_sol)
            return
        brief = _get_next_persistent_topic(topic_state)
        if not (brief or '').strip():
            brief = _generate_topic_via_llm(api_key, base_url) or _DEFAULT_TOPIC
            logger.info('Brief was blank; generated: {}', brief[:200] + '...' if len(brief) > 200 else brief)
        _tui_current_topic = (brief or '')[:60].strip() or '—'
        logger.info('Brief: {}', brief[:200] + '...' if len(brief) > 200 else brief)
        snip: str
        if USE_MULTIHOP_WEB_RAG:
            snip = _multihop_web_rag(brief, max_hops=RAG_MAX_HOPS, threshold=RAG_CONVERGE_THRESHOLD, total_k=RAG_TOTAL_K, ddg_per_hop=RAG_DDG_PER_HOP)
            if not snip:
                snip = search_duckduckgo(f'{brief} {random.choice(SEARCH_VARIANT_SUFFIXES)}', max_results=DUCKDUCKGO_MAX_RESULTS)
        else:
            snip = search_duckduckgo(f'{brief} {random.choice(SEARCH_VARIANT_SUFFIXES)}', max_results=DUCKDUCKGO_MAX_RESULTS)
        snip = _append_keyless_api_search(brief, snip)
        payload: Optional[dict[str, Any]] = None
        if USE_PLANNER and _plan_from_topic_and_search and _execute_plan and _get_template:
            try:
                plan = _plan_from_topic_and_search(brief, snip, use_model=True)
                payload = _execute_plan(plan, _get_template, required_payload_keys=REQUIRED_PAYLOAD_KEYS)
                code = str(payload.get('agent') or '')
                if _is_skeleton_agent_code(code) and USE_DESIGNER:
                    tmpl = (plan.get('template_id') or '').strip() or tmpl
                    pname, pticker = (plan.get('name') or '', plan.get('ticker') or '')
                    if pname or pticker:
                        brief = f'{brief}\n(Preferred: name={pname}, ticker={pticker})'
                    payload = None
            except Exception as e:
                logger.warning('Planner failed: {}', e)
                payload = None
        if payload is None and USE_DESIGNER:
            try:
                retrieved = _retrieve_similar_exemplars(brief, snip, top_k=3)
                if (os.environ.get('LUAF_DESIGNER_SUBPROCESS', '1') or '').strip().lower() not in ('0', 'false', 'no'):
                    raw = _run_designer_in_subprocess(topic=brief, search_snippets=snip, model=LLM_MODEL, api_key=api_key, base_url=base_url, existing_names=used_n, existing_tickers=used_t, temperature=LLM_TEMPERATURE, validation_feedback=vfb, template_id=tmpl, retrieved_exemplars=retrieved)
                else:
                    raw = get_agent_payload_from_llm(topic=brief, search_snippets=snip, model=LLM_MODEL, api_key=api_key, base_url=base_url, existing_names=used_n, existing_tickers=used_t, temperature=LLM_TEMPERATURE, validation_feedback=vfb, template_id=tmpl, retrieved_exemplars=retrieved)
            except Exception as e:
                logger.error('LLM failed: {}', e)
                vfb = f'LLM: {e!s}'
                if loop_sleep_seconds > 0:
                    time.sleep(loop_sleep_seconds)
                continue
            try:
                payload = parse_agent_payload(raw)
            except ValueError as e:
                logger.error('Parse: {}', e)
                vfb = f'Parse: {e!s}'
                if loop_sleep_seconds > 0:
                    time.sleep(loop_sleep_seconds)
                continue
        if payload is None:
            logger.warning('No payload; skipping iteration.')
            if loop_sleep_seconds > 0:
                time.sleep(loop_sleep_seconds)
            continue
        code = str(payload.get('agent') or '')
        sk_fb = _skeleton_validation_feedback(code)
        if sk_fb is not None:
            vfb = sk_fb
            logger.warning('Unit code too short or skeleton: {}', sk_fb[:200])
            if loop_sleep_seconds > 0:
                time.sleep(loop_sleep_seconds)
            continue
        ok, fb = run_agent_code_validation(code, validation_timeout)
        if not ok:
            vfb = fb
            logger.warning('Unit validation failed: {}', fb[:1500])
            logger.info('Validation full feedback:\n{}', fb[:3000] + ('...' if len(fb) > 3000 else ''))
            if _ask_publish_without_validation():
                logger.info('Publishing without validation (user confirmed).')
            else:
                if loop_sleep_seconds > 0:
                    time.sleep(loop_sleep_seconds)
                continue
        n, t = ((payload.get('name') or '').strip(), (payload.get('ticker') or '').strip())
        used_n.add(n.lower())
        used_t.add(t.upper())
        dry_run_this = dry_run
        if not dry_run:
            bal = get_solana_balance(pubkey, SOLANA_RPC_URL)
            if bal < min_sol_to_tokenize:
                dry_run_this = True
                logger.info('Insufficient balance ({:.4f} < {}); dry-run publish.', bal, min_sol_to_tokenize)
        res = publish_agent(payload, swarms_key, pkey or '', dry_run_this, image_url=_agent_image_url_for_publish(payload), creator_wallet=cwallet)
        run_task = run_task_override or brief
        run_ok, run_out = run_agent_once(code, run_task, timeout=validation_timeout)
        if run_ok:
            logger.info('Run unit once: OK. Output length={}', len(run_out or ''))
        else:
            logger.warning('Run unit once: {}', run_out[:300] if run_out else 'failed')
        if use_run_in_new_terminal:
            saved_path = _save_generated_agent(code, n, t, 0)
            if saved_path:
                run_agent_in_new_terminal(saved_path, (run_task or '').strip() or 'Run a quick check.')
        if res and (not dry_run_this):
            lu, rid, ca = (res.get('listing_url'), res.get('id'), res.get('token_address'))
            if lu or rid or ca:
                published_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                append_agent_to_registry(AGENTS_REGISTRY_PATH, name=n, ticker=t, listing_url=lu, id_=rid, token_address=ca, published_at=published_at)
                _schedule_x_post_for_agent(payload, res)
                _tui_session_published += 1
                _tui_session_last_name = n or '—'
        run_delayed_claim_pass(AGENTS_REGISTRY_PATH, pkey or '', swarms_key, claim_delay_hours)
        if loop_sleep_seconds > 0:
            time.sleep(loop_sleep_seconds)

def run_standalone_cli() -> None:
    run_profile_selection()
    _title = _style_heading('LUAF  —  brief → research → launch')
    _opts = _style_muted('    1. Pipeline   (design & launch autonomous unit)\n    2. Persistent (autonomous loop until target SOL)\n    0. Exit')
    _menu = '\n  ╭─────────────────────────────────────╮\n  │  ' + _title + '  │\n  ╰─────────────────────────────────────╯\n' + _opts + '\n'
    _handlers: dict[str, Any] = {'1': main, '2': run_persistent, '0': None}
    while True:
        print(_menu)
        try:
            c = input('  Choice [1]: ').strip() or '1'
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if c == '0':
            return
        fn = _handlers.get(c)
        if fn is not None:
            fn()
        else:
            print(_style_warn('  Unknown. Use 1 (Pipeline), 2 (Autonomous loop), or 0 (Exit).'))

def run_interactive_menu() -> None:
    if create_luaf_app:
        def _set_stop() -> None:
            global _tui_stop_requested
            _tui_stop_requested = True
        def _get_state() -> tuple[str, int, str, str]:
            return (_tui_current_topic, _tui_session_published, _tui_session_last_name, _tui_stopped_reason)
        default = _get_default_profile()
        profile_options_list: list[dict[str, Any]] = [default]
        if _list_profiles is not None and PROFILES_DIR.is_dir():
            profile_options_list = [default] + _list_profiles(PROFILES_DIR)
        def _on_profile_selected(idx: int) -> None:
            global _active_profile
            if 0 <= idx < len(profile_options_list):
                _active_profile = profile_options_list[idx]
                logger.info('Profile: {}', _active_profile.get('display_name', _active_profile.get('id', 'default')))
        config: dict[str, Any] = {'get_creator_pubkey': get_creator_pubkey, 'get_solana_balance': get_solana_balance, 'load_agents_registry': lambda: _load_agents_registry(AGENTS_REGISTRY_PATH), 'target_sol': _env_float('LUAF_PERSISTENT_TARGET_SOL', 10.0, 0.0, 1000000.0), 'registry_path': AGENTS_REGISTRY_PATH, 'rpc_url': SOLANA_RPC_URL, 'set_stop_requested': _set_stop, 'get_tui_state': _get_state, 'log_queue': _log_queue, 'profile_options': profile_options_list, 'on_profile_selected': _on_profile_selected}
        LUAFApp = create_luaf_app(run_persistent, config)
        app = LUAFApp()
        app.run()
    else:
        run_standalone_cli()

def main() -> None:
    try:
        org = _luaf_get_current_organism()
        if org.get('planner_weights_path'):
            os.environ['LUAF_PLANNER_WEIGHTS'] = str(org['planner_weights_path'])
        if (org.get('config') or {}).get('template_id'):
            os.environ['LUAF_TEMPLATE'] = str(org['config']['template_id'])
    except Exception:
        pass
    dry_run = _env_bool('LUAF_DRY_RUN', '1')
    max_steps = _env_int('LUAF_MAX_STEPS', 3)
    use_run_in_new_terminal = _env_bool('LUAF_RUN_IN_NEW_TERMINAL', '1')
    validation_timeout = _env_int('LUAF_VALIDATION_TIMEOUT', 600, lo=5)
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
    swarms_key = (os.environ.get('SWARMS_API_KEY') or SWARMS_API_KEY_FALLBACK or '').strip()
    if not api_key:
        logger.error('OPENAI_API_KEY not set')
        return
    if not swarms_key and (not dry_run):
        logger.warning('SWARMS_API_KEY not set')
    pkey = get_private_key_from_env()
    cwallet = (os.environ.get('SOLANA_PUBKEY') or os.environ.get('CREATOR_WALLET') or '').strip()
    if not dry_run and (not (pkey or '').strip()):
        logger.warning('No SOLANA_PRIVATE_KEY; skipping publish.')
    if not dry_run and pkey and (not cwallet):
        logger.warning('No CREATOR_WALLET; tokenized publish may fail.')
    if dry_run:
        logger.info('Dry run mode.')
    brief = read_design_brief_interactive()
    if not (brief or '').strip():
        brief = _generate_topic_via_llm(api_key, base_url) or _DEFAULT_TOPIC
        logger.info('Brief was blank; generated: {}', brief[:200] + '...' if len(brief) > 200 else brief)
    name_override, ticker_override = read_optional_name_and_ticker()
    if name_override:
        brief = f'{brief}\n(Use exactly this unit name: {name_override})'
    if ticker_override:
        brief = f'{brief}\n(Use exactly this ticker: {ticker_override})'
    logger.info('Brief: {}', brief[:200] + '...' if len(brief) > 200 else brief)
    used_n: set[str] = set()
    used_t: set[str] = set()
    vfb: Optional[str] = None
    tmpl = (os.environ.get('LUAF_TEMPLATE') or '').strip() or None
    for step in range(1, max_steps + 1):
        logger.info('Step {}/{}', step, max_steps)
        if USE_MULTIHOP_WEB_RAG:
            snip = _multihop_web_rag(brief, max_hops=RAG_MAX_HOPS, threshold=RAG_CONVERGE_THRESHOLD, total_k=RAG_TOTAL_K, ddg_per_hop=RAG_DDG_PER_HOP)
            if not snip:
                snip = search_duckduckgo(f'{brief} {random.choice(SEARCH_VARIANT_SUFFIXES)}', max_results=DUCKDUCKGO_MAX_RESULTS)
        else:
            snip = search_duckduckgo(f'{brief} {random.choice(SEARCH_VARIANT_SUFFIXES)}', max_results=DUCKDUCKGO_MAX_RESULTS)
        snip = _append_keyless_api_search(brief, snip)
        payload = None
        if USE_PLANNER and _plan_from_topic_and_search and _execute_plan and _get_template:
            try:
                plan = _plan_from_topic_and_search(brief, snip, use_model=True)
                payload = _execute_plan(plan, _get_template, required_payload_keys=REQUIRED_PAYLOAD_KEYS)
                logger.info('Planner OK (template={})', plan.get('template_id', ''))
                code = str(payload.get('agent') or '')
                if _is_skeleton_agent_code(code):
                    if USE_DESIGNER:
                        logger.info('Skeleton detected; expanding via designer LLM.')
                        tmpl = (plan.get('template_id') or '').strip() or tmpl
                        pname, pticker = (plan.get('name') or '', plan.get('ticker') or '')
                        if pname or pticker:
                            brief = f'{brief}\n(Preferred: name={pname}, ticker={pticker})'
                        payload = None
                    else:
                        logger.error('Planner produced skeleton only. Set LUAF_USE_DESIGNER=1 to expand via LLM.')
                        payload = None
                        break
            except Exception as e:
                logger.warning('Planner failed: {}', e)
                payload = None
        if payload is None and USE_DESIGNER:
            try:
                retrieved = _retrieve_similar_exemplars(brief, snip, top_k=3)
                if (os.environ.get('LUAF_DESIGNER_SUBPROCESS', '1') or '').strip().lower() not in ('0', 'false', 'no'):
                    raw = _run_designer_in_subprocess(topic=brief, search_snippets=snip, model=LLM_MODEL, api_key=api_key, base_url=base_url, existing_names=used_n, existing_tickers=used_t, temperature=LLM_TEMPERATURE, validation_feedback=vfb, template_id=tmpl, retrieved_exemplars=retrieved)
                else:
                    raw = get_agent_payload_from_llm(topic=brief, search_snippets=snip, model=LLM_MODEL, api_key=api_key, base_url=base_url, existing_names=used_n, existing_tickers=used_t, temperature=LLM_TEMPERATURE, validation_feedback=vfb, template_id=tmpl, retrieved_exemplars=retrieved)
            except Exception as e:
                logger.error('LLM failed: {}', e)
                vfb = f'LLM: {e!s}'
                continue
            try:
                payload = parse_agent_payload(raw)
            except ValueError as e:
                logger.error('Parse: {}', e)
                vfb = f'Parse: {e!s}'
                continue
        if payload is None and (not USE_DESIGNER) and (not USE_PLANNER or not _plan_from_topic_and_search):
            logger.error('No planner or designer. Set LUAF_USE_DESIGNER=1.')
            break
        if payload is None:
            continue
        if name_override:
            payload['name'] = name_override
        if ticker_override:
            payload['ticker'] = ticker_override
        code = str(payload.get('agent') or '')
        sk_fb = _skeleton_validation_feedback(code)
        if sk_fb is not None:
            vfb = sk_fb
            logger.warning('Unit code too short or skeleton (step {}): {}', step, sk_fb[:200])
            continue
        saved_path = _save_generated_agent(code, payload.get('name'), payload.get('ticker'), step)
        ok, fb = run_agent_code_validation(code, validation_timeout)
        if ok:
            logger.info('Validation OK (step {}). Publishing to marketplace (dry_run={}).', step, dry_run)
            n, t = ((payload.get('name') or '').strip(), (payload.get('ticker') or '').strip())
            used_n.add(n.lower())
            used_t.add(t.upper())
            res = publish_agent(payload, swarms_key, pkey or '', dry_run, image_url=_agent_image_url_for_publish(payload), creator_wallet=cwallet)
            logger.info('Publish returned; updating registry and X only if not dry run.')
            if res and (not dry_run):
                lu, rid, ca = (res.get('listing_url'), res.get('id'), res.get('token_address'))
                if lu or rid or ca:
                    append_agent_to_registry(AGENTS_REGISTRY_PATH, name=n, ticker=t, listing_url=lu, id_=rid, token_address=ca)
                    _schedule_x_post_for_agent(payload, res)
            if use_run_in_new_terminal and saved_path:
                run_agent_in_new_terminal(saved_path, (brief or '').strip() or 'Run a quick check.')
            break
        vfb = fb
        logger.warning('Unit validation failed (step {}): {}', step, fb[:1500])
        logger.info('Validation full feedback (step {}):\n{}', step, fb[:3000] + ('...' if len(fb) > 3000 else ''))
        if _ask_publish_without_validation():
            logger.info('Publishing without validation (user confirmed). dry_run={}', dry_run)
            n, t = ((payload.get('name') or '').strip(), (payload.get('ticker') or '').strip())
            used_n.add(n.lower())
            used_t.add(t.upper())
            res = publish_agent(payload, swarms_key, pkey or '', dry_run, image_url=_agent_image_url_for_publish(payload), creator_wallet=cwallet)
            logger.info('Publish returned.')
            if res and (not dry_run):
                lu, rid, ca = (res.get('listing_url'), res.get('id'), res.get('token_address'))
                if lu or rid or ca:
                    append_agent_to_registry(AGENTS_REGISTRY_PATH, name=n, ticker=t, listing_url=lu, id_=rid, token_address=ca)
                    _schedule_x_post_for_agent(payload, res)
            if use_run_in_new_terminal and saved_path:
                run_agent_in_new_terminal(saved_path, (brief or '').strip() or 'Run a quick check.')
            break
    else:
        logger.warning('Max steps ({}) reached.', max_steps)
    if CLAIM_FEES_AFTER_RUN and (pkey or '').strip() and not dry_run:
        reg = _load_agents_registry(AGENTS_REGISTRY_PATH)
        claimable = [e for e in reg if (e.get('token_address') or e.get('ca')) and len((e.get('token_address') or e.get('ca') or '')) >= 32]
        if claimable:
            logger.info('Claiming fees for {} agent(s)...', len(claimable))
            for e in claimable:
                ca = e.get('token_address') or e.get('ca')
                if not ca or len(ca) < 32:
                    continue
                logger.info('Claiming fees for {}', ca[:16])
                cr = claim_fees(ca, pkey.strip(), api_key=swarms_key)
                if cr and cr.get('success'):
                    logger.info('Claimed: sig={} sol={}', cr.get('signature'), cr.get('amountClaimedSol'))
                elif cr:
                    logger.warning('Claim: {}', cr)
        else:
            logger.debug('No claimable agents in registry.')
    else:
        if dry_run and CLAIM_FEES_AFTER_RUN:
            logger.debug('Skipping claim fees (dry run).')
    logger.info('Draining X queue (if enabled)...')
    _drain_x_queue_if_enabled()
    logger.info('Done.')
    if _luaf_should_evolve():
        try:
            _, upd = _luaf_run_evolution()
            if upd:
                logger.info('Evolution: organism updated.')
        except Exception as e:
            logger.warning('Evolution: {}', e)
        if os.environ.get('LUAF_BACKGROUND_TRAIN', '').strip() != '0':
            try:
                ta = (brief or '').strip()[:500]
                if ta:
                    subprocess.Popen([sys.executable, str(_luaf_agent_dir() / 'LUAF.py'), '--self-train', ta], cwd=str(_luaf_agent_dir()), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    if os.environ.get('LUAF_MOLTBOOK_SOCIAL', '').strip() == '1' and _run_social_autonomy:
        try:
            if _run_social_autonomy(brief):
                logger.info('Social: sent.')
        except Exception:
            pass

# Bundled env template for pip installs (no repo/env.example on disk). Same as env.example.
_INIT_BUNDLED_ENV_TEMPLATE = '''# LUAF — copy to .env and fill in. Do not commit .env (secrets).
# Use: luaf init  to set API keys and create .env from this template.

# --- API keys (luaf run / persistent need these) ---
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
SWARMS_API_KEY=
SOLANA_PUBKEY=
CREATOR_WALLET=
SOLANA_PRIVATE_KEY=
SOLANA_PRIVATE_KEY_FILE=

# X (Twitter) posting — all four required if LUAF_POST_TO_X=1
X_API_KEY=
X_API_SECRET=
X_ACCESS_TOKEN=
X_ACCESS_SECRET=

# --- Publish / Swarms ---
LUAF_DRY_RUN=1
LUAF_SWARMS_BASE_URL=https://swarms.world

# --- Solana ---
LUAF_SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
LUAF_MIN_SOL_TO_TOKENIZE=0.05

# --- Persistent loop ---
LUAF_PERSISTENT_TARGET_SOL=10
LUAF_PERSISTENT_TOPIC_SOURCE=single
LUAF_TOPIC_LIST=
LUAF_TOPIC_FILE=
LUAF_PERSISTENT_LOOP_SLEEP_SECONDS=0
LUAF_CLAIM_DELAY_HOURS=24

# --- Brief / topic ---
LUAF_TOPIC=
LUAF_DESIGN_BRIEF=
LUAF_INTERACTIVE=1

# --- Designer / LLM ---
LUAF_LLM_MODEL=gpt-4.1
LUAF_LLM_TEMPERATURE=0.9
LUAF_DESIGNER_AGENT_ARCHITECTURE=agent
LUAF_DESIGNER_USE_DIRECT_API=1
LUAF_DESIGNER_STREAM=0
LUAF_DESIGNER_SUBPROCESS=1
LUAF_USE_PLANNER=1
LUAF_USE_DESIGNER=1
LUAF_TRY_SWARMS_CLOUD_FIRST=0
LUAF_TEMPLATE=
LUAF_USE_RETRIEVAL=1

# --- RAG (multi-hop) ---
LUAF_USE_MULTIHOP_WEB_RAG=0
LUAF_RAG_MAX_HOPS=3
LUAF_RAG_CONVERGE_THRESHOLD=0.7
LUAF_RAG_TOTAL_K=20
LUAF_RAG_DDG_PER_HOP=15

# --- Validation ---
LUAF_VALIDATION_TIMEOUT=600
LUAF_MAX_MISSING_IMPORT_RETRIES=3

# --- Execution / UX ---
LUAF_RUN_IN_NEW_TERMINAL=1
LUAF_KEYLESS_API_SEARCH=1

# --- Agent image (keyless: LUAF_GENERATE_AGENT_IMAGE=1 or LUAF_AGENT_IMAGE_URL=) ---

# --- X posting ---
LUAF_POST_TO_X=0

# --- Misc ---
LUAF_MAX_STEPS=3
LUAF_DESIGNER_MAX_LOOPS=2
LUAF_LOG_FILE=1
LUAF_GENERATED_AGENTS_DIR=generated_agents
WORKSPACE_DIR=
'''

# Init: API keys we prompt for (one block; Enter to skip any). Doctor/--check use _INIT_CHECK_KEYS.
_INIT_API_KEYS = (
    'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'SWARMS_API_KEY', 'SOLANA_PUBKEY', 'CREATOR_WALLET',
    'SOLANA_PRIVATE_KEY', 'SOLANA_PRIVATE_KEY_FILE',
    'X_API_KEY', 'X_API_SECRET', 'X_ACCESS_TOKEN', 'X_ACCESS_SECRET',
)
_INIT_CHECK_KEYS = ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'SWARMS_API_KEY', 'SOLANA_PUBKEY')
# Exact strings treated as "not set" (doctor and init --check). Only these; real keys never match.
_INIT_PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    'yoursolanawalletpublickeybase58', 'sk-proj-your-openai-key-here', 'sk-your-swarms-api-key-here',
})
def _is_placeholder_value(val: str) -> bool:
    return (val or '').strip().lower() in _INIT_PLACEHOLDER_VALUES

_INIT_HINTS: dict[str, str] = {
    'OPENAI_API_KEY': 'Create at https://platform.openai.com/api-keys (API keys → Create new secret key)',
    'OPENAI_BASE_URL': 'Default: https://api.openai.com/v1 — change only if using a proxy or compatible endpoint',
    'SWARMS_API_KEY': 'Get from https://swarms.world (Swarms dashboard / sign-up)',
    'SOLANA_PUBKEY': 'Your Solana wallet public key (base58). From Phantom/Solflare or: solana address',
    'CREATOR_WALLET': 'Same as SOLANA_PUBKEY or leave blank',
    'SOLANA_PRIVATE_KEY': 'Base58 secret key; needed for tokenized publish and fee claiming. Export from wallet or Solana CLI',
    'SOLANA_PRIVATE_KEY_FILE': 'Or path to file containing the private key (one line)',
    'X_API_KEY': 'X (Twitter) app API key: https://developer.x.com — Project → App → Keys and tokens',
    'X_API_SECRET': 'X app API secret (same app)',
    'X_ACCESS_TOKEN': 'X OAuth 1.0a access token (user context)',
    'X_ACCESS_SECRET': 'X OAuth 1.0a access token secret',
}

def _parse_env_file(path: Path) -> dict[str, str]:
    """Read .env-style file into key -> value. Comments and empty lines omitted."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            k, _, v = line.partition('=')
            k = k.strip()
            if k:
                v = v.strip().strip('"').strip("'")
                out[k] = v
    return out

def _write_env_updates(path: Path, updates: dict[str, str], template_lines: list[str]) -> None:
    """Write .env file: apply updates to keys that appear in template_lines, else append."""
    existing = _parse_env_file(path)
    existing.update(updates)
    seen: set[str] = set()
    result: list[str] = []
    for line in template_lines:
        stripped = line.strip()
        if stripped and '=' in stripped and not stripped.startswith('#'):
            k = stripped.partition('=')[0].strip()
            if k in existing:
                seen.add(k)
                result.append(f'{k}={existing[k]}')
                continue
        result.append(line.rstrip())
    for k, v in existing.items():
        if k not in seen:
            result.append(f'{k}={v}')
    path.write_text('\n'.join(result) + '\n', encoding='utf-8')

def _ensure_designer_prompt_in_cwd() -> None:
    """Create designer_system_prompt.txt in cwd if missing (PyPI users have no repo file)."""
    prompt_path = Path.cwd() / 'designer_system_prompt.txt'
    if prompt_path.exists():
        return
    try:
        from luaf_defaults import DEFAULT_DESIGNER_SYSTEM_PROMPT as _default_prompt
        prompt_path.write_text(_default_prompt, encoding='utf-8')
        print(_style_success('  Created designer_system_prompt.txt in current directory.'))
    except Exception as e:
        logger.warning('Could not create designer_system_prompt.txt: {}', e)

def run_init(init_args: Any) -> int:
    """Create or update .env; interactive prompts with hints. Returns exit code (0 = success)."""
    from_example = getattr(init_args, 'from_example', False)
    force = getattr(init_args, 'force', False)
    check = getattr(init_args, 'check', False)
    # PyPI users: .env lives in cwd. Repo users: cwd or repo root.
    env_path = Path.cwd() / '.env'
    example_name = '.env.example'
    example_path = Path.cwd() / example_name
    if not example_path.exists():
        example_path = Path.cwd() / 'env.example'
    if not example_path.exists():
        example_path = _REPO_ROOT / example_name
    if not example_path.exists():
        example_path = _REPO_ROOT / 'env.example'
    if not example_path.exists():
        example_path = _LUAF_DIR / example_name
    if not example_path.exists():
        example_path = _LUAF_DIR / 'env.example'
    if check:
        load_dotenv(Path.cwd() / '.env')
        load_dotenv(_REPO_ROOT / '.env', override=False)
        missing = [k for k in _INIT_CHECK_KEYS if not (os.environ.get(k) or '').strip() or _is_placeholder_value((os.environ.get(k) or '').strip())]
        if not missing:
            print(_style_success('All required env vars are set.'))
            return 0
        print(_style_error('Missing or placeholder: ') + ', '.join(missing))
        print(_style_muted('  Run luaf init to set API keys and config.'))
        return 1
    template_lines: list[str] = []
    if example_path.exists():
        template_lines = example_path.read_text(encoding='utf-8', errors='replace').splitlines()
    else:
        # Pip install: no env.example on disk; use bundled template (same as env.example)
        template_lines = _INIT_BUNDLED_ENV_TEMPLATE.strip().splitlines()
    if not env_path.exists():
        env_path.write_text('\n'.join(template_lines) + '\n', encoding='utf-8')
        print(_style_success('Created .env in current directory (from env.example).'))
    else:
        if from_example:
            return 0
        print(_style_muted('.env exists in current directory.'))
    current = _parse_env_file(env_path)
    is_tty = getattr(sys.stdin, 'isatty', lambda: False)()
    if not is_tty or from_example:
        _ensure_designer_prompt_in_cwd()
        return 0
    getpass = __import__('getpass', fromlist=['getpass']).getpass
    updates: dict[str, str] = {}
    print(_style_heading('\n  Set up API keys'))
    print(_style_muted('  Enter to skip any. Pipeline needs OPENAI_API_KEY and SWARMS_API_KEY; publish needs Solana keys.\n'))
    for key in _INIT_API_KEYS:
        existing = (current.get(key) or '').strip()
        if existing and not force and not _is_placeholder_value(existing):
            continue
        hint = _INIT_HINTS.get(key, '')
        print(_style_heading(f'  {key}'))
        if hint:
            print(_style_muted(f'    {hint}'))
        if key in ('OPENAI_API_KEY', 'SWARMS_API_KEY', 'SOLANA_PRIVATE_KEY', 'X_API_SECRET', 'X_ACCESS_SECRET'):
            val = getpass(f'    Value (Enter to skip): ').strip()
        elif key == 'OPENAI_BASE_URL':
            val = input(f'    Value (Enter = https://api.openai.com/v1): ').strip()
        else:
            try:
                val = input(f'    Value (Enter to skip): ').strip()
            except (EOFError, KeyboardInterrupt):
                val = ''
        if val:
            updates[key] = val
    if updates:
        _write_env_updates(env_path, updates, template_lines)
        print(_style_success('Updated .env with provided values.'))
    _ensure_designer_prompt_in_cwd()
    print(_style_heading('\n  Next steps:'))
    print(_style_muted('    luaf doctor   — check config and connectivity'))
    print(_style_muted('    luaf run     — single pipeline'))
    print(_style_muted('    luaf persistent — loop until target SOL'))
    print(_style_muted('    Edit .env for LUAF_* options (dry-run, target SOL, timeouts, etc.).'))
    return 0

def _env_path_for_user() -> Path:
    """Where .env is expected: cwd first (PyPI users), then repo/package root."""
    cwd_env = Path.cwd() / '.env'
    if cwd_env.exists():
        return cwd_env
    return _REPO_ROOT / '.env'

def _doctor_check_openai() -> tuple[bool, Optional[str]]:
    """Health check: call OpenAI-compatible API. Returns (ok, error_message)."""
    api_key = (os.environ.get('OPENAI_API_KEY') or '').strip()
    base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
    if not api_key:
        return False, 'not set'
    try:
        url = f"{base_url.rstrip('/')}/chat/completions"
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': 'gpt-4o-mini', 'messages': [{'role': 'user', 'content': 'Hi'}], 'max_tokens': 5},
            timeout=15,
        )
        if resp.ok:
            return True, None
        err = _resp_json(resp)
        code = resp.status_code
        msg = (err.get('error', {}) or err) if isinstance(err.get('error'), dict) else err
        detail = (msg.get('message') or msg.get('error') or resp.text[:200] or str(code)).strip()
        return False, f'{code} {detail}' if detail else str(code)
    except requests.exceptions.RequestException as e:
        return False, f'{getattr(e.response, "status_code", None) or "network"} {str(e)[:80]}'

def _doctor_check_swarms() -> tuple[bool, Optional[str]]:
    """Health check: call Swarms API with Bearer. 401/403 = bad auth; 200/400/422 = auth OK."""
    api_key = (os.environ.get('SWARMS_API_KEY') or '').strip()
    base_url = (os.environ.get('LUAF_SWARMS_BASE_URL') or 'https://swarms.world').strip()
    if not api_key:
        return False, 'not set'
    try:
        url = f"{base_url.rstrip('/')}/api/add-agent"
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={},
            timeout=15,
        )
        if resp.status_code in (401, 403):
            return False, f'{resp.status_code} Unauthorized/Forbidden'
        if resp.status_code in (200, 400, 422):
            return True, None
        return False, f'{resp.status_code} {resp.text[:80] or "error"}'
    except requests.exceptions.RequestException as e:
        return False, f'{getattr(e.response, "status_code", None) or "network"} {str(e)[:80]}'

def _doctor_check_solana() -> tuple[bool, Optional[str]]:
    """Health check: Solana RPC getBalance. Returns (ok, error_message)."""
    pubkey = get_creator_pubkey()
    if not pubkey or _is_placeholder_value(pubkey):
        return False, 'not set or placeholder'
    try:
        resp = requests.post(
            SOLANA_RPC_URL,
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'getBalance', 'params': [pubkey]},
            timeout=10,
        )
        if not resp.ok:
            return False, f'{resp.status_code} {resp.text[:80] or "RPC error"}'
        data = resp.json()
        err = data.get('error')
        if err is not None:
            code = err.get('code', '') if isinstance(err, dict) else ''
            msg = err.get('message', str(err))[:80] if isinstance(err, dict) else str(err)[:80]
            return False, f'{code} {msg}'.strip()
        return True, None
    except requests.exceptions.RequestException as e:
        return False, f'{getattr(e.response, "status_code", None) or "network"} {str(e)[:80]}'

def _doctor_check_x() -> tuple[bool, Optional[str]]:
    """Health check: X API 2 users/me with OAuth 1.0a. Returns (ok, error_message)."""
    try:
        from requests_oauthlib import OAuth1Session
    except ImportError:
        return False, 'requests_oauthlib not installed'
    key = (os.environ.get('X_API_KEY') or '').strip()
    secret = (os.environ.get('X_API_SECRET') or '').strip()
    token = (os.environ.get('X_ACCESS_TOKEN') or '').strip()
    token_secret = (os.environ.get('X_ACCESS_SECRET') or '').strip()
    if not all((key, secret, token, token_secret)):
        return False, 'not set'
    try:
        oauth = OAuth1Session(key, secret, token, token_secret)
        resp = oauth.get('https://api.twitter.com/2/users/me', timeout=10)
        if resp.ok:
            return True, None
        err = resp.text[:100] if resp.text else str(resp.status_code)
        return False, f'{resp.status_code} {err}'
    except requests.exceptions.RequestException as e:
        return False, f'{getattr(e.response, "status_code", None) or "network"} {str(e)[:80]}'

def _doctor_symbols() -> tuple[str, str, str]:
    """Return (ok, fail, neutral) symbols safe for stdout encoding (e.g. Windows cp1252)."""
    enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    try:
        for c in '\u2713\u2717\u00b7':
            c.encode(enc)
        return '\u2713', '\u2717', '\u00b7'
    except (UnicodeEncodeError, LookupError, TypeError):
        return '+', 'x', '-'

def run_doctor(doctor_args: Any) -> int:
    """Check .env, required vars, and live API health. Returns 0 if required OK, 1 otherwise."""
    env_path = _env_path_for_user()
    if env_path.exists():
        load_dotenv(env_path, override=True)
    load_dotenv(Path.cwd() / '.env', override=False)
    load_dotenv(_REPO_ROOT / '.env', override=False)
    _ok, _fail, _dot = _doctor_symbols()
    has_issues = False
    if not env_path.exists():
        print(_style_error(f'  {_fail} ') + '.env not found (run: luaf init)')
        has_issues = True
    else:
        print(_style_success(f'  {_ok} ') + '.env exists')

    for k in _INIT_CHECK_KEYS:
        v = (os.environ.get(k) or '').strip()
        if not v or _is_placeholder_value(v):
            print(_style_error(f'  {_fail} ') + f'{k} (not set or placeholder)')
            has_issues = True
            continue
        if k == 'OPENAI_API_KEY':
            ok, err = _doctor_check_openai()
            if ok:
                print(_style_success(f'  {_ok} ') + 'OPENAI_API_KEY (health check OK)')
            else:
                print(_style_error(f'  {_fail} ') + f'OPENAI_API_KEY ({err})')
                has_issues = True
        elif k == 'OPENAI_BASE_URL':
            continue
        elif k == 'SWARMS_API_KEY':
            ok, err = _doctor_check_swarms()
            if ok:
                print(_style_success(f'  {_ok} ') + 'SWARMS_API_KEY (health check OK)')
            else:
                print(_style_error(f'  {_fail} ') + f'SWARMS_API_KEY ({err})')
                has_issues = True
        elif k == 'SOLANA_PUBKEY':
            ok, err = _doctor_check_solana()
            if ok:
                print(_style_success(f'  {_ok} ') + 'SOLANA_PUBKEY (health check OK)')
            else:
                print(_style_error(f'  {_fail} ') + f'SOLANA_PUBKEY ({err})')
                has_issues = True
        else:
            print(_style_success(f'  {_ok} ') + f'{k} set')

    x_keys = ('X_API_KEY', 'X_API_SECRET', 'X_ACCESS_TOKEN', 'X_ACCESS_SECRET')
    x_set = sum(1 for k in x_keys if (os.environ.get(k) or '').strip())
    if x_set > 0 and x_set < 4:
        print(_style_error(f'  {_fail} ') + 'X posting: set all four X_* vars or leave all unset')
        has_issues = True
    elif x_set == 4:
        ok, err = _doctor_check_x()
        if ok:
            print(_style_success(f'  {_ok} ') + 'X posting (health check OK)')
        else:
            print(_style_error(f'  {_fail} ') + f'X posting ({err})')
            has_issues = True

    if (os.environ.get('SOLANA_PRIVATE_KEY') or '').strip() or (os.environ.get('SOLANA_PRIVATE_KEY_FILE') or '').strip():
        print(_style_success(f'  {_ok} ') + 'Solana private key configured (publish enabled)')
    else:
        print(_style_muted(f'  {_dot} ') + 'Solana private key not set (publish will be dry-run only)')

    if has_issues:
        try:
            pubkey = get_creator_pubkey()
            if pubkey and not _is_placeholder_value(pubkey):
                bal = get_solana_balance(pubkey, SOLANA_RPC_URL)
                print(_style_info(f'  Solana balance: {bal:.4f} SOL') + ' (at ' + (pubkey[:8] + '...') + ')')
        except Exception:
            pass
        print(_style_warn('\n  Set values in .env and run luaf init to add or change keys.'))
        return 1
    try:
        pubkey = get_creator_pubkey()
        if pubkey and not _is_placeholder_value(pubkey):
            bal = get_solana_balance(pubkey, SOLANA_RPC_URL)
            print(_style_success(f'  Solana balance: {bal:.4f} SOL'))
    except Exception as e:
        print(_style_warn(f'  Solana balance check: {e}'))
    print(_style_success('\n  Doctor: required config OK.'))
    print(_style_muted('  Run: luaf run  or  luaf persistent'))
    return 0

def _apply_cli_config(args: Any) -> None:
    """Apply CLI config flags to os.environ so runtime code sees overrides."""
    if getattr(args, 'dry_run', None) is not None:
        os.environ['LUAF_DRY_RUN'] = '1' if args.dry_run else '0'
    if getattr(args, 'target_sol', None) is not None:
        os.environ['LUAF_PERSISTENT_TARGET_SOL'] = str(args.target_sol)
    if getattr(args, 'topic', None) is not None:
        os.environ['LUAF_TOPIC'] = str(args.topic).strip()
    if getattr(args, 'generate_agent_image', None) is not None:
        os.environ['LUAF_GENERATE_AGENT_IMAGE'] = '1' if args.generate_agent_image else '0'
    if getattr(args, 'claim_delay_hours', None) is not None:
        os.environ['LUAF_CLAIM_DELAY_HOURS'] = str(args.claim_delay_hours)
    if getattr(args, 'min_sol_to_tokenize', None) is not None:
        os.environ['LUAF_MIN_SOL_TO_TOKENIZE'] = str(args.min_sol_to_tokenize)
    if getattr(args, 'max_steps', None) is not None:
        os.environ['LUAF_MAX_STEPS'] = str(args.max_steps)
    if getattr(args, 'interactive', None) is not None:
        os.environ['LUAF_INTERACTIVE'] = '1' if args.interactive else '0'
    if getattr(args, 'run_in_new_terminal', None) is not None:
        os.environ['LUAF_RUN_IN_NEW_TERMINAL'] = '1' if args.run_in_new_terminal else '0'
    if getattr(args, 'validation_timeout', None) is not None:
        os.environ['LUAF_VALIDATION_TIMEOUT'] = str(args.validation_timeout)
    if getattr(args, 'loop_sleep_seconds', None) is not None:
        os.environ['LUAF_PERSISTENT_LOOP_SLEEP_SECONDS'] = str(args.loop_sleep_seconds)
    if getattr(args, 'agent_image_url', None) is not None and (args.agent_image_url or '').strip():
        os.environ['LUAF_AGENT_IMAGE_URL'] = args.agent_image_url.strip()
    if getattr(args, 'topic_file', None) is not None and (args.topic_file or '').strip():
        os.environ['LUAF_TOPIC_FILE'] = str(args.topic_file).strip()
    if getattr(args, 'topic_list', None) is not None and (args.topic_list or '').strip():
        os.environ['LUAF_TOPIC_LIST'] = str(args.topic_list).strip()
    if getattr(args, 'topic_source', None) is not None and (args.topic_source or '').strip():
        os.environ['LUAF_PERSISTENT_TOPIC_SOURCE'] = str(args.topic_source).strip().lower()

def _build_parser() -> Any:
    import argparse
    p = argparse.ArgumentParser(description='LUAF: brief -> research -> build -> validate -> launch autonomous business units.', formatter_class=argparse.RawDescriptionHelpFormatter, epilog='\nExamples:\n  luaf                 CLI menu (default; use --tui for experimental TUI)\n  luaf init            Setup wizard: .env and API keys\n  luaf init --check    Verify required env vars\n  luaf doctor          Check config and connectivity\n  luaf run             Single pipeline\n  luaf persistent      Autonomous loop until target SOL\n  luaf help            Show this help\n')
    p.add_argument('--no-tui', '-n', action='store_true', help='Use CLI menu only (default)')
    p.add_argument('--tui', action='store_true', help='Use TUI (experimental); default is CLI')
    p.add_argument('--no-color', action='store_true', help='Disable ANSI colors (also NO_COLOR=1)')
    p.add_argument('--once', '-o', action='store_true', help='Run single pipeline (same as run)')
    p.add_argument('--persistent', '-p', action='store_true', help='Run autonomous loop until target SOL')
    p.add_argument('--self-train', metavar='TOPIC', nargs='?', const='', default=None, help='Run self-train pipeline; TOPIC optional')
    # Config overrides (apply to env; used by run / persistent)
    cfg = p.add_argument_group('config (override .env)')
    cfg.add_argument('--dry-run', dest='dry_run', action='store_true', default=None, help='Publish as dry-run only (default from LUAF_DRY_RUN)')
    cfg.add_argument('--no-dry-run', dest='dry_run', action='store_false', help='Real publish (tokenization)')
    cfg.add_argument('--target-sol', type=float, default=None, metavar='N', help='Stop persistent when balance >= N SOL (default 10)')
    cfg.add_argument('--topic', type=str, default=None, metavar='TEXT', help='Brief / topic (overrides LUAF_TOPIC)')
    cfg.add_argument('--generate-agent-image', dest='generate_agent_image', action='store_true', default=None, help='Generate keyless agent image from name/description')
    cfg.add_argument('--no-generate-agent-image', dest='generate_agent_image', action='store_false', help='Do not generate agent image')
    cfg.add_argument('--agent-image-url', type=str, default=None, metavar='URL', help='Fixed image URL for published agents')
    cfg.add_argument('--claim-delay-hours', type=float, default=None, metavar='H', help='Hours after publish before claiming fees (default 24)')
    cfg.add_argument('--min-sol-to-tokenize', type=float, default=None, metavar='N', help='Below this balance, dry-run only (default 0.05)')
    cfg.add_argument('--max-steps', type=int, default=None, metavar='N', help='Max pipeline retry steps per run (default 3)')
    cfg.add_argument('--interactive', dest='interactive', action='store_true', default=None, help='Prompt for brief/name/ticker')
    cfg.add_argument('--no-interactive', dest='interactive', action='store_false', help='Use env only, no prompts')
    cfg.add_argument('--run-in-new-terminal', dest='run_in_new_terminal', action='store_true', default=None, help='Launch agent in new terminal after publish')
    cfg.add_argument('--no-run-in-new-terminal', dest='run_in_new_terminal', action='store_false', help='Do not launch in new terminal')
    cfg.add_argument('--validation-timeout', type=int, default=None, metavar='SEC', help='Agent validation subprocess timeout (default 600)')
    cfg.add_argument('--loop-sleep-seconds', type=int, default=None, metavar='SEC', help='Sleep between persistent loop iterations (default 0)')
    cfg.add_argument('--topic-file', type=str, default=None, metavar='PATH', help='Persistent: one topic per line (LUAF_TOPIC_FILE)')
    cfg.add_argument('--topic-list', type=str, default=None, metavar='LIST', help='Persistent: comma-separated topics (LUAF_TOPIC_LIST)')
    cfg.add_argument('--topic-source', type=str, default=None, choices=('single', 'env', 'file'), help='Persistent: topic source (default single)')
    sub = p.add_subparsers(dest='command', help='Command')
    init_p = sub.add_parser('init', help='Setup wizard: create .env and prompt for API keys')
    init_p.add_argument('--from-example', action='store_true', help='Non-interactive: only ensure .env exists from template')
    init_p.add_argument('--force', action='store_true', help='Allow overwriting existing keys when re-prompting')
    init_p.add_argument('--check', action='store_true', help='Validate required env vars are set; exit 0/1')
    sub.add_parser('run', help='Run single pipeline and exit')
    sub.add_parser('persistent', help='Run autonomous loop until target SOL')
    st = sub.add_parser('self-train', help='Run self-train pipeline')
    st.add_argument('topic', nargs='?', default='', help='Topic (default from env/TOPIC)')
    sub.add_parser('doctor', help='Check config, env vars, and connectivity')
    sub.add_parser('help', help='Show help and exit')
    return p

def _parse_cli() -> Any:
    import argparse
    set_cli_no_color((os.environ.get('NO_COLOR') or '').strip() != '')
    p = _build_parser()
    args = p.parse_args()
    if getattr(args, 'no_color', False):
        set_cli_no_color(True)
    if getattr(args, 'once', False):
        args.command = 'run'
    if getattr(args, 'persistent', False):
        args.command = 'persistent'
    if args.self_train is not None:
        args.command = 'self-train'
        if not hasattr(args, 'topic'):
            args.topic = args.self_train if args.self_train else ''
    for a in sys.argv[1:]:
        a = a.strip().lower()
        if a == 'run':
            args.command = 'run'
            break
        if a in ('persistent', '--persistent'):
            args.command = 'persistent'
            break
    if os.environ.get('LUAF_MODE', '').strip().lower() == 'persistent' and (not args.command or args.command not in ('run', 'self-train')):
        args.command = 'persistent'
    if not getattr(args, 'command', None) and (getattr(args, 'once', False) or getattr(args, 'persistent', False)):
        args.command = 'persistent' if getattr(args, 'persistent', False) else 'run'
    return args

def run_cli() -> None:
    """Entry point for the `luaf` console script. Parses CLI and dispatches to init, run, persistent, or interactive menu."""
    args = _parse_cli()
    _apply_cli_config(args)
    cmd = getattr(args, 'command', None)
    if cmd == 'init':
        sys.exit(run_init(args))
    if cmd == 'self-train':
        topic = (getattr(args, 'topic', '') or TOPIC).strip()[:500] or TOPIC
        sys.exit(0 if _luaf_run_self_train(topic) else 1)
    if cmd == 'persistent':
        run_profile_selection()
        run_persistent()
    elif cmd == 'run':
        run_profile_selection()
        main()
    elif cmd == 'doctor':
        sys.exit(run_doctor(args))
    elif cmd == 'help':
        _build_parser().print_help()
        sys.exit(0)
    elif cmd is not None:
        print(_style_error(f'Unknown command: {cmd}'))
        sys.exit(1)
    elif getattr(args, 'tui', False):
        run_interactive_menu()
    else:
        run_standalone_cli()


if __name__ == '__main__':
    run_cli()
