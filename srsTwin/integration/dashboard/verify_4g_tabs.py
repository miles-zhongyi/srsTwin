#!/usr/bin/env python3
"""Quick check: 4G tabs and data present in generated index.html."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
html = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()

checks = [
    ("panel-lte4g present",      'id="panel-lte4g"'),
    ("panel-lte4gtrace present",  'id="panel-lte4gtrace"'),
    ("4G LTE tab button",         'data-tab="lte4g"'),
    ("4G Trace tab button",       'data-tab="lte4gtrace"'),
    ("DATA4G JS variable",        "const DATA4G ="),
    ("trace_recs key",            '"trace_recs":'),
    ("per_templates key",         '"per_templates":'),
    ("lte-ev-list element",       'id="lte-ev-list"'),
    ("ltetrace-tbody element",    'id="ltetrace-tbody"'),
    ("lte-inject-bar element",    'id="lte-inject-bar"'),
    ("per-template-json element", 'id="per-template-json"'),
    ("4G tab CSS class",          "tab-4g"),
    ("renderLteEvList fn",        "function renderLteEvList"),
    ("renderLtetraceTable fn",    "function renderLtetraceTable"),
]

ok = fail = 0
for name, needle in checks:
    found = needle in html
    print(("OK  " if found else "FAIL"), name)
    if found:
        ok += 1
    else:
        fail += 1

print(f"\n{ok}/{ok+fail} checks passed")
