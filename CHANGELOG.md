# Changelog

## 2.0.0 — 2026-10-02

A rewrite. The repository is now the installable package `alex`.

**Data**
- The chunk store, the BM25 index and the entity index are gone, and with them
  96 MB of derived JSON in git. `alex build` reads the edition's original TEI
  dump into one SQLite document store: every document whole, with pages,
  sections, paragraphs, notes, entity mentions, measurements and dates as
  character offsets into its text.
- Built from the newest dump (version 12, September 2026) instead of the
  pinned v11; the corpus version and commit are recorded in the store.
- New in the store: Humboldt's marginal notes (previously dropped with the
  editorial notes), author footnotes of the research articles, editorial
  introductions, the edition's "about" pages, animal and mineral names,
  bibliographic references, the unit register with SI conversions, the
  edition's citation recommendation per document, facsimile links.
- Entities missing from the dump's registers are rebuilt from annotations.

**Agent**
- Models: Claude Sonnet 5.5 and Claude Opus 5.5 at effort `medium`; any local
  model behind an OpenAI-compatible endpoint.
- Tools: `search` (ranked passages), `grep`, `read_document`,
  `list_documents`, `lookup_entity`, `entity_mentions`, `chronology`,
  `query_data` (read-only SQL), `run_python` (own sandbox with pandas,
  matplotlib, plotly; can emit maps, timelines, networks, charts and tables),
  `create_map`, `create_timeline`, `create_network`, `wikipedia_context`.
  Replaces `corpus_stats`, `query_measurements`, `chunks_for_entity`,
  `get_context`, `where_was_humboldt` and Anthropic's hosted code execution.
- The loop is provider-neutral and append-only; the nudges and the citation
  repair pass of v1 are gone.
- Query results and computations are citable sources, shown with their SQL or code.

**API and deployment**
- `/api/chat` streams typed events (see `docs/API.md`); `/api/passage`,
  `/api/notes`, `/api/entities`, `/api/config`, `/api/feedback`, `/api/health`.
- Rate limiting with configurable proxy trust, a concurrency cap, security
  headers and a content security policy, configurable framing for embedding.
- Docker Compose with the sandbox as a separate container without network access.
- Tests on a miniature TEI corpus; CI workflow.

**Web client**
- New interface: source panel, live activity feed, artifacts as figure cards
  (map, timeline, network, interactive chart, image, table), light and dark
  theme, German and English, embeddable widget. No third-party scripts or fonts.
