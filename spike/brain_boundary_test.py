"""本体（server.py）とハブの頭の線引きの試験。線が崩れると落ちる。
- 本体のコードに、頭の固有の名前が出てこない（使ってよいのは、部品を読み込んで置く配線の行だけ。説明の文は数えない）
- 頭の部品が本体を呼ぶ名前は、約束の CORE_TOOLS の中だけ
- 約束の形（Brain・Talker）を、Claude Code と Hermes の両方が満たしている
- 頭を替えても、本体の口の一覧は同じ
常駐の 8790・herdr・docker には触れない。使い方: uv run spike/brain_boundary_test.py"""
import ast
import inspect
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import server as s  # noqa: E402
from hub_brain import CORE_TOOLS, Brain, Talker  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


src = (ROOT / "server.py").read_text()
tree = ast.parse(src)
docstrings = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
              and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
WORD = re.compile(r"hermes|hsk_|hmem_", re.I)
WIRING = {"hermes", "hub_hermes"}  # 配線で使う名前（読み込みと、部品を指す名前）
found = set()
for n in ast.walk(tree):
    if id(n) in docstrings:
        continue
    if isinstance(n, ast.Name) and WORD.search(n.id) and n.id not in WIRING:
        found.add(n.id)
    elif isinstance(n, ast.Attribute) and WORD.search(n.attr):
        if not (isinstance(n.value, ast.Name) and n.value.id == "hermes" and n.attr == "HERMES_HUB"):
            found.add(n.attr)
    elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and WORD.search(n.name):
        found.add(n.name)
    elif isinstance(n, ast.Constant) and isinstance(n.value, str) and WORD.search(n.value):
        found.add(n.value[:40])
check("本体のコードに頭の固有の名前が無い", sorted(found), [])
wired = [i + 1 for i, line in enumerate(src.splitlines()) if re.search(r"\bhermes\.", line.split("#")[0])]
check("部品に触るのは配線の 4 行だけ", len(wired), 4)
check("本体の /api/hermes の口はコードに書いていない", any(isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("/api/hermes") for n in ast.walk(tree)), False)

part = (ROOT / "scripts" / "hub_hermes.py").read_text()
check("頭の部品が使う本体の名前は約束の中だけ", sorted(set(re.findall(r"\bcore\.(\w+)", part)) - CORE_TOOLS), [])
check("約束の名前はどれも本体にある", sorted(n for n in CORE_TOOLS if not hasattr(s, n)), [])
imports = {a.name for n in ast.walk(ast.parse(part)) if isinstance(n, ast.Import) for a in n.names} | {n.module for n in ast.walk(ast.parse(part)) if isinstance(n, ast.ImportFrom)}
check("頭の部品は本体を読み込まない（core で受け取る）", "server" in imports, False)


def abstract(cls):
    return sorted(k for k, v in vars(cls).items() if inspect.isfunction(v) and "raise NotImplementedError" in inspect.getsource(v))


def missing(obj, cls):
    return sorted(k for k in abstract(cls) if getattr(type(obj), k) is getattr(cls, k))


check("ハブの頭は VL_HUB=hermes なら Hermes", s.HUB is s.hermes.TALKER, True)
check("Hermes は頭の約束を満たす", missing(s.HUB, Brain), [])
check("Hermes は話し相手の約束を満たす", missing(s.HUB, Talker), [])
check("Claude Code は頭の約束を満たす", missing(s.ClaudeBrain(), Brain), [])
check("Claude Code だけが herdr の中の頭", (s.ClaudeBrain().in_herdr, s.HUB.in_herdr), (True, False))

# 頭を替えても本体の口は同じ（別のプロセスで VL_HUB=claude にして読み込み、一覧を比べる）
code = "import sys; sys.path.insert(0, '.'); import server as s; print(type(s.HUB).__name__); print('\\n'.join(sorted(f'{sorted(getattr(r, \"methods\", []) or [])} {r.path}' for r in s.app.routes)))"
env = {**os.environ, "VL_HUB": "claude"}
out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
head, _, routes = out.stdout.partition("\n")
check("VL_HUB=claude なら Claude Code", head, "ClaudeBrain")
here = "\n".join(sorted(f'{sorted(getattr(r, "methods", []) or [])} {r.path}' for r in s.app.routes))
check("頭を替えても本体の口の一覧は同じ", routes.strip(), here)
check("口の数は分ける前と同じ（画面の /api/hermes/ の 9 個を含む）", sum(1 for line in here.splitlines() if "/api/hermes/" in line), 9)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    if not passed:
        print(f"NG {name}\n   得た: {got!r}\n   期待: {want!r}")
print(f"{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
