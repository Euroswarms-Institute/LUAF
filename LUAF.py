#!/usr/bin/env python3
from __future__ import annotations
import functools, hashlib, json, os, pickle, queue, random, re, subprocess, sys, tempfile, time, uuid
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
    from luaf_profiles import list_profiles as _list_profiles, get_default_profile as _get_default_profile_impl
except ImportError:
    _list_profiles = None
    _get_default_profile_impl = None
_REPO_ROOT = Path(__file__).resolve().parents[1]
_LUAF_DIR = Path(__file__).resolve().parent
load_dotenv(_REPO_ROOT / '.env')
load_dotenv(Path.cwd() / '.env')
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
USE_PLANNER = _env_bool('LUAF_USE_PLANNER', '1')
USE_DESIGNER = _env_bool('LUAF_USE_DESIGNER', '1')
SWARMS_AGENT_DOCS = "\nGenerated code MUST use swarms: from swarms import Agent; Agent(agent_name=str, agent_description=str, system_prompt=str, model_name=str, max_loops=int|'auto'); result = agent.run(task). No stubs, no placeholders. Cloud API: POST https://api.swarms.world/v1/agent/completions with agent_config and task.\n"
REQUIRED_PAYLOAD_KEYS = frozenset({'name', 'agent', 'description', 'language', 'requirements', 'useCases', 'tags', 'is_free', 'ticker'})
from luaf_designer import parse_agent_payload as _parse_agent_payload_impl, retrieve_similar_exemplars
DESIGNER_EXEMPLARS_PATH = _LUAF_DIR / 'designer_exemplars.jsonl'
def parse_agent_payload(raw: str) -> dict[str, Any]:
    return _parse_agent_payload_impl(raw, REQUIRED_PAYLOAD_KEYS)
def _retrieve_similar_exemplars(topic: str, search_snippets: str, top_k: int = 3) -> list[str]:
    return retrieve_similar_exemplars(topic, search_snippets, DESIGNER_EXEMPLARS_PATH, top_k)
_designer_prompt_path = _LUAF_DIR / 'designer_system_prompt.txt'
DESIGNER_SYSTEM_PROMPT = _designer_prompt_path.read_text(encoding='utf-8') if _designer_prompt_path.exists() else ''
if not DESIGNER_SYSTEM_PROMPT.strip():
    logger.warning('designer_system_prompt.txt not found next to LUAF.py; designer may not behave as expected')
PROFILES_DIR = _LUAF_DIR / 'profiles'
_DEFAULT_TOPIC_PROMPT = 'Generate exactly one concrete, autonomous business idea that is monetizable and tokenizable. It must make money without a frontend: e.g. API usage, token fees, data/arbitrage/sellable output, automated backends—no subscription sites, dashboards, or SaaS UIs. Reply with only that one sentence, no quotes, no explanation, no bullet points.'
_DEFAULT_PRODUCT_FOCUS = 'Product focus: Tokenized units only; revenue via API usage, token fees, data/arbitrage/sellable output—not via products that need a web frontend (no subscription UI, dashboard, or SaaS customer-facing app).'
_active_profile: Optional[dict[str, Any]] = None


