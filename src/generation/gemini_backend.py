"""Advanced RAG: query expansion + reranking + generation."""
from __future__ import annotations
import os, re, logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Du bist ein wissenschaftlicher Assistent für die edition humboldt digital,
eine digitale Edition der Reisetagebücher, Briefe, Dokumente und Chronologie
Alexander von Humboldts.

Regeln:
1. Antworte auf Grundlage der bereitgestellten Quellen und deines Allgemeinwissens. Erfinde nichts.
2. Zitiere JEDE Aussage mit [Quelle N] direkt nach dem Satz.
3. Wenn keine Quelle eine Antwort enthält, sage ehrlich dass die Quellen dazu keine Information enthalten.
4. Antworte auf Deutsch, es sei denn die Frage ist auf Englisch oder Französisch.
5. Verwende konkrete Details AUS dem Text der Quellen — Namen, Daten, Orte, Messungen.
6. Gib eine ausführliche Antwort (5-15 Sätze) mit Details aus den Quellen.
7. Ignoriere Quellen die nichts zur Frage beitragen.
8. Paraphrasiere den Inhalt der Quellen und gib konkrete Informationen wieder.
9. Nutze Stichpunkte für die wichtigsten Infos."""

@dataclass
class GeminiGenerator:
    model: str = "gemini-2.5-flash"
    project_id: str | None = None
    location: str = "europe-west1"
    use_vertex: bool = True
    max_tokens: int = 3000
    temperature: float = 1

    def __post_init__(self):
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig
        project = self.project_id or os.environ.get("GOOGLE_CLOUD_PROJECT")
        vertexai.init(project=project, location=self.location)
        self._model = GenerativeModel(self.model, system_instruction=SYSTEM_PROMPT)
        self._utility = GenerativeModel(self.model)
        self._gc = GenerationConfig
        logger.info(f"Gemini via Vertex AI: {self.model} in {self.location}")

    def expand_query(self, query: str) -> list[str]:
        prompt = f"""Du hilfst bei der Suche in Alexander von Humboldts digitalem Archiv
(Tagebücher, Briefe, Dokumente, Chronologie, Personenregister, Ortsregister, Glossar).

Generiere 3 verschiedene Suchanfragen auf Deutsch (3-6 Wörter),
die verschiedene Aspekte und Dokumenttypen abdecken.

Antworte NUR mit den 3 Suchanfragen, eine pro Zeile, ohne Nummerierung.

Frage: {query}"""
        try:
            resp = self._utility.generate_content(prompt,
                generation_config=self._gc(max_output_tokens=200, temperature=0.4))
            lines = [l.strip().lstrip("0123456789.-) ") for l in resp.text.strip().split("\n")
                     if l.strip() and len(l.strip()) > 3]
            queries = [query] + lines[:3]
            logger.info(f"Expanded: {query!r} -> {queries}")
            return queries
        except Exception as e:
            logger.warning(f"Expansion failed: {e}")
            return [query]

    def rerank(self, query: str, chunks: list[dict], top_n: int = 6) -> list[dict]:
        if len(chunks) <= top_n:
            return chunks
        summaries = []
        for i, c in enumerate(chunks):
            preview = c.get("text", "")[:500]
            label = c.get("citation_label", f"Dok {i+1}")
            dtype = c.get("doc_type", "")
            summaries.append(f"[{i}] ({dtype}) {label}:\n{preview}")

        prompt = f"""Welche Dokumente enthalten konkrete Informationen zur Beantwortung der Frage?
Bewerte den INHALT, nicht nur Schlüsselwörter.

Frage: {query}

Dokumente:
{chr(10).join(summaries)}

Antworte NUR mit den Nummern der relevanten Dokumente, komma-getrennt (z.B. 0, 2, 5)."""
        try:
            resp = self._utility.generate_content(prompt,
                generation_config=self._gc(max_output_tokens=100, temperature=0.0))
            seen = set()
            indices = []
            for num in re.findall(r"\d+", resp.text):
                idx = int(num)
                if 0 <= idx < len(chunks) and idx not in seen:
                    indices.append(idx)
                    seen.add(idx)
            if indices:
                reranked = [chunks[i] for i in indices[:top_n]]
                logger.info(f"Reranked: {len(chunks)} -> {len(reranked)}")
                return reranked
            return chunks[:top_n]
        except Exception as e:
            logger.warning(f"Rerank failed: {e}")
            return chunks[:top_n]

    def generate(self, query: str, retrieval_results=None, context_chunks=None, **kw) -> dict:
        chunks = retrieval_results or context_chunks or []
        chunk_dicts = []
        for c in chunks:
            if isinstance(c, dict):
                chunk_dicts.append(c)
            else:
                chunk_dicts.append({
                    "text": getattr(c, "text_regularized", "") or getattr(c, "text", ""),
                    "citation_label": getattr(c, "citation_label", ""),
                    "url": getattr(c, "url", ""),
                    "doc_type": getattr(c, "doc_type", "")
                        or (c.metadata.get("doc_type", "") if hasattr(c, "metadata") else ""),
                    "date_iso": getattr(c, "date_iso", "")
                        or (c.metadata.get("date_iso", "") if hasattr(c, "metadata") else ""),
                    "persons": ", ".join(c.metadata.get("persons", []) if hasattr(c, "metadata") else []),
                    "places": ", ".join(c.metadata.get("places", []) if hasattr(c, "metadata") else []),
                })

        chunk_dicts = self.rerank(query, chunk_dicts, top_n=6)
        ctx = self._build_context(chunk_dicts)

        prompt = f"""Kontext aus der edition humboldt digital:

{ctx}

===
Frage: {query}

Beantworte die Frage ausführlich. Zitiere jede Aussage mit [Quelle N].
Verwende konkrete Details, Namen, Daten aus den Quellentexten.
Ignoriere irrelevante Quellen."""

        try:
            resp = self._model.generate_content(prompt,
                generation_config=self._gc(max_output_tokens=self.max_tokens, temperature=self.temperature))
            answer = resp.text
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            return {"answer": f"Fehler: {e}", "citations": [], "sources": []}

        return {
            "answer": answer,
            "citations": self._extract_citations(answer, chunk_dicts),
            "sources": [{"index": i+1, "citation_label": c.get("citation_label",""),
                         "url": c.get("url",""), "doc_type": c.get("doc_type","")}
                        for i, c in enumerate(chunk_dicts)],
        }

    def _build_context(self, chunks):
        parts = []
        for i, c in enumerate(chunks):
            label = c.get("citation_label", "") or f"Dokument {i+1}"
            h = f"[Quelle {i+1}] {label}"
            if c.get("doc_type"): h += f" ({c['doc_type']})"
            if c.get("date_iso"): h += f" — {c['date_iso']}"
            meta = []
            if c.get("url"): meta.append(f"URL: {c['url']}")
            if c.get("persons"): meta.append(f"Personen: {c['persons']}")
            if c.get("places"): meta.append(f"Orte: {c['places']}")
            if meta: h += "\n" + " | ".join(meta)
            h += f"\n\n{c.get('text', '')}"
            parts.append(h)
        return ("\n\n" + "="*60 + "\n\n").join(parts)

    def _extract_citations(self, answer, chunks):
        cited = set()
        for m in re.finditer(r"\[Quelle\s+(\d+)\]", answer):
            idx = int(m.group(1))
            if 1 <= idx <= len(chunks): cited.add(idx)
        return [{"index": i, "citation_label": chunks[i-1].get("citation_label",""),
                 "url": chunks[i-1].get("url","")} for i in sorted(cited)]
