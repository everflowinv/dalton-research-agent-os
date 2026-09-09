# Model routing: catalog sync by retirement, and fallback chains per purpose tier

**P14-M — 2026-09-09 — branch `model-routing-fallback`**

Two long-standing things. The Dalton model catalog and the OpenClaw broker's had
drifted and there was no move available that fixed it without breaking history.
And a Dalton model call picked one model: if the provider was down, the call was
lost.

---

## 1. The drift, by name

Measured against the live `~/.openclaw/openclaw.json` and a read-only copy of
`~/Library/Application Support/Dalton/state/dalton-core/model-router.sqlite`
(31 profile ids, 5,201 route decisions).

**Five static profiles the broker no longer offers** (`_ENDPOINTS` minus broker):

| profile id | model | note |
|---|---|---|
| `profile:gemini-3-7-flash` | `google/gemini-3.7-flash` | **this is `VERIFIER_PROFILE_ID`** — see §5 |
| `profile:gemini-flash-latest` | `google/gemini-flash-latest` | |
| `profile:glm-5-2` | `qwen/glm-5.2` | superseded by `zai/glm-5.3` |
| `profile:gpt-5-5` | `openai/gpt-5.5` | |
| `profile:openrouter-ox-alpha` | `openrouter/stealth/ox-alpha` | provider gone from the config entirely |

The live router holds a sixth the static catalog never had: `profile:qwen-deepseek-v4-pro`,
registered by a canary script and offered by nobody. It retires with the rest.

**Four broker profiles Dalton had no static profile for:**

| profile id | model |
|---|---|
| `profile:claude-fable-5-1` | `claude-cli-gateway/claude-fable-5-1` |
| `profile:gemini-3-8-flash` | `google/gemini-3.8-flash` |
| `profile:qwen-deepseek-v4-flash-0731-low-calibration` | `qwen/deepseek-v4-flash-0731` (low thinking) |
| `profile:zai-glm-5-3` | `zai/glm-5.3` |

And one the *broker* was missing: `zai/glm-5.3-flash` was in
`models.providers.zai` and in `agents.defaults.modelPolicy.allow`, but in
neither `plugins.entries.dalton-openclaw-model-broker.llm.allowedModels` nor its
`config.profiles`. That is the OpenClaw-side half of the same drift (§4).

## 2. Retire semantics — the third move

Deleting a profile was never possible: a route decision from June names a
profile version, and a version chain with a hole in it can no longer be
replayed. So nothing was ever deleted, and the stale five stayed.

A profile the broker has stopped offering now gets a **new version of itself**
that says so:

```
status: "retired"
retirement: { reason: "not_in_broker_catalog",
              retired_at: <RFC3339>,
              broker_catalog_hash: <sha256 of the broker catalog that proved it> }
availability.state: "unavailable"
```

* Append-only. Every earlier version is byte-identical, so an old decision's
  `selected_profile_hash` still verifies against `get_profile(...)`.
* Routing refuses it: the candidate snapshot carries `profile_retired` and the
  decision is `rejected` with that reason. It fails *early and legibly* instead
  of reaching the broker and coming back as a provider error.
* Reversible without a rewrite: a retired profile the broker offers again gets a
  live version appended on top (`revived_profile_ids`).
* `status` is **absent** on a live profile rather than `"live"`, so no profile
  version registered before today changes its content hash.
* Scoped to the `profile:` id namespace. The six pre-broker `model-profile:` ids
  are left alone — no broker catalog has ever described them, so retiring them
  against one would assert something that catalog does not say.

`catalog_in_sync` is true when every model the broker offers has a live profile
here **and** every non-retired profile here is offered by the broker. Retired
profiles sit outside both halves; counting them would make sync unreachable.

## 3. Fallback chains

The broker does not fall back — deliberately: the adapter cannot choose an
agent, endpoint, credential or fallback model, because a silent substitution
would mean a Claim was produced by a model nobody named. So the chain is
Dalton's, and it keeps that property.

**The tier map.** Every registered purpose, explicitly; an unmapped purpose is
refused at route time.

| purpose | tier |
|---|---|
| `ask`, `goal`, `steer`, `draft`, `plan`, `model_spec` | `brain` |

