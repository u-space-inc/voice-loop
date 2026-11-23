"""学んだこと（Hermes の手順の書き足しを自動で当て、記憶の変化とあわせて跡を残し、画面の「戻す」で戻す）の試験。
Hermes の置き場と手順の置き場は一時のフォルダに作り、コンテナの台本（hermes_skills.run_script・hermes_learn.run_script・hermes_memory.run_script・
still_running）は差し替える。docker にも、常駐の 8790 にも触れない。
確かめること:
  - 手順のフォルダを読む・戻す（コンテナの中の台本に写す関数）: 中身と指紋・symlink のフォルダとファイル・通常でないファイル・hardlink・大きすぎる・
    「..」と絶対の場所・当てた直後から変わっていたら書かない・増えたファイルを消し前の中身を書く・当てて作られた手順はフォルダごと消す（ほかに置かれたら残す）
  - 自動で当てる: 1 日（日本時間）の上限まで（本物の上限は 20。試験は 5 に置き換えて回す。超えた分は承認待ちに残り、次の日に当たる）・扱えない形・当てた／分からない id・中身を読めない手順は当てない
    （読めない版は試し直さない）・控え（前の中身・当てた直後の指紋）は状態フォルダに 0600 で、Hermes の置き場には書かない・当てたあとの中身を読めなければ戻せない印
  - 戻す: 前の中身と指紋を台本に渡す・戻したら印・二度は戻さない・変わっていたら戻さない・分からないときは戻せない印・前の処理の終わりを確かめられなければ断る
  - 記憶: 初めは跡を残さない・足された・消えた項目を跡に・画面で書いたものは跡にしない・戻すは足された項目を消し消えた項目を足し直す（もう変わった項目は飛ばす）
  - 画面の口: 一覧（新しい順・今日の数と上限）・中身・コンテナからは使えない
使い方: uv run spike/hermes_learn_test.py"""
import asyncio
import base64
import json
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
HOME = Path(TMP) / "hermes"
PEND = HOME / "pending" / "skills"
PEND.mkdir(parents=True)
(HOME / "memories").mkdir(parents=True)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9", VL_HERMES_HOME=str(HOME),
                  VL_HERMES_CONTAINER="hermes-test")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import server as s  # noqa: E402
import hermes_learn as hl  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REAL_LIMIT = hl.LIMIT
hl.LIMIT = 5  # 上限の働きは小さい数で確かめる（本物の数は下で見る）

results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


async def nothing(*a, **k):
    return None


pushes = []


async def fake_push(title, body, tag, urgency="normal", only=None):
    pushes.append(body)
    return "sent"


s.push_all = fake_push
s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.hermes.watch_hermes_skills = nothing
s.list_agents = lambda: []

def raises(fn):
    try:
        fn()
    except (ValueError, OSError):
        return True
    return False


# ---------- 1. 手順のフォルダを読む・戻す（コンテナの中で動く関数を、ここで直に） ----------
SK = Path(TMP) / "skills"
(SK / "voice-loop" / "hub" / "references").mkdir(parents=True)
(SK / "voice-loop" / "hub" / "SKILL.md").write_text("前の手順\n")
(SK / "voice-loop" / "hub" / "references" / "a.md").write_text("参考\n")
before = hl.snap_dir(str(SK), "voice-loop/hub")
check("自動: 本物の上限は 1 日 20 件（本体が見ているのと同じ定数）", (REAL_LIMIT, s.hermes.hermes_learn is hl), (20, True))
check("読む: フォルダの中のファイルを、場所ごとに base64 で", (before["exists"], sorted(before["files"]), base64.b64decode(before["files"]["SKILL.md"]).decode()),
      (True, ["SKILL.md", "references/a.md"], "前の手順\n"))
check("読む: 無い手順は exists が False", hl.snap_dir(str(SK), "voice-loop/none"), {"exists": False, "files": {}, "modes": {}})
(SK / "voice-loop" / "hub" / "SKILL.md").write_text("当てたあとの手順\n")
(SK / "voice-loop" / "hub" / "references" / "b.md").write_text("足された\n")
after = hl.snap_dir(str(SK), "voice-loop/hub")
check("指紋: 中身が変われば変わる・同じなら同じ", (hl.digest(before) != hl.digest(after), hl.digest(after) == hl.digest(hl.snap_dir(str(SK), "voice-loop/hub"))), (True, True))
check("戻す: いまの中身が当てた直後と違えば、何も書かない", (hl.restore_dir(str(SK), "voice-loop/hub", before, hl.digest(before)),
      (SK / "voice-loop" / "hub" / "SKILL.md").read_text()), ("changed", "当てたあとの手順\n"))
check("戻す: 前の中身を書き、当てたときに増えたファイルを消す", (hl.restore_dir(str(SK), "voice-loop/hub", before, hl.digest(after)),
      (SK / "voice-loop" / "hub" / "SKILL.md").read_text(), (SK / "voice-loop" / "hub" / "references" / "b.md").exists(), hl.digest(hl.snap_dir(str(SK), "voice-loop/hub")) == hl.digest(before)),
      ("restored", "前の手順\n", False, True))
# 実行できる台本を含む手順: SKILL.md だけを当てて戻しても、台本の x は残る。権限を変えたら指紋も変わる
(SK / "voice-loop" / "tool" / "scripts").mkdir(parents=True)
(SK / "voice-loop" / "tool" / "SKILL.md").write_text("前\n")
(SK / "voice-loop" / "tool" / "scripts" / "run.sh").write_text("#!/bin/sh\necho ok\n")
os.chmod(SK / "voice-loop" / "tool" / "scripts" / "run.sh", 0o755)
os.chmod(SK / "voice-loop" / "tool" / "SKILL.md", 0o644)
tb = hl.snap_dir(str(SK), "voice-loop/tool")
(SK / "voice-loop" / "tool" / "SKILL.md").write_text("当てたあと\n")
ta = hl.snap_dir(str(SK), "voice-loop/tool")
old_umask = os.umask(0o077)  # umask に左右されないこと
try:
    tr = hl.restore_dir(str(SK), "voice-loop/tool", tb, hl.digest(ta))
finally:
    os.umask(old_umask)
check("戻す: 実行できる台本の権限（x）を控えて、同じ権限で書き戻す（umask に左右されない）",
      (tb["modes"], tr, oct(os.stat(SK / "voice-loop" / "tool" / "scripts" / "run.sh").st_mode & 0o777), oct(os.stat(SK / "voice-loop" / "tool" / "SKILL.md").st_mode & 0o777),
       (SK / "voice-loop" / "tool" / "SKILL.md").read_text()),
      ({"SKILL.md": 0o644, "scripts/run.sh": 0o755}, "restored", "0o755", "0o644", "前\n"))
os.chmod(SK / "voice-loop" / "tool" / "scripts" / "run.sh", 0o700)
check("指紋: 権限が変われば変わる（当てたあとに権限を変えられたら、戻さない）", hl.digest(hl.snap_dir(str(SK), "voice-loop/tool")) != hl.digest(tb), True)
check("戻す: 控えの権限の形が違えば、何も書かずに断る", raises(lambda: hl.restore_dir(str(SK), "voice-loop/tool", {**tb, "modes": {"SKILL.md": "rwx"}}, hl.digest(hl.snap_dir(str(SK), "voice-loop/tool")))), True)
longname = "r" * 250 + ".md"  # 名前の長さの上限（255）に近いファイル
(SK / "voice-loop" / "tool" / longname).write_text("長い名前\n")
lb = hl.snap_dir(str(SK), "voice-loop/tool")
(SK / "voice-loop" / "tool" / longname).write_text("長い名前を書き換えた\n")
try:
    lr = hl.restore_dir(str(SK), "voice-loop/tool", lb, hl.digest(hl.snap_dir(str(SK), "voice-loop/tool")))
except OSError as e:
    lr = type(e).__name__
check("戻す: 名前の長さの上限に近いファイルも戻せる（一時のファイルの名前に元の名前を入れない）", (lr, (SK / "voice-loop" / "tool" / longname).read_text()), ("restored", "長い名前\n"))
(SK / "voice-loop" / "new" / "scripts").mkdir(parents=True)
(SK / "voice-loop" / "new" / "SKILL.md").write_text("作られた\n")
(SK / "voice-loop" / "new" / "scripts" / "x.py").write_text("print(1)\n")
made = hl.snap_dir(str(SK), "voice-loop/new")
check("戻す: 当てて作られた手順は、フォルダごと消す", (hl.restore_dir(str(SK), "voice-loop/new", {"exists": False, "files": {}}, hl.digest(made)),
      (SK / "voice-loop" / "new").exists(), (SK / "voice-loop").exists()), ("restored", False, True))
(SK / "flat").mkdir()  # 分類の無い手順（置き場の根の真下）
(SK / "flat" / "SKILL.md").write_text("作られた\n")
flat = hl.snap_dir(str(SK), "flat")
check("戻す: 置き場の根の真下に作られた手順も、フォルダごと消す", (hl.restore_dir(str(SK), "flat", {"exists": False, "files": {}}, hl.digest(flat)), (SK / "flat").exists()),
      ("restored", False))
(SK / "voice-loop" / "new2").mkdir()
(SK / "voice-loop" / "new2" / "SKILL.md").write_text("作られた\n")
made2 = hl.snap_dir(str(SK), "voice-loop/new2")
(SK / "voice-loop" / "new2" / "other").mkdir()  # 指紋に入らない空のフォルダ（Hermes がほかに置いたもの）
check("戻す: 作られた手順のフォルダに、ほかのものが置かれていれば、フォルダは残す（ファイルだけ消す）",
      (hl.restore_dir(str(SK), "voice-loop/new2", {"exists": False, "files": {}}, hl.digest(made2)), (SK / "voice-loop" / "new2" / "SKILL.md").exists(),
       (SK / "voice-loop" / "new2" / "other").exists()), ("restored", False, True))
OUTSIDE = Path(TMP) / "outside"
OUTSIDE.mkdir()
(OUTSIDE / "secret.txt").write_text("外のファイル")
(SK / "voice-loop" / "hub" / "link.md").symlink_to(OUTSIDE / "secret.txt")


check("読む: フォルダの中の symlink は断る（たどらない）", raises(lambda: hl.snap_dir(str(SK), "voice-loop/hub")), True)
(SK / "voice-loop" / "hub" / "link.md").unlink()
(SK / "voice-loop" / "linked").symlink_to(OUTSIDE, target_is_directory=True)
check("読む: 手順のフォルダそのものが symlink なら断る", raises(lambda: hl.snap_dir(str(SK), "voice-loop/linked")), True)
(SK / "cat").symlink_to(SK / "voice-loop", target_is_directory=True)
check("読む: 途中のフォルダが symlink なら断る", raises(lambda: hl.snap_dir(str(SK), "cat/hub")), True)
os.mkfifo(SK / "voice-loop" / "hub" / "fifo")
check("読む: 通常でないファイル（FIFO）は断る", raises(lambda: hl.snap_dir(str(SK), "voice-loop/hub")), True)
(SK / "voice-loop" / "hub" / "fifo").unlink()
os.link(SK / "voice-loop" / "hub" / "SKILL.md", OUTSIDE / "hard.md")
check("読む: ほかの名前からも指されるファイル（hardlink）は断る", raises(lambda: hl.snap_dir(str(SK), "voice-loop/hub")), True)
(OUTSIDE / "hard.md").unlink()
(SK / "voice-loop" / "hub" / "big.md").write_bytes(b"x" * (hl.SNAP_FILE_MAX + 1))
check("読む: 大きすぎるファイルは断る", raises(lambda: hl.snap_dir(str(SK), "voice-loop/hub")), True)
(SK / "voice-loop" / "hub" / "big.md").unlink()
check("場所: 「..」・絶対の場所・空の段・バックスラッシュは断る",
      [raises(lambda r=r: hl.snap_dir(str(SK), r)) for r in ("../outside", "/etc", "voice-loop//hub", "voice-loop\\hub", "voice-loop/./hub")], [True] * 5)
