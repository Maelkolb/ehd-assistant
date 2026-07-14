# Test-Prompts für Alex (ehd-assistant)

Kurze Prompts, um jede Funktion zu testen. In der erwarteten Spur (🔎 Recherche-Schritte
aufklappen) sollte das genannte Werkzeug auftauchen. Modell: Haiku reicht, wenn nichts
anderes angegeben ist.

## Retrieval-Grundlagen

| Prompt | Erwartet |
|---|---|
| `Wer war Bonpland?` | 📇 Register (`lookup_entity`), Antwort mit Registereintrag als [Quelle N] |
| `Wo wird Carl Sigismund Kunth in den Tagebüchern erwähnt?` | 🔗 Annotierte Stellen (`chunks_for_entity`) |
| `Was schreibt Humboldt über Erdbeben in Cumaná?` | 🔍 Suche (`search`) |
| `Finde die exakte Formulierung "toises" in den Tagebüchern` | ⌕ Grep (`grep`) |
| `Was bedeutet "alii dicunt"?` | 📇 Register (Glossar) |
| `Was merken die Herausgeber zum Vulkan Soufrière an?` | Quelle vom Typ `editorial_note` ("Editorischer Kommentar") |

## Chronologie & Dokumente

| Prompt | Erwartet |
|---|---|
| `Wo war Humboldt im März 1801?` | 📅 Chronologie (`where_was_humboldt`), tagesgenaue Route |
| `Was geschah am 23. Juni 1802?` | 📅 Chronologie (Chimborazo-Besteigung) |
| `Fasse den Brief H0000003 zusammen.` | 📜 Dokument (`get_document`) |

## Karten & Zeitleisten (Artefakte unter der Antwort)

| Prompt | Erwartet |
|---|---|
| `Erstelle eine Karte der Stationen auf Humboldts Reise nach Italien 1805 (Hinreise Paris bis Rom).` | 🗺️ Karte — interaktive Leaflet-Karte, ~40 nummerierte Stationen mit Routenlinie *(Sonnet empfohlen)* |
| `Zeichne eine Karte der wichtigsten Orte der Amerikareise 1799–1804.` | 🗺️ Karte |
| `Erstelle eine Zeitleiste der Vesuvbesteigungen Humboldts 1805.` | 📈 Zeitleiste — visuelle Zeitleiste mit Datums-Punkten |

## Messwerte & Berechnungen

| Prompt | Erwartet |
|---|---|
| `Welche Höhenmessungen in Toisen finden sich zum Chimborazo?` | 📏 Messwerte (`query_measurements`) mit metrischen Umrechnungen |
| `Nutze code_execution: rechne die Toisen-Werte 50, 60, 1600 und 3016 in Meter um und berechne Mittelwert und Standardabweichung.` | 🐍 Code (`code_execution`) — **nur Sonnet/Opus**; ausgeführter Python-Code in der Spur sichtbar |

## UI-Funktionen

- **Modell-Dropdown** (links neben dem Eingabefeld): gleiche Frage mit Haiku und Sonnet stellen — Modellname erscheint in der Spur-Zusammenfassung (`… · claude-…`).
- **Sprachumschalter** (oben rechts, "English"/"Deutsch"): UI-Texte wechseln; englische Frage stellen (`Who was Bonpland?`) → englische Antwort.
- **Quellen-Links** unter der Antwort anklicken → korrekte Detailseite auf edition-humboldt.de (Tagebuch-Links mit Folio-Sprung).
- **Recherche-Schritte** aufklappen: Werkzeuge erscheinen live während der Recherche (blinkend), danach eingeklappt mit Dauer und Modell.

## Erwartetes Fehlverhalten (negativ testen)

| Prompt | Erwartet |
|---|---|
| `Was ist Humboldts Lieblingspizza?` | Ehrliches "kann ich anhand der Quellen nicht beantworten" — keine erfundenen Quellen |
| `Wo war Humboldt am 1.1.2020?` | Kein Treffer in der Chronologie, ehrliche Antwort |
