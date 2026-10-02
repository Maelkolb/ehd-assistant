# Deployment

## The short version

```bash
cp .env.example .env        # models, limits, who may embed Alex
docker compose up --build   # API on 127.0.0.1:8000, sandbox on an internal network
```

Put a reverse proxy with TLS in front of port 8000 and set `PROXY_HOPS=1`.

## Corpus

The image build clones the edition's TEI dump and builds the document store
(about ten seconds). `CORPUS_REF` chooses the state of the dump:

```bash
docker compose build --build-arg CORPUS_REF=main        # newest data
docker compose build --build-arg CORPUS_REF=f128f89     # a pinned commit, reproducible
```

`GET /api/config` and `/api/health` report the version and commit in use. To
update the data, rebuild the image. Without Docker: `alex fetch && alex build`,
then restart the service.

New places in a data release have GeoNames ids but no coordinates.
`alex coordinates` resolves them (in bulk through Wikidata, the rest from
GeoNames' own records) and updates `data/geo/coordinates.json`; commit the
file. Places without a GeoNames id (regions such as "Tropen", historical
provinces) stay off maps unless the model supplies coordinates.

## Models

### Claude

Set `ANTHROPIC_API_KEY`. Alex offers Claude Sonnet 5.5 (default) and Claude
Opus 5.5, both at `EFFORT=medium`. Opus is the premium tier: each client gets
`RATE_LIMIT_PREMIUM_PER_HOUR` Opus answers per hour, further Opus requests are
answered by Sonnet. `ANTHROPIC_MODELS=claude-sonnet-5-5` offers Sonnet only.

A request the API's safety classifiers decline is re-run once on the fallback
model Anthropic recommends (`ANTHROPIC_FALLBACKS=1`, the default; a beta
feature of the API). Set it to `0` to disable.

### A local model

Any server that speaks the OpenAI chat completions protocol with tool calling:

```bash
LOCAL_LLM_BASE_URL=http://gpu-host:8000/v1
LOCAL_LLM_MODEL=qwen3.8-27b            # the name the server serves
LOCAL_LLM_LABEL=Qwen 3.8 (lokal)       # shown in the model menu
LOCAL_LLM_CONTEXT_TOKENS=64000         # the server's context window
LOCAL_LLM_API_KEY=…                    # if the server wants one
```

With both a Claude key and a local model configured, the user can choose;
`DEFAULT_MODEL` sets the default. With only `LOCAL_LLM_*` set, Alex runs
without any external service.

What the model must be able to do: follow a long system prompt, call tools
with JSON arguments, and work in a context window of 32 000 tokens or more
(64 000 recommended). Below 100 000 tokens Alex shortens tool results and
drops old ones when the window fills. Local models cite with `[S12]` markers
instead of quoted spans, so the source panel opens the passage without
highlighting the exact sentence.

Example server (vLLM):

```bash
vllm serve Qwen/Qwen3.8-27B-FP8 --served-model-name qwen3.8-27b \
  --max-model-len 65536 --kv-cache-dtype fp8 --language-model-only \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_xml
```

`scripts/modal_local_llm.py` runs exactly this on Modal for testing. The
reasoning parser matters: without it the model's thinking arrives as answer
text (Alex strips `<think>…</think>` if that happens).

## Python sandbox

| `SANDBOX_MODE` | Where the code runs | Use for |
|---|---|---|
| `http` | the `sandbox` container of `docker-compose.yml` | public deployments |
| `subprocess` | a guarded child process of the API | development, trusted users |
| `off` | nowhere; the `run_python` tool is not offered | when in doubt |

The sandbox container has no route to the internet (internal Docker network),
no secrets in its environment, a read-only root filesystem with a small tmpfs,
no Linux capabilities, and limits on memory, CPU and process count. The corpus
it can read is public data. If you deploy without Compose, keep these
properties: the worker's in-process guard (see ARCHITECTURE.md) is not a
sufficient boundary against a determined attacker on its own.

## Behind a reverse proxy

The chat endpoint is a server-sent event stream; the proxy must not buffer it.

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 600s;
}
```

Set `PROXY_HOPS` to the number of trusted proxies in front of Alex (1 for the
setup above). The rate limiter then takes the client address from that
position of `X-Forwarded-For`; with `PROXY_HOPS=0` the header is ignored.

Run **one** API process. Rate limits, the daily budget and the concurrency cap
are kept in memory; several processes would each grant the full allowance.

## Embedding in the edition's website

Allow the site to frame Alex and add one script tag:

```bash
FRAME_ANCESTORS='self' https://edition-humboldt.de
```

```html
<script src="https://alex.example.org/widget.js" data-lang="de" defer></script>
```

The script adds a launcher button that opens Alex in a panel
(`/?embed=1` in an iframe). A link to the full page works as well.

## Privacy and transparency

- No cookies, no tracking, no server-side conversation storage. The
  conversation lives in the browser; share links carry the answer in the URL
  fragment, which is never sent to the server.
- Logs record per answer: model, tools used, token counts, duration, and no
  client address. Questions are logged only with `LOG_QUERIES=1`. The rate
  limiter keeps a salted hash of the client address in memory for one hour;
  the salt is random per process and never stored.
- Questions and the passages retrieved for them are sent to the configured
  model provider. With a local model nothing leaves your infrastructure.
- Third-party requests from the browser: map tiles (`MAP_TILE_URL`, default the
  same OpenStreetMap server the edition's own maps use) when a map is shown,
  and images from Wikimedia Commons when the user switched on Wikipedia
  context. Fonts and scripts are served by Alex itself.
- The interface states that answers are generated by an AI system and must be
  checked against the cited sources; the imprint and privacy links are
  configurable (`LINK_IMPRINT`, `LINK_PRIVACY`).
- `FEEDBACK_LOG=/path/feedback.jsonl` enables thumbs up/down; mount a writable
  volume for it (the API container's filesystem is read-only).

## Cost control

`RATE_LIMIT_PER_HOUR`, `RATE_LIMIT_PREMIUM_PER_HOUR`, `DAILY_REQUEST_BUDGET`,
`MAX_CONCURRENT_CHATS` and `MAX_ROUNDS` bound what a deployment can spend.
Every answer's log line carries its token counts and estimated cost.

## Health

`GET /api/health` returns 200 with the corpus version and the configured
models, 503 when no model is configured.