cur = hl.snap_dir(str(SK), "voice-loop/hub")
evil = {"exists": True, "files": {"../../outside/secret.txt": base64.b64encode(b"x").decode()}}
check("戻す: 前の中身のファイルの場所に「..」があれば、何も書かず・何も消さずに断る", (raises(lambda: hl.restore_dir(str(SK), "voice-loop/hub", evil, hl.digest(cur))),
      (OUTSIDE / "secret.txt").read_text(), hl.digest(hl.snap_dir(str(SK), "voice-loop/hub")) == hl.digest(cur)), (True, "外のファイル", True))
check("台本: コンテナの中で動く台本は、ここの関数をそのまま写す（Python として読める・上限の数が同じ）",
      (compile(hl.SCRIPT, "learn", "exec") is not None, f"SNAP_FILE_MAX = {hl.SNAP_FILE_MAX}" in hl.SCRIPT, "def restore_dir" in hl.SCRIPT), (True, True, True))
cmd = hl.command("hermes-test", "restore")
check("台本の命令: Hermes の利用者・時間切れ・戻すは当てると同じ目印（終わりの確かめに数える）・読むは確かめの目印",
      (cmd[cmd.index("-u") + 1], "timeout" in cmd, cmd[-2:], hl.command("hermes-test", "snap")[-2:]) if cmd else None,
      ("10000:10000", True, [hl.MARK, "restore"], [hl.CHECK_MARK, "snap"]) if cmd else None)

# ---------- 2. 自動で当てる ----------
SNAPS: dict = {}  # 手順の場所 → いまの中身（偽のコンテナ）
LCALLS: list = []
LOUT: dict = {}
SCALLS: list = []
RUNNING = {"v": False}


AFTER = {"none": False, "edit_before_apply": False}  # 当てたあとの指紋を返さない／控えのあと・当てる前に Hermes が書いた（偽）


def fake_skills_run(container, mode, pid, raw, wait=90, env=None, script=None):
    SCALLS.append((mode, pid))
    if mode == "approve":  # 当てると、手順の中身が変わる（偽）。自動で当てるときは、控えの指紋を確かめ、当てたあとの指紋を返す
        place = json.loads(raw)["payload"]["operations"][0]["name"]
        if script is not None:
            if AFTER["edit_before_apply"]:
                SNAPS[place] = {"exists": True, "files": {"SKILL.md": base64.b64encode("Hermes が控えのあとに書いた".encode()).decode()}}
            now = {place: hl.digest(SNAPS.get(place, {"exists": False, "files": {}}))}
            if now != json.loads((env or {}).get("VL_LEARN_EXPECT") or "{}"):
                return {"result": "changed", "why": "控えを取ったあとに手順が変わりました"}
        SNAPS[place] = {"exists": True, "files": {"SKILL.md": base64.b64encode(f"{pid} を当てた".encode()).decode()}}
        out = {"result": "applied", "discarded": True}
        if script is not None and not AFTER["none"]:
            out["after"] = {place: hl.digest(SNAPS[place])}
        return out
    return {"result": "ok"} if mode == "check" else {"result": "diff", "diff": f"差分 {pid}"} if mode == "diff" else {"result": "rejected", "discarded": True}


def fake_learn_run(container, mode, req, wait=90, env=None):
    LCALLS.append((mode, json.loads(json.dumps(req))))
    if mode in LOUT:
        r = LOUT[mode]
        return r(req) if callable(r) else dict(r)
    if mode == "snap":
        names = [op["name"] for op in req["payload"]["operations"]]
        return {"result": "snap", "skills": {n: SNAPS.get(n, {"exists": False, "files": {}}) for n in names}}
    return {"result": "restored"}


s.hermes.hermes_skills.run_script = fake_skills_run
s.hermes.hermes_skills.still_running = lambda container: RUNNING["v"]
s.hermes.hermes_learn.run_script = fake_learn_run


def record(pid, name, payload=None, at=None):
    payload = payload or {"action": "batch", "operations": [{"action": "patch", "name": name, "old_string": "a", "new_string": "a\nb"}]}
    raw = json.dumps({"id": pid, "subsystem": "skills", "action": "batch", "summary": f"{name} に足す", "origin": "background_review",
                      "created_at": at or time.time(), "payload": payload}, ensure_ascii=False).encode()
    (PEND / f"{pid}.json").write_bytes(raw)


def learned():
    with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
        return c.get("/api/hermes/learned").json()


SNAPS["hub"] = {"exists": True, "files": {"SKILL.md": base64.b64encode("前".encode()).decode()}}
for i in range(7):
    record(f"aaaa{i:04d}", "hub" if i == 0 else f"s{i}", at=time.time() + i)
record("bbbbbbbb", "del", {"action": "delete", "name": "x"})  # 扱えない形
n = asyncio.run(s.hermes._learn_skills())
lr = learned()
check("自動: 1 日の上限（試験では 5 件）まで当てる（古い順）。扱えない形は当てない", (n, sorted(p for m, p in SCALLS if m == "approve")), (5, [f"aaaa{i:04d}" for i in range(5)]))
check("自動: 跡に、今日の数と上限・手順の要約・戻せる印", (lr["today"], lr["limit"], len(lr["entries"]), all(e["can_undo"] and e["kind"] == "skill" for e in lr["entries"])),
      (5, 5, 5, True))
first = [e for e in lr["entries"] if e["pid"] == "aaaa0000"][0]
det = json.loads((s.hermes.HLEARN_DIR / f"{first['id']}.json").read_text())
check("自動: 控えは状態フォルダに 0600 で（前の中身・当てた直後の指紋・差分）。Hermes の置き場には何も書かない",
      (stat.S_IMODE((s.hermes.HLEARN_DIR / f"{first['id']}.json").stat().st_mode), det["before"]["hub"]["files"]["SKILL.md"], det["expect"].get("hub") == hl.digest(SNAPS["hub"]),
       det["diff"], sorted(p.name for p in HOME.rglob("*") if p.is_file() and "pending" not in p.parts)),
      (0o600, base64.b64encode("前".encode()).decode(), True, "差分 aaaa0000", []))
SCALLS.clear()
check("自動: 上限に届いた日は、もう当てない（残りは承認待ちに残る）", (asyncio.run(s.hermes._learn_skills()), [m for m, p in SCALLS if m == "approve"],
      sorted(p.stem for p in PEND.glob("aaaa*.json"))[-2:]), (0, [], ["aaaa0005", "aaaa0006"]))
st = json.loads(s.hermes.HLEARN_FILE.read_text())
st["entries"] = [e for e in st["entries"] if e["kind"] != "skill"] + [e for e in st["entries"] if e["kind"] == "skill"][:0]
saved_entries = json.loads(s.hermes.HLEARN_FILE.read_text())["entries"]
s.hermes.HLEARN_FILE.write_text(json.dumps(st))
SCALLS.clear()
check("自動: 日ごとの数は跡とは別に持つ（跡が消えても・押し出されても、同じ日にもう当てない）", (asyncio.run(s.hermes._learn_skills()), [m for m, p in SCALLS if m == "approve"]), (0, []))
st["entries"] = saved_entries
today = s.hermes._hlearn_day(time.time())
st["days"] = {s.hermes._hlearn_day(time.time() - 86400): st["days"][today]}  # 前の日に当てたことにする
s.hermes.HLEARN_FILE.write_text(json.dumps(st))
check("自動: 次の日（日本時間）には、残りを当てる。当てた id は二度当てない", (asyncio.run(s.hermes._learn_skills()), sorted(p for m, p in SCALLS if m == "approve")),
      (2, ["aaaa0005", "aaaa0006"]))
record("tttttt01", "t1")
record("tttttt02", "t2")
real_skills_run = s.hermes.hermes_skills.run_script
s.hermes.hermes_skills.run_script = lambda c, mode, pid, raw, wait=90, env=None, script=None: (SCALLS.append((mode, pid)), {"result": "timeout", "why": "時間切れ"})[1] if mode == "approve" else real_skills_run(c, mode, pid, raw, wait, env, script)
SCALLS.clear()
asyncio.run(s.hermes._learn_skills())
tl = [e for e in learned()["entries"] if e.get("pid") == "tttttt01"]
check("自動: 当てて時間切れになったら、その回はもう当てない（終わりを確かめるまで）。跡は「分からない」で残し、戻せない。注記も「分からない」",
      ([p for m, p in SCALLS if m == "approve"], [(e["state"], e["can_undo"], e["note"].startswith("当たったか分かりません")) for e in tl]), (["tttttt01"], [("unknown", False, True)]))
s.hermes.hermes_skills.run_script = real_skills_run
RUNNING["v"] = False
SCALLS.clear()
asyncio.run(s.hermes._learn_skills())  # 終わりを確かめたあと（台本はいない）の回で、残りを当てる
check("自動: 終わりを確かめたあとの回で、残りを当てる", [p for m, p in SCALLS if m == "approve"], ["tttttt02"])
real_write = s.write_json
s.write_json = lambda path, data: (_ for _ in ()).throw(OSError("書けない")) if "hermes-learned" in str(path) else real_write(path, data)
record("ffffffff", "f1")
SCALLS.clear()
check("自動: 控えと跡を書けなければ、当てない（当てる前に書く）", (asyncio.run(s.hermes._learn_skills()), [m for m, p in SCALLS if m == "approve"]), (0, []))
s.write_json = real_write
(PEND / "ffffffff.json").unlink()
LOUT["snap"] = {"result": "unsupported", "why": "手順のフォルダに symlink か通常でないファイルがあります"}
record("cccccccc", "odd")
SCALLS.clear()
check("自動: 中身を読めない手順は当てない（承認待ちに残す）", (asyncio.run(s.hermes._learn_skills()), [m for m, p in SCALLS if m == "approve"]), (0, []))
lc = len(LCALLS)
asyncio.run(s.hermes._learn_skills())
check("自動: 読めなかった版は、本体を起動し直すまで試し直さない", len(LCALLS), lc)
del LOUT["snap"]
s.hermes._HLEARN_SKIP.clear()
RUNNING["v"] = True
st = json.loads(s.hermes.HSK_FILE.read_text())
st["verify"] = True
s.hermes.HSK_FILE.write_text(json.dumps(st))
SCALLS.clear()
check("自動: 前の処理の終わりを確かめられない間は当てない", (asyncio.run(s.hermes._learn_skills()), [m for m, p in SCALLS if m == "approve"]), (0, []))
RUNNING["v"] = False
AFTER["none"] = True
asyncio.run(s.hermes._learn_skills())
AFTER["none"] = False
odd = [e for e in learned()["entries"] if e["pid"] == "cccccccc"][0]
check("自動: 当てたあとの中身を読めなければ、当てた跡は残し、戻せない印にする", (odd["can_undo"], odd["note"]), (False, "当てたあとの中身を読めなかったので、戻せません"))
check("自動: 当てるのは、控えの指紋を渡した自動の台本（鍵を持って確かめ・当て・当てたあとの指紋まで一続き）", [m for m, p in SCALLS if p == "cccccccc"][-1:], ["approve"])
st = json.loads(s.hermes.HLEARN_FILE.read_text())
st["days"] = {}
s.hermes.HLEARN_FILE.write_text(json.dumps(st))
SNAPS["hub2"] = {"exists": True, "files": {"SKILL.md": base64.b64encode("前".encode()).decode()}}
record("dddddddd", "hub2")
AFTER["edit_before_apply"] = True
asyncio.run(s.hermes._learn_skills())
AFTER["edit_before_apply"] = False
dd = [e for e in learned()["entries"] if e["pid"] == "dddddddd"]
check("自動: 控えを取ったあと・当てる前に Hermes が手順を書いたら、当てない（承認待ちに残し、Hermes の書いた中身はそのまま）",
      ([(e["state"], e["can_undo"], "控えを取ったあとに手順が変わりました" in e["note"]) for e in dd], (PEND / "dddddddd.json").exists(),
       base64.b64decode(SNAPS["hub2"]["files"]["SKILL.md"]).decode()),
      ([("failed", False, True)], True, "Hermes が控えのあとに書いた"))
