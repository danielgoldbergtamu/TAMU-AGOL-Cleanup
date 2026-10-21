"""Re-applies full_template.html to the data already inside the two built dashboards, for template-only
changes (layout, wording, keys). Seconds instead of a full rebuild; the numbers are unchanged.

    python dashboard/rerender.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import agol_db as db  # noqa: E402
template = open(os.path.join(HERE, "full_template.html"), encoding="utf-8").read()
for path in (os.path.join(db.output_dir(), "AGOL_Cleanup_Dashboard_FULL.html"), os.path.join(db.output_dir(), "AGOL_Cleanup_Dashboard.html")):
    page = open(path, encoding="utf-8").read()
    found = re.search(r"const D = (\{.*?\});\nconst \$ = ", page, re.S)
    if not found:
        raise SystemExit(f"no embedded data found in {path}; run build_full_dashboard.py instead")
    open(path, "w", encoding="utf-8").write(template.replace("/*__DATA__*/null", found.group(1)))
    print(f"re-rendered {path} ({os.path.getsize(path) / 1024:.0f} KB)")
