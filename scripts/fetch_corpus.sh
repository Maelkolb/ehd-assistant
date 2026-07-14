#!/usr/bin/env bash
# Fetch the TEI-XML source corpus (telota/edition-humboldt-digital) at a pinned tag.
# Makes data/raw reproducible: the previous data/raw was a one-off local copy.
set -euo pipefail

CORPUS_TAG="${CORPUS_TAG:-v11.0.0}"
REPO_URL="https://github.com/telota/edition-humboldt-digital.git"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data/edh-source"

if [ -d "$DEST/.git" ]; then
    echo "Corpus already present at $DEST — verifying tag $CORPUS_TAG"
    git -C "$DEST" fetch --depth 1 origin "refs/tags/$CORPUS_TAG:refs/tags/$CORPUS_TAG" 2>/dev/null || true
    git -C "$DEST" checkout -q "tags/$CORPUS_TAG"
else
    echo "Cloning $REPO_URL @ $CORPUS_TAG into $DEST ..."
    git clone --depth 1 --branch "$CORPUS_TAG" "$REPO_URL" "$DEST"
fi

echo "Corpus ready: $(git -C "$DEST" describe --tags) ($(find "$DEST" -name '*.xml' | wc -l) XML files)"
