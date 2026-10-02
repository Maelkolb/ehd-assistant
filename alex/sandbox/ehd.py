"""The `ehd` module available to code running in Alex's Python sandbox.

import ehd
letters = ehd.sql("SELECT date_from, recipient FROM letters")
ehd.show(figure, title="Briefe pro Jahr")
"""

from __future__ import annotations

import base64
import io
import json
import math
import sqlite3
from pathlib import Path

import pandas as pd

MAX_TABLE_ROWS = 500
MAX_FIGURE_BYTES = 2_000_000
MAX_ARTIFACTS = 8

_connection: sqlite3.Connection | None = None
_store_path: str = ""
_corpus_dir: str = ""
_artifacts: list[dict] = []
palette: dict = json.loads(Path(__file__).with_name("palette.json").read_text(encoding="utf-8"))
colors: list[str] = list(palette["categorical"])


def _configure(store_path: str, corpus_dir: str) -> None:
    global _connection, _store_path, _corpus_dir
    _store_path, _corpus_dir = store_path, corpus_dir
    _connection = sqlite3.connect(f"file:{Path(store_path).as_posix()}?mode=ro", uri=True)
    _apply_chart_style()


def _apply_chart_style() -> None:
    import matplotlib
    from cycler import cycler

    matplotlib.rcParams.update(
        {
            "figure.figsize": (8, 4.6),
            "figure.dpi": 100,
            "savefig.dpi": 160,
            "figure.facecolor": palette["background"],
            "axes.facecolor": palette["background"],
            "axes.edgecolor": palette["grid"],
            "axes.labelcolor": palette["muted"],
            "axes.titlecolor": palette["ink"],
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.titlepad": 12,
            "axes.labelsize": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": palette["grid"],
            "grid.linewidth": 0.8,
            "axes.axisbelow": True,
            "axes.prop_cycle": cycler(color=colors),
            "xtick.color": palette["muted"],
            "ytick.color": palette["muted"],
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "text.color": palette["ink"],
            "font.family": "sans-serif",
            "font.sans-serif": [palette["font_sans"], "DejaVu Sans", "Arial"],
            "legend.frameon": False,
            "legend.fontsize": 9,
            "lines.linewidth": 2,
        }
    )
    try:
        import plotly.graph_objects as go
        import plotly.io as pio

        pio.templates["ehd"] = go.layout.Template(
            layout={
                "colorway": colors,
                "font": {
                    "family": f"{palette['font_sans']}, sans-serif",
                    "color": palette["ink"],
                    "size": 13,
                },
                "paper_bgcolor": "rgba(0,0,0,0)",
                "plot_bgcolor": "rgba(0,0,0,0)",
                "xaxis": {
                    "gridcolor": palette["grid"],
                    "linecolor": palette["grid"],
                    "zeroline": False,
                },
                "yaxis": {
                    "gridcolor": palette["grid"],
                    "linecolor": palette["grid"],
                    "zeroline": False,
                },
                "margin": {"l": 56, "r": 24, "t": 48, "b": 48},
                "title": {"x": 0, "xanchor": "left", "font": {"size": 16}},
            }
        )
        pio.templates.default = "ehd"
    except ImportError:
        pass


def sql(query: str, parameters: tuple | list | dict | None = None) -> pd.DataFrame:
    """Run a read-only SQL query on the corpus database and return a DataFrame."""
    return pd.read_sql_query(query, _connection, params=parameters)


def text(doc_id: str) -> str:
    """The complete reading text of one document."""
    row = _connection.execute("SELECT text FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown doc_id {doc_id!r}")
    return row[0]


def tei(doc_id: str) -> str:
    """The original TEI-XML source of one document, as published by the edition."""
    row = _connection.execute(
        "SELECT source_file FROM documents WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown doc_id {doc_id!r}")
    path = Path(_corpus_dir) / "data" / row[0]
    if not _corpus_dir or not path.exists():
        raise FileNotFoundError("The TEI dump is not available in this deployment.")
    return path.read_text(encoding="utf-8")


