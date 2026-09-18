"""Which token-less POST forms actually target a CSRF-enforcing route?

A form with no _csrf_token is only broken if its route is decorated with
@csrf_protect (or calls csrf_check_request itself). Public forms such as the
login page deliberately do not enforce it.
"""
import ast
import os
import re

ROOT = r"C:\Users\user\Downloads\Crainbow_CBT_Phase3H_Live_Messages_Fix\crainbow"
APP = os.path.join(ROOT, "app.py")
TEMPLATES = os.path.join(ROOT, "templates")

src = open(APP, encoding="utf-8").read()
tree = ast.parse(src)

protected = set()
routes = {}
for node in tree.body:
    if not isinstance(node, ast.FunctionDef):
        continue
    decs = []
    for d in node.decorator_list:
        decs.append(ast.unparse(d))
    if any("csrf_protect" in d for d in decs):
        protected.add(node.name)
    body = ast.unparse(node)
    if "csrf_check_request()" in body:
        protected.add(node.name)
    for d in decs:
        if d.startswith("app.route") or d.startswith("app.post") or d.startswith("app.get"):
            routes[node.name] = d

FORM = re.compile(r"<form\b[^>]*>(.*?)</form>", re.S | re.I)
METHOD_POST = re.compile(r'method\s*=\s*["\']post["\']', re.I)
GOOD = re.compile(r'name\s*=\s*["\']_csrf_token["\']', re.I)
ENDPOINT = re.compile(r"url_for\(\s*['\"](\w+)['\"]")

print(f"routes enforcing CSRF: {len(protected)}\n")
broken = []
for fn in sorted(os.listdir(TEMPLATES)):
    if not fn.endswith(".html"):
        continue
    text = open(os.path.join(TEMPLATES, fn), encoding="utf-8", errors="replace").read()
    for m in FORM.finditer(text):
        tag_end = text.index(">", m.start())
        opening = text[m.start():tag_end + 1]
        if not METHOD_POST.search(opening) or GOOD.search(m.group(1)):
            continue
        line = text[:m.start()].count("\n") + 1
        ep = ENDPOINT.search(opening)
        target = ep.group(1) if ep else None
        # A form with no action posts back to the page that rendered it, so the
        # rendering route is also the receiving route.
        if target is None:
            action = re.search(r'action\s*=\s*["\']([^"\']*)["\']', opening)
            if action and not action.group(1).strip():
                target = "(self)"
            elif not action:
                target = "(self)"
        status = ("ENFORCED" if target in protected else
                  "self-post" if target == "(self)" else
                  "not enforced" if target else "unknown")
        broken.append((fn, line, target, status))

for fn, line, target, status in broken:
    mark = "BROKEN " if status == "ENFORCED" else "       "
    print(f"{mark}{fn}:{line}  -> {target}  [{status}]")

print("\nSelf-posting forms need the rendering route checked by hand:")
for fn, line, target, status in broken:
    if status == "self-post":
        print(f"   {fn}:{line}")