(PEND / "dddddddd.json").unlink(missing_ok=True)  # 次の試験で当て直さない
SNAPS["hub3"] = {"exists": True, "files": {"SKILL.md": base64.b64encode("前".encode()).decode()}}
record("eeeeeee3", "hub3")
LOUT["snap"] = {"result": "timeout", "why": "時間切れ"}
asyncio.run(s.hermes._learn_skills())
del LOUT["snap"]
skipped_now = any(p == "eeeeeee3" for p, _ in s.hermes._HLEARN_SKIP)
SCALLS.clear()
asyncio.run(s.hermes._learn_skills())
check("自動: 控えを取るのが一時的に失敗した（時間切れ）書き足しは、飛ばす印を付けず、次の回に試し直して当てる",
      (skipped_now, [p for m, p in SCALLS if m == "approve"]), (False, ["eeeeeee3"]))

# 当てて「failed」と返った書き足し: 手順が当てる前のままと確かめられたときだけ、承認待ちから外す
FAIL = {"partial": False, "reject": None}  # 失敗の前に手順の一部を書いた（偽）／捨てる台本の答えの差し替え
MATCH = "operations[0] (patch on 'x') failed: Could not find a match for old"


def failing_run(c, mode, pid, raw, wait=90, env=None, script=None):
    if mode == "approve" and pid.startswith("9999"):
        SCALLS.append((mode, pid))
        if FAIL["partial"]:
            place = json.loads(raw)["payload"]["operations"][0]["name"]
            SNAPS[place] = {"exists": True, "files": {"SKILL.md": base64.b64encode("途中まで書いた".encode()).decode()}}
        return {"result": "failed", "why": MATCH}
    if mode == "reject" and pid.startswith("9999"):
        SCALLS.append((mode, pid))
        if FAIL["reject"]:
            return dict(FAIL["reject"])
        (PEND / f"{pid}.json").unlink()
        return {"result": "rejected", "discarded": True}
    return real_skills_run(c, mode, pid, raw, wait, env, script)


def acts():
    return [(m, p) for m, p in SCALLS if m in ("approve", "reject")]


def fresh_day():
    d = json.loads(s.hermes.HLEARN_FILE.read_text())
    d["days"] = {}
    s.hermes.HLEARN_FILE.write_text(json.dumps(d))


def entry_of(pid):
    return [(e["state"], e["can_undo"], e["note"]) for e in learned()["entries"] if e.get("pid") == pid]


def hsk_of(pid):
    d = json.loads(s.hermes.HSK_FILE.read_text())
    return (d["items"].get(pid) or {}).get("state"), d["verify"]


s.hermes.hermes_skills.run_script = failing_run
for n in ("g1", "g2", "g3", "g4", "g5", "g6"):
    SNAPS[n] = {"exists": True, "files": {"SKILL.md": base64.b64encode("前".encode()).decode()}}
fresh_day()
record("99990001", "g1", at=time.time())
record("gggggg02", "g2", at=time.time() + 1)
SCALLS.clear()
n = asyncio.run(s.hermes._learn_skills())
arch = json.loads((s.hermes.HSK_DIR / "approved" / "99990001.json").read_text())
check("自動: failed で手順が当てる前のままなら、承認待ちから外し、跡は「当てられませんでした」。同じ回で次の書き足しを当てる",
      (n, acts(), entry_of("99990001"), hsk_of("99990001"), (PEND / "99990001.json").exists(), (arch["result"], "payload" in arch["record"]),
       base64.b64decode(SNAPS["g1"]["files"]["SKILL.md"]).decode()),
      (1, [("approve", "99990001"), ("reject", "99990001"), ("approve", "gggggg02")],
       [("failed", False, f"当てられませんでした（{MATCH}）。手順は変わっていないので、承認待ちから外しました")], ("failed", False), False, ("failed", True), "前"))
check("自動: 外した書き足しは、日の数に数える（当てようとした数）", learned()["today"], 2)
record("99990001", "g1", at=time.time())  # 相手が、外された記録と同じものを置き直す
SCALLS.clear()
before_today = learned()["today"]
asyncio.run(s.hermes._learn_skills())
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    again = [i for i in c.get("/api/hermes/skills").json()["items"] if i["id"] == "99990001"][0]
    manual = c.post("/api/hermes/skills/approve", json={"id": again["id"], "v": again["v"]}).json()
check("自動: 外した書き足しと同じ記録を置き直されても、試し直さない（日の数も増えない）。画面でも採れず、捨てるだけ",
      (acts(), learned()["today"] - before_today, again["state"], again["can_apply"], manual["ok"], manual["reason"], acts()),
      ([], 0, "failed", False, False, "failed", []))
(PEND / "99990001.json").unlink()
FAIL["reject"] = {"result": "rejected", "discarded": False}
record("99990007", "g1", at=time.time())
SCALLS.clear()
asyncio.run(s.hermes._learn_skills())
FAIL["reject"] = None
check("自動: 外す台本が「消せなかった」と答えたら、外した扱いにせず「分からない」に残す（試し直さない）",
      (acts(), [st for st, u, note in entry_of("99990007")], hsk_of("99990007"), (PEND / "99990007.json").exists(), asyncio.run(s.hermes._learn_skills()), acts()[2:]),
      ([("approve", "99990007"), ("reject", "99990007")], ["unknown"], ("unknown", False), True, 0, []))
(PEND / "99990007.json").unlink()
fresh_day()
FAIL["partial"] = True
record("99990003", "g3", at=time.time())
record("gggggg04", "g4", at=time.time() + 1)
SCALLS.clear()
asyncio.run(s.hermes._learn_skills())
FAIL["partial"] = False
check("自動: failed でも手順が変わっていたら（途中まで書いた）、外さず「分からない」に残し、その回はもう当てない",
      (acts(), [(st, u, note.startswith("当たったか分かりません")) for st, u, note in entry_of("99990003")], hsk_of("99990003"), (PEND / "99990003.json").exists()),
      ([("approve", "99990003")], [("unknown", False, True)], ("unknown", False), True))
asyncio.run(s.hermes._learn_skills())  # gggggg04 を当てて片づける
record("99990005", "g5", at=time.time())
fresh_day()
LOUT["snap"] = lambda req: {"result": "timeout", "why": "時間切れ"} if any(m == "approve" and p == "99990005" for m, p in SCALLS) else \
    {"result": "snap", "skills": {op["name"]: SNAPS[op["name"]] for op in req["payload"]["operations"]}}
SCALLS.clear()
asyncio.run(s.hermes._learn_skills())
del LOUT["snap"]
check("自動: failed のあと手順を読み直せなければ、外さず「分からない」に残す", (acts(), hsk_of("99990005"), (PEND / "99990005.json").exists()),
      ([("approve", "99990005")], ("unknown", False), True))
for pid, out, want_verify, label in (("99990061", {"result": "timeout", "why": "時間切れ"}, True, "時間切れ（終わりを確かめるまで次を当てない）"),
                                     ("99990062", {"result": "changed", "why": "承認待ちの記録が変わっていました"}, False, "記録が変わっていた"),
                                     ("99990063", {"result": "error", "why": "docker を動かせません"}, False, "動かせない")):
    record(pid, "g6", at=time.time())
    fresh_day()
    FAIL["reject"] = out
    SCALLS.clear()
    asyncio.run(s.hermes._learn_skills())
    FAIL["reject"] = None
    check(f"自動: 外す台本が {label} なら、「分からない」に残す", (acts(), [st for st, u, note in entry_of(pid)], hsk_of(pid), (PEND / f"{pid}.json").exists()),
          ([("approve", pid), ("reject", pid)], ["unknown"], ("unknown", want_verify), True))
    (PEND / f"{pid}.json").unlink()
    RUNNING["v"] = False
    asyncio.run(s.hermes._learn_skills())  # 終わりを確かめる（時間切れのあと）
rec9 = {"summary": "台本の答え"}
check("台本が「dropped」のような答えを返しても、外した扱いにはしない（外したと決めるのは本体だけ。画面の「採る」の failed も「分からない」のまま）",
      (s.hermes._hsk_finish("99999998", "v", rec9, b"{}", {"result": "dropped", "dropped": True})["reason"],
       s.hermes._hsk_finish("99999997", "v", rec9, b"{}", {"result": "failed", "why": MATCH})["reason"],
       s.hermes._hsk_finish("99999996", "v", rec9, b"{}", {"result": "applied"}, True)["ok"], hsk_of("99999996")[0]),
      ("unknown", "unknown", True, "applied"))
s.hermes.hermes_skills.run_script = real_skills_run
fresh_day()

