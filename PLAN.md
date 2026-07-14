# Humboldt RAG — Retrieval Overhaul Plan

Date: 2026-07-14. Based on a full audit of the codebase at commit `82a8946`.

## 1. Diagnosis — why retrieval is bad today

Ranked by impact:

1. **Vector search is probably returning noise or nothing.**
   - `docker-compose.yml` sets `EMBEDDING_BACKEND=mock`; `config/settings.py` also defaults to `mock`; `ingest.py` defaults `use_real_embeddings=False`. `MockBackend.embed()` re-seeds `rng(42)` on every call, so *every* text — including the query — gets the same sequence of random vectors. If the deployed `data/vectordb/` (gitignored, but COPY'd into the Docker image from the local machine) was built with mock embeddings, the vector leg of hybrid retrieval is pure noise, and RRF gives that noise a **0.4 weight** — it actively degrades the BM25 results it is fused with.
   - Even if the vectordb was built with `--embeddings real`: `embeddings.py` embeds passages **without** the prefix/instruction format that `multilingual-e5-large-instruct` requires, while `main.py` embeds queries with a `"query: "` prefix (which is the convention of the *non*-instruct e5 models, not the instruct one). Asymmetric/incorrect prefixes measurably degrade e5 ranking.
   - `EMBEDDING_DIM=384` in config vs. 1024 for e5-large — config drift; a mock-built (384-dim) collection would make real 1024-dim queries throw.

2. **Register flooding: 70% of the index is stub entries.**
   Chunk store: 25,118 chunks, of which 17,536 are register entries (9,330 persons, 4,508 places, 3,222 plants, 226 institutions, 250 glossary). Median chunk length is 147 chars. Any query mentioning a person or place gets its BM25 top-k swamped by register stubs instead of actual diary/letter passages — especially since `keyword_search.py` *double-weights* entity names. The one ranked list mixes "who was X" lookup material with narrative sources.

3. **The generation model is retired.**
   `main.py:118` defaults to `gemini-2.0-flash`, discontinued on Vertex AI **2026-06-01** → the deployed bot's generation now fails outright. Note `gemini-2.5-flash` retires **2026-10-16**, so migrating to it buys only 3 months.

4. **Chunk size is wildly inconsistent.**
   p10 = 47 chars, max = 6,472 chars. Letters are parsed as a single chunk (`_parse_letter`) and registers bypass `ChunkingPipeline`'s min-length filter entirely. Long letters blow past `MAX_CHUNK_CHARS=1500` un-split; stubs of <50 chars get indexed.

5. **The editorial annotations are extracted but unused for retrieval.**
   `tei_parser.py` already pulls out `persName`/`placeName` with GND/GeoNames/VIAF/humboldt-IDs, editorial notes, measurements, normalized dates. But retrieval only ever matches free text. Nothing exploits:
   - entity IDs for exact, spelling-variant-immune lookup ("Bonpland" vs historical spellings),
   - the register ↔ text cross-references (`humboldt_id`),
   - date filters (`date_iso` exists on every chunk; `filters` param exists but is never populated),
   - editorial notes as a distinct, attributable voice (they're inlined as `[Anm.: …]` or dropped).
   The Neo4j graph builder (`src/graph/builder.py`) is dead code — never wired into retrieval.

## 2. RAG vs. Grep — weighing the architectures

The paper: **"Is Grep All You Need? How Agent Harnesses Reshape Agentic Search"** (Sen et al., PwC — [arXiv:2605.15184](https://arxiv.org/abs/2605.15184)). What it actually shows: on a 116-question sample of **LongMemEval** (conversational memory, modern English), grep-based agentic retrieval generally beat vector retrieval, *and* the harness/tool-calling style mattered as much as the retrieval method. Its stated failure mode for grep: **vocabulary mismatch** — grep only wins when the agent can generate high-precision patterns.

How that transfers to this corpus:

| Factor | Favors | Why |
|---|---|---|
| Historical multilingual text (German/French/Spanish/Latin, 18th-c. orthography) vs. modern-German user queries | RAG / structured | This is exactly grep's "vocabulary mismatch" failure mode. A user asking "Krokodile am Orinoco" must hit "Crocodile"/"Cocodrilo"/"Kaiman" spellings. |
| Regularized edition + entity annotations with normalized names & GND IDs | Agentic lexical | The editors already solved spelling variance for entities. Entity-ID lookup beats both embeddings and raw grep for who/where questions. |
| Corpus size (~60 MB text, 25k chunks, static) | Agentic | Small enough to search exhaustively per query; no scale argument for ANN indexes. |
| Public chat widget: latency & cost per query | One-shot RAG | An agent loop is 2–5 LLM calls per answer vs. 1–2 today. |
| Current embeddings are broken/stale anyway | Agentic | The dense leg is contributing nothing today; sunk cost is zero. |
| Paper's own headline finding | Agentic | The *harness* (iterative tool loop, ability to re-query, read context around hits) drives accuracy more than dense-vs-sparse. |

**Verdict: adopt the agentic loop, but not raw grep.** The real lesson of the paper is "give the model an iterative search harness," not "regex beats everything." Raw grep over this corpus would faceplant on historical orthography — precisely the caveat the authors state. The winning move here is an **agentic loop over structured lexical tools** built from the TEI annotations (entity index + BM25 + date/type filters + grep for exact phrases/measurements). Embeddings become optional; re-add a *correctly built* dense leg later only if the eval (Phase 3) shows recall gaps on paraphrase-style questions.

## 3. Target architecture

```
User query
   │
   ▼
Agent loop (LLM with tools, budget ≈ 4 tool rounds)
   ├─ lookup_entity(name)            → register index (fuzzy name → normalized entity, bio, humboldt_id/GND)
   ├─ chunks_for_entity(humboldt_id) → inverted entity index (annotation-based, spelling-immune)
   ├─ search(query, doc_type?, date_from?, date_to?) → BM25 over TEXT chunks only (registers excluded)
   ├─ grep(pattern, doc_type?)      → regex over regularized text (exact phrases, measurements, units)
   ├─ get_context(chunk_id)         → neighboring chunks / parent document
   └─ final answer with [Quelle N] citations (existing citation machinery kept)
```

- `/search` endpoint stays as the deterministic single-shot debug path and doubles as the tool backend.
- Registers serve two roles, never mixed into narrative ranking: (a) entity resolution tool, (b) directly answering "Wer war X?" definition questions.

## 4. Phases

### Phase 0 — Stop the bleeding (deployed bot is down) — ~1h
- Replace `gemini-2.0-flash` default in `main.py:118`. Recommendation: **`gemini-3.5-flash`** (no announced shutdown; leapfrogs the 2026-10-16 retirement of 2.5-flash — see [Gemini deprecations](https://ai.google.dev/gemini-api/docs/deprecations)). Alternative: Claude via the existing `AnswerGenerator(provider="anthropic")` path — `claude-sonnet-5` — if moving off Vertex is acceptable.
- Make the model ID env-only (no hardcoded fallbacks in two places — `main.py` and `gemini_backend.py` currently disagree: 2.0 vs 2.5).
- Startup health check that fires one tiny generation call and logs loudly on failure.

### Phase 1 — Fix the index (biggest quality win, architecture-independent) — ~1–2 days
1. **Re-ingest from source.** Add [telota/edition-humboldt-digital](https://github.com/telota/edition-humboldt-digital) (v11, TEI-XML) as a submodule or pinned download; `data/raw/` is currently not reproducible.
2. **Split the index**: `text_index` (diary/letter/document/chronology/research_article, ~6.6k chunks) vs. `register_index` (17.5k entries). BM25 search runs on `text_index` only.
3. **Build the entity inverted index** from the annotations: `humboldt_id → [chunk_ids]`, plus a name→entity resolver over registers (normalized names, GND). This is the main way the editorial annotation work starts paying off.
4. **Uniform chunking**: split letters like diaries (they're currently single chunks up to 6.5k chars); enforce min length for everything; keep parent-child.
5. **Editorial notes as first-class objects**: index each `note[@type='editorial']` as its own retrievable unit linked to its anchor chunk; in generation context, label them "Editorischer Kommentar" vs. Humboldt's own text (the system prompt already asks for this distinction — the data just never arrives labeled).
6. **Populate date/type filters** in retrieval (plumbing exists, `filters` is never passed).
7. **Drop the dense leg for now** (it's noise); delete mock-embedding defaults so this failure mode can't silently recur. Optionally re-add later via an embedding API (avoids the torch/e5 CPU image) — only if Phase 3 shows paraphrase-recall gaps.

### Phase 2 — Agentic retrieval loop — ~1–2 days
- Implement the tool loop (native function calling; works with both Gemini and Claude backends) with a hard budget (e.g. max 4 rounds, then forced answer).
- Tools as in §3; each returns compact JSON with `chunk_id`, `citation_label`, `url`, text.
- Keep query-expansion/rerank code path as fallback mode behind a flag for A/B comparison.
- Add per-answer trace logging (which tools, which chunks) — needed for eval and debugging.

### Phase 3 — Evaluation harness (defines "working") — ~1 day
- Gold set of 40–60 QA pairs across question types: person ("Wer war Bonpland?"), place, date/chronology ("Wo war Humboldt im März 1801?"), measurement ("Höhe des Chimborazo"), glossary, editorial ("Was merken die Herausgeber zu X an?"). Chronology entries give cheap ground truth.
- Metrics: retrieval hit@k (gold doc/chunk), citation precision, answer correctness (LLM-judged), latency, cost.
- Compare: (a) current pipeline as deployed, (b) Phase-1 fixed one-shot, (c) Phase-2 agentic. **This answers RAG-vs-grep for this corpus with data** instead of extrapolating from 116 English memory questions.

### Phase 4 — Deploy — ~½ day
- If the dense leg stays out: drop torch + sentence-transformers + the baked e5 model from the Dockerfile → image shrinks from ~3 GB to ~200 MB, Cloud Run cold starts drop dramatically.
- Ship indexes as build artifacts; pin model IDs via env; restrict CORS.

## 5. Decision points (need Malte's call)
1. **Generation backend**: stay on Vertex (`gemini-3.5-flash`, least infra change) vs. switch to Claude (better German prose & citation discipline in my experience; needs API key billing). Default if no answer: gemini-3.5-flash.
2. **Latency budget** for the public widget — is ~5–10 s acceptable for agentic answers? If not: agentic loop only for hard queries (router), one-shot for simple ones.
3. **Embeddings**: drop entirely (recommended initially) vs. rebuild correctly via API.

## Sources
- [Is Grep All You Need? — arXiv:2605.15184](https://arxiv.org/abs/2605.15184)
- [Gemini API deprecations](https://ai.google.dev/gemini-api/docs/deprecations) · [Vertex AI Gemini 2.0 Flash](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/2-0-flash)
- [telota/edition-humboldt-digital (TEI-XML dataset, v11)](https://github.com/telota/edition-humboldt-digital)
