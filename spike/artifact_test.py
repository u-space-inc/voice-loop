# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "anthropic>=1.0", "openai>=1.40", "pywebpush>=2.0", "httpx>=0.27"]
# ///
"""成果物の画像・動画を返す口（/api/file）の試験。相手の位置から、返してはいけないものを頼んで断られることを確かめる。
相手は、乗せられたハブやセッション（会話に好きな場所を書ける）、ブラウザで開いた別のサイトのページ（<img> で本体を読ませられる）、コンテナ。
本体は TestClient で読み込むだけ（常駐の 8790・herdr・docker には触れない）。
見ること: そのセッションのフォルダの中の画像・動画は返す（相対・絶対・空白や日本語の名前・フォルダの中を指す symlink）。型は拡張子で決め、
nosniff と sandbox を付ける（中身が HTML の .png も画像として返す）。断るもの: フォルダの外（../・絶対パス・外を指す symlink とフォルダの symlink）、
隠しフォルダと隠しファイル（symlink で指しても・../ で回り込んでも）、画像と動画のほかの拡張子（SVG・HTML・.txt・.env）、フォルダ、無いファイル、
空や NUL の入った場所、無いセッション、ホームか / をフォルダにしたセッション。ハブ（Claude Code と Hermes）の会話では、いまあるセッションの
フォルダの中を指す絶対パスか「フォルダ名/場所」だけ（フォルダ名で始まらない相対・同じフォルダ名が二つ・ハブ自身のフォルダは断る。
フォルダ名は symlink をたどる前の名前で照らし、その名前のフォルダの中だけを見る）。Sec-Fetch-Site が cross-site・same-site なら断り、same-origin・none・
無しは通す。コンテナからは断る。HEAD と Range（動画の早送り）。画面（/）に CSP（画像と動画は自分からだけ）。隠しフォルダの中で動くセッションは、
そのフォルダから先で見る。
使い方: uv run spike/artifact_test.py"""
import base64
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp()).resolve()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HUB="hermes", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_HERMES_HOME=f"{TMP}/hermes")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient  # noqa: E402

import server as s  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PROJ, OTHER, HUBD, OUTSIDE = TMP / "proj", TMP / "other", TMP / "hubdir", TMP / "outside"
for d in (PROJ / "out", PROJ / ".git", PROJ / "dir.png", OTHER, HUBD, OUTSIDE):
    d.mkdir(parents=True)
for f in (PROJ / "out" / "a.png", PROJ / "out" / "検証 1.png", PROJ / ".git" / "x.png", PROJ / "out" / ".hidden.png", OUTSIDE / "secret.png",
          OTHER / "b.png", HUBD / "h.png"):
    f.write_bytes(PNG)
(PROJ / "out" / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 1000)
(PROJ / "notes.txt").write_text("x")
(PROJ / ".env").write_text("SECRET=1")
(PROJ / "page.html").write_text("<script>window.x=1</script>")
(PROJ / "pic.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>window.x=1</script></svg>")
(PROJ / "fake.png").write_text("<html><script>window.x=1</script></html>")  # 拡張子は png、中身は HTML
(PROJ / "out" / "link_out.png").symlink_to(OUTSIDE / "secret.png")
(PROJ / "out" / "link_in.png").symlink_to(PROJ / "out" / "a.png")
(PROJ / "out" / "link_hidden.png").symlink_to(PROJ / ".git" / "x.png")
(PROJ / "out" / "dirlink").symlink_to(OUTSIDE, target_is_directory=True)

A = lambda pane, cwd, hub=False: {"pane": pane, "title": "t", "cwd": str(cwd), "kind": "claude", "session": pane, "status": "idle", "name": "", "hub": hub}
AGENTS = [A("w1:p1", PROJ), A("w2:p1", OTHER), A("w3:p1", Path.home()), A("w4:p1", "/"), A("w9:p1", HUBD, hub=True)]
s.list_agents = lambda: [dict(a) for a in AGENTS]
c = TestClient(s.app, base_url="http://127.0.0.1:8790")  # with を使わない（常駐の見張りなどを動かさない）
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


def get(pane, path, **headers):
    return c.get("/api/file", params={"pane": pane, "path": path}, headers=headers)


def reason(r):
    return r.json().get("reason") if r.headers.get("content-type", "").startswith("application/json") else None


r = get("w1:p1", "out/a.png")
check("セッションのフォルダの中の画像を、相対の場所で返す（型は拡張子で決め、nosniff と sandbox を付ける）",
      (r.status_code, r.content == PNG, r.headers.get("content-type"), r.headers.get("x-content-type-options"), "sandbox" in r.headers.get("content-security-policy", "")),
      (200, True, "image/png", "nosniff", True))