# ---------- 2b. コンテナの中の台本の入口（Hermes の部品を偽物にして、台本そのものを動かす） ----------
import subprocess  # noqa: E402
FAKE = Path(TMP) / "fake-hermes"
(FAKE / "tools").mkdir(parents=True)
(FAKE / "tools" / "__init__.py").write_text("")
(FAKE / "tools" / "skill_manager_tool.py").write_text("""import os
from pathlib import Path
ROOT = Path(os.environ["VL_TEST_SKILLS"])
def _skills_dir():
    return ROOT
def _read_frontmatter_name(skill_md):
    # Hermes と同じ: utf-8-sig で読み、parse_frontmatter の name。読めなければ None（読めないことを試験で起こせる）
    if os.environ.get("VL_TEST_READFAIL"):
        return None
    from agent.skill_utils import parse_frontmatter
    try:
        fm, _ = parse_frontmatter(Path(skill_md).read_text(encoding="utf-8-sig", errors="replace")[:4000])
    except OSError:
        return None
    n = fm.get("name")
    return n[:64] if isinstance(n, str) else None
def _find_skill(name):
    for p in ROOT.rglob("SKILL.md"):  # フォルダの名前でも、別名（frontmatter の name）でも見つかる（Hermes と同じ）
        if p.parent.is_symlink():
            continue
        if p.parent.name == name or _read_frontmatter_name(p) == name:
            return {"path": p.parent}
    return {"path": Path(os.environ["VL_TEST_OUTSIDE"])} if name == "outside" else None
def _resolve_skill_dir(name, category=None):
    return ROOT / (category or "") / name
import contextlib, fcntl, json
_held = set()
@contextlib.contextmanager
def _one(p):
    if p in _held:  # 同じ処理の中では入れ子に取れる（Hermes と同じ）
        yield
        return
    p.parent.mkdir(exist_ok=True)
    with open(p, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        _held.add(p)
        try:
            yield
        finally:
            _held.discard(p)
            fcntl.flock(f, fcntl.LOCK_UN)
def _skill_mutation_locks(names):
    stack = contextlib.ExitStack()
    for n in sorted(set(names)):
        stack.enter_context(_one(ROOT / ".locks" / (n + ".lock")))
    return stack
def apply_skill_pending(payload):
    ops = payload["operations"] if payload.get("action") == "batch" else [payload]
    with _skill_mutation_locks([op["name"] for op in ops]):
        for op in ops:
            f = _find_skill(op["name"])
            d = f["path"] if f else _resolve_skill_dir(op["name"], op.get("category"))
            Path(d).mkdir(parents=True, exist_ok=True)
            ns = op.get("new_string", "")
            (Path(d) / "SKILL.md").write_text(ns if ns.startswith("---") else "当てた: " + ns)
    return json.dumps({"success": True})
if os.environ.get("VL_TEST_NOLOCK"):  # 手順の鍵が無い版の Hermes
    del _skill_mutation_locks
if os.environ.get("VL_TEST_NOFRONT"):  # 別名を読む関数が無い版の Hermes（別名では探さない。フォルダの名前だけ）
    def _find_skill(name):
        for p in ROOT.rglob("SKILL.md"):
            if p.parent.name == name and not p.parent.is_symlink():
                return {"path": p.parent}
        return None
    del _read_frontmatter_name
""")
(FAKE / "agent").mkdir(exist_ok=True)
(FAKE / "agent" / "__init__.py").write_text("")
(FAKE / "agent" / "skill_utils.py").write_text("""import re
import os
def parse_frontmatter(content):
    # Hermes と同じ形: 頭の BOM を外し、--- の間を読む（YAML の代わりに、引用符で囲んだキー・値を外す 1 行ずつの読み方）
    if os.environ.get("VL_TEST_PARSEFAIL"):
        raise RuntimeError("読めない")
    content = content.removeprefix("\\ufeff")
    m = re.search(r"\\n---\\s*\\n", content[3:]) if content.startswith("---") else None
    if not m:
        return {}, content
    fm = {}
    for line in content[3:m.start() + 3].strip().split("\\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip().strip("'\\"")] = v.strip().strip("'\\"")
    return fm, content[m.end() + 3:]
""")
(FAKE / "tools" / "write_approval.py").write_text("""import os
from pathlib import Path
SKILLS = "skills"
def _pending_path(sub, pid):
    return Path(os.environ["VL_TEST_PENDING"]) / (pid + ".json")
def discard_pending(sub, pid):
    p = _pending_path(sub, pid)
    if p.exists():
        p.unlink()
        return True
    return False
def skill_pending_diff(rec):
    return "差分"
""")
SK2 = Path(TMP) / "skills2"
(SK2 / "voice-loop" / "hub").mkdir(parents=True)
(SK2 / "voice-loop" / "hub" / "SKILL.md").write_text("前\n")


def script(mode, req, text=None, extra=None):
    p = subprocess.run([sys.executable, "-c", text or hl.SCRIPT, hl.CHECK_MARK if mode == "snap" else hl.MARK, mode], input=json.dumps(req).encode(),
                       capture_output=True, cwd=FAKE, env={**os.environ, "PYTHONPATH": str(FAKE), "VL_TEST_SKILLS": str(SK2), "VL_TEST_OUTSIDE": str(OUTSIDE), **(extra or {})},
                       timeout=60)
    return s.hermes.hermes_skills.read_result(p)


op = lambda name, **k: {"action": "batch", "operations": [{"action": "patch", "name": name, "old_string": "a", "new_string": "b", **k}]}
sn = script("snap", {"payload": op("hub")})
check("台本の入口: snap は置き場の根からの場所ごとに中身を返す・作る手順は exists が False・置き場の外の手順は扱わない",
      (sn.get("result"), sorted(sn.get("skills", {})), script("snap", {"payload": op("new", category="voice-loop")}).get("skills"),
       script("snap", {"payload": op("outside")}).get("result")),
      ("snap", ["voice-loop/hub"], {"voice-loop/new": {"exists": False, "files": {}, "modes": {}}}, "unsupported"))
mk = {"action": "batch", "operations": [{"action": "create", "name": "made", "category": "voice-loop", "content": "x"},
                                         {"action": "write_file", "name": "made", "file_path": "references/a.md", "file_content": "y"}]}
check("台本の入口: 同じ書き足しの中で作る手順に、続けてカテゴリを省いて書いても、控える置き場は作る先の 1 つ", sorted(script("snap", {"payload": mk}).get("skills", {})), ["voice-loop/made"])
(SK2 / "voice-loop" / "hub" / "SKILL.md").write_text("後\n")
after2 = hl.snap_dir(str(SK2), "voice-loop/hub")
check("台本の入口: restore は指紋が違えば何も書かずに changed、合えば戻して restored",
      (script("restore", {"skills": {"voice-loop/hub": {"before": sn["skills"]["voice-loop/hub"], "expect": hl.digest(sn["skills"]["voice-loop/hub"])}}}).get("result"),
       (SK2 / "voice-loop" / "hub" / "SKILL.md").read_text(),
       script("restore", {"skills": {"voice-loop/hub": {"before": sn["skills"]["voice-loop/hub"], "expect": hl.digest(after2)}}}).get("result"),
       (SK2 / "voice-loop" / "hub" / "SKILL.md").read_text()),
      ("changed", "後\n", "restored", "前\n"))
