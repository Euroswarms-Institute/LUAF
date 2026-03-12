#!/usr/bin/env python3
"""
Build and publish the LUAF agent payload to Swarms (e.g. swarms.world).
Run from the repo root with SWARMS_API_KEY set. Use --dry-run to build payload without publishing.
Repository: https://github.com/Euroswarms-Institute/LUAF
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Run from repo root so LUAF is importable
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load env before importing LUAF (it uses dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
    load_dotenv(Path.cwd() / ".env")
except ImportError:
    pass

from LUAF import publish_agent, get_private_key_from_env, get_creator_pubkey  # noqa: E402

# -----------------------------------------------------------------------------
# Paste agent code here to use instead of reading from file. If non-empty, this
# is used as the "agent" payload; otherwise the script reads from LUAF.py (or
# --luaf-path). Use this when you want to publish a specific version or snippet.
# -----------------------------------------------------------------------------
AGENT_CODE_MANUAL = """
#!/usr/bin/env python3
from __future__ import annotations
import functools, hashlib, json, os, pickle, queue, random, re, subprocess, sys, tempfile, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
_RE_TRAILING_COMMA = re.compile(',(\\s*[}\\]])')
import requests
from dotenv import load_dotenv
from loguru import logger
_TEXTUAL_AVAILABLE = False
try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
    from textual.widgets import Footer, Header, RichLog, Static
    from textual import on
    from textual.worker import Worker
    _TEXTUAL_AVAILABLE = True
except ImportError:
    pass
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
BASE_URL = 'https://swarms.world'
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
DESIGN_ANGLES = ('backtesting', 'real-time alerts', 'multi-DEX comparison', 'risk metrics', 'historical patterns', 'on-chain metrics', 'tutorial / step-by-step', 'best practices', 'revenue-generating', 'lead gen')
SEARCH_VARIANT_SUFFIXES = ('best practices', 'tutorial', 'guide', '2026', 'overview')
QUALITY_PACKAGES_BY_CATEGORY: dict[str, list[str]] = {'core': ['swarms', 'loguru'], 'http': ['requests', 'httpx'], 'search': ['ddgs'], 'data_analytics': ['pandas', 'numpy'], 'defi_trading': ['ccxt', 'web3'], 'parsing_html': ['beautifulsoup4', 'lxml'], 'async_io': ['aiohttp', 'aiofiles'], 'config_env': ['pydantic', 'python-dotenv'], 'crypto_utils': ['base58', 'ecdsa'], 'time_scheduling': ['schedule', 'apscheduler'], 'storage': ['sqlalchemy', 'tinydb']}
QUALITY_CATEGORY_KEYWORDS: dict[str, list[str]] = {'core': [], 'http': ['api', 'rest', 'http', 'fetch', 'request', 'webhook', 'scraper', 'crawl'], 'search': ['search', 'duckduckgo', 'google', 'find', 'lookup', 'discover'], 'data_analytics': ['data', 'analytics', 'backtest', 'metric', 'chart', 'pandas', 'numpy', 'trading', 'strategy'], 'defi_trading': ['defi', 'crypto', 'trading', 'dex', 'swap', 'token', 'blockchain', 'ethereum', 'solana', 'ccxt', 'web3'], 'parsing_html': ['scrape', 'html', 'parse', 'web page', 'beautifulsoup', 'lxml'], 'async_io': ['async', 'concurrent', 'parallel', 'aio'], 'config_env': ['config', 'env', 'settings', 'dotenv'], 'crypto_utils': ['wallet', 'sign', 'crypto', 'blockchain', 'solana', 'ethereum'], 'time_scheduling': ['schedule', 'cron', 'periodic', 'alert', 'reminder'], 'storage': ['database', 'sql', 'store', 'persist', 'tinydb', 'sqlalchemy']}

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
HTTP_PUBLISH_TIMEOUT = 350
HTTP_CLAIM_FEES_TIMEOUT = 3099
LLM_HTTP_TIMEOUT = 1200
DESIGNER_AGENT_ARCHITECTURE = (os.environ.get('LUAF_DESIGNER_AGENT_ARCHITECTURE') or 'agent').strip().lower()
if DESIGNER_AGENT_ARCHITECTURE not in ('agent', 'react'):
    DESIGNER_AGENT_ARCHITECTURE = 'agent'
