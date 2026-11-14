"""Export the admin settings specs (group/label/key) as JSON for the JS harness.

Parses hearth/runtime_settings.py rather than importing it, so this works without
the app's runtime deps (sqlalchemy etc.) installed.
"""
import json
import re
from pathlib import Path

src = Path("hearth/runtime_settings.py").read_text(encoding="utf-8")

# Each SettingSpec(...) call, captured by balancing the parentheses after the name.
out = []
for m in re.finditer(r"SettingSpec\(", src):
    i = m.end()
    depth = 1
    while depth:
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    body = src[m.end() : i - 1]
    args = []
    for part in re.finditer(r'"((?:[^"\\]|\\.)*)"|(\b[A-Za-z_][A-Za-z_0-9]*\b|\d+\.?\d*)', body):
        if part.group(1) is not None:
            args.append(part.group(1))
        else:
            args.append(part.group(2))
    key = args[0] if args else "?"
    group = re.search(r'group="([^"]+)"', body)
    out.append({"key": key, "group": group.group(1) if group else None})

Path("scratch/specs.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
print(f"{len(out)} specs -> scratch/specs.json")