# 戻す台本は、Hermes が手順を書き換えるときと同じ鍵を、指紋を照らしてから戻し終えるまで持つ（鍵を持つ Hermes の書き込みを上書きしない）
(SK2 / "voice-loop" / "hub" / "SKILL.md").write_text("後\n")
lock_before, lock_expect = sn["skills"]["voice-loop/hub"], hl.digest(hl.snap_dir(str(SK2), "voice-loop/hub"))
(SK2 / ".locks").mkdir(exist_ok=True)
lockf = open(SK2 / ".locks" / "hub.lock", "a")
import fcntl  # noqa: E402
fcntl.flock(lockf, fcntl.LOCK_EX)  # Hermes が手順を書き換えている
proc = subprocess.Popen([sys.executable, "-c", hl.SCRIPT, hl.MARK, "restore"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        cwd=FAKE, env={**os.environ, "PYTHONPATH": str(FAKE), "VL_TEST_SKILLS": str(SK2), "VL_TEST_OUTSIDE": str(OUTSIDE)})
proc.stdin.write(json.dumps({"skills": {"voice-loop/hub": {"before": lock_before, "expect": lock_expect}}}).encode())
proc.stdin.close()
time.sleep(1.0)  # 台本は鍵を待っている（鍵が無ければ、この間に戻してしまう）
(SK2 / "voice-loop" / "hub" / "SKILL.md").write_text("Hermes が鍵を持って書いた\n")
fcntl.flock(lockf, fcntl.LOCK_UN)
lockf.close()
pout = proc.stdout.read()
proc.wait(timeout=60)
proc.stderr.close()
lres = json.loads(next((l for l in reversed(pout.decode().splitlines()) if l.startswith("VLRESULT ")), "VLRESULT {}")[len("VLRESULT "):])
check("戻す: Hermes が手順を書き換えている間（同じ鍵）は待ち、照らしてから書いたものは上書きしない", (lres.get("result"), (SK2 / "voice-loop" / "hub" / "SKILL.md").read_text()),
      ("changed", "Hermes が鍵を持って書いた\n"))
PEND2 = Path(TMP) / "pending2"
PEND2.mkdir()


def apply_script(pid, payload, expect, hold=None, extra=None):
    """自動で当てる台本（APPLY_SCRIPT）を、偽の Hermes で動かす。hold は、台本が鍵を待っている間に Hermes として行うこと。"""
    raw = json.dumps({"id": pid, "payload": payload}).encode()
    (PEND2 / f"{pid}.json").write_bytes(raw)
    env = {**os.environ, "PYTHONPATH": str(FAKE), "VL_TEST_SKILLS": str(SK2), "VL_TEST_OUTSIDE": str(OUTSIDE), "VL_TEST_PENDING": str(PEND2),
           "VL_LEARN_EXPECT": json.dumps(expect), **(extra or {})}
    pr = subprocess.Popen([sys.executable, "-c", hl.APPLY_SCRIPT, s.hermes.hermes_skills.MARK, "approve", pid], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=FAKE, env=env)
    pr.stdin.write(raw)
    pr.stdin.close()
    if hold:
        hold()
    so = pr.stdout.read()
    pr.wait(timeout=60)
    pr.stderr.close()
    return json.loads(next((l for l in reversed(so.decode().splitlines()) if l.startswith("VLRESULT ")), "VLRESULT {}")[len("VLRESULT "):])


(SK2 / "voice-loop" / "ap").mkdir(parents=True, exist_ok=True)
(SK2 / "voice-loop" / "ap" / "SKILL.md").write_text("前\n")
pa = {"action": "batch", "operations": [{"action": "patch", "name": "ap", "old_string": "前", "new_string": "後"}]}
e0 = {"voice-loop/ap": hl.digest(hl.snap_dir(str(SK2), "voice-loop/ap"))}
a1 = apply_script("apapap01", pa, e0)
check("自動で当てる台本: 控えの指紋と同じなら当て、承認待ちから消し、鍵を持ったまま読んだ当てた直後の指紋を返す",
      (a1.get("result"), a1.get("after") == {"voice-loop/ap": hl.digest(hl.snap_dir(str(SK2), "voice-loop/ap"))}, (SK2 / "voice-loop" / "ap" / "SKILL.md").read_text(),
       (PEND2 / "apapap01.json").exists()), ("applied", True, "当てた: 後", False))
(SK2 / "voice-loop" / "ap" / "SKILL.md").write_text("前\n")
a2 = apply_script("apapap02", pa, {"voice-loop/ap": "0" * 64})
check("自動で当てる台本: 控えの指紋と違えば当てない（承認待ちに残す）", (a2.get("result"), (SK2 / "voice-loop" / "ap" / "SKILL.md").read_text(), (PEND2 / "apapap02.json").exists()),
      ("changed", "前\n", True))
lk = open(SK2 / ".locks" / "ap.lock", "a")
fcntl.flock(lk, fcntl.LOCK_EX)  # Hermes が手順 ap を書き換えている


def hermes_edits():
    time.sleep(1.0)  # 台本は鍵を待っている（鍵が無ければ、この間に確かめて当ててしまう）
    (SK2 / "voice-loop" / "ap").joinpath("SKILL.md").write_text("Hermes が鍵を持って書いた\n")
    fcntl.flock(lk, fcntl.LOCK_UN)
    lk.close()


a3 = apply_script("apapap03", pa, e0, hermes_edits)
check("自動で当てる台本: Hermes が手順を書き換えている間（同じ鍵）は待ち、その間に変わっていたら当てない（控えのあとの書き込みを、当てた中身に混ぜない）",
      (a3.get("result"), (SK2 / "voice-loop" / "ap" / "SKILL.md").read_text(), (PEND2 / "apapap03.json").exists()),
      ("changed", "Hermes が鍵を持って書いた\n", True))
(SK2 / "voice-loop" / "ap" / "SKILL.md").write_text("前\n")
check("自動で当てる: 手順の鍵が無い版の Hermes では、控えを取る段で断り（今日の枠を使わない）、当てる台本でも当てない",
      (script("snap", {"payload": pa}, extra={"VL_TEST_NOLOCK": "1"}).get("result"), apply_script("apapap04", pa, e0, extra={"VL_TEST_NOLOCK": "1"}).get("result"),
       (SK2 / "voice-loop" / "ap" / "SKILL.md").read_text()), ("unsupported", "unsupported", "前\n"))
(SK2 / "voice-loop" / "fmhub").mkdir(parents=True, exist_ok=True)
(SK2 / "voice-loop" / "fmhub" / "SKILL.md").write_text("---\nname: alias-fm\n---\n本文\n")
(SK2 / "voice-loop" / "same").mkdir(parents=True, exist_ok=True)
(SK2 / "voice-loop" / "same" / "SKILL.md").write_text("---\nname: same\n---\n本文\n")
check("自動で当てる: 別名（frontmatter の名前）がフォルダの名前と違う手順は、どちらの名前で頼まれても扱わない（鍵がずれる）。同じなら扱う",
      (script("snap", {"payload": op("alias-fm")}).get("result"), script("snap", {"payload": op("fmhub")}).get("result"), script("snap", {"payload": op("same")}).get("result")),
      ("unsupported", "unsupported", "snap"))
(SK2 / "voice-loop" / "bomq").mkdir(parents=True, exist_ok=True)
(SK2 / "voice-loop" / "bomq" / "SKILL.md").write_text("\ufeff---\nname: bom-alias\n---\n本文\n")
(SK2 / "voice-loop" / "quoted").mkdir(parents=True, exist_ok=True)
(SK2 / "voice-loop" / "quoted" / "SKILL.md").write_text('---\n"name": "q-alias"\n---\n本文\n')
check("自動で当てる: 別名は Hermes 自身の読み方で読む（BOM 付き・引用符で囲んだキーでも見落とさない）。読み方の無い版の Hermes では扱わない",
      (script("snap", {"payload": op("bomq")}).get("result"), script("snap", {"payload": op("quoted")}).get("result"),
       script("snap", {"payload": op("same")}, extra={"VL_TEST_NOFRONT": "1"}).get("result")), ("unsupported", "unsupported", "unsupported"))
(SK2 / "voice-loop" / "crlf").mkdir(parents=True, exist_ok=True)
(SK2 / "voice-loop" / "crlf" / "SKILL.md").write_bytes("---\rname: cr-alias\r---\r本文\r".encode())
(SK2 / "voice-loop" / "crlf2").mkdir(parents=True, exist_ok=True)
crlf2 = "---\r\n" + "a: b\r\n" * 700 + "name: crlf-alias\r\n---\r\n本文\r\n"  # 名前の行は、CRLF のままなら 4000 字の外、LF にそろえると内
assert crlf2.index("name:") > 4000 and crlf2.replace("\r\n", "\n").index("---\n本文") < 4000
(SK2 / "voice-loop" / "crlf2" / "SKILL.md").write_bytes(crlf2.encode())
check("自動で当てる: 改行が CR・CRLF の SKILL.md も、Hermes と同じく LF にそろえてから読む（別名を見落とさない・4000 字の境界も同じ）",
      (script("snap", {"payload": op("crlf")}).get("result"), script("snap", {"payload": op("crlf2")}).get("result")), ("unsupported", "unsupported"))
check("自動で当てる: 別名は控えとして読んだ同じバイト列から読む（Hermes の読み取りが失敗して別名が無いように見えても、見落とさない）",
      (script("snap", {"payload": op("fmhub")}, extra={"VL_TEST_READFAIL": "1"}).get("result"), script("snap", {"payload": op("bomq")}, extra={"VL_TEST_READFAIL": "1"}).get("result")),
      ("unsupported", "unsupported"))
(SK2 / "voice-loop" / "ap" / "SKILL.md").write_text("前\n")
pal = {"action": "batch", "operations": [{"action": "patch", "name": "ap", "old_string": "前", "new_string": "---\nname: newalias\n---\n"}]}
a5 = apply_script("apapap05", pal, e0)
check("自動で当てる台本: 当てて別名ができたら、当てた直後の指紋を返さない（戻せない）", (a5.get("result"), a5.get("after")), ("applied", None))
fm_before = hl.snap_dir(str(SK2), "voice-loop/fmhub")
(SK2 / "voice-loop" / "fmhub" / "SKILL.md").write_text("---\nname: alias-fm\n---\n当てたあと\n")
fm_expect = hl.digest(hl.snap_dir(str(SK2), "voice-loop/fmhub"))
lk2 = open(SK2 / ".locks" / "alias-fm.lock", "a")
fcntl.flock(lk2, fcntl.LOCK_EX)  # Hermes が別名で手順を書き換えている
proc = subprocess.Popen([sys.executable, "-c", hl.SCRIPT, hl.MARK, "restore"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        cwd=FAKE, env={**os.environ, "PYTHONPATH": str(FAKE), "VL_TEST_SKILLS": str(SK2), "VL_TEST_OUTSIDE": str(OUTSIDE)})
proc.stdin.write(json.dumps({"skills": {"voice-loop/fmhub": {"before": fm_before, "expect": fm_expect}}}).encode())
proc.stdin.close()
time.sleep(1.0)
(SK2 / "voice-loop" / "fmhub" / "SKILL.md").write_text("---\nname: alias-fm\n---\nHermes が別名で書いた\n")
fcntl.flock(lk2, fcntl.LOCK_UN)
lk2.close()
fo = proc.stdout.read()
proc.wait(timeout=60)
proc.stderr.close()
fres = json.loads(next((l for l in reversed(fo.decode().splitlines()) if l.startswith("VLRESULT ")), "VLRESULT {}")[len("VLRESULT "):])
check("戻す: 別名を読む関数があるのに別名を読めないときは、鍵を決められないので戻さない（フォルダの名前の鍵だけに倒さない）",
      (script("restore", {"skills": {"voice-loop/fmhub": {"before": fm_before, "expect": hl.digest(hl.snap_dir(str(SK2), "voice-loop/fmhub"))}}},
              extra={"VL_TEST_PARSEFAIL": "1"}).get("result"), (SK2 / "voice-loop" / "fmhub" / "SKILL.md").read_text()),
      ("failed", "---\nname: alias-fm\n---\nHermes が別名で書いた\n"))
check("戻す: フォルダの名前と別名の両方の鍵を取る（Hermes が別名で書いている間も待ち、上書きしない）",
      (fres.get("result"), (SK2 / "voice-loop" / "fmhub" / "SKILL.md").read_text()), ("changed", "---\nname: alias-fm\n---\nHermes が別名で書いた\n"))
(SK2 / "voice-loop" / "par").mkdir(parents=True, exist_ok=True)
(SK2 / "voice-loop" / "par" / "SKILL.md").write_text("親\n")
(SK2 / "voice-loop" / "par" / "kid").mkdir(parents=True, exist_ok=True)
(SK2 / "voice-loop" / "par" / "kid" / "SKILL.md").write_text("子\n")
pk = {"action": "batch", "operations": [{"action": "patch", "name": n, "old_string": "a", "new_string": "b"} for n in ("par", "kid", "hub")]}
check("自動で当てる: 1 つの書き足しが親と子の関係にある手順のフォルダを両方触るときは、自動では当てない（二度戻して途中で止まらないように）",
      (script("snap", {"payload": pk}).get("result"), script("snap", {"payload": {**pk, "operations": pk["operations"][1:2]}}).get("result")), ("unsupported", "snap"))
check("自動で当てる: 親の手順だけでも、中に別の手順（子の SKILL.md）を含むなら自動では当てない（控える範囲と、取る鍵の範囲がずれる）",
      script("snap", {"payload": {**pk, "operations": pk["operations"][:1]}}).get("result"), "unsupported")
(SK2 / "voice-loop" / "par-x").mkdir(parents=True, exist_ok=True)
(SK2 / "voice-loop" / "par-x" / "SKILL.md").write_text("間\n")
pk2 = {"action": "batch", "operations": [{"action": "patch", "name": n, "old_string": "a", "new_string": "b"} for n in ("par", "par-x", "kid")]}
check("自動で当てる: 親と子の間に、並べると間に入る別の手順（par-x）があっても、親子を見逃さない", script("snap", {"payload": pk2}).get("result"), "unsupported")
for i in range(hl.SNAP_BATCH_SKILLS_MAX + 1):
    (SK2 / "many" / f"m{i:02d}").mkdir(parents=True, exist_ok=True)
    (SK2 / "many" / f"m{i:02d}" / "SKILL.md").write_text("小さい\n")
manyb = {"action": "batch", "operations": [{"action": "patch", "name": f"m{i:02d}", "old_string": "a", "new_string": "b"} for i in range(hl.SNAP_BATCH_SKILLS_MAX + 1)]}
small = hl.SCRIPT.replace(f"SNAP_BATCH_TOTAL_MAX = {hl.SNAP_BATCH_TOTAL_MAX}", "SNAP_BATCH_TOTAL_MAX = 30")
twob = {"action": "batch", "operations": [{"action": "patch", "name": n, "old_string": "a", "new_string": "b"} for n in ("m00", "m01")]}
check("台本の入口: 書き足し全体で触る手順が多すぎる・控えの合計が大きすぎるときは、自動では当てない（unsupported）",
      (script("snap", {"payload": manyb}).get("result"), script("snap", {"payload": twob}, small).get("result"), script("snap", {"payload": twob}).get("result")),
      ("unsupported", "unsupported", "snap"))
(SK2 / "voice-loop" / "hub" / "SKILL.md").write_text("後\n")
moved = Path(TMP) / "moved-hub"
held = hl._ident(str(SK2), ["voice-loop", "hub"])
dfd = hl._open_parent(str(SK2), ["voice-loop", "hub", "SKILL.md"])
os.rename(SK2 / "voice-loop" / "hub", moved)  # 開いたあとに、手順のフォルダを置き場の外へ移す
check("戻す: 開いたあとに手順のフォルダを移されたら、書かずに断る", raises(lambda: hl._same(str(SK2), ["voice-loop", "hub"], held, dfd, ["voice-loop", "hub", "SKILL.md"])), True)
os.close(dfd)
os.rename(moved, SK2 / "voice-loop" / "hub")

# ---------- 3. 戻す ----------
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    undo = lambda eid: c.post("/api/hermes/learned/undo", json={"id": eid}).json()
    LCALLS.clear()
    r1 = undo(first["id"])
    sent = [r for m, r in LCALLS if m == "restore"]
    check("戻す: 台本に前の中身と当てた直後の指紋を渡し、戻したら印を付ける",
          (r1, sent[0]["skills"]["hub"]["before"]["files"]["SKILL.md"], sent[0]["skills"]["hub"]["expect"] == det["expect"]["hub"],
           [e["state"] for e in learned()["entries"] if e["id"] == first["id"]]),
          ({"ok": True}, base64.b64encode("前".encode()).decode(), True, ["undone"]))
    check("戻す: 二度は戻さない", undo(first["id"]), {"ok": False, "reason": "done"})
    second = [e for e in learned()["entries"] if e["pid"] == "aaaa0001"][0]
    LOUT["restore"] = {"result": "changed", "why": "当てたあとに手順が変わっています"}
    check("戻す: 当てた直後から変わっていれば戻さず、そう書く（戻すはまだ押せる）",
          (undo(second["id"]), [(e["state"], e["note"], e["can_undo"]) for e in learned()["entries"] if e["id"] == second["id"]]),
          ({"ok": False, "reason": "changed"}, [("applied", "当てたあとに手順が変わっているので、戻せませんでした", True)]))
    LOUT["restore"] = {"result": "timeout", "why": "時間切れ"}
    third = [e for e in learned()["entries"] if e["pid"] == "aaaa0002"][0]
    r3 = undo(third["id"])
    check("戻す: 分からない（時間切れ）ときは、戻せない印にし、前の処理の終わりを確かめるまで当てる・戻すを断る",
          (r3["reason"], [e["can_undo"] for e in learned()["entries"] if e["id"] == third["id"]], json.loads(s.hermes.HSK_FILE.read_text())["verify"]),
          ("unknown", [False], True))
    RUNNING["v"] = True
    fourth = [e for e in learned()["entries"] if e["pid"] == "aaaa0003"][0]
    check("戻す: 前の処理がまだ動いている間は断る", undo(fourth["id"])["reason"], "busy")
    RUNNING["v"] = False
    del LOUT["restore"]
    check("戻す: 無い跡は断る", undo("lnone"), {"ok": False, "reason": "gone"})
    check("中身: 手順は差分を返す", c.post("/api/hermes/learned/detail", json={"id": fourth["id"]}).json(), {"ok": True, "diff": "差分 aaaa0003"})
    check("一覧: 新しい順", [e["at"] for e in learned()["entries"]] == sorted((e["at"] for e in learned()["entries"]), reverse=True), True)
    check("画面の口: コンテナからは使えない（一覧・中身・戻す）",
          [c.request(m, u, json={"id": first["id"]} if m == "POST" else None, headers={"Origin": "http://127.0.0.1:8790"},
                     ).status_code for m, u in [("GET", "/api/hermes/learned"), ("POST", "/api/hermes/learned/detail"), ("POST", "/api/hermes/learned/undo")]] ==
          [200, 200, 200] and __import__("origin_guard").DOCKER_ALLOWED.isdisjoint({("GET", "/api/hermes/learned"), ("POST", "/api/hermes/learned/detail"), ("POST", "/api/hermes/learned/undo")}),
          True)

st = json.loads(s.hermes.HLEARN_FILE.read_text())
old = [e for e in st["entries"] if e.get("kind") == "skill" and e.get("can_undo")][0]
for e in st["entries"]:
    if e["id"] == old["id"]:
        e["at"] -= 400 * 86400  # 400 日前の跡にする
s.hermes.HLEARN_FILE.write_text(json.dumps(st))
s.hermes._hlearn_set("lnone")  # 書き直しのきっかけ
kept = [e for e in learned()["entries"] if e["id"] == old["id"]]
odet = json.loads((s.hermes.HLEARN_DIR / f"{old['id']}.json").read_text())
check("跡: 古い跡も消さない（変わった跡は全部残す）。90 日を過ぎたら戻すための控えだけを片づけ、戻せない印にする（差分は残す）",
      (len(kept), kept[0]["can_undo"] if kept else None, "before" in odet, bool(odet.get("diff"))), (1, False, False, True))
st = json.loads(s.hermes.HLEARN_FILE.read_text())
nu = s.hermes._hlearn_new({"kind": "skill", "pid": "zz", "summary": "時間切れ", "state": "unknown", "can_undo": False}, {"before": {"x": 1}, "expect": "e", "diff": "差分"})
nu["at"] -= 100 * 86400
st["entries"].append(nu)
s.hermes.HLEARN_FILE.write_text(json.dumps(st))
s.hermes._hlearn_set("lnone")
ndet = json.loads((s.hermes.HLEARN_DIR / f"{nu['id']}.json").read_text())
check("跡: 戻せない跡（時間切れ・当てたあとを読めなかった）も、90 日を過ぎたら当てる前の中身の控えを外す（差分は残す）",
      ("before" in ndet, "expect" in ndet, ndet.get("diff"), [e.get("pruned") for e in json.loads(s.hermes.HLEARN_FILE.read_text())["entries"] if e["id"] == nu["id"]]),
      (False, False, "差分", [True]))

# ---------- 4. 記憶 ----------
MEM = HOME / "memories"
D = s.hermes.hermes_memory.DELIM
(MEM / "USER.md").write_text(D.join(["名前は山田", "朝は 7 時に起きる"]))
(MEM / "MEMORY.md").write_text("週次定例は情報交換の場")
MCALLS = []


def fake_mem_run(container, op, req=None, wait=90, env=None):
    if op == "limits":
        return {"result": "limits", "limits": {"user": 1375, "memory": 2200}}
    MCALLS.append((op, dict(req or {})))
    path = MEM / s.hermes.hermes_memory.TARGETS[req["target"]]
    items = [x for x in path.read_text().split(D) if x]
    if op in ("remove", "edit"):
        if req["entry"] not in items:
            return {"result": "changed"}
        items = [req["content"] if x == req["entry"] else x for x in items] if op == "edit" else [x for x in items if x != req["entry"]]
    elif op == "add":
        items.append(req["content"])
    path.write_text(D.join(items))
    return {"result": "applied"}


s.hermes.hermes_memory.run_script = fake_mem_run
s.hermes.hermes_memory.still_running = lambda container: False
count = lambda: len([e for e in learned()["entries"] if e["kind"] == "memory"])
before_n = count()
asyncio.run(s.hermes._learn_memory())
check("記憶: 初めて読んだときは跡に残さない（今の中身を覚えるだけ）", count(), before_n)
(MEM / "USER.md").write_text(D.join(["名前は山田", "返事は短く"]))
asyncio.run(s.hermes._learn_memory())
mem = [e for e in learned()["entries"] if e["kind"] == "memory"][0]
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    md = c.post("/api/hermes/learned/detail", json={"id": mem["id"]}).json()
check("記憶: 足された・消えた項目を跡に残す（数と中身）", (mem["target"], mem["added"], mem["removed"], md), ("user", 1, 1, {"ok": True, "added": ["返事は短く"], "removed": ["朝は 7 時に起きる"]}))
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    s.hermes.HMEM_SHOWN.clear()
    c.get("/api/hermes/memory")
    c.post("/api/hermes/memory/add", json={"target": "memory", "content": "画面で足した"})
check("記憶: 画面で書いたものは、Hermes の学びとして跡に残さない", count(), before_n + 1)
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    MCALLS.clear()
    ru = c.post("/api/hermes/learned/undo", json={"id": mem["id"]}).json()
check("記憶の戻す: 足された項目を消し、消えた項目を足し直す。戻したら印。戻したことは跡にしない",
      (ru, [(op, r.get("entry") or r.get("content")) for op, r in MCALLS], [x for x in (MEM / "USER.md").read_text().split(D) if x],
       [e["state"] for e in learned()["entries"] if e["id"] == mem["id"]], count()),
      ({"ok": True, "skipped": 0}, [("remove", "返事は短く"), ("add", "朝は 7 時に起きる")], ["名前は山田", "朝は 7 時に起きる"], ["undone"], before_n + 1))
(MEM / "USER.md").write_text(D.join(["名前は山田", "朝は 7 時に起きる", "新しい項目"]))
asyncio.run(s.hermes._learn_memory())
mem2 = [e for e in learned()["entries"] if e["kind"] == "memory" and e["state"] == "applied"][0]
(MEM / "USER.md").write_text(D.join(["名前は山田", "朝は 7 時に起きる"]))  # Hermes がもう消した
asyncio.run(s.hermes._learn_memory())  # Hermes が消したことも跡に残る
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    MCALLS.clear()
    ru2 = c.post("/api/hermes/learned/undo", json={"id": mem2["id"]}).json()
check("記憶の戻す: もう変わっていた項目は飛ばす（書かない）", (ru2, MCALLS), ({"ok": True, "skipped": 1}, []))

# 画面で書く前に、Hermes が別の記憶のファイル（と同じファイル）を書き換えていた: Hermes の変化は跡に残し、画面で書いた項目だけを外す
asyncio.run(s.hermes._learn_memory())
n0 = count()
(MEM / "USER.md").write_text(D.join(["名前は山田", "朝は 7 時に起きる", "Hermes が足した"]))
(MEM / "MEMORY.md").write_text(D.join([x for x in (MEM / "MEMORY.md").read_text().split(D) if x] + ["Hermes も同じファイルに足した"]))
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    s.hermes.HMEM_SHOWN.clear()
    c.get("/api/hermes/memory")
    c.post("/api/hermes/memory/add", json={"target": "memory", "content": "画面でもう一つ"})
mems = [e for e in learned()["entries"] if e["kind"] == "memory"][: count() - n0]
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    dets = {e["target"]: c.post("/api/hermes/learned/detail", json={"id": e["id"]}).json() for e in mems}
check("記憶: 画面で書く前に Hermes が書き換えていた分は跡に残し、画面で書いた項目だけを外す（同じファイルでも）",
      (count() - n0, dets.get("user", {}).get("added"), dets.get("memory", {}).get("added")), (2, ["Hermes が足した"], ["Hermes も同じファイルに足した"]))
# 跡を書けなければ、比べるもとも進めない（次に読んだときに残し直す）
n1 = count()
(MEM / "USER.md").write_text(D.join(["名前は山田", "朝は 7 時に起きる", "Hermes が足した", "書けなかった回に足した"]))
real_write = s.hermes.core.write_json


def write_fails(path, data):
    if Path(path).parent == s.hermes.HLEARN_DIR and Path(path).name != "state.json":
        raise OSError("控えを書けません")
    return real_write(path, data)


s.hermes.core.write_json = write_fails
try:
    asyncio.run(s.hermes._learn_memory())
finally:
    s.hermes.core.write_json = real_write
mid = count()
asyncio.run(s.hermes._learn_memory())
check("記憶: 跡を書けなかった回は比べるもとも進めず、次に読んだときにその変化を跡に残す",
      (mid - n1, count() - n1, [e["summary"] for e in learned()["entries"] if e["kind"] == "memory"][0]), (0, 1, "書けなかった回に足した"))

# 画面の操作が当たらなかった（古い画面から、Hermes がもう消した項目を消した）: Hermes が消したことは跡に残す
asyncio.run(s.hermes._learn_memory())
n3 = count()
items = [x for x in (MEM / "MEMORY.md").read_text().split(D) if x]
victim = items[0]
vv = s.hermes.hermes_memory.version("memory", victim)
s.hermes.HMEM_SHOWN[vv] = ("memory", victim)  # 古い画面に出ていた
(MEM / "MEMORY.md").write_text(D.join(items[1:]))  # Hermes が先に消した
rr = json.loads(asyncio.run(s.hermes._hmem_write("remove", {"target": "memory", "v": vv})).body)
lm = [e for e in learned()["entries"] if e["kind"] == "memory"][0]
check("記憶: 画面の操作が当たらなかったら、その項目を跡から外さない（Hermes が先に消したことは跡に残す）",
      (rr.get("reason"), count() - n3, lm["target"], lm["removed"], lm["summary"]), ("changed", 1, "memory", 1, victim[:120]))
# 戻すで、画面の口では書けない項目（長すぎる）を足し直せないときは、戻したことにしない
longx = "長" * (s.hermes.hermes_memory.TEXT_MAX + 1)
(MEM / "USER.md").write_text(D.join([x for x in (MEM / "USER.md").read_text().split(D) if x] + [longx]))
asyncio.run(s.hermes._learn_memory())
(MEM / "USER.md").write_text(D.join([x for x in (MEM / "USER.md").read_text().split(D) if x and x != longx]))
asyncio.run(s.hermes._learn_memory())
gone = [e for e in learned()["entries"] if e["kind"] == "memory" and e["removed"] == 1 and e["target"] == "user"][0]
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    ru3 = c.post("/api/hermes/learned/undo", json={"id": gone["id"]}).json()
check("記憶の戻す: 書けない項目（長すぎる）を足し直せないときは、戻したことにしない（一部を戻せなかった）",
      (ru3.get("reason"), [e["state"] for e in learned()["entries"] if e["id"] == gone["id"]]), ("partial", ["applied"]))
# 画面で書いたあとに跡を保存できなくても、外す項目は持ち続ける（次の見張りで本人の書き込みを学びにしない）
asyncio.run(s.hermes._learn_memory())
n4 = count()
real_save = s.hermes._hlearn_save


def save_fails(d):
    raise OSError("書けない")


def save_fails_after(n):
    """n 回目までは書け、そのあとは書けない（画面で書く前の読み取りは書け、書いたあとの跡を保存できない）"""
    calls = []

    def run(d):
        calls.append(1)
        if len(calls) > n:
            raise OSError("書けない")
        return real_save(d)
    return run


s.hermes._hlearn_save = save_fails_after(1)
try:
    r4 = json.loads(asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "保存できない間に画面で足した"})).body)
