"""Build web/index.html from a fresh run of the demo.

    python web/build.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from mandate_firewall.demo import run  # noqa: E402

data = run()
assert data["verify"]["ok"] and data["replays_match"], "Demo run failed its own checks"
assert all(s["decision"]["outcome"] == s["expected"] for s in data["steps"]), "Unexpected outcome"

payload = json.dumps(data).replace("</", "<\\/")
html = (ROOT / "template.html").read_text()
html = html.replace("__SHA256__", (ROOT / "sha256.js").read_text()).replace("__DATA__", payload)
(ROOT / "index.html").write_text(html)
docs = ROOT.parent / "docs"
docs.mkdir(exist_ok=True)
(docs / "index.html").write_text(html)  # served by GitHub Pages
print(f"Wrote web/index.html and docs/index.html ({len(html):,} bytes)")