def search(query: str, limit: int = 20, **filters) -> pd.DataFrame:
    """Ranked full-text search; one row per passage (doc_id, title, page, start, end, text)."""
    from alex.corpus.store import DocumentStore

    store = DocumentStore(_store_path)
    passages = store.search(query, limit=limit, **filters)
    return pd.DataFrame(
        [
            {
                "doc_id": passage.doc_id,
                "doc_type": passage.doc_type,
                "title": passage.title,
                "kind": passage.kind,
                "page": passage.page,
                "date_from": passage.date_from,
                "start": passage.start,
                "end": passage.end,
                "score": round(passage.score, 2),
                "text": passage.text,
                "url": passage.url,
            }
            for passage in passages
        ]
    )


def _json_safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def _add(artifact: dict) -> None:
    if len(_artifacts) >= MAX_ARTIFACTS:
        raise RuntimeError(f"At most {MAX_ARTIFACTS} outputs per run.")
    _artifacts.append(artifact)


def show(thing, title: str = "") -> None:
    """Display a matplotlib figure, a plotly figure, a DataFrame or a Series to the user."""
    module = type(thing).__module__
    if module.startswith("matplotlib"):
        figure = thing if hasattr(thing, "savefig") else thing.get_figure()
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", bbox_inches="tight")
        import matplotlib.pyplot as plt

        plt.close(figure)
        if buffer.tell() > MAX_FIGURE_BYTES:
            raise ValueError("Figure too large; reduce its size or dpi.")
        _add(
            {
                "type": "image",
                "title": title,
                "media_type": "image/png",
                "data": base64.b64encode(buffer.getvalue()).decode(),
            }
        )
    elif module.startswith("plotly"):
        payload = thing.to_json()
        if len(payload) > MAX_FIGURE_BYTES:
            raise ValueError("Figure too large; plot fewer points or aggregate first.")
        figure = json.loads(payload)
        chart_title = title or (figure.get("layout", {}).get("title") or {}).get("text", "")
        _add({"type": "chart", "title": chart_title, "figure": figure})
    elif isinstance(thing, (pd.DataFrame, pd.Series)):
        frame = thing.reset_index() if isinstance(thing, pd.Series) or thing.index.name else thing
        rows = [
            [_json_safe(value) for value in row]
            for row in frame.head(MAX_TABLE_ROWS).itertuples(index=False)
        ]
        _add(
            {
                "type": "table",
                "title": title,
                "columns": [str(column) for column in frame.columns],
                "rows": rows,
                "truncated": len(frame) > MAX_TABLE_ROWS,
            }
        )
    else:
        raise TypeError(
            "show() takes a matplotlib figure, a plotly figure, a DataFrame or a Series."
        )


def _records(rows) -> list[dict]:
    if isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")
    return [
        {
            key: _json_safe(value)
            for key, value in dict(row).items()
            if _json_safe(value) is not None
        }
        for row in rows
    ]


def show_map(stations, title: str = "", connect_route: bool = False) -> None:
    """Interactive map. stations: DataFrame or list of dicts with lat, lon, label and
    optionally date, description, url, group."""
    cleaned = [
        station
        for station in _records(stations)
        if isinstance(station.get("lat"), (int, float))
        and isinstance(station.get("lon"), (int, float))
    ]
    if not cleaned:
        raise ValueError("show_map() needs rows with numeric lat and lon.")
    _add(
        {
            "type": "map",
            "title": title,
            "connect_route": bool(connect_route),
            "stations": cleaned[:400],
        }
    )


def show_timeline(events, title: str = "") -> None:
    """Timeline. events: DataFrame or list of dicts with date, label and optionally
    description, url."""
    cleaned = [event for event in _records(events) if event.get("date") and event.get("label")]
    if not cleaned:
        raise ValueError("show_timeline() needs rows with date and label.")
    _add({"type": "timeline", "title": title, "events": cleaned[:300]})


def show_network(nodes, edges, title: str = "") -> None:
    """Network graph. nodes: id, label and optionally kind, weight, url, description, focus;
    edges: source, target and optionally weight, label."""
    node_list, edge_list = _records(nodes), _records(edges)
    known = {str(node.get("id")) for node in node_list}
    edge_list = [
        edge
        for edge in edge_list
        if str(edge.get("source")) in known and str(edge.get("target")) in known
    ]
    if len(node_list) < 2:
        raise ValueError("show_network() needs at least two nodes.")
    _add({"type": "network", "title": title, "nodes": node_list[:150], "edges": edge_list[:600]})
