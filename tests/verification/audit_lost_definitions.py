"""Which top-level names from their app.py are missing from the merged app.py?

The function-replacement patches cut from a def to the next def, which could
swallow module-level constants that sat between two functions. This finds every
one so nothing is silently lost.
"""
import ast

MINE = r"C:\Users\user\Downloads\Crainbow_CBT_Phase3H_Live_Messages_Fix\crainbow\app.py"
THEIRS = r"C:\Users\user\Downloads\Latest Crainbow_CBT_Phase13_Current_Project\Crainbow_CBT_Phase13_Current_Project\app.py"


def top_level(path):
    src = open(path, encoding="utf-8", errors="replace").read()
    tree = ast.parse(src)
    lines = src.split("\n")
    names = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] = ("def", node.lineno, node.end_lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names[t.id] = ("assign", node.lineno, node.end_lineno)
    return names, lines


mine, _ = top_level(MINE)
theirs, tlines = top_level(THEIRS)

lost = sorted(set(theirs) - set(mine), key=lambda n: theirs[n][1])
print(f"mine={len(mine)} top-level names, theirs={len(theirs)}")
print(f"\nMISSING FROM MERGED FILE: {len(lost)}")
for n in lost:
    kind, start, end = theirs[n]
    print(f"  {kind:6} {n:35} their lines {start}-{end} ({end-start+1} lines)")

extra = sorted(set(mine) - set(theirs))
print(f"\nONLY IN MERGED (my helpers): {len(extra)}")
print("  " + ", ".join(extra))

# Emit the missing blocks so they can be restored verbatim.
if lost:
    with open("lost_blocks.py.txt", "w", encoding="utf-8") as f:
        for n in lost:
            kind, start, end = theirs[n]
            f.write("\n".join(tlines[start - 1:end]) + "\n\n\n")
    print("\nwrote lost_blocks.py.txt")
