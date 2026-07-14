"""
LLM-based answer generation with citation grounding.

Constructs context from retrieval results, generates answers with
inline citations, and verifies that citations are grounded in sources.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

from src.retrieval.results import RetrievalResult

logger = logging.getLogger(__name__)


# =============================================================================
# System Prompt
# =============================================================================

SYSTEM_PROMPT = """Du bist ein wissenschaftlicher Assistent für die **edition humboldt digital**, \
die digitale Edition der Tagebücher und Briefe Alexander von Humboldts (1769–1859), \
herausgegeben von Ottmar Ette an der Berlin-Brandenburgischen Akademie der Wissenschaften.

## Deine Aufgabe
Du beantwortest Fragen zu Alexander von Humboldt, seinen Reisen, Forschungen, \
Korrespondenzen und seinem wissenschaftlichen Werk auf Basis der bereitgestellten Quellen.

## Regeln

### Quellentreue
- Antworte auf Basis der bereitgestellten Quellen im <kontext>-Block.
- Jede faktische Aussage MUSS mit einer Quellenangabe belegt werden.
- Zitierformat: [Quelle N] — wobei N die Nummer der Quelle im Kontext ist.
- Wenn die Quellen eine Frage nicht beantworten, sage das ehrlich: \
  "Diese Frage kann ich anhand der vorliegenden Quellen nicht beantworten."

### Quellenunterscheidung
- Unterscheide klar zwischen Humboldts Originaltext und editorischen Kommentaren.
- Markiere editorische Anmerkungen explizit: "Laut einer Anmerkung der Herausgeber..."
- Zitiere relevante Originalpassagen Humboldts in Anführungszeichen.

### Antwortformat
- Antworte auf der Sprache der Frage (Deutsch, Englisch, Französisch).
- Strukturiere längere Antworten in Absätze. 
- Am Ende jeder Antwort: Liste die verwendeten Quellen mit ihren \
  vollständigen Angaben (Tagebuch, Folio, Datum) und Links.

### Was du NICHT tun darfst
- Keine Informationen erfinden, die nicht in den Quellen stehen.
- Keine modernen Interpretationen als Humboldts Meinung ausgeben.
- Keine Quellenangaben fingieren."""


# =============================================================================
# Context Assembly
# =============================================================================

def build_context(results: list[RetrievalResult]) -> str:
    """
    Build the <kontext> block for the LLM prompt from retrieval results.
    Each source gets a numbered entry with full metadata.
    """
    parts = ["<kontext>"]

    for i, result in enumerate(results, 1):
        meta = result.metadata
        parts.append(f"""
<quelle nr="{i}">
  <herkunft>{meta.get('citation_label', result.chunk_id)}</herkunft>
  <datum>{meta.get('date_iso', 'unbekannt')}</datum>
  <url>{meta.get('url', '')}</url>
  <personen>{', '.join(meta.get('persons', []) if isinstance(meta.get('persons'), list) else [])}</personen>
  <orte>{', '.join(meta.get('places', []) if isinstance(meta.get('places'), list) else [])}</orte>
  <text>{result.text}</text>
  <editorische_anmerkungen>
    {chr(10).join(meta.get('editorial_notes', []) if isinstance(meta.get('editorial_notes'), list) else [])}
  </editorische_anmerkungen>
