"""
Knowledge Graph builder for Neo4j.

Creates a graph of Persons, Places, Entries, Documents, and their relationships
from parsed Humboldt chunks. Enables structured queries like:
- "Wen hat Humboldt in Wien getroffen?"
- "Welche Orte besuchte er im Juli 1797?"
- "In welchen Einträgen wird der Königstein erwähnt?"

Usage:
    builder = GraphBuilder("bolt://localhost:7687", "neo4j", "humboldt2024")
    builder.build_from_chunks(chunks)
    results = builder.query_persons_at_place("Wien")
"""
from __future__ import annotations

import logging
from src.models import HumboldtChunk

logger = logging.getLogger(__name__)


# Cypher queries for schema creation
SCHEMA_QUERIES = [
    # Constraints (unique IDs)
    "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (pl:Place) REQUIRE pl.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entry) REQUIRE e.chunk_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    # Indexes for fast lookup
    "CREATE INDEX IF NOT EXISTS FOR (p:Person) ON (p.name)",
    "CREATE INDEX IF NOT EXISTS FOR (pl:Place) ON (pl.name)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entry) ON (e.date_iso)",
    "CREATE INDEX IF NOT EXISTS FOR (p:Person) ON (p.gnd_id)",
    "CREATE INDEX IF NOT EXISTS FOR (pl:Place) ON (pl.geonames_id)",
]