def _get_default_profile() -> dict[str, Any]:
    """Return the default profile (current designer_system_prompt.txt + default topic/focus)."""
    if _get_default_profile_impl is None:
        return {
            'id': 'default',
            'display_name': 'default',
            'system_prompt': DESIGNER_SYSTEM_PROMPT,
            'topic_prompt': _DEFAULT_TOPIC_PROMPT,
            'product_focus': _DEFAULT_PRODUCT_FOCUS,
        }
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
        return None
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
    if not api_key:
        logger.warning('OPENAI_API_KEY not set; cannot generate profile from keywords.')
        return None
    system = """You generate a LUAF designer profile from keywords. Output exactly three sections, using these headers alone on their own line:
## SYSTEM_PROMPT
## TOPIC_PROMPT
## PRODUCT_FOCUS

Rules: Plain text only in all sections. No Markdown (no bold, no ## inside content, no bullet lists). Revenue must be achievable without a customer-facing web app (API, data, automation, backend only). SYSTEM_PROMPT must be a full designer system prompt (same structure as LUAF: programming excellence, size 300+ lines, utility and monetization, product focus paragraph, output rules, process, agent architecture, code quality, listing metadata, JSON format). TOPIC_PROMPT is one paragraph to generate a single business idea. PRODUCT_FOCUS is one short paragraph for the designer user message. Output nothing before ## SYSTEM_PROMPT and nothing after the PRODUCT_FOCUS content."""
    user = f"Generate a LUAF profile for these keywords: {keywords}"
    try:
        resp = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': LLM_MODEL, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'temperature': 0.7, 'max_tokens': 4096}, timeout=min(120, LLM_HTTP_TIMEOUT))
        if not resp.ok:
            logger.warning('LLM profile generation failed: {}', resp.status_code)
            return None
        content = (resp.json().get('choices') or [{}])[0].get('message', {}).get('content') or ''
        if not content.strip():
            return None
        parts = re.split(r'\n##\s+(SYSTEM_PROMPT|TOPIC_PROMPT|PRODUCT_FOCUS)\s*\n', content.strip(), flags=re.IGNORECASE)
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
            logger.warning('LLM profile response missing SYSTEM_PROMPT.')
            return None
        return {
            'id': 'generated',
            'display_name': f'Generated: {keywords[:40]}{"…" if len(keywords) > 40 else ""}',
            'system_prompt': result.get('system_prompt', ''),
            'topic_prompt': result.get('topic_prompt') or None,
            'product_focus': result.get('product_focus') or None,
        }
    except Exception as e:
        logger.warning('Generate profile from keywords failed: {}', e)
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
        return f'Unit code has {n} substantive lines; minimum required is {MIN_AGENT_LINES} (400+ preferred). No stubs, boilerplate, or examples—only fully functioning runnable code.'
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
    try:
        proc = subprocess.run([sys.executable, '-m', 'pip', 'install', '--break-system-packages', module], capture_output=True, timeout=timeout, env=os.environ.copy())
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
    try:
        for attempt in range(max_import_retries + 1):
            try:
                proc = subprocess.run([sys.executable, script_path], capture_output=True, timeout=timeout, cwd=script_dir, env=os.environ.copy())
            except subprocess.TimeoutExpired:
                return (False, f'Validation failed: timed out after {timeout}s.')
            except Exception as e:
                return (False, f'Validation failed: {e!s}')
            out = (proc.stdout or b'').decode('utf-8', errors='replace').strip()
            err = (proc.stderr or b'').decode('utf-8', errors='replace').strip()
            last_fb = f"Validation failed (exit {proc.returncode}).\nStdout:\n{out or '(empty)'}\nStderr:\n{err or '(empty)'}"
            if proc.returncode == 0:
                return (True, '')
            missing = _parse_missing_module(err)
            if not missing or attempt >= max_import_retries or missing in tried_install:
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