DESIGNER_USE_DIRECT_API = _env_bool('LUAF_DESIGNER_USE_DIRECT_API', '1')
USE_PLANNER = _env_bool('LUAF_USE_PLANNER', '1')
USE_DESIGNER = _env_bool('LUAF_USE_DESIGNER', '1')
SWARMS_AGENT_DOCS = "\nGenerated code MUST use swarms: from swarms import Agent; Agent(agent_name=str, agent_description=str, system_prompt=str, model_name=str, max_loops=int|'auto'); result = agent.run(task). No stubs, no placeholders. Cloud API: POST https://api.swarms.world/v1/agent/completions with agent_config and task.\n"
REQUIRED_PAYLOAD_KEYS = frozenset({'name', 'agent', 'description', 'language', 'requirements', 'useCases', 'tags', 'is_free', 'ticker'})
DESIGNER_SYSTEM_PROMPT = '\nYou design autonomous business units that create measurable value. You are an expert agent designer and an expert programmer. You produce production-grade, runnable Swarms agents from a topic and search context. Your code is precise, correct, and maintainable—you think in types, edge cases, and failure modes before writing a line.\n\n**Programming excellence (your identity):** You write like a senior engineer. Use type hints on all function signatures and return types. Prefer small, pure functions and clear data flow; avoid deep nesting and globals. Validate inputs at boundaries and fail fast with clear errors. Use dataclasses or typed dicts for structured data. Prefer list/dict comprehensions and generator patterns where they improve readability. Handle I/O and network in dedicated helpers with explicit timeouts and retries; never let a single failing call take down the agent. Log at decision points and on errors, not in tight loops. Your code must run correctly on first execution: no "run twice" or "comment out this line" hacks. Read the search context and use real APIs, patterns, and library idioms—not pseudocode or placeholder logic.\n\n**Size (mandatory):** The agent code (the "agent" string in your JSON) must be at least 300 substantive lines (non-empty, non-comment); 400+ lines is the target. No stubs, boilerplate, or example snippets—only fully functioning, runnable code. Count non-empty, substantive lines; comments and blank lines do not count. Below 300 lines is rejected; 300+ is acceptable, 400+ preferred.\n\n**Utility and monetization (mandatory):** Regardless of the topic, design the agent so that when run it can create tangible value (revenue, leads, arbitrage, sellable output, data for trading, etc.). The task the agent completes must have a clear path to monetization or measurable business outcome. Avoid toy, demo-only, or purely illustrative agents.\n\n**Output rules (mandatory):** Your entire response must be exactly one JSON object. Nothing else. No reasoning, no "Here is the agent…", no markdown (no ```), no explanatory text before or after the JSON. The response body must be parseable by json.loads() from start to finish.\n\n## Your process (follow in order)\n1. **Clarify** – From the topic and search context, define the agent\'s purpose, primary inputs/outputs, and key external dependencies (APIs, data sources, tools).\n2. **Architect** – Decide the structure: config/env, helper functions, Swarms Agent setup, main entrypoint. Plan retries, timeouts, and error handling for every external call.\n3. **Implement** – Write the full Python code (Python 3.10+): real APIs (or os.environ.get for keys), real logic, real error handling. Use type hints on every function (args and return); use typing (List, Dict, Optional, etc.) where helpful. Prefer dataclasses or TypedDict for structured data. Write defensive code: validate inputs, handle None and empty collections, use timeouts and retries on all external calls. Ensure the agent runs as a single script. When a code skeleton is supplied, expand it into a complete, correct implementation; do not leave placeholders or stubs.\n4. **Describe** – Write a clear description (2–4 sentences typical; longer when needed) and at least 3 concrete useCases (3–5 typical; more allowed if relevant) with title and description each. Tags: comma-separated, no comma inside a tag.\n\n## Agent code architecture (required structure)\n- **Imports and config** – Standard library first, then third-party (requests, ddgs, swarms, etc.). Read configuration from environment only (os.environ.get); never hardcode secrets. Model name: use a sensible default (e.g. gpt-4o-mini) with env override. max_loops: default (e.g. 5) with env override.\n- **Helpers** – One or more focused functions for: fetching data (search, APIs), parsing, validation. Each helper must have a single responsibility, handle errors, and use timeouts (30–120s per HTTP call; designer picks per call). External HTTP calls must use retries with backoff for transient failures.\n- **Swarms Agent** – Construct the swarms Agent with agent_name, agent_description, system_prompt, model_name, max_loops. The system_prompt must contain full, concrete instructions for the LLM (behavior, input/output format, constraints); no placeholder instructions.\n- **Entrypoint** – The agent must be runnable via if __name__ == "__main__": only (direct script run). Call agent.run(task), then print or return the result. No interactive prompts unless the topic explicitly requires them. **Validation runs the script with no arguments** (python script.py). Make all CLI arguments optional (e.g. in argparse use add_argument with default=, never required=True) so the script runs when invoked with zero arguments.\n- **Logging** – Prefer loguru (from loguru import logger) for important steps and errors; be consistent with stdout/stderr for result vs progress/errors.\n- **HTTP and I/O** – For web search use: from ddgs import DDGS; DDGS().text(query, max_results=N). When the agent performs web search, include ddgs in requirements. Treat HTTP 2xx (including 200, 202) as success; do not raise on 2xx. For REST APIs use requests with timeout= and raise_for_status(); catch requests.RequestException and log or re-raise with context. For urllib3.util.retry.Retry use allowed_methods= (e.g. allowed_methods=["GET", "POST"]), not the deprecated method_whitelist=.\n- **Temp files** – Use the tempfile module; clean up in finally or context manager. When opening files, use encoding="utf-8" and errors="replace".\n\n## Code quality (non-negotiable)\n- **Quality packages (mandatory)** – A "Required quality packages" section is provided per topic, with packages grouped by category. You MUST use at least one package from each listed category in your agent code and in requirements. Select packages that match the topic; use more when they add value. This ensures production-quality agents, not minimal stubs.\n- **No placeholders** – No TODO, pass, NotImplementedError, example.com, or fake URLs. Every branch must do something real or fail explicitly.\n- **Real dependencies** – requirements must list every third-party package used (at least swarms and loguru; plus packages from the required quality-packages list for this topic). Unpinned: e.g. {"package": "swarms", "installation": "pip install swarms"}.\n- **Error handling** – Prefer specific exceptions (requests.RequestException, json.JSONDecodeError, etc.). Use try/except with logging; avoid bare except. For subprocess/system ops use real subprocess.run or os.system with timeouts where possible. Never swallow exceptions without logging; re-raise or log and raise with context.\n- **Complete implementation** – No shortcuts; full implementations only. The agent code must be at least 300 substantive lines (400+ preferred). No stubs, boilerplate, or examples—only fully functioning runnable code. Every function must be used; dead code is forbidden.\n- **Types and structure** – Annotate function parameters and return types. Use typing.List, typing.Dict, Optional, etc. where they clarify contracts. Prefer explicit structure (dataclasses, TypedDict) over loose dicts when the shape is fixed.\n- **Credentials** – Always os.environ.get for API keys and secrets; never hardcode.\n- **No PII** – Do not include PII or real user data in examples (in code or in useCases/description).\n\n## Listing metadata\n- **name** – Short, memorable, unique. Alphanumeric and spaces only; no special characters. No reuse of names in "Used names". No emoji.\n- **ticker** – Short uppercase symbol (e.g. DEFI, ALERT). Alphanumeric only; no special characters. No reuse of tickers in "Used tickers".\n- **description** – What the agent does, for whom, key capability. Technical but readable. Longer than 2–4 sentences allowed when needed. No emoji.\n- **useCases** – Array of at least 3 objects (3–5 typical): {"title": "...", "description": "..."}. Each use case concrete (e.g. "Backtest a strategy on ETH/USDT" not "Backtesting"). No emoji.\n- **tags** – Comma-separated string; each tag must not contain commas. Domain, method, audience (e.g. "DeFi,backtesting,Python,API"). No emoji.\n- **language** – "python" (or as appropriate).\n- **is_free** – Must always be the boolean true (not the string "true").\n\n## Topic overrides\nIf the topic (or user instructions) specifies an agent name or ticker exactly, use those exactly. Otherwise generate a distinct name and ticker.\n\n## Template (when provided)\nTemplate skeleton and usage are guidance only; you may deviate if the topic clearly requires it.\n\n## Similar agents / design context (when provided)\nUse it only for inspiration and structure; do not copy code or metadata verbatim. Your output must remain your single JSON object.\n\n## JSON output format (mandatory)\nYour **entire** response must be the single JSON object: no characters before the opening { or after the closing }. Output must be instantly publication-ready (no post-processing needed).\n\n**Forbidden:** Any text, reasoning, or explanation before the opening {. Any text, summary, or "Done" after the closing }. Markdown code fences (```json or ```). The key private_key. Placeholder or empty values for required keys.\n\nOutput valid JSON only; no trailing commas. Suggested key order for readability: name, ticker, description, agent, useCases, tags, requirements, language, is_free.\n\nRequired top-level keys:\n- name (string)\n- ticker (string, short uppercase)\n- description (string)\n- agent (string: full Python code; literal newlines in the string are fine)\n- useCases (array of {"title": string, "description": string}); at least 3 items\n- tags (string, comma-separated; no comma inside a tag)\n- requirements (array of {"package": string, "installation": string}); MUST include {"package": "swarms", "installation": "pip install swarms"}\n- language (string)\n- is_free (boolean true only)\n\nDo NOT include private_key. Do NOT wrap the output in ``` or any other formatting.\n'

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
    prompt = 'Generate exactly one concrete, autonomous business idea that is monetizable. It should be an idea that can run with minimal oversight and create real value (revenue, leads, data, arbitrage, sellable output). Reply with only that one sentence, no quotes, no explanation, no bullet points.'
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
        raw = get_agent_payload_from_llm(topic=data['topic'], search_snippets=data.get('search_snippets', ''), model=data.get('model', LLM_MODEL), api_key=data.get('api_key', ''), base_url=data.get('base_url', 'https://api.openai.com/v1'), existing_names=data.get('existing_names'), existing_tickers=data.get('existing_tickers'), temperature=data.get('temperature'), validation_feedback=data.get('validation_feedback'), template_id=data.get('template_id'), retrieved_exemplars=data.get('retrieved_exemplars'))
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
        payload = {'topic': topic, 'search_snippets': search_snippets, 'model': model, 'api_key': api_key, 'base_url': base_url, 'existing_names': list(existing_names) if existing_names is not None else [], 'existing_tickers': list(existing_tickers) if existing_tickers is not None else [], 'temperature': temperature, 'validation_feedback': validation_feedback, 'template_id': template_id, 'retrieved_exemplars': retrieved_exemplars if retrieved_exemplars is not None else []}
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

