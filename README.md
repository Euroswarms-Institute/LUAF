# LUAF — Large-scale Unified Agent Foundry

**One CLI. Brief → research → build → validate → launch.**  
Turn a business idea (or a blank line for AI-generated alpha) into a **tokenized autonomous unit** on [swarms.world](https://swarms.world), with optional Solana tokenization.

- **Repo:** [Euroswarms-Institute/LUAF](https://github.com/Euroswarms-Institute/LUAF)
- **PyPI:** `pip install luaf` → then `luaf init`, `luaf doctor`, `luaf run`

---

## What it actually does

The **`luaf`** CLI (single entrypoint) runs a pipeline that:

1. **Takes a brief** — You type a use case (or set `LUAF_TOPIC` / `--topic`), or leave it blank and let the LLM generate one.
2. **Researches** — DuckDuckGo (and optionally multi-hop RAG) for real-world context; search is biased toward **keyless/public APIs** when possible.
3. **Builds** — Optional planner (templates) + designer LLM (swarms/ReAct or direct chat) to generate a full agent spec. The designer **prefers APIs that do not require API keys**; when keys are needed, generated code uses `os.environ.get` and comments on where to get/set them. **No mock or example data** — real code paths and inline comments for where to plug in credentials or data.
4. **Validates** — Runs the generated Python in a subprocess; auto `pip install` on `ModuleNotFoundError` (with retries); min ~300 substantive lines, no stubs.
5. **Launches** — `POST https://swarms.world/api/add-agent`; optional Solana tokenization. You can attach an **agent image** via `LUAF_AGENT_IMAGE_URL`, or use **keyless AI image generation** (`LUAF_GENERATE_AGENT_IMAGE=1` or `--generate-agent-image`) to create one from name/description (Pollinations.ai, no API key). After publish, the agent can be **run in a new terminal window** via `LUAF_RUN_IN_NEW_TERMINAL=1` (default). Optional X (Twitter) batch posting via `luaf_x_post` when `LUAF_POST_TO_X=1` and X API credentials are set.

So: **one command, one brief, one unit on-chain** (if you turn off dry-run and have keys).

---

## Quick start (no TOS, just facts)

**From PyPI:**

```bash
pip install luaf
luaf init      # setup wizard: .env + API keys
luaf doctor    # check config and connectivity
luaf run       # single pipeline
luaf persistent   # loop until target SOL
```

**From repo (clone or `pip install -e .`):**

```bash
pip install -r requirements.txt   # or: pip install -e .
luaf init      # create .env from env.example and set API keys
luaf doctor    # check config and connectivity
luaf run       # single pipeline
luaf persistent   # loop until target SOL
```

**Other commands:** `luaf init --check`, `luaf init --from-example`, `luaf self-train [TOPIC]`, `luaf --no-tui`, `luaf --tui` (experimental TUI). All config can be overridden via **CLI flags** (see [CLI config flags](#cli-config-flags)); flags take precedence over `.env`.

- **Dry run (default):** `LUAF_DRY_RUN=1` or `luaf run --dry-run` → no real POST to add-agent, no SOL spent.  
- **Real publish:** `luaf run --no-dry-run` (or `LUAF_DRY_RUN=0`) + `SWARMS_API_KEY` (+ `SOLANA_PRIVATE_KEY` for tokenization).  
- **Persistent mode:** Generates topics (single/env/file), runs pipeline per topic, publishes until balance ≥ target SOL or you stop (TUI: `s` stop, `q` quit).

---

## Pipeline flow (technical)

- **Brief**  
  From `LUAF_DESIGN_BRIEF` / `LUAF_TOPIC` or interactive prompt. If empty, `_generate_topic_via_llm()` one-shots a single “autonomous business idea” from the LLM.

- **Search**  
  `search_duckduckgo(brief + random SEARCH_VARIANT_SUFFIX)` or, if `LUAF_USE_MULTIHOP_WEB_RAG=1`, `_multihop_web_rag()` (embedding-guided hops, converge threshold, dedup).

- **Planner (optional)**  
  If `LUAF_USE_PLANNER=1` and `planner`/`executor`/`toolbox` are importable: `plan_from_topic_and_search(brief, snippets)` → `execute_plan(plan, get_template, required_payload_keys)`. If the result is a skeleton (<300 lines / `NotImplementedError`), LUAF can hand off to the designer. For pyclips support, install separately: `pip install "pyclips @ git+https://github.com/kyordhel/pyclips3.git@master"` (PyPI does not allow git URL deps in published metadata).

- **Designer (optional)**  
  If `LUAF_USE_DESIGNER=1`: builds a system prompt from `designer_system_prompt.txt` + `SWARMS_AGENT_DOCS` + quality packages for the topic (`luaf_quality.json`); can use Swarms Agent, ReAct, or direct OpenAI-compatible `/chat/completions`. Subprocess designer is default (`LUAF_DESIGNER_SUBPROCESS=1`); writes `final_agent_payload.json` into workspace. Exemplar retrieval (optional) from `designer_exemplars.jsonl` via `luaf_designer.retrieve_similar_exemplars`.

- **Parse**  
  `parse_agent_payload(raw)` (in `luaf_designer`) — first JSON object, trailing-comma fix, required keys: `name`, `agent`, `description`, `language`, `requirements`, `useCases`, `tags`, `is_free`, `ticker`.

- **Validation**  
  `run_agent_code_validation(code, VALIDATION_TIMEOUT)`: temp file, stub rewrites (search/LLM/publish), `USE_SEARCH=False`, `USE_LLM=False`, `method_whitelist`→`allowed_methods`; on `ModuleNotFoundError`, `_pip_install_module` + retry (up to `LUAF_MAX_MISSING_IMPORT_RETRIES`). On failure, prompt “Publish without validation?” (if TTY).

- **Publish**  
  `luaf_publish.publish_agent(payload, api_key, private_key, dry_run, ...)`: `POST {BASE_URL}/api/add-agent`. If not dry_run and payload not `tokenized_on: false`, tokenization uses `private_key`; balance from `LUAF_SOLANA_RPC_URL` (cached). Success → `append_agent_to_registry(AGENTS_REGISTRY_PATH, ...)`. If `luaf_x_post` is enabled, the agent is added to the X pending queue for batched 2-tweet threads.

- **Execution phase**  
  When `LUAF_RUN_IN_NEW_TERMINAL=1` (default), after a successful publish the generated agent is launched in a **new terminal window** (Windows: new console; Linux: gnome-terminal or xterm) with the brief as the task argument, so you can observe it. Disable with `LUAF_RUN_IN_NEW_TERMINAL=0`.

- **Fee claiming**  
  `claim_fees(ca, private_key, api_key)` → `POST {BASE_URL}/api/product/claimfees`. After run, or in persistent loop via `run_delayed_claim_pass(registry_path, pkey, swarms_key, CLAIM_DELAY_HOURS)`.

---

## CLI reference

**Default:** Running `luaf` (or `python LUAF.py`) with no command shows the **CLI menu** (1=Pipeline, 2=Persistent, 0=Exit). Use `luaf --tui` for the experimental TUI.

| Invocation | Behavior |
|------------|----------|
| `luaf` | CLI menu (default). |
| `luaf --tui` | Use experimental TUI (Rich). |
| `luaf init` | **Setup:** create/update `.env` from `env.example` (or bundled template when pip-installed). Prompts for API keys (OpenAI, Swarms, Solana, X); Enter to skip any. Edit `.env` for other `LUAF_*` options. |
| `luaf init --from-example` | Non-interactive: ensure `.env` exists from template only. |
| `luaf init --check` | Verify required env vars; exit 0 if OK, 1 if missing. |
| `luaf doctor` | Load `.env`, check required vars and live API health. Keys set in `.env` (manually or via init) are validated the same. Exit 0/1. |
| `luaf help` | Show help (same as `luaf -h`). |
| `luaf run` | Single pipeline then exit (same as `--once`). |
| `luaf persistent` | Run persistent loop until target SOL or stop. |
| `luaf self-train [TOPIC]` | Self-train pipeline; TOPIC optional (else `LUAF_TOPIC`). |
| `luaf --once` / `-o` | Single pipeline then exit. |
| `luaf --persistent` / `-p` | Persistent loop. |
| `luaf --no-tui` / `-n` | CLI menu only (default). |
| `luaf --no-color` | Disable ANSI colors (also `NO_COLOR=1`). |

If `LUAF_MODE=persistent` and you don’t pass `run` or `self-train`, `persistent` is used.

### CLI config flags

Override `.env` when passed. Apply to `luaf run` and `luaf persistent`.

| Flag | Env | Description |
|------|-----|-------------|
| `--dry-run` / `--no-dry-run` | `LUAF_DRY_RUN` | Publish as dry-run only vs real. |
| `--target-sol N` | `LUAF_PERSISTENT_TARGET_SOL` | Stop persistent when balance ≥ N SOL. |
| `--topic TEXT` | `LUAF_TOPIC` | Brief / topic for this run. |
| `--generate-agent-image` / `--no-generate-agent-image` | `LUAF_GENERATE_AGENT_IMAGE` | Keyless AI image (Pollinations.ai). |
| `--agent-image-url URL` | `LUAF_AGENT_IMAGE_URL` | Fixed image URL for published agents. |
| `--claim-delay-hours H` | `LUAF_CLAIM_DELAY_HOURS` | Hours after publish before claiming fees. |
| `--min-sol-to-tokenize N` | `LUAF_MIN_SOL_TO_TOKENIZE` | Below this balance, dry-run only. |
| `--max-steps N` | `LUAF_MAX_STEPS` | Max pipeline retry steps per run. |
| `--interactive` / `--no-interactive` | `LUAF_INTERACTIVE` | Prompt for brief/name/ticker. |
| `--run-in-new-terminal` / `--no-run-in-new-terminal` | `LUAF_RUN_IN_NEW_TERMINAL` | Launch agent in new terminal after publish. |
| `--validation-timeout SEC` | `LUAF_VALIDATION_TIMEOUT` | Agent validation subprocess timeout. |
| `--loop-sleep-seconds SEC` | `LUAF_PERSISTENT_LOOP_SLEEP_SECONDS` | Sleep between persistent iterations. |
| `--topic-file PATH` | `LUAF_TOPIC_FILE` | Persistent: one topic per line. |
| `--topic-list LIST` | `LUAF_TOPIC_LIST` | Persistent: comma-separated topics. |
| `--topic-source single\|env\|file` | `LUAF_PERSISTENT_TOPIC_SOURCE` | Persistent: topic source. |

Examples: `luaf run --no-dry-run --topic "DeFi feed" --generate-agent-image` · `luaf persistent --target-sol 20 --claim-delay-hours 0.25` · `luaf persistent --topic-file ./topics.txt --topic-source file`

---

## Environment variables

Loaded from **cwd first**, then repo root (so your `.env` wins). Can be overridden by [CLI config flags](#cli-config-flags).

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

**Execution / UX**

- `LUAF_RUN_IN_NEW_TERMINAL` — `1` (default): after publish, launch the agent in a new terminal window for observation. `0`: skip.
- `LUAF_KEYLESS_API_SEARCH` — `1` (default): append keyless/public-API-focused search snippets for the designer.

**Agent image (for publish)**

- `LUAF_GENERATE_AGENT_IMAGE` — `1`: generate a keyless AI image from agent name/description (Pollinations.ai, no API key). `0` (default): no auto image.
- `LUAF_AGENT_IMAGE_URL` — Fixed image URL for all published agents (overrides payload and keyless).
- `LUAF_AGENT_IMAGE_BASE_URL` — Override keyless image base (default `https://gen.pollinations.ai`).

**X (Twitter) posting** (optional; requires `luaf_x_post.py`)

- `LUAF_POST_TO_X` — `1` to enable batched 2-tweet threads for published agents.
- `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` — X OAuth 1.0a credentials (all four required if posting).

**Misc**

- `LUAF_MAX_STEPS` — Max pipeline steps per run (default 3).
- `LUAF_DESIGNER_MAX_LOOPS` — Designer agent max_loops (default 2).
- `LUAF_LOG_FILE` — `1` (default): log to `logs/luaf.log` next to script.
- `LUAF_GENERATED_AGENTS_DIR` — Where to save generated `.py` (default `generated_agents`).
- `WORKSPACE_DIR` — Designer workspace (default `./agent_workspace`).
- `LUAF_EVOLVE`, `LUAF_BACKGROUND_TRAIN`, `LUAF_MOLTBOOK_SOCIAL` — Evolution / background self-train / social; only if corresponding code paths exist.

---

## Files LUAF expects (same directory)

- **`luaf_publish.py`** — `publish_agent`, balance, registry, claim_fees. Required for publish.
- **`luaf_designer.py`** — `parse_agent_payload`, `retrieve_similar_exemplars`. Required for designer flow.
- **`luaf_tui.py`** — `create_luaf_app`. Optional; experimental Rich TUI (use `luaf --tui`).
- **`luaf_x_post.py`** — Optional. X (Twitter) batch posting: add agent to pending, post 2–3 agents per 2-tweet thread when `LUAF_POST_TO_X=1` and X API credentials are set.
- **`luaf_profiles.py`** — Optional. Profile selection for designer system prompt / topic focus.
- **`tui.css`** — Design reference for `luaf_tui`. Optional.
- **`designer_system_prompt.txt`** — Full designer system prompt (keyless-API preference, no mocks, comment-based data placement). If missing, designer behavior may be wrong.
- **`luaf_quality.json`** — Optional. `design_angles`, `search_variant_suffixes`, `quality_packages_by_category`, `quality_category_keywords`. Fallback used if missing.
- **`designer_exemplars.jsonl`** — Optional. One JSON object per line for retrieval.
- **`.env`** — Loaded from **cwd first**, then repo root (your `.env` wins). Use **`env.example`** or **`.env.example`** as template; `luaf init` creates/updates `.env` from it (or uses a bundled template when installed via pip). Edit `.env` for all `LUAF_*` options.

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

- **“First time setup”**  
  `luaf init` (creates `.env` from template, prompts for API keys), then `luaf doctor` to verify. Works from repo or `pip install luaf`.

- **“I just want one unit live”**  
  Set keys in `.env` (or use `luaf init`), then `luaf run --no-dry-run --topic "Your brief"` (or enter brief at prompt). Add `--generate-agent-image` for a keyless agent image. Optionally `SOLANA_PRIVATE_KEY` + `CREATOR_WALLET` in `.env` for tokenization.

- **“I want it to run until I’m at 10 SOL”**  
  `luaf persistent --target-sol 10` (or default). With TUI (`luaf --tui` then 2): `s` stop, `q` quit.

- **“I want 50 different topics from a file”**  
  `luaf persistent --topic-file ./topics.txt --topic-source file`.

- **“Validation keeps failing”**  
  Check `logs/luaf.log`. LUAF auto-installs missing deps and retries. If it’s stubs/short code, add more context to the brief. You can answer “y” to “Publish without validation?” (TTY only).

- **“CLI only / no TUI”**  
  Default. Use `luaf` (no `--tui`). Menu: 1 = pipeline, 2 = persistent, 0 = exit.

- **“Dry run forever”**  
  Default. Use `luaf run --no-dry-run` or `LUAF_DRY_RUN=0` only when you’re ready for real listing and (optionally) SOL.

---

## Summary

**LUAF** = one CLI: **brief → research → build → validate → launch**.  
Install: `pip install luaf` or clone + `pip install -e .`. Commands: **`luaf init`** (setup .env and API keys), **`luaf doctor`** (check config), **`luaf run`**, **`luaf persistent`**, **`luaf help`**. All config is overridable via **CLI flags** (e.g. `--dry-run`, `--target-sol`, `--topic`, `--generate-agent-image`). Default UI is the CLI menu; use **`luaf --tui`** for the experimental TUI.  
Designer prefers **keyless APIs** and **no mock data**; when credentials are needed, generated code uses env and comments for where to get/set them. You can add a **keyless agent image** per publish (`LUAF_GENERATE_AGENT_IMAGE=1` or `--generate-agent-image`, Pollinations.ai). After publish, agents can be **launched in a new terminal** (`LUAF_RUN_IN_NEW_TERMINAL=1`). Optional **X (Twitter)** batch posting via `luaf_x_post` when configured.  
Requires **`luaf_publish.py`** and **`luaf_designer.py`** for full flow; **`luaf_tui.py`** and **`luaf_x_post.py`** are optional.  
Publish endpoint: **`POST https://swarms.world/api/add-agent`**.