finally:
    s.hermes._hlearn_save = real_save
s.hermes._HMEM_MINE.clear()  # 本体を起動し直した（本体の中の控えは消え、ファイルの控えから読み戻す）
s.hermes._HMEM_MINE.update(s.hermes._load_hmem_mine())
asyncio.run(s.hermes._watch_memory())
check("記憶: 画面で書いたあとに跡を保存できなくても、外す項目はファイルにも控え、本体を起動し直したあとの見張りでも本人の書き込みを学びにしない",
      (r4.get("ok"), count() - n4, "保存できない間に画面で足した" in (MEM / "MEMORY.md").read_text(), s.hermes.HMEM_MINE_FILE.exists()), (True, 0, True, False))
# 画面で足した本文の改行が CRLF でも、読む側と同じく LF にそろえて照らす（本人の書き込みを学びにしない）
asyncio.run(s.hermes._learn_memory())
n6 = count()
r6 = json.loads(asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "改行\r\nのある本文"})).body)
asyncio.run(s.hermes._watch_memory())
check("記憶: 画面で足した本文の改行が CRLF でも、読む側と同じく LF にそろえて照らし、本人の書き込みを学びにしない", (r6.get("ok"), count() - n6), (True, 0))
# 外す項目の控えのファイルに書けなければ、画面からも記憶を書かない（起動し直すと本人の書き込みが学びになるため）
n7 = count()
before_mem = (MEM / "MEMORY.md").read_text()
real_wj = s.hermes.core.write_json