def _build_designer_user_message(topic: str, search_snippets: str, existing_names: Optional[Iterable[str]], existing_tickers: Optional[Iterable[str]], validation_feedback: Optional[str], template: Any, retrieved_exemplars: Optional[list[str]]=None) -> str:
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
    sections.append(f"\n## Instructions\nFollow your process: Clarify → Architect → Implement → Describe. Write as an expert programmer: type hints, defensive code, real APIs, no dead code. The agent's task must be executable to generate profit or measurable value; design for real-world utility. Output ONLY the single JSON object. No other text, no reasoning, no markdown. Use the swarms Agent; no stubs or placeholders; full production-quality code. The agent code must be at least 300 substantive lines (400+ preferred); no boilerplate or examples—only fully functioning runnable code. The script is validated by running it with no arguments (python script.py). Make all CLI arguments optional (e.g. argparse: use default=, never required=True).\n**Required top-level keys (exactly these, no others): {required_keys}. agent = full Python code string; useCases = array of {{{{title, description}}}}; requirements = array of {{{{package, installation}}}}; is_free = true.")
    if validation_feedback and validation_feedback.strip():
        sections.append('\n## Previous validation failure (fix before re-outputting)\nAddress every line of the validation error below. Fix all reported issues (imports, syntax, runtime). Then output only the corrected JSON object with no other text.\n\n' + validation_feedback.strip())
    if template:
        if getattr(template, 'usage_instructions', None):
            sections.append('\n## Template usage\n' + template.usage_instructions)
        if getattr(template, 'code_skeleton', None):
            sections.append('\n## Code skeleton (expand into full implementation)\n\n' + template.code_skeleton)
    return '\n'.join(sections).strip()

