"""After the whole e2e journey, every original must be exactly as it was (the project's hardest rule)."""
import hashlib, os, sys

A = "D:/pi_cache/e2e/pristine/lib"   # before
B = "D:/pi_cache/e2e/lib"            # after delete, restore, archive, hide, fix date, album, upload...


def tree(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, root).replace("\\", "/")
            with open(p, "rb") as fh:
                out[rel] = (hashlib.sha256(fh.read()).hexdigest(), os.path.getsize(p))
    return out


before, after = tree(A), tree(B)
missing = sorted(set(before) - set(after))
added = sorted(set(after) - set(before))
changed = sorted(r for r in set(before) & set(after) if before[r] != after[r])
print(f"originals before: {len(before)}  after: {len(after)}")
print("MISSING (gone from the library folder):", missing)
print("CHANGED (bytes differ):", changed)
print("NEW files in the library folder:", added)
sys.exit(1 if (missing or changed) else 0)