def _generate_topic_via_llm(api_key: str, base_url: str, model: str=LLM_MODEL) -> str:
    if not (api_key or '').strip() or not (base_url or '').strip():
        return ''
    profile = get_active_profile()
    prompt = (profile.get('topic_prompt') or _DEFAULT_TOPIC_PROMPT).strip()
    try:
        resp = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 120, 'temperature': 0.8}, timeout=min(60, LLM_HTTP_TIMEOUT))
        if not resp.ok:
            return ''
        data = resp.json()
        content = (data.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        return content.strip()[:500] or ''
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
    sections.append(f"\n## Instructions\nFollow your process: Clarify → Architect → Implement → Describe. Write as an expert programmer: type hints, defensive code, real APIs, no dead code. The agent's task must be executable to generate profit or measurable value; design for real-world utility. {product_focus} Output ONLY the single JSON object. No other text, no reasoning, no markdown. Use the swarms Agent; no stubs or placeholders; full production-quality code. The agent code must be at least 300 substantive lines (400+ preferred); no boilerplate or examples—only fully functioning runnable code. The script is validated by running it with no arguments (python script.py). Make all CLI arguments optional (e.g. argparse: use default=, never required=True).\n**Required top-level keys (exactly these, no others): {required_keys}. agent = full Python code string; useCases = array of {{{{title, description}}}}; requirements = array of {{{{package, installation}}}}; is_free = true.")
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
    while True:
        if _tui_stop_requested:
            _tui_stopped_reason = 'stop'
            logger.info('Stop requested; exiting persistent loop.')
            return
        balance = get_solana_balance(pubkey, SOLANA_RPC_URL)
        logger.info('Persistent: balance={:.4f} SOL, target={} SOL', balance, PERSISTENT_TARGET_SOL)
        if balance >= PERSISTENT_TARGET_SOL:
            _tui_stopped_reason = 'target'
            logger.info('Target SOL reached ({} >= {}). Exiting.', balance, PERSISTENT_TARGET_SOL)
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
                if PERSISTENT_LOOP_SLEEP_SECONDS > 0:
                    time.sleep(PERSISTENT_LOOP_SLEEP_SECONDS)
                continue
            try:
                payload = parse_agent_payload(raw)
            except ValueError as e:
                logger.error('Parse: {}', e)
                vfb = f'Parse: {e!s}'
                if PERSISTENT_LOOP_SLEEP_SECONDS > 0:
                    time.sleep(PERSISTENT_LOOP_SLEEP_SECONDS)
                continue
        if payload is None:
            logger.warning('No payload; skipping iteration.')
            if PERSISTENT_LOOP_SLEEP_SECONDS > 0:
                time.sleep(PERSISTENT_LOOP_SLEEP_SECONDS)
            continue
        code = str(payload.get('agent') or '')
        sk_fb = _skeleton_validation_feedback(code)
        if sk_fb is not None:
            vfb = sk_fb
            logger.warning('Unit code too short or skeleton: {}', sk_fb[:200])
            if PERSISTENT_LOOP_SLEEP_SECONDS > 0:
                time.sleep(PERSISTENT_LOOP_SLEEP_SECONDS)
            continue
        ok, fb = run_agent_code_validation(code, VALIDATION_TIMEOUT)
        if not ok:
            vfb = fb
            logger.warning('Unit validation failed: {}', fb[:1500])
            logger.debug('Validation full feedback: {}', fb)
            if _ask_publish_without_validation():
                logger.info('Publishing without validation (user confirmed).')
            else:
                if PERSISTENT_LOOP_SLEEP_SECONDS > 0:
                    time.sleep(PERSISTENT_LOOP_SLEEP_SECONDS)
                continue
        n, t = ((payload.get('name') or '').strip(), (payload.get('ticker') or '').strip())
        used_n.add(n.lower())
        used_t.add(t.upper())
        dry_run_this = DRY_RUN
        if not DRY_RUN:
            bal = get_solana_balance(pubkey, SOLANA_RPC_URL)
            if bal < PERSISTENT_MIN_SOL_TO_TOKENIZE:
                dry_run_this = True
                logger.info('Insufficient balance ({:.4f} < {}); dry-run publish.', bal, PERSISTENT_MIN_SOL_TO_TOKENIZE)
        res = publish_agent(payload, swarms_key, pkey or '', dry_run_this, creator_wallet=cwallet)
        run_task = run_task_override or brief
        run_ok, run_out = run_agent_once(code, run_task, timeout=VALIDATION_TIMEOUT)
        if run_ok:
            logger.info('Run unit once: OK. Output length={}', len(run_out or ''))
        else:
            logger.warning('Run unit once: {}', run_out[:300] if run_out else 'failed')
        if res and (not dry_run_this):
            lu, rid, ca = (res.get('listing_url'), res.get('id'), res.get('token_address'))
            if lu or rid or ca:
                published_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                append_agent_to_registry(AGENTS_REGISTRY_PATH, name=n, ticker=t, listing_url=lu, id_=rid, token_address=ca, published_at=published_at)
                _tui_session_published += 1
                _tui_session_last_name = n or '—'
        run_delayed_claim_pass(AGENTS_REGISTRY_PATH, pkey or '', swarms_key, CLAIM_DELAY_HOURS)
        if PERSISTENT_LOOP_SLEEP_SECONDS > 0:
            time.sleep(PERSISTENT_LOOP_SLEEP_SECONDS)

def run_standalone_cli() -> None:
    run_profile_selection()
    _menu = '\n  ╭─────────────────────────────────────╮\n  │  LUAF  —  brief → research → launch  │\n  ╰─────────────────────────────────────╯\n    1. Pipeline   (design & launch autonomous unit)\n    2. Persistent (autonomous loop until target SOL)\n    0. Exit\n'
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
            print('  Unknown. Use 1 (Pipeline), 2 (Autonomous loop), or 0 (Exit).')

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
        config: dict[str, Any] = {'get_creator_pubkey': get_creator_pubkey, 'get_solana_balance': get_solana_balance, 'load_agents_registry': lambda: _load_agents_registry(AGENTS_REGISTRY_PATH), 'target_sol': PERSISTENT_TARGET_SOL, 'registry_path': AGENTS_REGISTRY_PATH, 'rpc_url': SOLANA_RPC_URL, 'set_stop_requested': _set_stop, 'get_tui_state': _get_state, 'log_queue': _log_queue, 'profile_options': profile_options_list, 'on_profile_selected': _on_profile_selected}
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
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    base_url = (os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1').strip()
    swarms_key = (os.environ.get('SWARMS_API_KEY') or SWARMS_API_KEY_FALLBACK or '').strip()
    if not api_key:
        logger.error('OPENAI_API_KEY not set')
        return
    if not swarms_key and (not DRY_RUN):
        logger.warning('SWARMS_API_KEY not set')
    pkey = get_private_key_from_env()
    cwallet = (os.environ.get('SOLANA_PUBKEY') or os.environ.get('CREATOR_WALLET') or '').strip()
    if not DRY_RUN and (not (pkey or '').strip()):
        logger.warning('No SOLANA_PRIVATE_KEY; skipping publish.')
    if not DRY_RUN and pkey and (not cwallet):
        logger.warning('No CREATOR_WALLET; tokenized publish may fail.')
    if DRY_RUN:
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
    for step in range(1, MAX_STEPS + 1):
        logger.info('Step {}/{}', step, MAX_STEPS)
        if USE_MULTIHOP_WEB_RAG:
            snip = _multihop_web_rag(brief, max_hops=RAG_MAX_HOPS, threshold=RAG_CONVERGE_THRESHOLD, total_k=RAG_TOTAL_K, ddg_per_hop=RAG_DDG_PER_HOP)
            if not snip:
                snip = search_duckduckgo(f'{brief} {random.choice(SEARCH_VARIANT_SUFFIXES)}', max_results=DUCKDUCKGO_MAX_RESULTS)
        else:
            snip = search_duckduckgo(f'{brief} {random.choice(SEARCH_VARIANT_SUFFIXES)}', max_results=DUCKDUCKGO_MAX_RESULTS)
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
        _save_generated_agent(code, payload.get('name'), payload.get('ticker'), step)
        ok, fb = run_agent_code_validation(code, VALIDATION_TIMEOUT)
        if ok:
            logger.info('Validation OK (step {}). Publishing.', step)
            n, t = ((payload.get('name') or '').strip(), (payload.get('ticker') or '').strip())
            used_n.add(n.lower())
            used_t.add(t.upper())
            res = publish_agent(payload, swarms_key, pkey or '', DRY_RUN, creator_wallet=cwallet)
            if res and (not DRY_RUN):
                lu, rid, ca = (res.get('listing_url'), res.get('id'), res.get('token_address'))
                if lu or rid or ca:
                    append_agent_to_registry(AGENTS_REGISTRY_PATH, name=n, ticker=t, listing_url=lu, id_=rid, token_address=ca)
            break
        vfb = fb
        logger.warning('Unit validation failed (step {}): {}', step, fb[:1500])
        logger.debug('Validation full feedback (step {}): {}', step, fb)
        if _ask_publish_without_validation():
            logger.info('Publishing without validation (user confirmed).')
            n, t = ((payload.get('name') or '').strip(), (payload.get('ticker') or '').strip())
            used_n.add(n.lower())
            used_t.add(t.upper())
            res = publish_agent(payload, swarms_key, pkey or '', DRY_RUN, creator_wallet=cwallet)
            if res and (not DRY_RUN):
                lu, rid, ca = (res.get('listing_url'), res.get('id'), res.get('token_address'))
                if lu or rid or ca:
                    append_agent_to_registry(AGENTS_REGISTRY_PATH, name=n, ticker=t, listing_url=lu, id_=rid, token_address=ca)
            break
    else:
        logger.warning('Max steps ({}) reached.', MAX_STEPS)
    if CLAIM_FEES_AFTER_RUN and (pkey or '').strip():
        for e in _load_agents_registry(AGENTS_REGISTRY_PATH):
            ca = e.get('token_address') or e.get('ca')
            if not ca or len(ca) < 32:
                continue
            logger.info('Claiming fees for {}', ca[:16])
            cr = claim_fees(ca, pkey.strip(), api_key=swarms_key)
            if cr and cr.get('success'):
                logger.info('Claimed: sig={} sol={}', cr.get('signature'), cr.get('amountClaimedSol'))
            elif cr:
                logger.warning('Claim: {}', cr)
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

def _parse_cli() -> Any:
    import argparse
    p = argparse.ArgumentParser(description='LUAF: brief → research → build → validate → launch autonomous business units.', formatter_class=argparse.RawDescriptionHelpFormatter, epilog='\nExamples:\n  LUAF.py              Interactive menu (TUI if available)\n  LUAF.py --no-tui     CLI menu only, no TUI\n  LUAF.py --once       Run single pipeline and exit (no autonomous loop)\n  LUAF.py --persistent Run autonomous loop until target SOL\n  LUAF.py run          Same as --once (legacy)\n  LUAF.py --self-train [TOPIC]  Self-train pipeline\n')
    p.add_argument('--no-tui', '-n', action='store_true', help='Disable TUI; use CLI menu only')
    p.add_argument('--once', '-o', action='store_true', help='Run single pipeline and exit (do not run autonomously)')
    p.add_argument('--persistent', '-p', action='store_true', help='Run autonomous loop until target SOL')
    p.add_argument('--self-train', metavar='TOPIC', nargs='?', const='', default=None, help='Run self-train pipeline; TOPIC optional (default from env/TOPIC)')
    p.add_argument('run_or_persistent', nargs='?', choices=['run', 'persistent'], help="Legacy: 'run' = single pipeline, 'persistent' = autonomous loop")
    args, rest = p.parse_known_args()
    pos = getattr(args, 'run_or_persistent', None)
    if pos == 'run':
        args.once = True
    if pos == 'persistent':
        args.persistent = True
    for a in sys.argv[1:]:
        a = a.strip().lower()
        if a == 'run':
            args.once = True
            break
        if a in ('persistent', '--persistent'):
            args.persistent = True
            break
    if os.environ.get('LUAF_MODE', '').strip().lower() == 'persistent' and (not (args.once or args.self_train is not None)):
        args.persistent = True
    return args
if __name__ == '__main__':
    args = _parse_cli()
    if args.self_train is not None:
        topic = (args.self_train if args.self_train else TOPIC).strip()[:500] or TOPIC
        sys.exit(0 if _luaf_run_self_train(topic) else 1)
    if args.persistent:
        run_profile_selection()
        run_persistent()
    elif args.once:
        run_profile_selection()
        main()
    elif args.no_tui:
        run_standalone_cli()
    else:
        run_interactive_menu()