def get_agent_payload_from_llm(topic: str, search_snippets: str, model: str, api_key: str, base_url: str, use_swarms_agent: bool=True, existing_names: Optional[Iterable[str]]=None, existing_tickers: Optional[Iterable[str]]=None, temperature: Optional[float]=None, validation_feedback: Optional[str]=None, template_id: Optional[str]=None, retrieved_exemplars: Optional[list[str]]=None) -> str:
    if temperature is None:
        temperature = LLM_TEMPERATURE
    template = None
    if template_id and (template_id := template_id.strip()) and (_get_template is not None):
        template = _get_template(template_id)
        if template:
            logger.info('Using template: {}', template_id)
    system = DESIGNER_SYSTEM_PROMPT.strip() + '\n\nSWARMS REF:' + SWARMS_AGENT_DOCS
    if template and getattr(template, 'system_fragment', None):
        system += '\n\n' + (template.system_fragment or '')
    user = _build_designer_user_message(topic=topic, search_snippets=search_snippets, existing_names=existing_names, existing_tickers=existing_tickers, validation_feedback=validation_feedback, template=template, retrieved_exemplars=retrieved_exemplars)
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

def _strip_json_code_fence(text: str) -> str:
    if not text:
        return text
    t = text.strip()
    if not t:
        return ''
    if t.startswith('```'):
        nl = t.find('\n')
        if nl != -1:
            t = t[nl + 1:]
        if t.endswith('```'):
            t = t[:t.rfind('```')].rstrip()
    return t.strip()

def _fix_common_json_issues(s: str) -> str:
    return _RE_TRAILING_COMMA.sub('\\1', s) if s else s

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

def _extract_first_json_object(text: str) -> str:
    s = _extract_json_object_spans(text)
    return text[s[0][0]:s[0][1]] if s else ''

def _extract_last_json_object(text: str) -> str:
    s = _extract_json_object_spans(text)
    return text[s[-1][0]:s[-1][1]] if s else ''

def parse_agent_payload(raw: str) -> dict[str, Any]:
    if not (raw or '').strip():
        raise ValueError('Empty LLM output')
    js = _extract_first_json_object(_strip_json_code_fence(raw))
    if not js:
        raise ValueError('No JSON object found')
    try:
        payload = json.loads(_fix_common_json_issues(js))
    except json.JSONDecodeError as e:
        raise ValueError(f'Invalid JSON: {e}') from e
    if not isinstance(payload, dict):
        raise ValueError('Not a JSON object')
    missing = REQUIRED_PAYLOAD_KEYS - set(payload.keys())
    if missing:
        raise ValueError(f'Missing keys: {sorted(missing)}')
    payload['is_free'] = True
    return payload

def publish_agent(payload: dict[str, Any], api_key: str, private_key: str, dry_run: bool, image_url: Optional[str]=None, creator_wallet: Optional[str]=None) -> Optional[dict[str, Any]]:
    tokenized = not dry_run
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
        resp = requests.post(f"{BASE_URL.rstrip('/')}/api/add-agent", headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json=out, timeout=HTTP_PUBLISH_TIMEOUT)
        status_code, data = (resp.status_code, _resp_json(resp))
    except requests.RequestException as e:
        logger.error('Publish failed: {}', e)
        return None
    if status_code >= 400:
        logger.error('Publish failed: {} {}', status_code, data)
        return data
    logger.info('Success: {} id={} ca={}', data.get('listing_url'), data.get('id'), data.get('token_address', 'N/A'))
    return data

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

def get_solana_balance(pubkey: Optional[str], rpc_url: str=SOLANA_RPC_URL) -> float:
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

def _load_agents_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
        return d if isinstance(d, list) else []
    except (json.JSONDecodeError, OSError):
        return []

def append_agent_to_registry(path: Path, name: str, ticker: str, listing_url: Optional[str]=None, id_: Optional[str]=None, token_address: Optional[str]=None, published_at: Optional[str]=None) -> None:
    reg = _load_agents_registry(path)
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

