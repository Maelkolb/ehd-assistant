# Architecture

## Document store

`alex build` turns a checkout of the edition's TEI dump into `data/store/ehd.sqlite`.

- **`documents`**: one row per TEI document with its complete reading text
  (abbreviations expanded, deletions omitted, Humboldt's marginal notes inline
  as ⟨…⟩, editorial supplements in [ ]) and its header metadata: dates,
  correspondents, repository and shelfmark, editors, the edition's citation
  recommendation.
- **Offsets instead of chunks.** `pages`, `sections`, `paragraphs`, `notes`,
  `mentions`, `measures` and `dates` hold character offsets into
  `documents.text`. Nothing is duplicated and nothing is cut by length.
- **`text_index`**: an FTS5 index over each document's own paragraphs and over
  the editorial notes and footnotes (diacritic-insensitive). It stores no text;
  a hit is a pointer `(doc_id, start, end)`. Search ranks paragraphs that
  contain more of the query's terms first, then by BM25, and returns a window
  of the document around the hit.
- **`entities`**: the registers (persons, places, institutions, plants,
  glossary, sigla). An entity the texts reference but the dump's registers lack
  is rebuilt from its annotations (commonest spelling, authority ids from the
  reference) and marked `in_register = 0`.
- **`units`** with the edition's SI conversions; the `measurements` view applies them.
- Views for the common questions: `letters`, `chronology`, `measurements`,
  `entity_mentions`.
- `meta` records the corpus version and commit the store was built from.

`alex/corpus/store.py` is the only reader. Its SQL tool path opens a separate
read-only connection with an authorizer (SELECT only), a time budget and a row limit.

## Agent

`Agent.run()` (in `alex/agent/loop.py`) is a generator of the events in
[API.md](API.md). One request:

1. A provider opens a conversation with the system prompt, the tool
   definitions, the client's text history and the question.
2. The model's turn is streamed. Tool calls are validated and executed
   (`alex/agent/tools.py`); every passage, register entry, query result or
   computation shown to the model is registered as a source (`sources.py`).
3. Results go back to the model; the loop repeats until the model answers or
   the round budget is spent, in which case tools are withdrawn for one final turn.
4. When the model ends its turn, two checks may send it back once each: a
   question that asked for a map, timeline, network or chart without one being
   created, and a long researched answer without a single citation. The
   reminder is appended as a user turn (after an assistant turn there is no
   user message a mid-conversation system message could follow).
5. The answer's citations are resolved against the registered sources and
   numbered in reading order. Citations of sources the model was never shown
   are dropped.

Tool inputs are validated and coerced before a tool runs (`ToolBox.run`):
numbers as strings, arrays as JSON text, parameter names the models like to
use instead of the declared ones (`ALIASES`), choice values in other spellings
(`CHOICE_SYNONYMS`). Unknown values and parameters go back to the model as a
hint inside the tool result rather than failing the call.

The transcript is append-only. With Claude Opus 5.5 and Sonnet 5.5 that keeps
the model's thinking blocks valid across tool rounds and the prompt cache warm
(system prompt and tools are cached for an hour, the conversation tail
automatically).

### Providers

`alex/llm/base.py` defines what the loop needs: `Conversation.stream()` and
`Conversation.add_tool_outputs()`.

- **Anthropic** (`anthropic_provider.py`): sources are sent as `search_result`
  blocks with citations enabled, so cited sentences carry the exact quoted
  span. Adaptive thinking with summaries for the activity feed, effort from
  `EFFORT`, server-side fallback for declined requests.
- **OpenAI-compatible** (`openai_compatible.py`): for local models. Sources are
  sent as text labelled `[S12]`; the model cites by writing these markers, which
  are turned into the same citation structure. Old tool results are dropped
  from the transcript when a small context window fills up.

Models whose context window is below 100 000 tokens get shorter tool results
(fewer and shorter passages, smaller reads).

## Python sandbox

`run_python` executes model-written code in a fresh worker process
(`alex/sandbox/worker.py`) with an empty environment and a private working
directory. After pandas, matplotlib and plotly are loaded, an audit hook
confines the process: no sockets, no subprocesses, no native-library loading,
reads only inside the Python installation, the corpus and the working
directory, writes only inside the working directory; the database is opened
read-only. Time, memory (POSIX) and output size are limited.

The audit hook is defence in depth, not a security boundary. In the Docker
deployment the worker runs in its own container on an internal network: no
route to the outside, no secrets in its environment, read-only filesystem,
dropped capabilities, limited memory, CPU and process count. The API reaches
it over HTTP (`SANDBOX_MODE=http`).

What the code can show the user: matplotlib figures (PNG), plotly figures
(interactive), DataFrames (sortable tables), and the same map, timeline and
network components the declarative tools produce.

## Web client

Static files under `alex/web/`, ES modules, no build step, no third-party
requests except map tiles and images from Wikimedia Commons. It consumes the
event stream, renders citations from the answer blocks, and fetches source
passages from `/api/passage` for the side panel. `widget.js` embeds Alex in a
host page as a launcher button with an iframe panel.

## What is deliberately not here

- No embeddings or vector index: the edition's own annotations resolve
  spelling variance for entities, and the agent searches iteratively in the
  languages of the sources.
- No server-side conversation state: history lives in the browser and is sent
  as text with each request; share links carry the answer in the URL fragment.