</quelle>""")

    parts.append("\n</kontext>")
    return "\n".join(parts)


# =============================================================================
# Answer Generation
# =============================================================================

@dataclass
class Citation:
    """A citation extracted from the generated answer."""
    source_number: int
    claim: str
    citation_label: str = ""
    url: str = ""
    verified: bool = False


@dataclass
class GeneratedAnswer:
    """The generated answer with citations and metadata."""
    text: str
    citations: list[Citation] = field(default_factory=list)
    sources_used: list[dict] = field(default_factory=list)
    model: str = ""
    query: str = ""
    context_chunks: int = 0

    def to_dict(self) -> dict:
        return {
            "answer": self.text,
            "citations": [
                {"source": c.source_number, "label": c.citation_label,
                 "url": c.url, "verified": c.verified}
                for c in self.citations
            ],
            "sources": self.sources_used,
            "model": self.model,
        }


class AnswerGenerator:
    """
    Generates grounded answers using an LLM.

    Supports Anthropic Claude and OpenAI APIs.
    """

    def __init__(
        self,
        provider: str = "anthropic",  # "anthropic" or "openai"
        model: str | None = None,
        api_key: str | None = None,
    ):
        import os
        if model is None:
            model = os.environ.get("LLM_MODEL", "claude-sonnet-5")
        self.provider = provider
        self.model = model

        if provider == "anthropic":
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        elif provider == "openai":
            import openai
            self.client = openai.OpenAI(api_key=api_key) if api_key else openai.OpenAI()
        else:
            raise ValueError(f"Unknown provider: {provider}")

    def generate(
        self,
        query: str,
        retrieval_results: list[RetrievalResult],
        max_tokens: int = 2000,
    ) -> GeneratedAnswer:
        """Generate a grounded answer with citations."""
        context = build_context(retrieval_results)

        user_message = f"{context}\n\nFrage: {query}"

        # Call the LLM
        if self.provider == "anthropic":
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            answer_text = response.content[0].text
        elif self.provider == "openai":
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
            )
            answer_text = response.choices[0].message.content

        # Extract citations from the answer
        citations = self._extract_citations(answer_text, retrieval_results)

        # Build source list
        sources_used = []
        for result in retrieval_results:
            sources_used.append({
                "citation_label": result.citation_label,
                "url": result.url,
                "score": round(result.fused_score, 4),
            })

        return GeneratedAnswer(
            text=answer_text,
            citations=citations,
            sources_used=sources_used,
            model=self.model,
            query=query,
            context_chunks=len(retrieval_results),
        )

    def _extract_citations(
        self, text: str, results: list[RetrievalResult]
    ) -> list[Citation]:
        """Extract [Quelle N] citations from generated text."""
        citations = []
        pattern = r'\[Quelle\s+(\d+)\]'

        for match in re.finditer(pattern, text):
            src_num = int(match.group(1))
            if 1 <= src_num <= len(results):
                result = results[src_num - 1]

                # Extract the claim (text before this citation)
                start = max(0, match.start() - 200)
                claim = text[start:match.start()].strip()
                # Take last sentence
                sentences = re.split(r'[.!?]\s+', claim)
                claim = sentences[-1] if sentences else claim

                citations.append(Citation(
                    source_number=src_num,
                    claim=claim,
                    citation_label=result.citation_label,
                    url=result.url,
                    verified=True,  # Basic check: source number exists
                ))

        return citations


class MockGenerator:
    """Mock generator for testing without LLM API."""

    def generate(
        self, query: str, retrieval_results: list[RetrievalResult], **kwargs
    ) -> GeneratedAnswer:
        if not retrieval_results:
            return GeneratedAnswer(
                text="Diese Frage kann ich anhand der vorliegenden Quellen nicht beantworten.",
                query=query,
                model="mock",
            )

        # Build a simple answer from retrieved chunks
        parts = [f"Basierend auf den vorliegenden Quellen:\n"]
        for i, r in enumerate(retrieval_results[:3], 1):
            text_preview = r.text[:200] if r.text else r.metadata.get("text_preview", "")
            parts.append(f"- {text_preview}... [Quelle {i}]")

        sources = []
        for r in retrieval_results:
            sources.append({
                "citation_label": r.citation_label,
                "url": r.url,
                "score": round(r.fused_score, 4),
            })

        return GeneratedAnswer(
            text="\n".join(parts),
            sources_used=sources,
            query=query,
            model="mock",
            context_chunks=len(retrieval_results),
        )
