# LUAF.py — Technical & Degen README

**One script. Brief → research → build → validate → launch.**  
Turn a business idea (or a blank line for AI-generated alpha) into a **tokenized autonomous unit** on [swarms.world](https://swarms.world), with optional Solana tokenization. No cap.

---

## What it actually does

`LUAF.py` is a single entrypoint that:

1. **Takes a brief** — You type a use case, or leave it blank and let the LLM cook.
2. **Researches** — DuckDuckGo (and optionally multi-hop RAG) for real-world context.
3. **Builds** — Optional planner (templates) + designer LLM (swarms/ReAct or direct chat) to generate a full agent spec (name, ticker, description, **agent code**, requirements, use cases, tags).
4. **Validates** — Runs the generated Python in a subprocess; auto `pip install` on `ModuleNotFoundError` (with retries); rewrites `method_whitelist` → `allowed_methods` for urllib3; min ~300 substantive lines, no `NotImplementedError` stubs.
5. **Launches** — `POST https://swarms.world/api/add-agent` with Bearer `SWARMS_API_KEY`. Dry-run by default; with `SOLANA_PRIVATE_KEY` you get tokenized deploy + optional fee claiming.

So: **one command, one brief, one unit on-chain** (if you turn off dry-run and have keys).

---

## Quick start (no TOS, just facts)

```bash
# Clone / drop LUAF.py + deps in same dir (see "Files LUAF.py expects" below)
pip install -r requirements.txt   # or at least: requests python-dotenv loguru ddgs

# Required for designer + publish
export OPENAI_API_KEY=sk-...
export SWARMS_API_KEY=...         # optional if LUAF_DRY_RUN=1 (default)

# Run
python LUAF.py                    # TUI if textual installed, else CLI menu
python LUAF.py --once             # Single pipeline, then exit
python LUAF.py --persistent       # Loop until LUAF_PERSISTENT_TARGET_SOL (default 10 SOL)
python LUAF.py --no-tui           # CLI menu only (1=Pipeline, 2=Persistent, 0=Exit)
python LUAF.py --self-train       # Self-train pipeline (topic from LUAF_TOPIC or env)
python LUAF.py --self-train "DeFi yield aggregator"
```

- **Dry run (default):** `LUAF_DRY_RUN=1` → no real POST to add-agent, no SOL spent.  
- **Real publish:** `LUAF_DRY_RUN=0` + `SWARMS_API_KEY` (+ `SOLANA_PRIVATE_KEY` for tokenization).  
- **Persistent mode:** Generates topics (single/env/file), runs pipeline per topic, publishes until balance ≥ target SOL or you stop (TUI: `s` stop, `q` quit).

---

## Pipeline flow (technical)

- **Brief**  
  From `LUAF_DESIGN_BRIEF` / `LUAF_TOPIC` or interactive prompt. If empty, `_generate_topic_via_llm()` one-shots a single “autonomous business idea” from the LLM.

- **Search**  
  `search_duckduckgo(brief + random SEARCH_VARIANT_SUFFIX)` or, if `LUAF_USE_MULTIHOP_WEB_RAG=1`, `_multihop_web_rag()` (embedding-guided hops, converge threshold, dedup).

- **Planner (optional)**  
  If `LUAF_USE_PLANNER=1` and `planner`/`executor`/`toolbox` are importable: `plan_from_topic_and_search(brief, snippets)` → `execute_plan(plan, get_template, required_payload_keys)`. If the result is a skeleton (<300 lines / `NotImplementedError`), LUAF can hand off to the designer.

- **Designer (optional)**  
  If `LUAF_USE_DESIGNER=1`: builds a system prompt from `designer_system_prompt.txt` + `SWARMS_AGENT_DOCS` + quality packages for the topic (`luaf_quality.json`); can use Swarms Agent, ReAct, or direct OpenAI-compatible `/chat/completions`. Subprocess designer is default (`LUAF_DESIGNER_SUBPROCESS=1`); writes `final_agent_payload.json` into workspace. Exemplar retrieval (optional) from `designer_exemplars.jsonl` via `luaf_designer.retrieve_similar_exemplars`.

- **Parse**  
  `parse_agent_payload(raw)` (in `luaf_designer`) — first JSON object, trailing-comma fix, required keys: `name`, `agent`, `description`, `language`, `requirements`, `useCases`, `tags`, `is_free`, `ticker`.

- **Validation**  
  `run_agent_code_validation(code, VALIDATION_TIMEOUT)`: temp file, stub rewrites (search/LLM/publish), `USE_SEARCH=False`, `USE_LLM=False`, `method_whitelist`→`allowed_methods`; on `ModuleNotFoundError`, `_pip_install_module` + retry (up to `LUAF_MAX_MISSING_IMPORT_RETRIES`). On failure, prompt “Publish without validation?” (if TTY).

- **Publish**  
  `luaf_publish.publish_agent(payload, api_key, private_key, dry_run, ...)`: `POST {BASE_URL}/api/add-agent`. If not dry_run and payload not `tokenized_on: false`, tokenization uses `private_key`; balance from `LUAF_SOLANA_RPC_URL` (cached). Success → `append_agent_to_registry(AGENTS_REGISTRY_PATH, ...)`.

- **Fee claiming**  
  `claim_fees(ca, private_key, api_key)` → `POST {BASE_URL}/api/product/claimfees`. After run, or in persistent loop via `run_delayed_claim_pass(registry_path, pkey, swarms_key, CLAIM_DELAY_HOURS)`.

---

## CLI reference

| Invocation | Behavior |
|------------|----------|
| `LUAF.py` | Interactive: TUI (if `textual` + `luaf_tui`) or CLI menu (1=Pipeline, 2=Persistent, 0=Exit). |
| `LUAF.py --once` / `-o` | One pipeline run then exit. |
| `LUAF.py --persistent` / `-p` | Run persistent loop until target SOL or stop. |
| `LUAF.py --no-tui` / `-n` | Force CLI menu, no TUI. |
| `LUAF.py --self-train [TOPIC]` | Self-train pipeline; TOPIC optional (else `LUAF_TOPIC`). |
| `LUAF.py run` | Treated as `--once`. |
| `LUAF.py persistent` | Treated as `--persistent`. |

If `LUAF_MODE=persistent` and you don’t pass `--once` or `--self-train`, `--persistent` is forced.

---

## Environment variables (LUAF.py only)

**Required for core run**

- `OPENAI_API_KEY` — Used for designer LLM and topic generation. No key → early exit after error.

**Publish / Swarms**

- `SWARMS_API_KEY` — Bearer for `POST .../api/add-agent`. Optional if `LUAF_DRY_RUN=1` (default).
- `LUAF_DRY_RUN` — `1` (default): no real publish. `0`: real publish (and tokenization if key provided).
- `LUAF_SWARMS_BASE_URL` — Set by `luaf_publish`; default `https://swarms.world`.

**Solana / tokenization**

- `SOLANA_PRIVATE_KEY` or `SOLANA_PRIVATE_KEY_FILE` — For tokenized deploy and fee claiming.
- `SOLANA_PUBKEY` or `CREATOR_WALLET` — Optional; for display and tokenization recipient.
- `LUAF_SOLANA_RPC_URL` — Default `https://api.mainnet-beta.solana.com`.
- `LUAF_MIN_SOL_TO_TOKENIZE` — Below this balance, persistent mode does dry-run publish (default 0.05).

**Persistent loop**

- `LUAF_PERSISTENT_TARGET_SOL` — Stop when wallet balance ≥ this (default 10).
- `LUAF_PERSISTENT_TOPIC_SOURCE` — `single` (TOPIC + suffix), `env` (LUAF_TOPIC_LIST), or `file` (LUAF_TOPIC_FILE).
- `LUAF_TOPIC_LIST` — Comma-separated topics when source=env.
- `LUAF_TOPIC_FILE` — Path to file, one topic per line, when source=file.
- `LUAF_PERSISTENT_LOOP_SLEEP_SECONDS` — Sleep between loop iterations (default 0).
- `LUAF_CLAIM_DELAY_HOURS` — Delay before claiming fees per listing (default 24).

**Brief / topic**

- `LUAF_TOPIC` / `LUAF_DESIGN_BRIEF` — Prefilled brief; no prompt if set.
- `LUAF_INTERACTIVE` — `1` (default): prompt for brief and optional name/ticker. `0`: use env/defaults only.

**Designer / LLM**

- `LUAF_LLM_MODEL` — e.g. `gpt-4.1` (default).
- `LUAF_LLM_TEMPERATURE` — Default 0.9.
- `OPENAI_BASE_URL` — Default `https://api.openai.com/v1`.
- `LUAF_DESIGNER_AGENT_ARCHITECTURE` — `agent` or `react`.
- `LUAF_DESIGNER_USE_DIRECT_API` — `1` (default): try one-shot `/chat/completions` before Swarms Agent.
- `LUAF_DESIGNER_SUBPROCESS` — `1` (default): run designer in subprocess.
- `LUAF_USE_PLANNER` — `1` (default): use planner if available.
- `LUAF_USE_DESIGNER` — `1` (default): use designer LLM.
- `LUAF_TRY_SWARMS_CLOUD_FIRST` — `0` (default): try Swarms Cloud before local agent when set.
- `LUAF_TEMPLATE` — Template id for designer/planner.
- `LUAF_USE_RETRIEVAL` — `1` (default): use exemplar retrieval from `designer_exemplars.jsonl`.

**RAG (multi-hop)**

- `LUAF_USE_MULTIHOP_WEB_RAG` — `0` (default). `1`: use embedding-guided multi-hop search.
- `LUAF_RAG_MAX_HOPS`, `LUAF_RAG_CONVERGE_THRESHOLD`, `LUAF_RAG_TOTAL_K`, `LUAF_RAG_DDG_PER_HOP` — Tune RAG.

**Validation**

- `LUAF_VALIDATION_TIMEOUT` — Subprocess timeout seconds (default 600).
- `LUAF_MAX_MISSING_IMPORT_RETRIES` — Retries after auto pip install (default 3).

**Misc**

- `LUAF_MAX_STEPS` — Max pipeline steps per run (default 3).
- `LUAF_DESIGNER_MAX_LOOPS` — Designer agent max_loops (default 2).
- `LUAF_LOG_FILE` — `1` (default): log to `logs/luaf.log` next to script.
- `LUAF_GENERATED_AGENTS_DIR` — Where to save generated `.py` (default `generated_agents`).
- `WORKSPACE_DIR` — Designer workspace (default `./agent_workspace`).
- `LUAF_EVOLVE`, `LUAF_BACKGROUND_TRAIN`, `LUAF_MOLTBOOK_SOCIAL` — Evolution / background self-train / social; only if corresponding code paths exist.

---

## Files LUAF.py expects (same directory)

- **`luaf_publish.py`** — `publish_agent`, balance, registry, claim_fees. Required for publish.
- **`luaf_designer.py`** — `parse_agent_payload(raw, required_keys)`, `retrieve_similar_exemplars(topic, snippets, exemplars_path, top_k)`. Required for designer flow.
- **`luaf_tui.py`** — `create_luaf_app(run_persistent_fn, config)`. Optional; for TUI.
- **`tui.css`** — Loaded by `luaf_tui` for the dashboard. Optional.
- **`designer_system_prompt.txt`** — Full designer system prompt. If missing, designer behavior may be wrong.
- **`luaf_quality.json`** — Optional. Keys: `design_angles`, `search_variant_suffixes`, `quality_packages_by_category`, `quality_category_keywords`. Fallback used if missing.
- **`designer_exemplars.jsonl`** — Optional. One JSON object per line (e.g. `{"text": "..."}`) for retrieval.
- **`.env`** — Loaded from repo root and cwd; use for keys and LUAF_*.

**Outputs**

- **`tokenized_agents.json`** — Registry of published units (name, ticker, listing_url, id, token_address, published_at).
- **`logs/luaf.log`** — Rotating log (if `LUAF_LOG_FILE=1`).
- **`agent_workspace/`** — Designer workspaces and any `final_agent_payload.json`.
- **`generated_agents/`** (or `LUAF_GENERATED_AGENTS_DIR`) — Saved generated agent `.py` files.

---

## API surface (what LUAF.py hits)

- **Publish:** `POST https://swarms.world/api/add-agent` (via `luaf_publish`), JSON body, `Authorization: Bearer <SWARMS_API_KEY>`.
- **Claim fees:** `POST https://swarms.world/api/product/claimfees` (via `luaf_publish`).
- **Solana:** RPC `getBalance` at `LUAF_SOLANA_RPC_URL` (and tokenization via Swarms backend when not dry-run).
- **LLM:** OpenAI-compatible `POST .../chat/completions` and `.../embeddings` at `OPENAI_BASE_URL` (and Swarms Cloud if used).

---

## Degen cheat sheet

- **“I just want one unit live”**  
  `OPENAI_API_KEY` + `SWARMS_API_KEY` + `LUAF_DRY_RUN=0`; run `LUAF.py --once`, enter brief (or Enter for AI idea). Optionally `SOLANA_PRIVATE_KEY` + `CREATOR_WALLET` for tokenization.

- **“I want it to run until I’m at 10 SOL”**  
  `LUAF_PERSISTENT_TARGET_SOL=10` (default), set keys, run `LUAF.py --persistent`. TUI: `s` stop, `q` quit.

- **“I want 50 different topics from a file”**  
  `LUAF_TOPIC_FILE=/path/to/topics.txt`, `LUAF_PERSISTENT_TOPIC_SOURCE=file`, `LUAF.py --persistent`.

- **“Validation keeps failing”**  
  Check `logs/luaf.log`. If it’s missing deps, LUAF auto-installs and retries. If it’s stubs/short code, add more context to the brief or set `LUAF_USE_DESIGNER=1` and ensure `designer_system_prompt.txt` is present. You can still answer “y” to “Publish without validation?” (TTY only).

- **“No TUI / I want CLI only”**  
  `LUAF.py --no-tui`. Menu: 1 = one pipeline, 2 = persistent loop, 0 = exit.

- **“Dry run forever”**  
  Default. Set `LUAF_DRY_RUN=0` only when you’re ready for real listing and (optionally) SOL.

---

## Summary

**LUAF.py** = one entrypoint: **brief → research → build → validate → launch**.  
Configure via env (and optional `luaf_quality.json` / `designer_system_prompt.txt`).  
Requires **`luaf_publish.py`** and **`luaf_designer.py`** in the same directory for full flow; **`luaf_tui.py`** + **`tui.css`** for the dashboard.  
Publish endpoint: **`POST https://swarms.world/api/add-agent`**.  
No fluff — just the technical and degen-friendly details for LUAF.py only.
