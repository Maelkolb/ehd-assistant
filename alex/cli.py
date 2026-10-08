"""Command line: fetch the corpus, build the store, serve, ask.

alex fetch            clone or update the edition's TEI dump
alex build            build the document store from the dump
alex coordinates      resolve place coordinates for new GeoNames ids
alex serve            run the web application
alex ask "question"   answer one question in the terminal
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys

from alex.config import load_settings


def fetch(settings, ref: str | None) -> None:
    """A shallow checkout of the requested ref (default: the newest state of main)."""
    ref = ref or settings.corpus_ref
    target = settings.corpus_dir
    if not (target / ".git").exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", settings.corpus_repository],
            check=True,
        )
    subprocess.run(
        ["git", "-C", str(target), "fetch", "-q", "--depth", "1", "origin", ref], check=True
    )
    subprocess.run(
        ["git", "-C", str(target), "checkout", "-q", "--detach", "FETCH_HEAD"], check=True
    )
    revision = subprocess.run(
        ["git", "-C", str(target), "log", "-1", "--format=%H %cs"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    print(f"Corpus at {target}: {ref} = {revision}")


def build(settings) -> None:
    from alex.corpus.build import build_store

    counts = build_store(settings.corpus_dir, settings.store_path, settings.coordinates_path)
    print(f"Store written to {settings.store_path}: {json.dumps(counts)}")


def coordinates(settings) -> None:
    from alex.corpus.coordinates import update_coordinates

    print(update_coordinates(settings.corpus_dir, settings.coordinates_path))


def serve(settings, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(
        "alex.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        proxy_headers=False,
        log_level="info",
    )


def ask(settings, question: str, model: str | None, language: str, show_events: bool) -> int:
    from alex.runtime import create_runtime

    runtime = create_runtime(settings)
    spec = settings.model(model)
    if spec is None:
        print(
            "No such model configured. Available: "
            + (", ".join(spec.id for spec in settings.models) or "none"),
            file=sys.stderr,
        )
        return 2
    for event in runtime.agent.run(question, spec, language=language):
        kind = event["type"]
        if kind == "tool_call":
            arguments = json.dumps(event["input"], ensure_ascii=False)
            print(f"\n  → {event['name']} {arguments[:300]}", file=sys.stderr)
        elif kind == "tool_result":
            print(
                f"    {'ok' if event['ok'] else 'ERROR'} {event['summary']} "
                f"({event['duration_ms']} ms, {len(event['sources'])} sources)",
                file=sys.stderr,
            )
        elif kind == "artifact":
            artifact = event["artifact"]
            print(
                f"    artifact: {artifact['type']} “{artifact.get('title', '')}”", file=sys.stderr
            )
        elif kind == "thinking" and show_events:
            print(event["text"], end="", file=sys.stderr)
        elif kind == "text":
            print(event["text"], end="", flush=True)
        elif kind == "error":
            print(f"\nError ({event['code']}): {event['message']}", file=sys.stderr)
            return 1
        elif kind == "done":
            answer = event["answer"]
            print("\n\nSources:")
            for source in answer["sources"]:
                print(
                    f"  [{source['n']}] {source['title'][:70]} — {source['label']} "
                    f"{source.get('url', '')}"
                )
            print(f"\n{json.dumps(answer['usage'])} · {answer['elapsed_s']} s", file=sys.stderr)
        elif kind == "suggestions":
            print("Follow-ups: " + " | ".join(event["questions"]), file=sys.stderr)
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="alex", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("fetch")
    fetch_parser.add_argument("--ref", help="branch, tag or commit (default: CORPUS_REF or main)")
    commands.add_parser("build")
    commands.add_parser("coordinates")
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    ask_parser = commands.add_parser("ask")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--model")
    ask_parser.add_argument("--language", default="de", choices=["de", "en"])
    ask_parser.add_argument("--thinking", action="store_true", help="print reasoning summaries")
    arguments = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = load_settings()
    if arguments.command == "fetch":
        fetch(settings, arguments.ref)
    elif arguments.command == "build":
        build(settings)
    elif arguments.command == "coordinates":
        coordinates(settings)
    elif arguments.command == "serve":
        serve(settings, arguments.host, arguments.port)
    elif arguments.command == "ask":
        return ask(
            settings,
            arguments.question,
            arguments.model,
            arguments.language,
            arguments.thinking,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
