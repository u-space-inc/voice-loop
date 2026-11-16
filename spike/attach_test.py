"""画面から添付したファイルを受け取り、話し相手に渡す（/api/attach と、発言の attachments）の試験。
状態フォルダは一時のフォルダ、herdr とセッションへの送信・Hermes・コンテナの台本は差し替える。常駐の 8790・本物の herdr・docker には触れない
（コンテナの中の台本は、この試験の中で、置き場を一時のフォルダにして直に動かす）。
確かめること:
  - 受け取る: 種類（画像・PDF・テキストだけ。SVG・HTML・実行ファイル・拡張子の無いものは断る）・中身の頭と拡張子の照らし・テキストの UTF-8 と NUL・
    大きさの上限・空・置く名前は本体が付ける（送られた名前の「..」やフォルダは使わない。表示の名前は 1 行）・0600・別のサイトとコンテナからは使えない
  - 渡す: セッションには状態フォルダの場所を、ハブ（Hermes）にはコンテナの中に置いた場所を、発言の末尾に書く・知らない id・多すぎる・形の違う添付は
    送らずに断る・コンテナに置けなければ送らない・声の発言にも付けられる
  - 片づけ: 7 日より古い添付を消す（名前の形が合うふつうのファイルだけ）
  - コンテナの中の台本: 置き場の voice-loop/attachments に O_EXCL で書く・途中が symlink なら書かない・名前の形が違えば書かない・古いものを消す
使い方: uv run spike/attach_test.py"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
import attach  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


async def nothing(*a, **k):
    return None


SESSION = {"pane": "w9:p1", "title": "受注", "cwd": "/x/shop", "kind": "claude", "session": "s1", "status": "idle", "name": "", "hub": False}
s.list_agents = lambda: [dict(SESSION)]
s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.hermes.watch_hermes_skills = s.speak_to = nothing
SENT: list[tuple[str, str]] = []


async def fake_ask(agent, text):
    SENT.append((agent["pane"], text))
    return "done", "わかりました"


s.ask = fake_ask
ASKED: list[str] = []
s.hermes.ask_hermes = lambda text: (ASKED.append(text), "わかりました")[1]
PUT: list[tuple[str, bytes]] = []
PUT_FAIL = {"v": False}


def fake_put(docker, container, user, python, name, data, env=None):
    if PUT_FAIL["v"]:
        raise RuntimeError("コンテナに置けませんでした（答えがありません）")
    PUT.append((name, data))
    return f"/opt/data/voice-loop/attachments/{name}"


s.hermes.attach.put_container = fake_put
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
HDR = {"Origin": "http://127.0.0.1:8790"}

with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    up = lambda name, data, pane="w9:p1", **h: c.post("/api/attach", params={"name": name, "pane": pane}, content=data, headers={**HDR, **h})
    r = up("写真.png", PNG)
    rid = r.json()
    files = [p for p in (Path(TMP) / "state" / "attachments").rglob("*") if p.is_file()]
    check("受け取る: 画像を受け取り、id と表示の名前と大きさを返す", (r.status_code, rid["ok"], rid["name"], rid["size"]), (200, True, "写真.png", len(PNG)))
    check("受け取る: 置く名前は本体が付ける（日時と乱数）・0600・中身はそのまま",
          (len(files), bool(attach.NAME_RE.fullmatch(files[0].name)), "写真" in files[0].name, stat.S_IMODE(files[0].stat().st_mode), files[0].read_bytes() == PNG),
          (1, True, False, 0o600, True))
    check("受け取る: 画像・PDF・テキストのほかは断る（SVG・HTML・実行ファイル・拡張子の無いもの）",
          [up(n, b"<svg/>").json().get("reason") for n in ("a.svg", "a.html", "a.exe", "a", "a.png.html")],
          ["type"] * 5)
    check("受け取る: 中身の頭が拡張子と合わなければ断る（PNG と名乗るテキスト・PDF と名乗る画像）",
          [up(n, d).json().get("reason") for n, d in (("a.png", b"hello"), ("a.pdf", PNG), ("a.jpg", PNG), ("a.webp", b"RIFF0000WAVE"))], ["content"] * 4)
    check("受け取る: テキストは UTF-8 で NUL が無いものだけ", [up(n, d).json().get("reason") for n, d in (("a.txt", "日本語".encode()), ("a.md", b"a\x00b"), ("a.csv", b"\xff\xfe"))],
          [None, "content", "content"])
    check("受け取る: 空は断る", up("a.txt", b"").json().get("reason"), "empty")
    real_max = attach.MAX
    attach.MAX = 1000
    big = up("a.txt", b"x" * 1001)
    attach.MAX = real_max
    check("受け取る: 上限より大きいものは断る（413）", (big.status_code, big.json().get("reason")), (413, "too_big"))
    ev = up("../../etc/‮passwd\n.png", PNG).json()
    newest = max((p for p in (Path(TMP) / "state" / "attachments").rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime_ns)
    check("受け取る: 送られた名前の「..」・フォルダ・改行・向きを変える文字は、置く場所に使わず、表示は 1 行に",
          (ev["ok"], ev["name"], newest.parent.parent == Path(TMP) / "state" / "attachments", ".." in str(newest)), (True, "passwd .png", True, False))
    check("口: 別のサイトからは使えない", up("a.png", PNG, **{"Sec-Fetch-Site": "cross-site"}).status_code, 403)
    check("口: コンテナからは使えない（鍵があっても）", c.post("/api/attach", params={"name": "a.png"}, content=PNG,
                                                     headers={"Host": "host.docker.internal:8790", "X-VL-Key": "k1"}).status_code, 403)

    def say(w, pane, text, files=None, **extra):
        w.send_json({"type": "text", "text": text, "pane": pane, "speaker": 0, **({"attachments": files} if files is not None else {}), **extra})
        while True:
            m = w.receive_json()
            if m["type"] in ("reply", "error"):
                return m

    check("受け取る: 宛先（pane）の無い添付は断る", up("a.png", PNG, pane="").json().get("reason"), "pane")
    a2 = up("資料.pdf", b"%PDF-1.7\n...").json()["id"]
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        m = say(w, "w9:p1", "これを見て", [rid["id"], a2])
    path1 = str(s.ATTACHED[rid["id"]]["path"])  # 同じ秒に置いた添付は名前の並びが乱数で決まるので、本体の控えから引く
    check("渡す: セッションには、状態フォルダの場所と表示の名前を、発言の末尾に書く",
          (m["type"], SENT[-1][0], SENT[-1][1].split("\n\n")[0], "添付（画面から）:" in SENT[-1][1], f"- {path1}（写真.png）" in SENT[-1][1], "（資料.pdf）" in SENT[-1][1]),
          ("reply", "w9:p1", "これを見て", True, True, True))
    n = len(SENT)
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        e1 = say(w, "w9:p1", "x", ["nosuchid"])
        e2 = say(w, "w9:p1", "x", [rid["id"]] * 6)
        e3 = say(w, "w9:p1", "x", "notalist")
    check("渡す: 知らない id・多すぎる・形の違う添付は、送らずに断る", ([e["type"] for e in (e1, e2, e3)], len(SENT) - n), (["error"] * 3, 0))
    k0 = len(ASKED)
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        mx = say(w, s.hermes.HERMES_PANE, "これを見て", [rid["id"]])
    check("渡す: セッション宛てに置いた添付は、ハブ（ほかの相手）への発言には付けない（送らずに断る・コンテナにも置かない）",
          (mx["type"], mx["text"].startswith("添付を置いたときと、送る相手が違います"), len(ASKED) - k0, len(PUT)), ("error", True, 0, 0))
    s.hermes.HERMES_HEARD["text"] = "proj の「テスト」が許可を求めています。許可しますか？"
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        mr = say(w, s.hermes.HERMES_PANE, "はい", ["nosuchid"])
    check("渡す: ハブへの発言を添付のせいで断ったら、前に聞いた問いを失効させる（送り直しの「はい」を、その問いへの答えに使わせない）",
          (mr["type"], s.hermes.HERMES_HEARD["text"]), ("error", ""))
    hid = up("写真.png", PNG, pane=s.hermes.HERMES_PANE).json()["id"]
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        m = say(w, s.hermes.HERMES_PANE, "これを見て", [hid])
    check("渡す: ハブ（Hermes）には、コンテナの中に置いた場所を書く（中身はそのまま渡す）",
          (m["type"], PUT[-1][1] == PNG, bool(attach.NAME_RE.fullmatch(PUT[-1][0])), f"- /opt/data/voice-loop/attachments/{PUT[-1][0]}（写真.png）" in ASKED[-1]),
          ("reply", True, True, True))
    PUT_FAIL["v"] = True
    k = len(ASKED)
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        m = say(w, s.hermes.HERMES_PANE, "これを見て", [hid])
    PUT_FAIL["v"] = False
    check("渡す: コンテナに置けなければ、送らない", (m["type"], m["text"].startswith("添付を渡せませんでした"), len(ASKED) - k), ("error", True, 0))
    s.transcribe = lambda data, name: "声で頼む"
    with c.websocket_connect("ws://127.0.0.1:8790/ws") as w:
        w.send_json({"type": "turn", "pane": "w9:p1", "speaker": 0, "filename": "rec.webm", "attachments": [a2]})
        w.send_bytes(b"x" * 3000)
        while w.receive_json()["type"] not in ("reply", "error"):
            pass
    check("渡す: 声の発言にも添付を付けられる", (SENT[-1][1].startswith("声で頼む\n\n添付（画面から）:"), "（資料.pdf）" in SENT[-1][1]), (True, True))

# ---------- 片づけ ----------
day = Path(TMP) / "state" / "attachments" / "20200101"
day.mkdir()
old = day / "20200101-000000-0123abcd.png"
old.write_bytes(PNG)
other = day / "keep.txt"
other.write_text("名前の形が違う")
os.utime(old, (time.time() - 8 * 86400,) * 2)
os.utime(other, (time.time() - 8 * 86400,) * 2)
attach.tidy(Path(TMP) / "state" / "attachments")
check("片づけ: 7 日より古い添付を消す（名前の形が違うものは消さない）", (old.exists(), other.exists()), (False, True))

# ---------- コンテナの中の台本（置き場を一時のフォルダにして直に動かす） ----------
CROOT = Path(TMP) / "container-data"
CROOT.mkdir()


def put(name, data, root=CROOT):
    p = subprocess.run([sys.executable, "-c", attach.SCRIPT, attach.MARK, name], input=data, capture_output=True, env={**os.environ, "VL_ATTACH_ROOT": str(root)}, timeout=60)
    line = next((l for l in reversed(p.stdout.decode().splitlines()) if l.startswith("VLRESULT ")), "VLRESULT {}")
    return json.loads(line[len("VLRESULT "):])


nm = attach.new_name(".png")
o = put(nm, PNG)
check("台本: 置き場の voice-loop/attachments に書き、コンテナの中の場所を返す", (o.get("result"), o.get("path") == f"{CROOT}/voice-loop/attachments/{nm}",
      (CROOT / "voice-loop" / "attachments" / nm).read_bytes() == PNG), ("put", True, True))
check("台本: 同じ名前には書かない（O_EXCL）・名前の形が違えば書かない", (put(nm, b"x").get("result"), put("../x.png", PNG).get("result")), ("error", "error"))
EVIL = Path(TMP) / "evil-root"
(EVIL / "elsewhere").mkdir(parents=True)
(EVIL / "voice-loop").symlink_to(EVIL / "elsewhere", target_is_directory=True)
check("台本: 途中のフォルダが symlink なら書かない", (put(attach.new_name(".png"), PNG, EVIL).get("result"), list((EVIL / "elsewhere").iterdir())), ("error", []))
oldc = CROOT / "voice-loop" / "attachments" / "20200101-000000-89abcdef.png"
oldc.write_bytes(PNG)
os.utime(oldc, (time.time() - 8 * 86400,) * 2)
put(attach.new_name(".txt"), "新しい".encode())
check("台本: 置くついでに、7 日より古い添付を消す", oldc.exists(), False)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
