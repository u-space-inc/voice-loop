"""試験の環境の試験。spike/ のファイルに「# /// script」のブロックが無いこと（先頭でなくても uv は読み、ファイルの置き場ごとに環境を作って共有のキャッシュに残す）、
本体（server.py の先頭）の依存が pyproject.toml の dev にそろっていること、この試験自体が作業木の .venv で動いていること、
spike/ の .py がどれも spike/ci.txt（CI で回す一覧と、回さないものとその理由）に載っていること、
本体を読み込む試験が、Hermes の置き場（VL_HERMES_HOME）を一時のフォルダに切り替えていること
（切り替えないと、試験の中で起動した本体の裏の仕事が、本物の ~/.hermes とコンテナに手順の書き足しを当てる。2026-11-23 に hermes_log_test で起きた）。
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

CI_RUN, CI_SKIP, CI_SKIP_LINES = [], {}, []
for line in (ROOT / "spike/ci.txt").read_text(encoding="utf-8").splitlines():
    if not line.strip() or line.startswith("#"):
        continue
    if line.startswith("- "):
        name, _, why = line[2:].partition("  ")
        CI_SKIP[name.strip()] = why.strip()
        CI_SKIP_LINES.append(name.strip())
    else:
        CI_RUN.append(line.strip())
names = {p.name for p in (ROOT / "spike").glob("*.py")}
check("spike/ の .py は、どれも spike/ci.txt に載っている（CI で回すか、回さない理由つきか）", names <= set(CI_RUN) | set(CI_SKIP), sorted(names - set(CI_RUN) - set(CI_SKIP)))
check("spike/ci.txt に、無いファイル・重なり・二重の行が無い",
      (set(CI_RUN) | set(CI_SKIP)) <= names and not set(CI_RUN) & set(CI_SKIP) and len(CI_RUN) == len(set(CI_RUN)) and len(CI_SKIP_LINES) == len(set(CI_SKIP_LINES)),
      (sorted((set(CI_RUN) | set(CI_SKIP)) - names), sorted(set(CI_RUN) & set(CI_SKIP))))
check("spike/ci.txt の回さないものには、どれも理由がある", all(CI_SKIP.values()), [n for n, w in CI_SKIP.items() if not w])
MUST_SKIP = {"ui_test.py", "guard_test.py", "report_test.py", "report_queue_test.py", "container_guard_test.py",
             "hermes_memory_container_test.py", "hermes_skills_container_test.py"}  # 常駐・本物の herdr・コンテナに触れる
check("本物の常駐・herdr・コンテナに触れる試験は、CI で回さない側にある", MUST_SKIP <= set(CI_SKIP) and not MUST_SKIP & set(CI_RUN), sorted(MUST_SKIP - set(CI_SKIP)))
def _loads_server(src: str) -> bool:
    """本体（server.py）を import する試験か（文字列ではなく、構文で見る）。"""
    import ast
    return any(isinstance(n, ast.Import) and any(a.name == "server" for a in n.names) for n in ast.walk(ast.parse(src)))


_unhomed = [p.name for p in sorted((ROOT / "spike").glob("*.py"))
            if _loads_server(p.read_text(encoding="utf-8")) and not re.search(r"VL_HERMES_HOME\s*=", p.read_text(encoding="utf-8"))]
check("本体を読み込む試験は、Hermes の置き場（VL_HERMES_HOME）を一時のフォルダに切り替えている（本物の ~/.hermes とコンテナに触れない）", not _unhomed, _unhomed)
check("この試験は作業木の .venv で動いている", Path(sys.prefix).resolve() == (ROOT / ".venv").resolve(), sys.prefix)

print(f"\n{sum(results)}/{len(results)} 件が期待どおり")
sys.exit(0 if all(results) else 1)
