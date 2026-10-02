# HTTP API

Everything the web client uses. All paths are relative to the deployment root;
JSON everywhere except the chat stream.

## `GET /api/config`

Static facts the client needs before the first question.

```json
{
  "assistant": "Alex",
  "corpus": {"version": "12.0.0", "commit": "f128f89…", "commit_date": "2026-09-10",
             "documents": {"diary": 13, "letter": 558, "document": 62,
                           "chronology": 1684, "research_article": 23, "about": 24},
             "entities": {"person": 9399, "place": 4749, "institution": 235,
                          "plant": 3292, "glossary": 245, "siglum": 41}},
  "models": [
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "provider": "anthropic",
     "tier": "standard", "default": true},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "provider": "anthropic",
     "tier": "premium", "default": false},
    {"id": "local", "label": "Qwen3 (lokal)", "provider": "local",
     "tier": "standard", "default": false}
  ],
  "features": {"python": true, "wikipedia": true, "feedback": true},
  "limits": {"max_query_chars": 2000, "max_history_turns": 12},
  "links": {"edition": "https://edition-humboldt.de",
            "imprint": "https://edition-humboldt.de/impressum.xql",
            "privacy": "https://edition-humboldt.de/impressum.xql"},
  "map": {"tile_url": "https://{s}.tile.openstreetmap.de/{z}/{x}/{y}.png",
          "attribution": "© OpenStreetMap-Mitwirkende", "max_zoom": 18}
}
```

## `POST /api/chat`

Request:

```json
{"query": "Wen traf Humboldt in Wien?",
 "model": "claude-sonnet-5-5",
 "history": [{"role": "user", "content": "…"}, {"role": "assistant", "content": "…"}],
 "wikipedia": false,
 "language": "de"}
```

`model`, `history`, `wikipedia` and `language` (UI language, `de` or `en`) are
optional. Errors before the stream starts are ordinary JSON responses:
`429 {"detail": "…"}` (rate limit), `422` (validation), `503` (no model configured).

Response: `text/event-stream`. Every event is one `data: {json}\n\n` line; the
`type` field tells them apart. Order within one answer:

| type | payload | meaning |
|---|---|---|
| `start` | `request_id`, `model: {id, label}` | the model that actually answers (premium requests can be downgraded by the rate limiter) |
| `thinking` | `text` | delta of the model's reasoning summary / progress note; may never occur |
| `tool_call` | `id`, `name`, `input`, `round` | the agent calls a tool; `input` is the full argument object |
| `tool_result` | `id`, `name`, `ok`, `summary`, `count`, `duration_ms`, `sources: [Source]` | outcome of that call; `sources` are the citable sources it produced |
| `artifact` | `artifact: Artifact` | a visualization exists now; show it immediately. An artifact with an `id` seen before replaces the earlier one |
| `block` | — | a new answer text block begins |
| `text` | `text` | delta appended to the current block |
| `citation` | `source_id`, `cited_text`, `in_note` | a citation that belongs to the current block |
| `reset` | — | discard all answer text streamed so far (the turn is being retried) |
| `done` | `answer: Answer` | authoritative final answer; replaces everything streamed |
| `suggestions` | `questions: [string]` | follow-up questions, may arrive after `done` |
| `error` | `message`, `code` | terminal failure; `code` ∈ `refused`, `overloaded`, `model_error`, `internal` |

The stream ends after `done` (optionally followed by `suggestions`) or `error`.

### Answer

```json
{"text": "full answer as markdown",
 "blocks": [{"text": "Humboldt traf in Wien …", "citations": [
              {"n": 1, "source_id": "S3", "cited_text": "…", "in_note": false}]}],
 "sources": [Source, …],
 "artifacts": [Artifact, …],
 "model": {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
 "usage": {"rounds": 4, "tool_calls": 6, "input_tokens": 0, "output_tokens": 0,
           "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0},
 "elapsed_s": 21.4,
 "stop_reason": "end_turn"}
```

`blocks` concatenated give `text`. A block's citations refer to the whole block.
`sources` holds only cited sources, numbered `n = 1…` in reading order. Models
without native citations write markers like `[S3]` inline; the server turns
them into block boundaries and citations before `done`, but while streaming the
client may see raw `[S3]` markers in `text` deltas and should render them as
chips using the sources received in `tool_result` events.

### Source

```json
{"id": "S3", "n": 1,
 "kind": "text",
 "title": "Dresden, Wien, Salzburg [= Tagebuch der Reise 1797/98]",
 "label": "Bl. 56v",
 "doc_id": "H0019734", "doc_type": "diary", "page": "56v",
 "date": "1797-06-01/1798-04-30",
 "url": "https://edition-humboldt.de/v12/H0019734/56v",
 "start": 7166, "end": 7682}
```

`kind` decides what the source panel shows:

| kind | extra fields | panel content |
|---|---|---|
| `text` | `doc_id`, `start`, `end`, `page` | fetch `/api/passage` |
| `editorial`, `footnote`, `introduction` | `doc_id`, `note_id`, `start`, `end` (anchor span) | fetch `/api/notes/{note_id}` |
| `register` | `entity_id` | fetch `/api/entities/{entity_id}` |
| `data` | `data: {sql, columns, rows, truncated, links}` | the table and the SQL that produced it; cells found in `links` link to the edition |
| `computation` | `computation: {code, output}` | the Python code and its printed output |
| `wikipedia` | `wiki: {lang, title, extract, description, url}` | the extract, marked as external |

### Artifact

