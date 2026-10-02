import os
from pathlib import Path

import pytest

BLOCKED = {
    "network": "import urllib.request\nurllib.request.urlopen('https://example.com')",
    "socket": "import socket\nsocket.create_connection(('example.com', 80))",
    "subprocess": "import subprocess\nsubprocess.run(['whoami'])",
    "shell": "import os\nos.system('whoami')",
    "native library": "import ctypes\nctypes.CDLL('libc.so.6')",
    "other database": "import sqlite3\nsqlite3.connect('other.db')",
    "write outside": f"open({str(Path.home() / 'alex-escape.txt')!r}, 'w').write('x')",
    "read outside": f"print(open({str(Path(__file__).resolve())!r}).read())",
    "list outside": f"import os\nprint(os.listdir({str(Path.home())!r}))",
}


@pytest.mark.parametrize("name", BLOCKED)
def test_sandbox_blocks(sandbox, name):
    result = sandbox.run(BLOCKED[name])
    assert not result.ok, f"{name} was allowed"
    assert "Blocked" in result.error or "PermissionError" in result.error


def test_sandbox_environment_is_empty_and_database_read_only(sandbox, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    result = sandbox.run("import os\nprint('ANTHROPIC_API_KEY' in os.environ)")
    assert result.output.strip() == "False"
    result = sandbox.run("ehd._connection.execute('DELETE FROM documents')")
    assert not result.ok and "readonly" in result.error
    assert not (Path.home() / "alex-escape.txt").exists()


def test_sandbox_can_work_inside_its_directory(sandbox):
    result = sandbox.run(
        "open('notes.txt', 'w').write('ok')\nprint(open('notes.txt').read())\n"
        "print(len(ehd.text('H0000100')) > 100, ehd.tei('H0000100')[:4])"
    )
    assert result.ok, result.error
    assert result.output.split() == ["ok", "True", "<TEI"]


def test_sandbox_times_out(store_path, corpus):
    from alex.sandbox.runner import Sandbox

    result = Sandbox("subprocess", store_path, corpus, timeout_seconds=12).run("while True: pass")
    assert not result.ok and "Timed out" in result.error


def test_sandbox_shows_maps_and_leftover_figures(sandbox):
    result = sandbox.run(
        "places = ehd.sql('SELECT name AS label, lat, lon FROM entities WHERE lat IS NOT NULL')\n"
        "ehd.show_map(places, title='Orte')\n"
        "plt.plot([1, 2, 3])"
    )
    assert result.ok, result.error
    assert [artifact["type"] for artifact in result.artifacts] == ["map", "image"]
    assert len(result.artifacts[0]["stations"]) == 2


def test_sandbox_can_be_switched_off(store_path):
    from alex.sandbox.runner import Sandbox

    assert not Sandbox("off", store_path).enabled
    assert not Sandbox("http", store_path, url="").enabled
    assert os.environ.get("PATH")