check("絶対パスでも返す", get("w1:p1", str(PROJ / "out" / "a.png")).status_code, 200)
check("名前に空白や日本語があっても返す", get("w1:p1", "out/検証 1.png").status_code, 200)
check("フォルダの中を指す symlink は返す", get("w1:p1", "out/link_in.png").status_code, 200)
r = get("w1:p1", "out/clip.mp4")
check("動画も返す（型は video/mp4）", (r.status_code, r.headers.get("content-type")), (200, "video/mp4"))
r = get("w1:p1", "fake.png")
check("中身が HTML でも、拡張子が png なら画像として返す（ブラウザに推し量らせない）", (r.status_code, r.headers.get("content-type"), r.headers.get("x-content-type-options")),
      (200, "image/png", "nosniff"))
r = c.get("/api/file", params={"pane": "w1:p1", "path": "out/clip.mp4"}, headers={"Range": "bytes=0-9"})
check("Range で一部だけ返す（動画の早送り）", (r.status_code, len(r.content), r.headers.get("content-range", "").startswith("bytes 0-9/")), (206, 10, True))
r = c.head("/api/file", params={"pane": "w1:p1", "path": "out/a.png"})
check("HEAD は中身を返さない（あるかどうかと大きさだけ）", (r.status_code, r.content, r.headers.get("content-length")), (200, b"", str(len(PNG))))

HIDDEN, OUT, TYPE = "隠しフォルダ・隠しファイルは見せません", "セッションのフォルダの外です", "画像と動画のほかは見せません"
REFUSE = [
    ("w1:p1", "../outside/secret.png", OUT, "../ でフォルダの外へ"),
    ("w1:p1", str(OUTSIDE / "secret.png"), OUT, "フォルダの外の絶対パス"),
    ("w1:p1", "out/link_out.png", OUT, "フォルダの外を指す symlink"),
    ("w1:p1", "out/dirlink/secret.png", OUT, "フォルダの外を指すフォルダの symlink"),
    ("w1:p1", str(OTHER / "b.png"), OUT, "別のセッションのフォルダ"),
    ("w1:p1", ".git/x.png", HIDDEN, "隠しフォルダ"),
    ("w1:p1", "out/.hidden.png", HIDDEN, "隠しファイル"),
    ("w1:p1", "out/link_hidden.png", HIDDEN, "隠しフォルダを指す symlink"),
    ("w1:p1", "out/../.git/x.png", HIDDEN, "../ で隠しフォルダへ回り込む"),
    ("w1:p1", ".env", HIDDEN, ".env"),
    ("w1:p1", "notes.txt", TYPE, "文字のファイル"),
    ("w1:p1", "page.html", TYPE, "HTML"),
    ("w1:p1", "pic.svg", TYPE, "SVG（中で script が動く）"),
    ("w1:p1", "dir.png", "ふつうのファイルではありません", "名前が .png のフォルダ"),
    ("w1:p1", "out/none.png", "ファイルがありません", "無いファイル"),
    ("w1:p1", "", "場所の形が読めません", "空の場所"),
    ("w1:p1", "out/a.png\x00.txt", "場所の形が読めません", "NUL の入った場所"),
    ("w8:p1", "out/a.png", "そのセッションがありません", "無いセッション"),
    ("w3:p1", "x.png", "見てよいフォルダがありません", "ホームをフォルダにしたセッション"),
    ("w4:p1", "x.png", "見てよいフォルダがありません", "/ をフォルダにしたセッション"),
    ("w9:p1", "out/a.png", "ハブの会話では、絶対パスか「フォルダ名/場所」だけを見ます（そのフォルダ名のセッションが無いか、二つ以上あります）", "Claude Code のハブの会話で、フォルダ名で始まらない相対の場所"),
    ("w9:p1", str(HUBD / "h.png"), OUT, "ハブ自身のフォルダ"),
    (s.HERMES_PANE, "out/a.png", "ハブの会話では、絶対パスか「フォルダ名/場所」だけを見ます（そのフォルダ名のセッションが無いか、二つ以上あります）", "Hermes の会話で、フォルダ名で始まらない相対の場所"),
    (s.HERMES_PANE, "proj", "ハブの会話では、絶対パスか「フォルダ名/場所」だけを見ます（そのフォルダ名のセッションが無いか、二つ以上あります）", "Hermes の会話で、フォルダ名だけ（場所が無い）"),
    (s.HERMES_PANE, "proj/.git/x.png", HIDDEN, "Hermes の会話で、フォルダ名からの隠しフォルダ"),
    (s.HERMES_PANE, "proj/../outside/secret.png", OUT, "Hermes の会話で、フォルダ名から ../ で外へ"),
    (s.HERMES_PANE, "proj/../other/b.png", OUT, "Hermes の会話で、フォルダ名から ../ で別のセッションのフォルダへ（その名前のフォルダの中だけを見る）"),
    (s.HERMES_PANE, "proj//" + str(OTHER / "b.png"), "ファイルがありません", "Hermes の会話で、フォルダ名のあとに絶対パス（そのフォルダからの場所として読む）"),
    (s.HERMES_PANE, "proj/out/link_out.png", OUT, "Hermes の会話で、フォルダ名から symlink で外へ"),
    (s.HERMES_PANE, "hubdir/h.png", "ハブの会話では、絶対パスか「フォルダ名/場所」だけを見ます（そのフォルダ名のセッションが無いか、二つ以上あります）", "Hermes の会話で、ハブ自身のフォルダ名"),
    (s.HERMES_PANE, str(PROJ / ".git" / "x.png"), HIDDEN, "Hermes の会話で、セッションの隠しフォルダ"),
    (s.HERMES_PANE, str(OUTSIDE / "secret.png"), OUT, "Hermes の会話で、どのセッションのフォルダでもない場所"),
]
for pane, path, why, what in REFUSE:
    r = get(pane, path)
    check(f"断る: {what}", (r.status_code, reason(r)), (404, why))
