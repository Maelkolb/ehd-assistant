# Alex — research assistant of the edition humboldt digital

Alex answers questions about Alexander von Humboldt from the
[edition humboldt digital](https://edition-humboldt.de) (Berlin-Brandenburgische
Akademie der Wissenschaften): the travel journals, letters, documents, the
chronology, the research articles and the registers. It researches in the
edition with tools, cites the passages it relies on, and builds maps,
timelines, networks and charts.

```
browser ── /api/chat (SSE) ──► agent loop ──► model (Claude, or a local model)
                                   │
        search · grep · read_document · list_documents      ┐
        lookup_entity · entity_mentions · chronology        │ document store
        query_data (read-only SQL)                          ┘ (SQLite, built from the TEI dump)
        run_python (sandbox: pandas, matplotlib, plotly)
        create_map · create_timeline · create_network
```

## What it is built on

**The original data, whole.** `alex build` reads the edition's own TEI-XML dump
([telota/edition-humboldt-digital](https://github.com/telota/edition-humboldt-digital))
into one SQLite file. Every document is stored as a whole; nothing is cut by
length. Pages, sections, paragraphs, editorial notes, annotated persons, places,
institutions, plants, measurements and dates are kept as character offsets into
the document text, so a search hit, a register mention or a citation always
points to an exact place in an exact document. The dump is fetched at build
time (`CORPUS_REF`, default: the newest state of `main`); no derived data is
committed to this repository.

**An agent with real tools.** The model decides how to research: ranked
full-text search, regular expressions over the complete texts, reading
documents by page or offset, the registers and their annotated mentions, the
chronology, read-only SQL over the whole corpus, and a Python sandbox for
analysis and charts.

**Citations a reader can check.** Every source the model was shown is
registered; cited sentences link to the passage with the quoted span
highlighted, to the edition's page, and to the edition's own citation
recommendation. Query results and computations are sources too, shown with the
SQL or the code that produced them.

**Any model.** Claude Opus 5.5 and Sonnet 5.5 through the Anthropic API, or a
locally hosted model behind an OpenAI-compatible endpoint (vLLM, Ollama,
llama.cpp, TGI). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env          # set ANTHROPIC_API_KEY, or LOCAL_LLM_BASE_URL + LOCAL_LLM_MODEL

alex fetch                    # the newest TEI dump of the edition  → data/ehd-source
alex build                    # the document store                  → data/store/ehd.sqlite
alex serve                    # http://127.0.0.1:8000

alex ask "Wo war Humboldt im Sommer 1805?"   # the same agent in the terminal
pytest                        # tests run on a miniature TEI corpus, no model needed
```

With Docker (API and sandbox as separate containers):

```bash
cp .env.example .env
docker compose up --build
```

## Layout

| Path | What |
|---|---|
| `alex/corpus/` | TEI reader (`tei.py`), registers, store builder, read API (`store.py`), coordinates |
| `alex/agent/` | agent loop, tools, sources and citations, prompts |
| `alex/llm/` | model providers: Anthropic, OpenAI-compatible |
| `alex/sandbox/` | guarded Python worker, the `ehd` helper module, sandbox service |
| `alex/api/` | FastAPI application, rate limiting |
| `alex/web/` | the web client (static, no build step) and the embeddable widget |
| `docs/` | [API](docs/API.md), [architecture](docs/ARCHITECTURE.md), [deployment](docs/DEPLOYMENT.md) |
| `scripts/` | asset vendoring, a mock API for frontend work, a Modal example for a local model |

## Data and licence of the edition

The texts are © Berlin-Brandenburgische Akademie der Wissenschaften,
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Alex shows the
edition's citation recommendation for every document it quotes. Place
coordinates (`data/geo/coordinates.json`) are resolved from the GeoNames ids
the edition records, through Wikidata (CC0) and GeoNames (CC BY 4.0).
