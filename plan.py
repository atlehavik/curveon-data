#!/usr/bin/env python3
"""Splits the areas to build into groups of roughly equal download size, one runner job each."""
import json
import os
import sys
import urllib.request

GROUP_BYTES = 1_500_000_000
MAX_GROUPS = 200

areas = json.load(open("areas.json"))
wanted = sys.argv[1] if len(sys.argv) > 1 else "all"
ids = sorted(areas) if wanted.strip() in ("", "all") else [a.strip() for a in wanted.split(",") if a.strip() in areas]


def size(url):
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get("Content-Length", 0))
    except Exception:
        return 0


sizes = {i: sum(size(u) for u in areas[i]) for i in ids}
groups, current, total = [], [], 0
for i in sorted(ids, key=lambda i: -sizes[i]):
    if current and total + sizes[i] > GROUP_BYTES:
        groups.append(current)
        current, total = [], 0
    current.append(i)
    total += sizes[i]
if current:
    groups.append(current)
while len(groups) > MAX_GROUPS:  # merge the smallest ones
    groups.sort(key=lambda g: sum(sizes[i] for i in g))
    groups[1].extend(groups.pop(0))
out = json.dumps([",".join(g) for g in groups])
print(f"{len(ids)} areas in {len(groups)} groups", file=sys.stderr)
with open(os.environ.get("GITHUB_OUTPUT", "/dev/stdout"), "a") as f:
    f.write(f"groups={out}\n")
