#!/usr/bin/env bash
# Builds and uploads the search index of each area in $1 (comma-separated ids). One failing area
# doesn't stop the others; the job fails at the end if any did.
set -uo pipefail
mkdir -p work
failed=""
for id in ${1//,/ }; do
  echo "::group::$id"
  (
    set -e
    urls=$(python3 -c "import json; print(' '.join(json.load(open('areas.json'))['$id']))")
    files=()
    for u in $urls; do
      f="work/$(basename "$u")"
      [ -f "$f" ] || curl -sSfL --retry 5 -o "$f" "$u"
      files+=("$f")
    done
    python3 build_search_index.py "$(IFS=,; echo "${files[*]}")" "work/$id.sqlite"
    gzip -6 -c "work/$id.sqlite" > "work/$id.sqlite.gz"
    if [ -n "${NO_UPLOAD:-}" ]; then ls -la "work/$id.sqlite.gz"; else gh release upload search "work/$id.sqlite.gz" --clobber; fi
  ) || { echo "::error::$id failed"; failed="$failed $id"; }
  [ -n "${NO_UPLOAD:-}" ] || rm -f work/*
  echo "::endgroup::"
done
[ -z "$failed" ] || { echo "Failed:$failed"; exit 1; }