All six are brain, and that is a statement rather than a default: the cockpit's
answer, the goal and steering proposals, the deliverable draft, the planner's
decision and the company model specification are all "form a view and argue it".
The cheap tier's work — extraction windows, claim-index tagging, quality
deterministic-assisted judging, batch classification — runs in lanes with their
own pinned policies that have not registered cockpit purposes; they take the
tier by pinning the cheap policy. `register_purpose_tier(purpose, tier)`
registers both at once, so a lane cannot acquire a purpose without a chain.

**The chains** (`model_fallback_chain._TIER_CHAINS`):

| tier | chain | families |
|---|---|---|
| `brain` | `profile:gpt-6-astra` → `profile:claude-fable-5-1` | `openai-gpt-6`, `anthropic-claude-5` |
| `cheap` | `profile:deepseek-v4-flash` → `profile:zai-glm-5-3-flash` → `profile:gemini-3-5-flash-lite` | `deepseek-v4`, `zhipu-glm-5.3`, `google-gemini-3` |
| `verifier` | `profile:claude-fable-5-1` → `profile:zai-glm-5-3` → `profile:gemini-3-5-flash-lite` | `anthropic-claude-5`, `zhipu-glm-5.3`, `google-gemini-3` |

No verifier link shares a family with any brain link's first choice, so a
gpt-6-astra producer can never be verified by another OpenAI model — the chain
is already right and the router's independence filter is the backstop rather
than the only guard. The verifier policy turns
`family_independence_capabilities: ["verify", "adjudicate"]` on.

**Chain order beats the policy's preferences.** The policy still sorts
cheapest-first, but chain position is the primary key. `zai/glm-5.3-flash` is a
fifth of `deepseek-v4-flash`'s price and second in the cheap chain; a
cost-sorted policy would take it first, and the chain says no.

**When a fallback is taken.** Only `transport_failure`, `provider_failure`,
`model_unavailable`. A `content_refusal`, `budget_refused` or
`contract_violation` **halts** the chain where it stands — otherwise a chain is
a machine for shopping a refused request around until some model says yes, which
is the opposite of what independent verification is for. An unclassified failure
raises rather than guessing.

**What is recorded.** Each link tried is a real route decision: the first
`initial`, each one after it a `switch` in the same attempt referencing the one
before, so the router's own no-cycling rule holds the chain to going forwards.
A new append-only table `model_route_chain_links` records purpose, tier, chain
position, profile, decision id, whether it served, and the skip reason. It is a
separate table rather than three more fields on the decision because the
decision's wire shape is validated key-for-key by the broker adapter: a decision
that grew a field would stop being admissible and every historical hash would
have to be recomputed. Links a *route* refused (retired, not independent, over
budget) are already named with their reasons in the decision's own candidate
snapshot, so the chain does not restate them.

**Budget.** `reserved_micros(route, profile)` reads the estimate for the exact
profile version the decision selected, so a fallback is admitted against the
model that is about to run. Measured in test: deepseek link = 550 µUSD,
glm-5.3-flash link = 200 µUSD, for the same 1,000-in/500-out request.

**Policy is a new pinned version.** `ensure_planner_policy(..., tier=...)`
appends a version carrying `fallback_chains` (optional key, omitted when absent,
so no existing policy hash moves). `deliverable_model_setup` defaults to the
`brain` tier; both it and `research_planner_setup` gained `--tier` and now
register their own model-config file name via `register_model_config_name`
instead of relying on the seed list in `model_configurations`.

## 4. What changed in `openclaw.json` (keys only)

Backup written first: `~/.openclaw/openclaw.json.bak-dalton-catalog-20260909T153655`.
JSON round-tripped, and diffed to confirm nothing outside
`plugins.entries.dalton-openclaw-model-broker` moved. Eight added lines, no
deletions:

* `plugins.entries.dalton-openclaw-model-broker.llm.allowedModels[]` — added `"zai/glm-5.3-flash"`.
* `plugins.entries.dalton-openclaw-model-broker.config.profiles[]` — added one object:
  `id: "profile:zai-glm-5-3-flash"`, `model: "zai/glm-5.3-flash"`,
  `maxTokens: 131072`, `timeoutMs: 600000`, `thinkingLevel: "low"`.

No credential, key, header or provider setting was read or written.

