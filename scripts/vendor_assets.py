"""Download the pinned third-party assets of the web client.

The client makes no third-party requests at runtime (except map tiles), so
Leaflet, Plotly and the PT fonts are served from alex/web/assets. This script
reproduces those files from their pinned npm releases and checks every file
against the sha256 recorded in vendor.lock.json.

    python scripts/vendor_assets.py                 # download + verify
    python scripts/vendor_assets.py --update-lock   # after bumping a version
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "alex" / "web" / "assets"
LOCK_FILE = ASSETS / "vendor" / "vendor.lock.json"
README_FILE = ASSETS / "vendor" / "README.md"
CDN = "https://cdn.jsdelivr.net/npm"

FONT_SUBSETS = ("latin", "latin-ext", "cyrillic")
FONT_STYLES = ("400-normal", "400-italic", "700-normal", "700-italic")

PACKAGES = [
    {
        "name": "Leaflet",
        "package": "leaflet",
        "version": "1.9.4",
        "licence": "BSD-2-Clause",
        "homepage": "https://leafletjs.com",
        "files": {
            "dist/leaflet.js": "vendor/leaflet/leaflet.js",
            "dist/leaflet.css": "vendor/leaflet/leaflet.css",
            "dist/images/layers.png": "vendor/leaflet/images/layers.png",
            "dist/images/layers-2x.png": "vendor/leaflet/images/layers-2x.png",
            "dist/images/marker-icon.png": "vendor/leaflet/images/marker-icon.png",
            "dist/images/marker-icon-2x.png": "vendor/leaflet/images/marker-icon-2x.png",
            "dist/images/marker-shadow.png": "vendor/leaflet/images/marker-shadow.png",
            "LICENSE": "vendor/leaflet/LICENSE",
        },
    },
    {
        "name": "Plotly.js (cartesian bundle)",
        "package": "plotly.js-cartesian-dist-min",
        "version": "4.1.1",
        "licence": "MIT",
        "homepage": "https://plotly.com/javascript/",
        "files": {
            "plotly-cartesian.min.js": "vendor/plotly/plotly-cartesian.min.js",
            "LICENSE": "vendor/plotly/LICENSE",
        },
    },
    {
        "name": "PT Serif",
        "package": "@fontsource/pt-serif",
        "version": "5.3.0",
        "licence": "OFL-1.1",
        "homepage": "https://fontsource.org/fonts/pt-serif",
        "files": {
            **{
                f"files/pt-serif-{subset}-{style}.woff2": f"fonts/pt-serif-{subset}-{style}.woff2"
                for subset in FONT_SUBSETS
                for style in FONT_STYLES
            },
            "LICENSE": "fonts/LICENSE-PT-Serif.txt",
        },
    },
    {
        "name": "PT Sans",
        "package": "@fontsource/pt-sans",
        "version": "5.3.0",
        "licence": "OFL-1.1",
        "homepage": "https://fontsource.org/fonts/pt-sans",
        "files": {
            **{
                f"files/pt-sans-{subset}-{style}.woff2": f"fonts/pt-sans-{subset}-{style}.woff2"
                for subset in FONT_SUBSETS
                for style in FONT_STYLES
            },
            "LICENSE": "fonts/LICENSE-PT-Sans.txt",
        },
    },
]


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ehd-assistant-vendor/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def write_readme(lock: dict[str, str]) -> None:
    lines = [
        "# Third-party assets",
        "",
        "Served from this deployment so the client needs no CDN. Reproduce with",
        "`python scripts/vendor_assets.py`; hashes are pinned in `vendor.lock.json`.",
        "",
        "| Asset | Version | Licence | Source | Files |",
        "|---|---|---|---|---|",
    ]
    for package in PACKAGES:
        source = f"{CDN}/{package['package']}@{package['version']}/"
        lines.append(
            f"| [{package['name']}]({package['homepage']}) | {package['version']} "
            f"| {package['licence']} | {source} | {len(package['files'])} |"
        )
    lines += ["", "## sha256", "", "```"]
    lines += [f"{digest}  {path}" for path, digest in sorted(lock.items())]
    lines += ["```", ""]
    README_FILE.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-lock",
        action="store_true",
        help="record the hashes of what is downloaded instead of verifying",
    )
    arguments = parser.parse_args()

    lock: dict[str, str] = {}
    if LOCK_FILE.exists() and not arguments.update_lock:
        lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    recorded: dict[str, str] = {}
    failures = []
    for package in PACKAGES:
        base = f"{CDN}/{package['package']}@{package['version']}"
        for source, destination in package["files"].items():
            target = ASSETS / destination
            expected = lock.get(destination)
            if (
                target.exists()
                and expected
                and hashlib.sha256(target.read_bytes()).hexdigest() == expected
            ):
                recorded[destination] = expected
                continue
            content = fetch(f"{base}/{source}")
            digest = hashlib.sha256(content).hexdigest()
            if expected and digest != expected:
                failures.append(f"{destination}: sha256 {digest} != locked {expected}")
                continue
            if not expected and not arguments.update_lock:
                failures.append(f"{destination}: not in vendor.lock.json (run --update-lock)")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            recorded[destination] = digest
            print(f"fetched {destination} ({len(content):,} bytes)")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_readme(recorded)
    print(f"{len(recorded)} assets present and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