def claim_fees(ca: str, private_key: str, api_key: Optional[str]=None) -> Optional[dict[str, Any]]:
    headers: dict[str, str] = {'Content-Type': 'application/json'}
    if api_key and api_key.strip():
        headers['Authorization'] = f'Bearer {api_key.strip()}'
    try:
        resp = requests.post(f"{BASE_URL.rstrip('/')}/api/product/claimfees", headers=headers, json={'ca': ca, 'privateKey': private_key}, timeout=HTTP_CLAIM_FEES_TIMEOUT)
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
    reg = _load_agents_registry(registry_path)
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
_log_sink_id: Optional[int] = None
_balance_cache: tuple[float, float] = (0.0, 0.0)
_BALANCE_CACHE_TTL_SECONDS = 60.0
_tui_stop_requested: bool = False
_tui_current_topic: str = ''
_tui_session_published: int = 0
_tui_session_last_name: str = ''
_tui_stopped_reason: str = ''

def _add_log_sink_for_tui() -> None:
    global _log_sink_id
    if _log_sink_id is not None:
        return

    def _sink(msg: Any) -> None:
        try:
            r = msg.record
            t = r['time'].strftime('%H:%M:%S')
            lvl = (r['level'].name or 'LOG').ljust(8)
            _log_queue.put(f"{t} | {lvl} | {r['message']}")
        except Exception:
            _log_queue.put(str(msg))
    _log_sink_id = logger.add(_sink, level=0)

def _remove_log_sink_for_tui() -> None:
    global _log_sink_id
    if _log_sink_id is not None:
        try:
            logger.remove(_log_sink_id)
        except Exception:
            pass
        _log_sink_id = None

def _run_pipeline_with_brief(brief: str) -> None:
    os.environ['LUAF_DESIGN_BRIEF'] = (brief or TOPIC).strip()
    main()
