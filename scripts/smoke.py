"""Run a set of real questions through the configured model and summarise what happened.

    python scripts/smoke.py                      # all questions, default model
    python scripts/smoke.py --model claude-opus-5-5 --only map chart
    python scripts/smoke.py --out smoke.jsonl    # keep the full answers

Costs real model usage. One line per question: tools used, cited sources,
artifacts, tokens, estimated cost, duration.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from alex.runtime import create_runtime

QUESTIONS = {
    "person": "Wer war Bonpland?",
    "mentions": "Wo wird Carl Sigismund Kunth in den Tagebüchern erwähnt?",
    "topic": "Was schreibt Humboldt über Erdbeben in Cumaná?",
    "editors": "Was merken die Herausgeber zum Vulkan Soufrière an?",
    "glossary": "Was bedeutet „alii dicunt“?",
    "date": "Wo war Humboldt im März 1801?",
    "summary": "Fasse den Brief Humboldts an Kunth vom 27. April 1826 zusammen.",
    "count": "Mit wem korrespondierte Humboldt in der Edition am häufigsten?",
    "measure": "Welche Höhenmessungen in Toisen finden sich zum Chimborazo?",
    "map": "Erstelle eine Karte der Stationen von Humboldts Italienreise 1805.",
    "timeline": "Erstelle eine Zeitleiste von Humboldts Aufenthalt in Neapel 1805.",
    "network": "Zeig mir ein Beziehungsnetz der Personen um Aimé Bonpland.",
    "chart": "Zeige als Diagramm, wie viele Briefe der Edition aus jedem Jahr stammen.",
    "analysis": "Vergleiche die Temperaturmessungen in Réaumur und Fahrenheit: "
    "Verteilung in °C als Diagramm.",
    "english": "Whom did Humboldt meet in Vienna in 1797?",
    "none": "Was ist Humboldts Lieblingspizza?",
    "outside": "Wo war Humboldt am 1. Januar 2020?",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model")
    parser.add_argument("--only", nargs="*", help="keys of the questions to run")
    parser.add_argument("--out", help="write full answers as JSON lines")
    arguments = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")

    runtime = create_runtime()
    spec = runtime.settings.model(arguments.model)
    if spec is None:
        print("No such model configured.", file=sys.stderr)
        return 2
    selected = {key: QUESTIONS[key] for key in (arguments.only or QUESTIONS)}
    output = open(arguments.out, "w", encoding="utf-8") if arguments.out else None
    total_cost = 0.0
    failures = 0
    print(f"model: {spec.label}   corpus: {runtime.store.meta.get('corpus_version')}\n")
    for key, question in selected.items():
        language = "en" if key == "english" else "de"
        started = time.time()
        tools, answer, error = [], None, ""
        for event in runtime.agent.run(question, spec, language=language):
            if event["type"] == "tool_call":
                tools.append(event["name"])
            elif event["type"] == "tool_result" and not event["ok"]:
                tools[-1] += "!"
            elif event["type"] == "done":
                answer = event["answer"]
            elif event["type"] == "error":
                error = f"{event['code']}: {event['message']}"
        if answer is None:
            failures += 1
            print(f"{key:9} FAILED {error}")
            continue
        usage = answer["usage"]
        total_cost += usage["cost_usd"]
        artifacts = ",".join(artifact["type"] for artifact in answer["artifacts"]) or "-"
        print(
            f"{key:9} {time.time() - started:5.0f}s  sources {len(answer['sources']):2}  "
            f"artifacts {artifacts:16} in {usage['input_tokens'] + usage['cache_read_tokens']:7} "
            f"out {usage['output_tokens']:6}  ${usage['cost_usd']:.3f}  "
            f"{' '.join(tools)}"
        )
        print(f"          {answer['text'][:160].replace(chr(10), ' ')}…\n")
        if output:
            output.write(
                json.dumps({"key": key, "question": question, "answer": answer}, ensure_ascii=False)
                + "\n"
            )
    print(f"total estimated cost ${total_cost:.2f}, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
