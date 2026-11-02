"""試験の環境の試験。spike/ のファイルに「# /// script」のブロックが無いこと（先頭でなくても uv は読み、ファイルの置き場ごとに環境を作って共有のキャッシュに残す）、
本体（server.py の先頭）の依存が pyproject.toml の dev にそろっていること、この試験自体が作業木の .venv で動いていること。
使い方: uv run spike/env_test.py"""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []


def check(name, ok, got=None):
    results.append(ok)
    print(("✓ " if ok else "✗ ") + name + ("" if ok else f"（得た値: {got}）"))


def script_deps(path):
    m = re.search(r"^# /// script\n((?:#.*\n)*?)# ///$", path.read_text(encoding="utf-8"), re.M)
    meta = tomllib.loads("".join(l[2:] if l.startswith("# ") else l[1:] for l in m.group(1).splitlines(keepends=True)))
    return meta.get("dependencies", [])


headed = [p.name for p in sorted((ROOT / "spike").glob("*.py")) if re.search(r"^# /// script$", p.read_text(encoding="utf-8"), re.M)]
check("spike/ のファイルに「# /// script」のブロックが無い（何行目でも）", not headed, headed)

dev = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["dependency-groups"]["dev"]
missing = [d for d in script_deps(ROOT / "server.py") if d not in dev]
check("本体（server.py）の依存が pyproject.toml の dev にそろっている", not missing, missing)

check("この試験は作業木の .venv で動いている", Path(sys.prefix).resolve() == (ROOT / ".venv").resolve(), sys.prefix)

print(f"\n{sum(results)}/{len(results)} 件が期待どおり")
sys.exit(0 if all(results) else 1)