class GraphBuilder:
    """Builds and queries a Neo4j knowledge graph from Humboldt chunks."""

    def __init__(self, uri: str, user: str, password: str):
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        logger.info(f"Connected to Neo4j at {uri}")

    def close(self):
        self.driver.close()

    def setup_schema(self):
        """Create constraints and indexes."""
        with self.driver.session() as session:
            for query in SCHEMA_QUERIES:
                try:
                    session.run(query)
                except Exception as e:
                    logger.warning(f"Schema query failed: {e}")
        logger.info("Graph schema created")

    def build_from_chunks(self, chunks: list[HumboldtChunk]) -> dict:
        """
        Populate the graph from parsed chunks.
        Returns stats about created nodes and relationships.
        """
        self.setup_schema()

        stats = {"documents": 0, "entries": 0, "persons": 0, "places": 0, "relationships": 0}

        with self.driver.session() as session:
            # Track unique entities across all chunks
            seen_docs = set()
            seen_persons = set()
            seen_places = set()

            for chunk in chunks:
                # --- Document node ---
                if chunk.doc_id not in seen_docs:
                    session.run("""
                        MERGE (d:Document {doc_id: $doc_id})
                        SET d.title = $title,
                            d.doc_type = $doc_type,
                            d.url = $url,
                            d.source_desc = $source_desc
                    """, {
                        "doc_id": chunk.doc_id,
                        "title": chunk.doc_title,
                        "doc_type": chunk.doc_type,
                        "url": chunk.url.rsplit("/", 1)[0] if "/" in chunk.url else chunk.url,
                        "source_desc": chunk.source_desc,
                    })
                    seen_docs.add(chunk.doc_id)
                    stats["documents"] += 1

                # --- Entry node ---
                session.run("""
                    MERGE (e:Entry {chunk_id: $chunk_id})
                    SET e.doc_id = $doc_id,
                        e.folio = $folio,
                        e.entry_number = $entry_number,
                        e.date_iso = $date_iso,
                        e.date_display = $date_display,
                        e.url = $url,
                        e.text_preview = $text_preview
                """, {
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "folio": chunk.folio_start,
                    "entry_number": chunk.entry_number,
                    "date_iso": chunk.date_iso,
                    "date_display": chunk.date_display,
                    "url": chunk.url,
                    "text_preview": chunk.text_regularized[:300],
                })
                stats["entries"] += 1

                # Entry → Document
                session.run("""
                    MATCH (e:Entry {chunk_id: $chunk_id})
                    MATCH (d:Document {doc_id: $doc_id})
                    MERGE (e)-[:PART_OF]->(d)
                """, {"chunk_id": chunk.chunk_id, "doc_id": chunk.doc_id})
                stats["relationships"] += 1

                # --- Person nodes ---
                for person in chunk.persons:
                    person_id = person.gnd_id or person.humboldt_id or person.name
                    if person_id not in seen_persons:
                        session.run("""
                            MERGE (p:Person {id: $id})
                            SET p.name = $name,
                                p.gnd_id = $gnd_id,
                                p.humboldt_id = $humboldt_id
                        """, {
                            "id": person_id,
                            "name": person.name,
                            "gnd_id": person.gnd_id,
                            "humboldt_id": person.humboldt_id,
                        })
                        seen_persons.add(person_id)
                        stats["persons"] += 1

                    # Person → Entry (MENTIONED_IN)
                    session.run("""
                        MATCH (p:Person {id: $person_id})
                        MATCH (e:Entry {chunk_id: $chunk_id})
                        MERGE (p)-[:MENTIONED_IN {date: $date}]->(e)
                    """, {
                        "person_id": person_id,
                        "chunk_id": chunk.chunk_id,
                        "date": chunk.date_iso,
                    })
                    stats["relationships"] += 1

                # --- Place nodes ---
                for place in chunk.places:
                    place_id = place.geonames_id or place.humboldt_id or place.name
                    if place_id not in seen_places:
                        session.run("""
                            MERGE (pl:Place {id: $id})
                            SET pl.name = $name,
                                pl.geonames_id = $geonames_id,
                                pl.humboldt_id = $humboldt_id
                        """, {
                            "id": place_id,
                            "name": place.name,
                            "geonames_id": place.geonames_id,
                            "humboldt_id": place.humboldt_id,
                        })
                        seen_places.add(place_id)
                        stats["places"] += 1

                    # Place → Entry (MENTIONED_IN)
                    session.run("""
                        MATCH (pl:Place {id: $place_id})
                        MATCH (e:Entry {chunk_id: $chunk_id})
                        MERGE (pl)-[:MENTIONED_IN {date: $date}]->(e)
                    """, {
                        "place_id": place_id,
                        "chunk_id": chunk.chunk_id,
                        "date": chunk.date_iso,
                    })
                    stats["relationships"] += 1

                # --- Co-occurrence: Persons at Places ---
                # If a person and a place are mentioned in the same entry,
                # create an implicit relationship
                for person in chunk.persons:
                    for place in chunk.places:
                        person_id = person.gnd_id or person.humboldt_id or person.name
                        place_id = place.geonames_id or place.humboldt_id or place.name
                        session.run("""
                            MATCH (p:Person {id: $person_id})
                            MATCH (pl:Place {id: $place_id})
                            MERGE (p)-[:CO_OCCURS_WITH {entry: $chunk_id, date: $date}]->(pl)
                        """, {
                            "person_id": person_id,
                            "place_id": place_id,
                            "chunk_id": chunk.chunk_id,
                            "date": chunk.date_iso,
                        })

        logger.info(f"Graph built: {stats}")
        return stats

    # =========================================================================
    # Query methods (used by the retrieval pipeline)
    # =========================================================================

    def query_persons_at_place(self, place_name: str) -> list[dict]:
        """Find persons mentioned in entries that also mention a given place."""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (p:Person)-[:MENTIONED_IN]->(e:Entry)<-[:MENTIONED_IN]-(pl:Place)
                WHERE pl.name CONTAINS $place_name
                RETURN DISTINCT p.name AS person, p.gnd_id AS gnd,
                       e.date_iso AS date, e.folio AS folio, e.url AS url
                ORDER BY e.date_iso
            """, {"place_name": place_name})
            return [dict(record) for record in result]

    def query_places_by_date_range(self, start: str, end: str) -> list[dict]:
        """Find places mentioned in entries within a date range."""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (pl:Place)-[:MENTIONED_IN]->(e:Entry)
                WHERE e.date_iso >= $start AND e.date_iso <= $end
                RETURN DISTINCT pl.name AS place, pl.geonames_id AS geonames,
                       e.date_iso AS date, e.folio AS folio
                ORDER BY e.date_iso
            """, {"start": start, "end": end})
            return [dict(record) for record in result]

    def query_entries_by_person(self, person_name: str) -> list[dict]:
        """Find all entries mentioning a person."""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (p:Person)-[:MENTIONED_IN]->(e:Entry)
                WHERE p.name CONTAINS $person_name
                RETURN e.entry_number AS entry, e.date_iso AS date,
                       e.folio AS folio, e.url AS url, e.text_preview AS text
                ORDER BY e.date_iso
            """, {"person_name": person_name})
            return [dict(record) for record in result]

    def query_person_place_network(self, person_name: str) -> list[dict]:
        """Find all places associated with a person (via co-occurrence)."""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (p:Person)-[r:CO_OCCURS_WITH]->(pl:Place)
                WHERE p.name CONTAINS $person_name
                RETURN DISTINCT pl.name AS place, r.date AS date, count(*) AS mentions
                ORDER BY mentions DESC
            """, {"person_name": person_name})
            return [dict(record) for record in result]