**`openai/gpt-6-astra` has no `providerControls.rateCard`.** Only
`profile:gemini-3-8-flash` and `profile:gemini-3-1-pro-preview` carry one, and
both do so because they use `mode: google-generative-ai-count-tokens-v1`. Astra's
cost comes from `models.providers.openai.models[].cost`: input 10, output 50 —
which is exactly what Dalton's static profile says. It was left alone. But see
the open question about its tiered pricing.

The two `antigravity-cli-gateway/*` models are in `models.providers` with no
broker profile. Left alone: they are in no chain, Dalton has no curated profile
for either, and adding them would queue two uncalibrated models for a smoke run.

## 5. Owner / integrator steps

1. **Reload the gateway so the broker picks up the new profile.** Not done here.
   ```
   openclaw gateway restart
   ```
   (or whatever the owner's usual reload is — the broker reads its plugin config
   at gateway start, and `~/.openclaw/dalton-model-broker.sock` was not touched.)
2. **Run the installer**, which now runs the catalog sync idempotently:
   ```
   deploy/macos/install.sh
   ```
   or the sync alone:
   ```
   PYTHONPATH=src .venv/bin/python scripts/sync_openclaw_model_catalog.py \
     --openclaw-config ~/.openclaw/openclaw.json \
     --model-router-db "$HOME/Library/Application Support/Dalton/state/dalton-core/model-router.sqlite"
   ```
   `--check-only` reports without writing and exits 2 when out of sync.
3. **Repoint the verifier phase pin.** `VERIFIER_POLICY_REF` pins
   `profile:gemini-3-7-flash`, which the broker has not offered for some time —
   `upgrade_openclaw_broker_catalog(openclaw_config_path=...)` already fails
   against the live config for exactly this reason. After the sync that profile
   is retired and verifier routing will be *refused* with `profile_retired`
   rather than failing at the broker. The `verifier` tier chain is the intended
   replacement, but repointing an immutable phase pin is an owner decision and
   was not taken here.

**`catalog_in_sync`, before and after** (against the patched config):

| when | `catalog_in_sync` | missing here | not in broker |
|---|---|---|---|
| now, before any sync run | `false` | the 4 + `profile:zai-glm-5-3-flash` | the 5 + `profile:qwen-deepseek-v4-pro` |
| after one sync run | `true` | — | — |
| after a second run | `true` (nothing written) | — | — |

## 6. Tests

```
Ran 2375 tests in 200.587s

OK (skipped=1)
```

`PYTHONPATH=$PWD/src .venv/bin/python -m unittest discover -s tests -t .`
New: `tests/test_model_catalog_sync.py` (8), `tests/test_model_fallback_chain.py`
(19), both offline against a fake broker. No live model call was made.

## 7. Open questions

* **Should `claude-opus-5` sit in the brain chain after `claude-fable-5-1`?**
  A two-link chain survives one provider outage; a third link would survive an
  Anthropic outage too. Opus 5 is half Fable's input price and a different model
  but the *same family* — so adding it lengthens the brain chain without adding
  independence, and a verifier drawn against an Opus producer would still be
  pushed off `anthropic-claude-5` correctly. My inclination is yes, as link
  three, but it is a spend decision.
* **Per-purpose cost.** Brain-tier unit cost is unchanged for the common case
  (astra serves first). The fallback link costs the same 10/50 as astra, so a
  brain fallback is cost-neutral. Cheap-tier fallbacks get *cheaper*
  (0.22/0.66 → 0.075/0.25 → 0.3/2.5), which means an outage quietly changes the
  quality of extraction output without changing the bill — worth watching.
* **`gpt-6-astra` has tiered pricing** above 272,000 input tokens (10/50 →
  20/75). Dalton's profile carries a flat 10/50, so the router's estimate — and
  therefore budget admission — understates a long-context astra call by up to
  2×. Not introduced here, but the fallback chain makes it more visible: a call
  admitted on the flat estimate is the one that then falls back.
* **`thinkingLevel: "low"` on the new `profile:zai-glm-5-3-flash`.** Chosen
  because it is a cheap-tier fallback for extraction-shaped work; the sibling
  `profile:zai-glm-5-3` uses `"high"`. Nothing has been calibrated on either. One
  key to change if the owner would rather have `"high"`.
* **The cheap tier has no registered purpose yet.** The lanes it describes route
  through their own policies. Pointing `document_extraction_setup` at the cheap
  tier is the obvious next step and was out of scope here.
