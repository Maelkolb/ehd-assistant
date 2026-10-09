# Changelog

## 2.3.0 — 2026-10-09

- Claude Haiku 5.5 joins the model menu as the fast tier (about a twentieth of
  Sonnet's price); Sonnet stays the default, `DEFAULT_MODEL` changes that.
  Haiku gets no server-side refusal fallback, which the API does not offer
  for it. Follow-up suggestions now come from Haiku 5.5 as well.

## 2.2.0 — 2026-10-09

**Agent**
- Three tools for the questions the probes showed the model answering by hand:
  `entities_in_document` (who and what is annotated in a document or a page
  range, in order of first appearance, with counts and coordinates),
  `itinerary` (stations of a journey with register coordinates, from the
  chronology for a date range or from a document's own place names, ready for
  `create_map`), and `dated_passages` (the passages around the dates the
  editors annotated in the texts: what Humboldt wrote on or about a date).
- The Wikipedia tool and its interface switch are gone. The edition is the
  only source again; the client makes no third-party requests except map tiles.

## 2.1.0 — 2026-10-09

Tool use, made reliable again on Claude Sonnet 5.5 and Opus 5.5.

**Agent**
- The instructions no longer tell the model to stop researching early; they
  ask it to use the tools even when it feels sure, to read the passages that
  matter, to follow up document lists and counts, and never to offer a search
  instead of running it. The current Claude models follow "match the effort"
  wording literally and answered from memory after one or two calls.
- Two reminders, each at most once per answer: when the question asked for a
  map, timeline, network or chart and none was created, the model is sent back
  to create it; when a long researched answer carries no citation at all, it
  writes the answer again with citations.
- Tool calls survive the models' habits: parameter names they tend to use
  (`entity_type`, `humboldt_id`, `top_k`, `document_id`, `page`, `query` for
  a pattern, …) are mapped to the declared ones, choice values in other
  spellings or plural (`Diaries`, `letters`, `Person`) are accepted, unknown
  values and parameters are reported back instead of failing or being dropped
  silently, and a failed call names the parameters the tool takes.
- `chronology` says when a date range was cut off at the limit and how to page on.
- The language of the question is detected from its function words and stated
  to the model, so an English question in the German interface is answered in
  English; the reminders are sent in the question's language, so they no
  longer pull a rewritten answer into English.

**Data**
- The `measurements` view gained a `context` column with the surrounding text,
  so measurements near a place or topic can be found in SQL
  (`WHERE context LIKE '%Chimborazo%'`), and the `letters` view carries `date_min`
  and `date_max` for ranges and sorting. Store schema 4; rebuild with `alex build`.

**Web client**
- Hovering or focusing a citation chip shows what it points at: the source,
  its page or date, and the quoted span.

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
