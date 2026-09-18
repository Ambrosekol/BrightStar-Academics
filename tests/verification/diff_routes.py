"""Compare the reference (their raw SQL) and new (my SQLAlchemy) crawls."""
import json

ref = json.load(open("c13_ref.json", encoding="utf-8"))
new = json.load(open("c13_new.json", encoding="utf-8"))

keys = sorted(set(ref) | set(new))
identical = []
differing = []
only_ref = []
only_new = []

for k in keys:
    a, b = ref.get(k), new.get(k)
    if a is None:
        only_new.append(k); continue
    if b is None:
        only_ref.append(k); continue
    if a == b:
        identical.append(k)
    else:
        differing.append((k, a, b))

print(f"routes compared     : {len(keys)}")
print(f"  byte-identical    : {len(identical)}")
print(f"  differing         : {len(differing)}")
print(f"  only in reference : {len(only_ref)}")
print(f"  only in new       : {len(only_new)}")

for k in only_ref:
    print(f"  ONLY IN REF: {k}")
for k in only_new:
    print(f"  ONLY IN NEW: {k}")

if differing:
    print("\n--- DIFFERENCES ---")
    for k, a, b in differing:
        print(f"\n{k}")
        for field in ("status", "location", "len", "hash", "error"):
            if a.get(field) != b.get(field):
                av, bv = a.get(field), b.get(field)
                if field == "error":
                    print(f"   error  ref: {str(av)[:140]}")
                    print(f"          new: {str(bv)[:140]}")
                else:
                    print(f"   {field:8} ref={av}  new={bv}")