def mine_write_fails(path, data):
    if Path(path).name == "memory-mine.json":
        raise OSError("控えに書けない")
    return real_wj(path, data)


s.hermes.core.write_json = mine_write_fails
try:
    r7 = json.loads(asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "控えに書けない間に足そうとした"})).body)
finally:
    s.hermes.core.write_json = real_wj
check("記憶: 外す項目の控えのファイルに書けなければ、画面からも記憶を書かない（断る）",
      (r7.get("ok"), r7.get("reason"), (MEM / "MEMORY.md").read_text() == before_mem, s.hermes._maybe(s.hermes._HMEM_MINE.get("memory", {"maybe_added": []}), "added")), (False, "state", True, []))
# 結果が分からない（時間切れ・失敗）書き込み: 外す項目は持ち続ける（書いていた・遅れて書き終えた、のどちらでも本人の書き込みを学びにしない）
def uncertain_run(write: bool):
    def run(container, op, req=None, wait=90, env=None):
        if op != "limits" and write:
            fake_mem_run(container, op, req, wait, env)
        return {"result": "limits", "limits": {"user": 1375, "memory": 2200}} if op == "limits" else {"result": "failed", "why": "時間切れ"}
    return run


asyncio.run(s.hermes._learn_memory())
n8 = count()
s.hermes.hermes_memory.run_script = uncertain_run(True)
r8 = json.loads(asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "書いたあとに時間切れ"})).body)
s.hermes.hermes_memory.run_script = fake_mem_run
s.hermes.HMEM_FILE.unlink(missing_ok=True)  # 終わりを確かめた（次の書き込みを止めない）
asyncio.run(s.hermes._watch_memory())
check("記憶: 書いたあとに結果が分からなくなった（時間切れ）書き込みも、本人の書き込みとして外す", (r8.get("reason"), count() - n8, "書いたあとに時間切れ" in (MEM / "MEMORY.md").read_text()),
      ("unknown", 0, True))
s.hermes.hermes_memory.run_script = uncertain_run(False)
r9 = json.loads(asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "遅れて書き終える"})).body)
s.hermes.hermes_memory.run_script = fake_mem_run
s.hermes.HMEM_FILE.unlink(missing_ok=True)
asyncio.run(s.hermes._watch_memory())  # まだ現れない（台本は動いている）
kept = "遅れて書き終える" in s.hermes._maybe(s.hermes._HMEM_MINE.get("memory", {"maybe_added": []}), "added")
(MEM / "MEMORY.md").write_text(D.join([x for x in (MEM / "MEMORY.md").read_text().split(D) if x] + ["遅れて書き終える"]))  # 遅れて書き終えた
asyncio.run(s.hermes._watch_memory())
check("記憶: 時間切れのあとで遅れて書き終えた本人の書き込みも学びにしない（外す項目は、照らし合うまで持ち続ける）",
      (r9.get("reason"), kept, count() - n8, "遅れて書き終える" in s.hermes._maybe(s.hermes._HMEM_MINE.get("memory", {"maybe_added": []}), "added")), ("unknown", True, 0, False))
# 比べるもとがまだ無いとき（初めての読み取りの前）に結果の分からない書き込みをし、本体を起動し直し、遅れて書き終えた
st = json.loads(s.hermes.HLEARN_FILE.read_text())
st["memory"] = None
s.hermes.HLEARN_FILE.write_text(json.dumps(st))
n9 = count()
s.hermes.hermes_memory.run_script = uncertain_run(False)
asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "比べるもと無しで遅れて書き終える"}))
s.hermes.hermes_memory.run_script = fake_mem_run
s.hermes.HMEM_FILE.unlink(missing_ok=True)
asyncio.run(s.hermes._watch_memory())  # 初めての読み取り（まだ現れない）
s.hermes._HMEM_MINE.clear()  # 本体を起動し直した
s.hermes._HMEM_MINE.update(s.hermes._load_hmem_mine())
(MEM / "MEMORY.md").write_text(D.join([x for x in (MEM / "MEMORY.md").read_text().split(D) if x] + ["比べるもと無しで遅れて書き終える"]))
asyncio.run(s.hermes._watch_memory())
check("記憶: 比べるもとがまだ無いときの、結果の分からない本人の書き込みも、起動し直して遅れて書き終えても学びにしない", count() - n9, 0)
asyncio.run(s.hermes._learn_memory())
n10 = count()
s.hermes._HMEM_MINE["memory"] = {"sure_added": [], "sure_removed": [], "maybe_added": [["期限のあとに Hermes が足した", time.time() - s.hermes.HMEM_MINE_KEEP - 1]], "maybe_removed": []}
(MEM / "MEMORY.md").write_text(D.join([x for x in (MEM / "MEMORY.md").read_text().split(D) if x] + ["期限のあとに Hermes が足した"]))
asyncio.run(s.hermes._watch_memory())
check("記憶: 期限を過ぎた外す項目は差分を見る前に手放す（期限のあとに Hermes が同じ中身を足した変化を、跡から落とさない）",
      (count() - n10, [e["summary"] for e in learned()["entries"] if e["kind"] == "memory"][0]), (1, "期限のあとに Hermes が足した"))
s.hermes._HMEM_MINE["memory"] = {"sure_added": [], "sure_removed": [], "maybe_added": [["ずっと現れない", time.time() - s.hermes.HMEM_MINE_KEEP - 1]], "maybe_removed": []}
asyncio.run(s.hermes._watch_memory())
check("記憶: 期限（HMEM_MINE_KEEP）を過ぎても現れない外す項目は手放す", "memory" in s.hermes._HMEM_MINE, False)
# 期限は項目ごと: 結果の分からない書き込みのあとに、同じ記憶のファイルへ別の書き込みをしても、前の項目の期限を延ばさない
asyncio.run(s.hermes._learn_memory())
n19 = count()
real_time = time.time
clock = [real_time() - s.hermes.HMEM_MINE_KEEP - 100]  # 時刻は過去から今へ進める（跡の新しい順を崩さない）
time.time = lambda: clock[0]
try:
    s.hermes.hermes_memory.run_script = uncertain_run(False)
    asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "期限は項目ごと"}))
    s.hermes.hermes_memory.run_script = fake_mem_run
    s.hermes.HMEM_FILE.unlink(missing_ok=True)
    clock[0] += s.hermes.HMEM_MINE_KEEP - 100
    r19 = json.loads(asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "あとから画面で足した"})).body)
    clock[0] = real_time()  # 前の書き込みから HMEM_MINE_KEEP + 100 秒あまり、あとの書き込みから 200 秒あまり
    (MEM / "MEMORY.md").write_text(D.join([x for x in (MEM / "MEMORY.md").read_text().split(D) if x] + ["期限は項目ごと"]))  # 期限のあとに Hermes が同じ中身を足した
    asyncio.run(s.hermes._watch_memory())
finally:
    time.time = real_time
check("記憶: 外す項目の期限は項目ごと（あとの書き込みで前の項目の期限を延ばさず、期限のあとに Hermes が同じ中身を足したことは跡に残す）",
      (r19.get("ok"), count() - n19, [e["summary"] for e in learned()["entries"] if e["kind"] == "memory"][0]), (True, 1, "期限は項目ごと"))
# 確かに書けた本人の書き込みは、比べるもとに取り込めるまで、期限を過ぎても外す（跡の状態を長く保存できなかった）
asyncio.run(s.hermes._learn_memory())
n11 = count()
s.hermes._hlearn_save = save_fails_after(1)
try:
    r11 = json.loads(asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "長く保存できない間に足した"})).body)
