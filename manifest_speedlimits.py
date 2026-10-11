#!/usr/bin/env python3
"""Writes index.json from the assets of the speedlimits release: {area id: {bytes, built}}."""
import json
import subprocess

assets = json.loads(subprocess.check_output(["gh", "release", "view", "speedlimits", "--json", "assets"]))["assets"]
index = {a["name"][:-len(".sqlite.gz")]: {"bytes": a["size"], "built": a["updatedAt"][:10]}
         for a in assets if a["name"].endswith(".sqlite.gz")}
json.dump({"areas": index}, open("index.json", "w"), indent=1, sort_keys=True)
print(f"{len(index)} countries")
