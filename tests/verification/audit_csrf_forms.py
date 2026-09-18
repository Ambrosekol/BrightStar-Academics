"""Every POST form must carry a _csrf_token field, or its route returns 403.

The app reads request.form['_csrf_token']. A form that omits the field, or
spells it differently, silently becomes unusable.
"""
import os
import re

TEMPLATES = r"C:\Users\user\Downloads\Crainbow_CBT_Phase3H_Live_Messages_Fix\crainbow\templates"

FORM = re.compile(r"<form\b[^>]*>(.*?)</form>", re.S | re.I)
METHOD_POST = re.compile(r'method\s*=\s*["\']post["\']', re.I)
GOOD = re.compile(r'name\s*=\s*["\']_csrf_token["\']', re.I)
WRONG = re.compile(r'name\s*=\s*["\']csrf_token["\']', re.I)

problems = []
total = 0
for fn in sorted(os.listdir(TEMPLATES)):
    if not fn.endswith(".html"):
        continue
    src = open(os.path.join(TEMPLATES, fn), encoding="utf-8", errors="replace").read()
    for m in FORM.finditer(src):
        tag_end = src.index(">", m.start())
        opening = src[m.start():tag_end + 1]
        if not METHOD_POST.search(opening):
            continue
        total += 1
        body = m.group(1)
        if GOOD.search(body):
            continue
        line = src[:m.start()].count("\n") + 1
        kind = "WRONG NAME (csrf_token)" if WRONG.search(body) else "NO TOKEN"
        action = re.search(r'action\s*=\s*["\']([^"\']*)["\']', opening)
        problems.append((fn, line, kind, action.group(1) if action else "(same page)"))

print(f"POST forms scanned: {total}")
print(f"forms that would be rejected with 403: {len(problems)}\n")
for fn, line, kind, action in problems:
    print(f"  {fn}:{line}  {kind}")
    print(f"      action: {action[:110]}")