finally:
    s.hermes._hlearn_save = real_save
for side in ("added", "removed"):  # 期限を過ぎた
    for m in s.hermes._HMEM_MINE.get("memory", {}).get("maybe_" + side, []):
        m[1] = time.time() - s.hermes.HMEM_MINE_KEEP - 1
asyncio.run(s.hermes._watch_memory())
check("記憶: 確かに書けた本人の書き込みは、期限を過ぎても比べるもとに取り込めるまで外す（学びにしない）", (r11.get("ok"), count() - n11, "memory" in s.hermes._HMEM_MINE),
      (True, 0, False))
# 中身を変えずに保存した（足した・消したの両方に同じ項目）: 取り込めたら手放し、あとで Hermes が消したことは跡に残す
items = [x for x in (MEM / "MEMORY.md").read_text().split(D) if x]
same = items[0]
vs = s.hermes.hermes_memory.version("memory", same)
s.hermes.HMEM_SHOWN[vs] = ("memory", same)
n12 = count()
r12 = json.loads(asyncio.run(s.hermes._hmem_write("edit", {"target": "memory", "v": vs, "content": same})).body)
left = s.hermes._HMEM_MINE.get("memory")
(MEM / "MEMORY.md").write_text(D.join([x for x in (MEM / "MEMORY.md").read_text().split(D) if x and x != same]))  # Hermes が消した
asyncio.run(s.hermes._watch_memory())
check("記憶: 中身を変えずに保存しても外す項目を残さない（あとで Hermes が同じ項目を消したことは跡に残す）",
      (r12.get("ok"), left, count() - n12, [e["summary"] for e in learned()["entries"] if e["kind"] == "memory"][0]), (True, None, 1, same[:120]))
# Hermes が足した項目を、見張りが読む前に画面で中身を変えずに保存した: Hermes が足したことは跡に残す（画面で書く前に読み取る）
asyncio.run(s.hermes._learn_memory())
n13 = count()
(MEM / "MEMORY.md").write_text(D.join([x for x in (MEM / "MEMORY.md").read_text().split(D) if x] + ["見張りの前に Hermes が足した"]))
v13 = s.hermes.hermes_memory.version("memory", "見張りの前に Hermes が足した")
s.hermes.HMEM_SHOWN[v13] = ("memory", "見張りの前に Hermes が足した")
r13 = json.loads(asyncio.run(s.hermes._hmem_write("edit", {"target": "memory", "v": v13, "content": "見張りの前に Hermes が足した"})).body)
asyncio.run(s.hermes._watch_memory())
lm13 = [e for e in learned()["entries"] if e["kind"] == "memory"][0]
check("記憶: Hermes が足した項目を、見張りが読む前に画面で中身を変えずに保存しても、Hermes が足したことは跡に残す（戻せる）",
      (r13.get("ok"), count() - n13, lm13["summary"], lm13["added"], lm13["removed"], lm13["can_undo"]), (True, 1, "見張りの前に Hermes が足した", 1, 0, True))
# 同じく、見張りが読む前に画面で別の中身に直した: Hermes が足したことは跡に残し、画面で直したことは跡にしない
n14 = count()
(MEM / "MEMORY.md").write_text(D.join([x for x in (MEM / "MEMORY.md").read_text().split(D) if x] + ["Hermes がまた足した"]))
v14 = s.hermes.hermes_memory.version("memory", "Hermes がまた足した")
s.hermes.HMEM_SHOWN[v14] = ("memory", "Hermes がまた足した")
r14 = json.loads(asyncio.run(s.hermes._hmem_write("edit", {"target": "memory", "v": v14, "content": "画面で直した中身"})).body)
asyncio.run(s.hermes._watch_memory())
check("記憶: Hermes が足した項目を、見張りが読む前に画面で直しても、Hermes が足したことは跡に残し、画面で直したことは跡にしない",
      (r14.get("ok"), count() - n14, [e["summary"] for e in learned()["entries"] if e["kind"] == "memory"][0]), (True, 1, "Hermes がまた足した"))
# 書く前の読み取りで跡を保存できない・記憶を読めないときは、画面からも書かない
before15 = (MEM / "MEMORY.md").read_text()
s.hermes._hlearn_save = save_fails
try:
    r15 = json.loads(asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "書く前の跡を保存できない"})).body)
finally:
    s.hermes._hlearn_save = real_save
real_rt = s.hermes.hermes_memory.read_targets


def memory_unreadable(home):
    got = real_rt(home)
    got["memory"] = {"status": "unreadable", "entries": []}
    return got


s.hermes.hermes_memory.read_targets = memory_unreadable
try:
    r16 = json.loads(asyncio.run(s.hermes._hmem_write("add", {"target": "memory", "content": "書く前に読めない"})).body)
finally:
    s.hermes.hermes_memory.read_targets = real_rt
check("記憶: 書く前の読み取りで跡を保存できない・記憶を読めないときは、画面からも書かない（断る）",
      (r15.get("reason"), r16.get("reason"), (MEM / "MEMORY.md").read_text() == before15), ("state", "unreadable", True))
# 戻すも、戻す前に読み取る: Hermes が消した項目を、見張りが読む前に Hermes がまた足していたら、足したことを跡に残し、戻すは足し直さない（飛ばす）
asyncio.run(s.hermes._learn_memory())
(MEM / "USER.md").write_text(D.join([x for x in (MEM / "USER.md").read_text().split(D) if x] + ["消されてまた足される"]))
asyncio.run(s.hermes._learn_memory())
(MEM / "USER.md").write_text(D.join([x for x in (MEM / "USER.md").read_text().split(D) if x and x != "消されてまた足される"]))
asyncio.run(s.hermes._learn_memory())
gone17 = [e for e in learned()["entries"] if e["kind"] == "memory" and e["summary"] == "消されてまた足される" and e["removed"] == 1][0]
n17 = count()
(MEM / "USER.md").write_text(D.join([x for x in (MEM / "USER.md").read_text().split(D) if x] + ["消されてまた足される"]))  # 見張りの前に Hermes がまた足した
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    MCALLS.clear()
    ru17 = c.post("/api/hermes/learned/undo", json={"id": gone17["id"]}).json()
asyncio.run(s.hermes._watch_memory())
check("記憶の戻す: 戻す前に読み取り、見張りの前に Hermes がまた足した項目は跡に残し、戻すは飛ばす（書かない）",
      (ru17, MCALLS, count() - n17, [e["summary"] for e in learned()["entries"] if e["kind"] == "memory"][0]),
      ({"ok": True, "skipped": 1}, [], 1, "消されてまた足される"))
# 戻す前の読み取り: Hermes が足して（跡 1）消した（跡 2）項目を、見張りの前にまた足していたとき、跡 1 を戻しても、また足したことは跡に残す
asyncio.run(s.hermes._learn_memory())
(MEM / "USER.md").write_text(D.join([x for x in (MEM / "USER.md").read_text().split(D) if x] + ["足して消してまた足す"]))
asyncio.run(s.hermes._learn_memory())
first18 = [e for e in learned()["entries"] if e["kind"] == "memory" and e["summary"] == "足して消してまた足す" and e["added"] == 1][0]
(MEM / "USER.md").write_text(D.join([x for x in (MEM / "USER.md").read_text().split(D) if x and x != "足して消してまた足す"]))
asyncio.run(s.hermes._learn_memory())
(MEM / "USER.md").write_text(D.join([x for x in (MEM / "USER.md").read_text().split(D) if x] + ["足して消してまた足す"]))  # 見張りの前にまた足した
n18 = count()
with TestClient(s.app, base_url="http://127.0.0.1:8790") as c:
    MCALLS.clear()
    ru18 = c.post("/api/hermes/learned/undo", json={"id": first18["id"]}).json()
asyncio.run(s.hermes._watch_memory())
again18 = [e for e in learned()["entries"] if e["kind"] == "memory"][0]
check("記憶の戻す: 戻す前に読み取り、見張りの前に Hermes がまた足した項目は、戻すで消しても、また足したことを跡に残す",
      (ru18, [(op, r.get("entry")) for op, r in MCALLS], count() - n18, again18["summary"], again18["added"]),
      ({"ok": True, "skipped": 0}, [("remove", "足して消してまた足す")], 1, "足して消してまた足す", 1))
# 見張りの 1 回: 手順の承認待ちを読めなくても、記憶は見る
n5 = count()
real_pending = s.hermes.hermes_skills.pending_ids
s.hermes.hermes_skills.pending_ids = lambda home: None
(MEM / "USER.md").write_text(D.join([x for x in (MEM / "USER.md").read_text().split(D) if x] + ["手順を読めない間に足した"]))
try:
    asyncio.run(s.hermes._watch_once())
finally:
    s.hermes.hermes_skills.pending_ids = real_pending
check("見張り: 手順の承認待ちを読めなくても、記憶の変化は跡に残す", (count() - n5, [e["summary"] for e in learned()["entries"] if e["kind"] == "memory"][0]),
      (1, "手順を読めない間に足した"))
# 起動のとき: 当てている途中のまま残った跡は「分からない」・戻せないにする
st = json.loads(s.hermes.HLEARN_FILE.read_text())
st["entries"].append({"id": "lstuck00000a", "at": time.time(), "state": "applying", "note": "", "kind": "skill", "auto": True, "pid": "zzzz0001",
                      "summary": "途中で止まった", "places": ["voice-loop/hub"], "can_undo": False})
s.hermes.HLEARN_FILE.write_text(json.dumps(st))
s.hermes._recover_hermes_skills()
stuck = [e for e in learned()["entries"] if e["id"] == "lstuck00000a"]
check("起動: 当てている途中のまま残った学んだことの跡は「分からない」・戻せないにする", [(e["state"], e["can_undo"], "本体が止まりました" in e["note"]) for e in stuck],
      [("unknown", False, True)])
# 画面で書いている間（書いたあと、比べるもとを進める前）に見張りが読んでも、画面で書いたものを Hermes の学びにしない。
# 鍵を待たせるので、ほかの試験のあと（別のイベントループ）に置く
GATE, WROTE = threading.Event(), threading.Event()


def slow_mem_run(container, op, req=None, wait=90, env=None):
    out = fake_mem_run(container, op, req, wait, env)
    if op == "add" and (req or {}).get("content") == "画面で遅く足す":
        WROTE.set()
        GATE.wait(5)  # 書いたあと、答えを返すまでの間
    return out


asyncio.run(s.hermes._learn_memory())
n2 = count()
s.hermes.hermes_memory.run_script = slow_mem_run


async def overlap():
    w = asyncio.create_task(s.hermes._hmem_write("add", {"target": "memory", "content": "画面で遅く足す"}))
    await asyncio.to_thread(WROTE.wait, 5)
    watch = asyncio.create_task(s.hermes._watch_memory())
    await asyncio.sleep(0.3)
    GATE.set()
    await asyncio.gather(w, watch)


asyncio.run(overlap())
s.hermes.hermes_memory.run_script = fake_mem_run
check("記憶: 画面で書いている間に見張りが読んでも、画面で書いたものを Hermes の学びとして残さない（同じ鍵で待つ）",
      (count() - n2, "画面で遅く足す" in (MEM / "MEMORY.md").read_text()), (0, True))

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
