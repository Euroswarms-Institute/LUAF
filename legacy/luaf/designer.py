from __future__ import annotations
import json, os, re
from pathlib import Path
from typing import Any, Iterable, Optional
import requests
from loguru import logger
_RE_TRAILING_COMMA = re.compile(r',(\s*[}\]])')

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
    return _RE_TRAILING_COMMA.sub(r'\1', s) if s else s

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

def parse_agent_payload(raw: str, required_keys: Iterable[str]) -> dict[str, Any]:
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
    missing = set(required_keys) - set(payload.keys())
    if missing:
        raise ValueError(f'Missing keys: {sorted(missing)}')
    payload['is_free'] = True
    return payload

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

def retrieve_similar_exemplars(topic: str, search_snippets: str, exemplars_path: Path, top_k: int = 3) -> list[str]:
    if (os.environ.get('LUAF_USE_RETRIEVAL', '1') or '').strip().lower() in ('0', 'false', 'no'):
        return []
    if not exemplars_path.exists():
        return []
    global _exemplar_cache
    try:
        exemplars_raw: list[dict[str, Any]] = []
        with open(exemplars_path, encoding='utf-8', errors='replace') as f:
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
