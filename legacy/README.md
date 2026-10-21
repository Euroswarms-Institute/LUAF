# legacy — the Python pipeline

Kept as the behavioural reference for the TypeScript rewrite, not as something to run. Its two
publish targets are gone from the product: the swarms.world listing and the Solana tokenization
both published onto another platform's ground, and the Solana path sent a raw wallet key to a
remote API.

What the rewrite took from it, and where each piece now lives:

| Here | Now |
| --- | --- |
| `luaf/designer.py` payload parsing | `core/src/payload.ts` |
| `luaf/profiles/` prompts | `core/data/profiles/` (unchanged) |
| `get_agent_payload_from_llm` | `core/src/designer.ts` |
| `run_agent_code_validation` | `core/src/validate.ts`, now with a stripped environment |
| `luaf/publishing/rapid.py` bundle | `core/src/publish/endpoint.ts`, now charging over x402 |
| `run_persistent` | `core/src/persistent.ts`, now stopping on measured ETH income |

**`LUAF.py` had a live Swarms API key committed in source** (`SWARMS_API_KEY_FALLBACK`). It has been
emptied here, but it is in this repository's history and in every published PyPI release that
carried it, so **that key must be treated as compromised and rotated.** Removing it from the current
tree does not un-publish it.