if _TEXTUAL_AVAILABLE:

    class LUAFApp(App[None]):
        TITLE = 'LUAF'
        SUB_TITLE = 'tokenomics dashboard'
        CSS = '\n        App {\n            background: #0a0b0d;\n            overflow: hidden;\n        }\n        Screen {\n            overflow: hidden;\n            height: 100%;\n        }\n        #app-body {\n            height: 100%;\n            overflow: hidden;\n        }\n        Header {\n            background: #0f1114;\n            color: #8b9dc3;\n            text-style: bold;\n            border-bottom: solid #1a1f2e;\n        }\n        Footer {\n            background: #0f1114;\n            color: #5a6478;\n            border-top: solid #1a1f2e;\n        }\n        #dashboard {\n            height: auto;\n            margin: 0 2;\n            padding: 0;\n        }\n        #dashboard-strip {\n            height: auto;\n            layout: horizontal;\n        }\n        .metric-card {\n            width: 1fr;\n            min-width: 8;\n            padding: 0 1;\n            margin-right: 1;\n            background: #11141a;\n            border: solid #1e2530;\n        }\n        .metric-card:last-child {\n            margin-right: 0;\n        }\n        .metric-label {\n            color: #5a6478;\n            padding-bottom: 0;\n        }\n        .metric-value {\n            color: #7dd3fc;\n            text-style: bold;\n        }\n        .metric-value.warn {\n            color: #fbbf24;\n        }\n        .metric-value.success {\n            color: #34d399;\n        }\n        #hero {\n            height: auto;\n            padding: 0 2;\n            background: #11141a;\n            border: solid #1e2530;\n            margin: 0 2;\n        }\n        #hero Static {\n            color: #8b9dc3;\n            text-style: bold;\n        }\n        #hero-tagline {\n            color: #4b5563;\n            text-style: none;\n            padding-top: 0;\n        }\n        #log-section {\n            height: 1fr;\n            min-height: 0;\n            margin: 0 2;\n        }\n        #log-title {\n            color: #8b9dc3;\n            padding: 0;\n        }\n        #log-scroll {\n            height: 1fr;\n            min-height: 0;\n            padding: 0;\n            scrollbar-background: #11141a;\n        }\n        #log {\n            min-height: 100%;\n            background: #070809;\n            padding: 0 1;\n            border: solid #1e2530;\n        }\n        '
        BINDINGS = [Binding('q', 'quit', 'Quit', show=True), Binding('s', 'request_stop', 'Stop', show=True)]

        def compose(self) -> ComposeResult:
            with Vertical(id='app-body'):
                yield Header(show_clock=True)
                with Container(id='hero'):
                    yield Static('[bold #e6b422]LUAF[/] · brief → research → build → validate → launch', id='hero-tagline')
                with Container(id='dashboard'):
                    with Horizontal(id='dashboard-strip'):
                        with Vertical(classes='metric-card'):
                            yield Static('BALANCE (SOL)', classes='metric-label')
                            yield Static('—', id='metric-balance', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('TARGET (SOL)', classes='metric-label')
                            yield Static('—', id='metric-target', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('LAUNCHED UNITS', classes='metric-label')
                            yield Static('0', id='metric-agents', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('STATUS', classes='metric-label')
                            yield Static('Ready', id='metric-status', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('CURRENT BRIEF', classes='metric-label')
                            yield Static('—', id='metric-topic', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('LAUNCHED THIS RUN', classes='metric-label')
                            yield Static('0', id='metric-session-published', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('LAST', classes='metric-label')
                            yield Static('—', id='metric-last', classes='metric-value')
                with Vertical(id='log-section'):
                    yield Static('[bold #7ec8e3]LIVE FEED[/]  [dim] s Stop   q Quit[/]', id='log-title')
                    with ScrollableContainer(id='log-scroll'):
                        yield RichLog(highlight=True, markup=True, id='log')
                yield Footer()

        def on_mount(self) -> None:
            global _tui_stop_requested, _tui_current_topic, _tui_session_published, _tui_session_last_name, _tui_stopped_reason
            _tui_stop_requested = False
            _tui_current_topic = ''
            _tui_session_published = 0
            _tui_session_last_name = ''
            _tui_stopped_reason = ''
            self._refresh_dashboard()
            log_widget = self.query_one('#log', RichLog)
            log_widget.write('[bold]Autonomous mode.[/] Persistent loop started. Log below.')
            _add_log_sink_for_tui()
            self._log_drain_timer = self.set_interval(0.15, self._drain_log_queue)
            self._persistent_worker = self.run_worker(run_persistent, thread=True, exclusive=True, name='persistent')

        def _refresh_dashboard(self) -> None:
            target = PERSISTENT_TARGET_SOL
            balance = None
            try:
                self.query_one('#metric-target', Static).update(f'{target:.1f}')
                agents = _load_agents_registry(AGENTS_REGISTRY_PATH)
                self.query_one('#metric-agents', Static).update(str(len(agents)))
                pubkey = get_creator_pubkey()
                balance = get_solana_balance(pubkey, SOLANA_RPC_URL) if pubkey else None
            except Exception:
                pass
            bal_w = self.query_one('#metric-balance', Static)
            if balance is not None:
                bal_w.update(f'{balance:.4f}')
                bal_w.remove_class('success')
                bal_w.remove_class('warn')
                if target > 0 and balance >= target:
                    bal_w.add_class('success')
                elif target > 0:
                    bal_w.add_class('warn')
            else:
                bal_w.update('—')
                bal_w.remove_class('success')
                bal_w.remove_class('warn')
            status_w = self.query_one('#metric-status', Static)
            persistent_worker = getattr(self, '_persistent_worker', None)
            if persistent_worker and persistent_worker.is_running:
                status_w.update('Running…')
            elif _tui_stopped_reason == 'target':
                status_w.update('Target reached')
            elif _tui_stopped_reason == 'stop':
                status_w.update('Stopped')
            else:
                status_w.update('Ready')
            self.query_one('#metric-topic', Static).update(_tui_current_topic or '—')
            self.query_one('#metric-session-published', Static).update(str(_tui_session_published))
            self.query_one('#metric-last', Static).update(_tui_session_last_name or '—')

        def _drain_log_queue(self) -> None:
            log_widget = self.query_one('#log', RichLog)
            while True:
                try:
                    line = _log_queue.get_nowait()
                except queue.Empty:
                    break
                log_widget.write(line)
                log_widget.scroll_end()
            self._refresh_dashboard()

        def action_request_stop(self) -> None:
            global _tui_stop_requested
            _tui_stop_requested = True
            self.query_one('#log', RichLog).write('[dim]Stop requested; loop will exit after current step.[/]')

        @on(Worker.StateChanged)
        def _on_worker_state_changed(self, event: 'Worker.StateChanged') -> None:
            persistent_worker = getattr(self, '_persistent_worker', None)
            if event.worker is not persistent_worker:
                return
            if not event.worker.is_finished:
                return
            timer = getattr(self, '_log_drain_timer', None)
            if timer is not None:
                timer.stop()
                del self._log_drain_timer
            _remove_log_sink_for_tui()
            self._drain_log_queue()
            self._refresh_dashboard()
            log_widget = self.query_one('#log', RichLog)
            if event.worker.error:
                log_widget.write(f'[red]Error: {event.worker.error}[/]')
            else:
                log_widget.write('[green]Persistent finished.[/]')
            self._persistent_worker = None

        def action_quit(self) -> None:
            self.exit()

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
    if _TEXTUAL_AVAILABLE:
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
        run_persistent()
    elif args.once:
        main()
    elif args.no_tui:
        run_standalone_cli()
    else:
        run_interactive_menu()
"""

LUAF_DESCRIPTION = """# LUAF — Large-scale Unified Agent Foundry

LUAF turns a **business brief** into a **research → build → validate → launch** pipeline for autonomous business units. You describe a use case (or leave it blank for an AI-generated idea); LUAF runs web search, optional planner/designer LLM steps, validation (with auto pip-install on missing imports), and publish to [swarms.world](https://swarms.world) with optional Solana tokenization.

**Repository:** [github.com/Euroswarms-Institute/LUAF](https://github.com/Euroswarms-Institute/LUAF)

---

## What it does

- **Topic / brief:** You provide a short description of the autonomous unit you want (e.g. "DeFi backtester for ETH/USDT") or press Enter to have the LLM generate a monetizable idea.
- **Research:** DuckDuckGo (and optional multi-hop RAG) gathers context.
- **Plan (optional):** Planner produces a structured plan; executor fills a template.
- **Design:** Designer LLM produces a full Python agent (name, ticker, code, requirements, use cases). Quality packages (e.g. swarms, loguru, tinydb, requests) are enforced per topic.
- **Validate:** Generated script is run in a subprocess. On `ModuleNotFoundError`, LUAF runs `pip install <module>` and retries. Optional urllib3/Retry compatibility fix is applied. If validation fails, the user can choose to publish anyway or validate manually.
- **Publish:** Payload is sent to `POST /api/add-agent` with Bearer token (SWARMS_API_KEY). Dry-run by default; set `LUAF_DRY_RUN=0` and provide `SOLANA_PRIVATE_KEY` for tokenized launch.
- **Persistent mode:** Loop until wallet balance reaches a target SOL; each iteration can use a new or fixed topic (env/file list).

---

## Environment variables (summary)

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | LLM (designer, topic generator, planner). Required for pipeline. |
| `OPENAI_BASE_URL` | Optional; default `https://api.openai.com/v1`. |
| `SWARMS_API_KEY` | Bearer token for publish and Swarms Cloud. |
| `LUAF_TOPIC` | Default topic/brief; empty = generate via LLM. |
| `LUAF_DRY_RUN` | `1` (default) = no real publish; `0` = publish. |
| `LUAF_USE_PLANNER` | Use planner + executor (default 1). |
| `LUAF_USE_DESIGNER` | Use designer LLM (default 1). |
| `LUAF_GENERATED_AGENTS_DIR` | Where to save generated .py files; `.` = same dir as LUAF.py. |
| `SOLANA_PRIVATE_KEY` / `SOLANA_PRIVATE_KEY_FILE` | For tokenized publish. |
| `CREATOR_WALLET` / `SOLANA_PUBKEY` | Creator pubkey for balance and claims. |
| `LUAF_MODE` | `persistent` = run autonomous loop on startup. |
| `LUAF_MAX_MISSING_IMPORT_RETRIES` | Retries for pip-install on validation (default 3). |

See repo README and in-code comments for the full list.

---

## Usage tutorials

### 1. Interactive menu (TUI or CLI)

```bash
python LUAF.py
```

- If Textual is installed: TUI with balance, target SOL, current brief, live log. Use **s** to stop, **q** to quit.
- Otherwise: text menu — 1 = Pipeline, 2 = Persistent, 0 = Exit.

### 2. Single pipeline run (no TUI)

```bash
python LUAF.py --no-tui -o
```

- Prompts: business use case or brief (Enter = generate), unit name (Enter = auto), ticker (Enter = auto).
- Runs one designer cycle (up to MAX_STEPS with validation retries). Saves generated unit to `<Name>.py` or `generated_agent_step<N>.py`. On validation failure, asks whether to publish without validation and mentions manual validation.

### 3. Persistent (autonomous loop)

```bash
python LUAF.py --persistent
# or
LUAF_MODE=persistent python LUAF.py
```

- Runs until Solana balance >= `LUAF_PERSISTENT_TARGET_SOL` (default 10) or user stops.
- Each iteration: get next topic (single / env list / file), search, designer, validate, publish if funds, run unit once, optional delayed claim.

### 4. Self-train pipeline

```bash
python LUAF.py --self-train "Your topic here"
python LUAF.py --self-train   # uses LUAF_TOPIC or default
```

### 5. Legacy positional

```bash
python LUAF.py run        # same as --once
python LUAF.py persistent # same as --persistent
```

### 6. Help

```bash
python LUAF.py --help
```

Shows description, epilog examples, and all flags (--no-tui, --once, --persistent, --self-train).

---

## Requirements (for running LUAF)

Core: `requests`, `python-dotenv`, `loguru`, `ddgs`. Optional: `textual` (TUI), `sentence-transformers` (RAG), `swarms`, `toolbox`, `planner`, `executor`, `tinydb` (designer quality-packages). See [requirements.txt](https://github.com/Euroswarms-Institute/LUAF) in the repo.
"""

LUAF_USE_CASES = [
    {
        "title": "Single pipeline run with generated topic",
        "description": "Run LUAF with --no-tui -o, press Enter at the brief prompt so the LLM generates an autonomous business idea. Enter at unit name and ticker for auto names. LUAF runs search, designer, validation (with optional auto pip install for missing deps), and dry-run publish. Generated Python unit is saved to the repo (e.g. PSAI.py). Use when you want one new unit from a single command without the TUI."
    },
    {
        "title": "Single pipeline with your own brief and unit name",
        "description": "Run LUAF.py --no-tui -o. At 'Business use case or brief' type e.g. 'DeFi backtester for ETH/USDT with ccxt and Sharpe reporting'. At 'Unit name' type e.g. 'QTSYS', at 'Ticker' type e.g. 'QTSYS'. LUAF runs the full pipeline for that brief and saves the unit. Use when you have a clear idea and want a named, publishable unit."
    },
    {
        "title": "Persistent autonomous loop until target SOL",
        "description": "Run python LUAF.py --persistent (or LUAF_MODE=persistent). LUAF loops: get topic (from LUAF_TOPIC, LUAF_TOPIC_LIST, or LUAF_TOPIC_FILE), run search and designer, validate, publish if balance allows, run unit once, optional claim. Stops when balance >= LUAF_PERSISTENT_TARGET_SOL or user stops (TUI: s, CLI: Ctrl+C). Use for unattended batch creation and tokenization."
    },
    {
        "title": "TUI dashboard (balance, target, current brief, live log)",
        "description": "Run python LUAF.py without --no-tui. If Textual is installed, the TUI shows BALANCE (SOL), TARGET (SOL), LAUNCHED UNITS, STATUS, CURRENT BRIEF, LAUNCHED THIS RUN, LAST. Start persistent run from the menu; watch the live log. Use s to stop, q to quit. Use when you want a visual dashboard and log tail."
    },
    {
        "title": "CLI-only menu (no TUI)",
        "description": "Run python LUAF.py --no-tui. Get the text menu: 1. Pipeline (design & launch autonomous unit), 2. Persistent (autonomous loop until target SOL), 0. Exit. Choose 1 or 2 then follow prompts. Use on headless or SSH sessions where Textual is not available."
    },
    {
        "title": "Self-train pipeline with topic",
        "description": "Run python LUAF.py --self-train 'Topic string' or python LUAF.py --self-train (uses LUAF_TOPIC). Runs the self-train pipeline for planner/embeddings/training data. Use when you are maintaining or extending the planner/RAG pipeline."
    },
    {
        "title": "Publish without validation (after validation failure)",
        "description": "When validation fails (e.g. missing env var or runtime error in generated code), LUAF prompts: 'Publish without validation? [y/N]' and explains you can validate manually by running the saved unit script (e.g. python <UnitName>.py). Answer y to publish anyway. Use when you trust the generated code and will fix or run it locally later."
    },
    {
        "title": "Dry-run publish (default)",
        "description": "By default LUAF_DRY_RUN=1: publish_agent is called with dry_run=True so no real POST to add-agent with tokenization. Set LUAF_DRY_RUN=0 and provide SWARMS_API_KEY (and optionally SOLANA_PRIVATE_KEY for tokenized launch) to perform a real publish. Use to test the full pipeline without listing on swarms.world."
    },
    {
        "title": "Designer with validation auto-install",
        "description": "When the generated unit fails with ModuleNotFoundError (e.g. tinydb), LUAF parses the missing module name, runs pip install <module>, and re-runs validation (up to LUAF_MAX_MISSING_IMPORT_RETRIES). Use when the designer emits code that uses optional deps; LUAF makes validation pass without pre-installing every possible package."
    },
    {
        "title": "Topic from env list or file (persistent)",
        "description": "Set LUAF_PERSISTENT_TOPIC_SOURCE=env and LUAF_TOPIC_LIST=topic1,topic2,topic3 (or LUAF_PERSISTENT_TOPIC_SOURCE=file and LUAF_TOPIC_FILE=/path/to/lines.txt). In persistent mode, LUAF rotates through the list/file for each iteration. Use for batch generation over a fixed set of briefs."
    },
]

REQUIREMENTS = [
    {"package": "requests", "installation": "pip install requests"},
    {"package": "python-dotenv", "installation": "pip install python-dotenv"},
    {"package": "loguru", "installation": "pip install loguru"},
    {"package": "ddgs", "installation": "pip install ddgs"},
    {"package": "swarms", "installation": "pip install swarms"},
    {"package": "textual", "installation": "pip install textual"},
    {"package": "tinydb", "installation": "pip install tinydb"},
]


def build_payload(luaf_source: str) -> dict:
    """Build the full agent payload for LUAF. Matches Swarms add-agent API schema."""
    return {
        "name": "LUAF Agent",
        "ticker": "LUAF",
        "description": LUAF_DESCRIPTION,
        "agent": luaf_source,
        "useCases": LUAF_USE_CASES,
        "tags": "autonomous-units,agent-foundry,swarms,python,LLM,designer,validation,publish,tokenization,cli,TUI",
        "requirements": REQUIREMENTS,
        "language": "python",
        "is_free": True,
        "is_tokenized": False,
        "category": "agent-foundry",
        "links": ["https://github.com/Euroswarms-Institute/LUAF"],
    }


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Build and optionally publish LUAF as an agent to Swarms.")
    p.add_argument("--dry-run", action="store_true", default=None, help="Build payload and skip publish (default if SWARMS_API_KEY not set)")
    p.add_argument("--no-dry-run", action="store_false", dest="dry_run", help="Perform real publish (requires SWARMS_API_KEY)")
    p.add_argument("--output", "-o", type=str, default="", help="Write payload JSON to this file (agent code included)")
    p.add_argument("--luaf-path", type=str, default="", help="Path to LUAF.py (default: same dir as this script)")
    args = p.parse_args()

    if (AGENT_CODE_MANUAL or "").strip():
        luaf_source = AGENT_CODE_MANUAL.strip()
        if not luaf_source.endswith("\n"):
            luaf_source += "\n"
    else:
        luaf_path = Path(args.luaf_path) if args.luaf_path else _REPO_ROOT / "LUAF.py"
        if not luaf_path.exists():
            print(f"Error: LUAF.py not found at {luaf_path}", file=sys.stderr)
            sys.exit(1)
        luaf_source = luaf_path.read_text(encoding="utf-8")

    payload = build_payload(luaf_source)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"Wrote payload to {out_path}")

    dry_run = args.dry_run
    if dry_run is None:
        dry_run = not bool(os.environ.get("SWARMS_API_KEY", "").strip())
        if dry_run:
            print("SWARMS_API_KEY not set; using dry-run (no publish).")
    api_key = (os.environ.get("SWARMS_API_KEY") or "").strip()
    if not api_key and not dry_run:
        print("Error: SWARMS_API_KEY required for real publish.", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print("Dry run: payload built; not calling publish_agent.")
        return

    pkey = get_private_key_from_env() or ""
    cwallet = get_creator_pubkey()
    res = publish_agent(payload, api_key, pkey, dry_run=False, creator_wallet=cwallet)
    if res:
        print("Published:", res.get("listing_url"), res.get("id"), res.get("token_address", "N/A"))
    else:
        print("Publish failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