check("ハブ（Claude Code）の会話では、セッションのフォルダの中を指す絶対パスを返す", get("w9:p1", str(PROJ / "out" / "a.png")).status_code, 200)
check("ハブ（Hermes）の会話では、セッションのフォルダの中を指す絶対パスを返す", get(s.HERMES_PANE, str(OTHER / "b.png")).status_code, 200)
check("ハブの会話では「フォルダ名/フォルダからの場所」も返す（Hermes・Claude Code）", (get(s.HERMES_PANE, "proj/out/a.png").status_code, get("w9:p1", "other/b.png").status_code), (200, 200))

for site, want in (("cross-site", 403), ("same-site", 403), ("same-origin", 200), ("none", 200)):
    check(f"Sec-Fetch-Site が {site} なら {want}", get("w1:p1", "out/a.png", **{"sec-fetch-site": site}).status_code, want)
check("コンテナからは、鍵があっても断る（画面の口）", get("w1:p1", "out/a.png", host="host.docker.internal:8790", **{"x-vl-key": "k1"}).status_code, 403)
for page in ("/", "/static/index.html"):  # 同じ画面を開ける場所のどちらにも
    csp = c.get(page).headers.get("content-security-policy", "")
    check(f"画面（{page}）の CSP: 画像・動画と声・CSS・フォント・接続は自分からだけ（画像は data:・blob:、動画と声は blob: も）、枠と object は無し、form は自分へだけ",
          [x in csp for x in ("img-src 'self' data: blob:", "media-src 'self' blob:", "style-src 'self' 'unsafe-inline'", "font-src 'self' data:",
                              "connect-src 'self'", "frame-src 'none'", "object-src 'none'", "form-action 'self'")], [True] * 8)

DUP = TMP / "dup" / "proj"  # 別の場所の、同じフォルダ名「proj」
DUP.mkdir(parents=True)
(DUP / "a.png").write_bytes(PNG)
AGENTS.append(A("w6:p1", DUP))
s._artifact_agents = (0.0, [])  # 覚えている一覧を捨てる
r = get(s.HERMES_PANE, "proj/out/a.png")
check("同じフォルダ名のセッションが二つあると、「フォルダ名/場所」はどちらか決めず返さない（絶対パスなら返す）", (r.status_code, reason(r), get(s.HERMES_PANE, str(PROJ / "out" / "a.png")).status_code), (404, "ハブの会話では、絶対パスか「フォルダ名/場所」だけを見ます（そのフォルダ名のセッションが無いか、二つ以上あります）", 200))
AGENTS.pop()
# 「フォルダ名/場所」は、ハブが見ているフォルダ名（cwd の末尾。symlink をたどる前の名前）で照らす。たどった先の名前では照らさない
LINKS, DATA = TMP / "links", TMP / "data"
for d in (DATA / "proj" / "out", DATA / "other2" / "out", LINKS):
    d.mkdir(parents=True)
(DATA / "proj" / "out" / "w.png").write_bytes(PNG)  # フォルダ名 blog の中身（たどった先の名前は proj）
(DATA / "other2" / "out" / "w.png").write_bytes(PNG + b"B")  # フォルダ名 proj の中身
(LINKS / "blog").symlink_to(DATA / "proj")
(LINKS / "proj").symlink_to(DATA / "other2")
saved = list(AGENTS)
AGENTS[:] = [A("w7:p1", LINKS / "blog"), A("w8:p1", LINKS / "proj")]
s._artifact_agents = (0.0, [])
r = get(s.HERMES_PANE, "proj/out/w.png")
check("「フォルダ名/場所」は、ハブが見ているフォルダ名（symlink をたどる前の名前）で照らす", (r.status_code, r.content == PNG + b"B"), (200, True))
AGENTS[:] = saved
AGENTS.append(A("w5:p1", PROJ / ".git"))
s._artifact_agents = (0.0, [])  # 覚えている一覧を捨てる
check("隠しフォルダの中で動くセッションは、そのフォルダから先で見る", get("w5:p1", "x.png").status_code, 200)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
