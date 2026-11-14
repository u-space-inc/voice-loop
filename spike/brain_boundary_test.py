"""本体（server.py）とハブの頭の線引きの試験。線が崩れると落ちる。
- 本体のコードに、頭の固有の名前が出てこない（使ってよいのは、部品を読み込んで置く配線の行だけ。説明の文は数えない）
- 頭の部品が本体を呼ぶ名前は、約束の CORE_TOOLS の中だけ
- 約束の形（Brain・Talker）を、Hermes が満たしている
- 頭は Hermes だけ（0.22.0 で Claude Code のハブを外した）。古い設定の VL_HUB=claude が残っていても、頭も本体の口の一覧も変わらない
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
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1")
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
        found.add(n.attr)
    elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and WORD.search(n.name):
        found.add(n.name)
    elif isinstance(n, ast.Constant) and isinstance(n.value, str) and WORD.search(n.value):
        found.add(n.value[:40])
check("本体のコードに頭の固有の名前が無い", sorted(found), [])
WIRED = {  # 本体が部品に触ってよい文（完全一致）。関数の中から部品を呼ぶと、その関数全体がここに無いので落ちる
    "hermes.mount(app)",
    "TALKERS = {hermes.TALKER.pane: hermes.TALKER}",
    "HUB = hermes.TALKER",
    "hermes.setup(sys.modules[__name__])",
}


def wiring(source: str) -> set[str]:
    """本体の上の階の文のうち、部品の名前 hermes を使うもの。"""
    return {ast.unparse(stmt) for stmt in ast.parse(source).body
            if any(isinstance(n, ast.Name) and n.id == "hermes" for n in ast.walk(stmt))}


check("部品に触るのは配線の文だけ", wiring(src), WIRED)
check("配線の検査は、配線の外で部品を呼ぶ文を見つける", wiring(src + "\nhermes.TALKER.history()\n") - WIRED, {"hermes.TALKER.history()"})
check("配線の検査は、関数の中で部品を呼ぶ文を見つける", len(wiring(src + "\ndef f():\n    return hermes.ask_hermes('x')\n") - WIRED), 1)
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


check("ハブの頭は Hermes", s.HUB is s.hermes.TALKER, True)
check("Hermes は頭の約束を満たす", missing(s.HUB, Brain), [])
check("Hermes は話し相手の約束を満たす", missing(s.HUB, Talker), [])
check("Claude Code のハブの部品は本体に無い", [n for n in ("ClaudeBrain", "HUB_DIR", "hub_stuck", "heard_before") if hasattr(s, n)], [])

# 古い設定が残っていても同じ（別のプロセスで VL_HUB=claude にして読み込み、頭と口の一覧を比べる）
code = "import sys; sys.path.insert(0, '.'); import server as s; print(type(s.HUB).__name__); print('\\n'.join(sorted(f'{sorted(getattr(r, \"methods\", []) or [])} {r.path}' for r in s.app.routes)))"
env = {**os.environ, "VL_HUB": "claude"}
out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
head, _, routes = out.stdout.partition("\n")
check("VL_HUB=claude が残っていても、頭は Hermes", head, "Hermes")
here = "\n".join(sorted(f'{sorted(getattr(r, "methods", []) or [])} {r.path}' for r in s.app.routes))
check("VL_HUB=claude が残っていても、本体の口の一覧は同じ", routes.strip(), here)
check("口の数は分ける前と同じ（画面の /api/hermes/ の 9 個を含む）", sum(1 for line in here.splitlines() if "/api/hermes/" in line), 9)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    if not passed:
        print(f"NG {name}\n   得た: {got!r}\n   期待: {want!r}")
print(f"{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