```json
{"id": "a1", "type": "map", "title": "Italienreise 1805", "connect_route": true,
 "stations": [{"label": "Rom", "lat": 41.9, "lon": 12.5, "date": "1805-05-01",
               "description": "", "url": "", "group": ""}]}

{"id": "a2", "type": "timeline", "title": "…",
 "events": [{"date": "1805-03-11", "label": "Abreise aus Paris", "description": "", "url": ""}]}

{"id": "a3", "type": "network", "title": "…",
 "nodes": [{"id": "H0003476", "label": "Bonpland, Aimé", "kind": "person", "weight": 340,
            "url": "", "description": "", "focus": true}],
 "edges": [{"source": "H0003476", "target": "H0006403", "weight": 23, "label": ""}]}

{"id": "a4", "type": "chart", "title": "Briefe pro Jahr",
 "figure": {"data": [], "layout": {}}}

{"id": "a5", "type": "image", "title": "…", "media_type": "image/png", "data": "<base64>",
 "alt": "", "attribution": null}

{"id": "a6", "type": "image", "title": "Aimé Bonpland", "url": "https://upload.wikimedia.org/…",
 "attribution": {"artist": "", "license": "", "license_url": "", "file_page": "",
                 "public_domain": true}}

{"id": "a7", "type": "table", "title": "…", "columns": ["doc_id", "briefe"],
 "rows": [["H0002656", 12]], "truncated": false,
 "links": {"H0002656": {"url": "https://edition-humboldt.de/v12/H0002656",
                        "kind": "document", "title": "Alexander von Humboldt an …"}}}
```

`links` (on tables and on `data` sources) maps cell values that name a document
or register entity (its id, or a title or name that is unique in the edition)
to its page in the edition. A `page` column in a row with a linked document
links to that page.

`chart.figure` is a Plotly figure (`data` + `layout`). Dates are ISO strings
that may be partial (`1805`, `1805-03`).

### Tools in the event stream

`tool_call.input` carries the tool's arguments as the model sent them;
`tool_result.summary` is a short phrase in the request's language ("8 Passagen",
"19 Stationen") or, when `ok` is false, a short error text.

| name | arguments |
|---|---|
| `search` | `query`, `doc_types[]`, `date_from`, `date_to`, `doc_id`, `limit` |
| `grep` | `pattern`, `doc_types[]`, `doc_id`, `limit`, `case_sensitive` |
| `read_document` | `doc_id`, `page_from`, `page_to`, `start`, `max_chars` |
| `list_documents` | `doc_type`, `date_from`, `date_to`, `person`, `title_contains`, `limit`, `offset` |
| `lookup_entity` | `name`, `kind` |
| `entity_mentions` | `entity_id`, `doc_types[]`, `doc_id`, `date_from`, `date_to`, `limit`, `offset` |
| `chronology` | `date_from`, `date_to`, `limit` |
| `query_data` | `sql`, `title` |
| `run_python` | `code`, `title` |
| `create_map` | `title`, `stations[]`, `connect_route` |
| `create_timeline` | `title`, `events[]` |
| `create_network` | `title`, `focus`, `kinds[]`, `max_nodes`, `min_shared`, `nodes[]`, `edges[]` |
| `wikipedia_context` | `term`, `gnd_id`, `geonames_id`, `lang`, `include_image` |

### Notes for clients

- `limits.max_history_turns` counts messages (a question and its answer are two).
- The stream may stay open after `done` until `suggestions` arrive; a client
  should accept the next question as soon as it has `done`.
- `chart.figure` uses cartesian Plotly trace types only (bar, scatter, line,
  histogram, box, heatmap, …). Figures arrive unstyled; the client applies the
  theme. Anything else comes as an `image`.
- Link fields (`url` of stations, events, nodes, images) are `http(s)` or empty.

## `GET /api/passage?doc_id=H0019734&start=7166&end=7682`

The exact window of a document plus what a reader needs around it.

```json
{"document": {"doc_id": "…", "doc_type": "diary", "title": "…", "date_from": "…",
              "date_to": "…", "url": "…", "citation": "…", "repository": "…",
              "shelfmark": "…", "editors": "…", "authors": "…", "n_chars": 68214},
 "start": 7166, "end": 7682, "page": "56v", "url": "…/H0019734/56v",
 "facsimile": "https://…",
 "text": "…",
 "pages": [{"label": "57r", "start": 7600, "facsimile": "https://…"}],
 "notes": [{"note_id": 1, "kind": "editorial", "label": "", "text": "…",
            "anchor_start": 7200, "anchor_end": 7230}],
 "before": 6400, "after": 8500}
```

`page` and `facsimile` belong to the page on which the window starts; `pages` are
the page breaks inside the window (offsets are document offsets).
`before` / `after` are offsets the client can pass as the new `start` / `end`
to widen the window by one step; `null` at the document's edges. The window
may not exceed 20 000 characters.

## `GET /api/notes/{note_id}`

```json
{"note": {"note_id": 1, "kind": "editorial", "label": "", "text": "…",
          "anchor_start": 7200, "anchor_end": 7230},
 "passage": { …same shape as /api/passage, a window around the anchor… }}
```

## `GET /api/entities/{entity_id}`

```json
{"entity_id": "H0003476", "kind": "person", "name": "Bonpland, Aimé",
 "alternative_names": ["Aimé Bonpland"], "description": "…", "born": "1773",
 "died": "1858", "gnd": "…", "geonames": "", "viaf": "", "wikidata": "",
 "url": "https://edition-humboldt.de/v12/H0003476", "lat": null, "lon": null,
 "links": {}, "in_register": 1, "mention_count": 340, "document_count": 68}
```

## `POST /api/feedback`

`{"request_id": "…", "rating": "up" | "down", "comment": ""}` → `204`.

## `GET /api/health`

`{"status": "ok", "corpus_version": "12.0.0", "models": ["claude-sonnet-5-5", …]}`;
`503` with `"status": "unavailable"` when no model is configured or the store is missing.
