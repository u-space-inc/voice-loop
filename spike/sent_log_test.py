"""出ていく側の記録（hubctl sent）の試験。案件の Teams の控え（teams-sync-log.md）から当方の投稿を読む処理（scripts/sent_log.py）と、
本体の口（/api/hub/sent）・コンテナから使える口の範囲・hubctl sent を確かめる。
常駐の 8790・本物のコンテナ・本物の案件のフォルダには触れない（一時フォルダに偽の案件を作る）。hubctl の確かめには、この試験の中で
127.0.0.1:8792 に本体の写しを立てる。git は一時フォルダの中のリポジトリにだけ使う。
相手の位置から見ること: コンテナ（鍵あり）から使えるのは GET の読む口だけで、POST・鍵なしは断る。案件の名前は場所に使わない（「..」・絶対パス・
symlink の案件・symlink の控え・置き場所の外を指す控えは読まない）。控えの中の制御文字・向きを変える文字は空白にし、行の長さを切る。
大きすぎる控えは読まない。案件のリポジトリの設定（fsmonitor）を git に動かさせない。
使い方: uv run spike/sent_log_test.py"""
import datetime as dt
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
PROJ = TMP / "projects"
PROJ.mkdir()
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9",
                  VL_HERMES_HOME=f"{TMP}/hermes", VL_PROJECTS_DIR=str(PROJ))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import server as s  # noqa: E402
import sent_log as S  # noqa: E402
from origin_guard import problem  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results = []
JST = dt.timezone(dt.timedelta(hours=9))


def check(name, got, want):
    results.append((name, got == want, got, want))


def at(y, mo, d, H, M):
    return dt.datetime(y, mo, d, H, M, tzinfo=JST).timestamp()


def fresh():
    S._found.update(at=0.0, root=None, logs={})
    S._parsed.clear()


HEAD = "| 確認した日時 | スレッド | 最後の投稿（日時・発言者） | 開いたか | この回に見つけた新しい投稿 | 対応 |\n|---|---|---|---|---|---|\n"

# ---------- 控えの行を読む ----------
text = HEAD + "\n".join([
    "| 2026-11-14 11:33 | 先方の担当との個人チャット | 11/14 11:33・当方 | 開いた | 当方の返信 1 件を投稿した（請求の確認） | 記録した |",
    "| 2026-11-14 19:03 | 先方の担当との個人チャット | 11/14 11:33・当方 | 開いた | 先方の新しい投稿なし | ― |",  # 同じ投稿を末尾として見ただけ
    "| 2026-11-15 09:00 | 新しいテンプレについて | 11/15 08:40・先方 | 一覧だけ | 先方 1 件 | 返信は案まで |",  # 先方
    "| 2026-11-19 14:4x | 「配信の確認」の返信欄 | 配信: 11/19 14:58・当方 | 開いた | 当方が送信 | 控えを作成 |",
    "| 2026-11-19 18:00 | 一覧＋2 つ | 深夜帯: 11/17 22:54・当方／完全削除: 11/19 17:41・当方 | 開いた | 2 件 | ― |",  # 1 つの欄に 2 つ
    "| 2026-11-19 18:30 | 削除の件 | チャット生成: 11/2 14:36・山田さん、完全削除: 11/19 18:10・当方側 | 開いた | 1 件 | ― |",
    "| 2026-11-20 08:00 | 予約の件 | 11/18 07:00 以降・当方 | 開いた | 予約の投稿 | ― |",
    "| 2027-01-02 10:00 | 年をまたぐ件 | 12/31 23:50・当方 | 開いた | 年末の連絡 | ― |",
    "| 2026-02-28 10:00 | 日付の誤り | 2/30 10:00・当方 | 開いた | 読めない日付 | ― |",
    "| 見出しでない行 | x | 11/19 10:00・当方 | x | x | x |",
    "| 2026-11-19 20:00 | 制御\x1b[31m文字\u202eの件 | 11/19 19:59・当方 | 開いた | 本文\x07に\u2028改行と" + "長" * 400 + " | ― |",
])
rows = S.parse(text)
got = [(dt.datetime.fromtimestamp(r["at"], JST).strftime("%Y-%m-%d %H:%M"), r["label"]) for r in rows]
check("控え: 「最後の投稿」が当方の行だけを読み、同じ投稿の後の回の行は 1 つにまとめる",
      got[:2], [("2026-11-14 11:33", ""), ("2026-11-19 14:58", "配信")])
check("控え: 1 つの欄の「／」「、」で区切った複数の投稿・「当方側」・「以降・当方」も読む",
      sorted(g for g in got if g[0] in ("2026-11-17 22:54", "2026-11-19 17:41", "2026-11-19 18:10", "2026-11-18 07:00")),
      [("2026-11-17 22:54", "深夜帯"), ("2026-11-18 07:00", ""), ("2026-11-19 17:41", "完全削除"), ("2026-11-19 18:10", "完全削除")])
check("控え: 年は確かめた日の年で、月が先になるなら前の年（12/31 を 1/2 に確かめた）", ("2026-12-31 23:50", "") in got, True)
check("控え: 読めない日付・見出しでない行・先方の行は出さない",
      (any(g[0].startswith("2026-02") or g[0].startswith("2026-03") for g in got), any(g[0] == "2026-11-15 08:40" for g in got),
       any(g[0] == "2026-11-19 10:00" for g in got)), (False, False, False))
ctl = next(r for r in rows if r["at"] == at(2026, 11, 19, 19, 59))
check("控え: 制御文字・向きを変える文字は空白にし、1 行にして字数で切る",
      (any(ch in ctl["thread"] + ctl["what"] for ch in "\x1b\x07\u202e\u2028\n"), len(ctl["what"]) <= S.CLIP["what"], ctl["what"].endswith("…")),
      (False, True, True))
check("控えのファイルの名前: 日付・時刻・要約を読み、形の違う名前は飛ばす",
      [(c["at"], c["copy"]) for c in S.copies(["2026-11-13_1439_out_配信の完了と再送.md", "2026-11-13_1434_in_先方の質問.md",
                                                 "メモ.md", "2026-13-01_1000_out_月の誤り.md"])],
      [(at(2026, 11, 13, 14, 39), "配信の完了と再送")])


# ---------- 案件のフォルダから探す ----------
def write(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEAD + body, encoding="utf-8")


write(PROJ / "shop" / "doc/internal" / S.LOG_NAME, "| 2026-11-19 10:00 | 見積もりの件 | 11/19 09:58・当方 | 開いた | 見積もりを送った | ― |\n")
(PROJ / "shop/doc/internal/thread").mkdir()
for n in ("2026-11-19_0958_out_見積もりの送付.md", "2026-11-18_1200_out_日程の候補.md"):
    (PROJ / "shop/doc/internal/thread" / n).write_text("中身は読まない", encoding="utf-8")
write(PROJ / "blog" / "a/b/c/d" / S.LOG_NAME, "| 2026-11-19 10:00 | 深すぎる | 11/19 09:00・当方 | 開いた | x | ― |\n")  # 深さ 5
write(PROJ / "blog" / "node_modules/x" / S.LOG_NAME, "| 2026-11-19 10:00 | 依存 | 11/19 09:01・当方 | 開いた | x | ― |\n")
write(PROJ / "blog" / ".hidden" / S.LOG_NAME, "| 2026-11-19 10:00 | 隠し | 11/19 09:02・当方 | 開いた | x | ― |\n")
OUTSIDE = TMP / "outside"
write(OUTSIDE / S.LOG_NAME, "| 2026-11-19 10:00 | 外 | 11/19 09:03・当方 | 開いた | 置き場所の外 | ― |\n")
(PROJ / "linked").symlink_to(OUTSIDE, target_is_directory=True)  # symlink の案件
(PROJ / "notes" / "doc").mkdir(parents=True)
(PROJ / "notes" / "doc" / S.LOG_NAME).symlink_to(OUTSIDE / S.LOG_NAME)  # symlink の控え
(PROJ / "notes" / "via").symlink_to(OUTSIDE, target_is_directory=True)  # symlink のフォルダの中の控え
write(PROJ / "big" / S.LOG_NAME, "| 2026-11-19 10:00 | 大きい | 11/19 09:04・当方 | 開いた | " + "x" * (S.MAX_BYTES + 10) + " | ― |\n")
fresh()
found = S.logs(PROJ)
check("探す: 置き場所の直下の案件の控えだけ（深すぎる・依存・隠しフォルダ・symlink の案件・symlink の控え・symlink のフォルダは除く）",
      {k: [str(p.relative_to(PROJ)) for p in v] for k, v in found.items()}, {"shop": ["shop/doc/internal/teams-sync-log.md"], "big": ["big/teams-sync-log.md"]})
r = S.sent(PROJ, since=at(2026, 11, 1, 0, 0))
check("読む: 控えの行に、同じ分の控えのファイルの要約を添え、行の無いファイルはファイルだけの行にする。大きすぎる控えは読まない",
      [(x["project"], x["at_text"], x["thread"], x.get("copy")) for x in r["rows"]],
      [("shop", "11/19 09:58", "見積もりの件", "見積もりの送付"), ("shop", "11/18 12:00", "", "日程の候補")])
check("読む: 時刻で絞る・数を切って残りの数を返す",
      (len(S.sent(PROJ, since=at(2026, 11, 19, 0, 0))["rows"]), S.sent(PROJ, since=at(2026, 11, 1, 0, 0), limit=1)["more"]), (1, 1))
check("読む: 案件の名前は控えのある案件と一字一句同じときだけ（場所に使わない）",
      [S.sent(PROJ, project=p).get("error") for p in ("shop", "../outside", str(OUTSIDE), "linked", "shop/doc", "")], [None, "no_project", "no_project", "no_project", "no_project", None])
check("読む: 置き場所が無ければ空", S.sent(None), {"rows": [], "more": 0, "projects": []})

# 同じ控えの写し（古い clone）は 1 行にする。新しく書かれたほうの案件の名前で出す
write(PROJ / "shop-old" / "doc/internal" / S.LOG_NAME, "| 2026-11-19 10:00 | 見積もりの件 | 11/19 09:58・当方 | 開いた | 見積もりを送った | ― |\n")
os.utime(PROJ / "shop-old/doc/internal" / S.LOG_NAME, (time.time() - 86400, time.time() - 86400))
fresh()
r = S.sent(PROJ, since=at(2026, 11, 19, 0, 0))
check("読む: 別のフォルダにある同じ控えの同じ行は、新しく書かれた控えの 1 行だけ", [(x["project"], x["at_text"]) for x in r["rows"]], [("shop", "11/19 09:58")])

# ---------- 手元の枝の控え ----------
G = PROJ / "repo"
git = lambda *a, **env: subprocess.run(["git", "-C", str(G), *a], capture_output=True, text=True, check=True,  # noqa: E731
                                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", **env})
G.mkdir()
git("init", "-q", "-b", "main")
write(G / "doc" / S.LOG_NAME, "| 2026-11-18 10:00 | 本番の件 | 11/18 09:00・当方 | 開いた | main にある行 | ― |\n")
git("add", "-A")
git("commit", "-q", "-m", "main")
git("checkout", "-q", "-b", "chore/sync")
(G / "doc" / S.LOG_NAME).write_text(HEAD + "| 2026-11-18 10:00 | 本番の件 | 11/18 09:00・当方 | 開いた | main にある行 | ― |\n"
                                    "| 2026-11-19 18:00 | 本番の件 | 11/19 17:59・当方 | 開いた | 枝にだけある行 | ― |\n", encoding="utf-8")
(G / "doc/thread").mkdir()
(G / "doc/thread/2026-11-19_1800_out_枝の控え.md").write_text("x", encoding="utf-8")
git("add", "-A")
git("commit", "-q", "-m", "sync")
git("checkout", "-q", "-b", "chore/old", "main")
(G / "doc" / S.LOG_NAME).write_text(HEAD + "| 2026-10-01 10:00 | 古い件 | 10/1 09:00・当方 | 開いた | 古い枝の行 | ― |\n", encoding="utf-8")
git("add", "-A")
old = str(int(time.time()) - 30 * 86400)
git("commit", "-q", "-m", "old", GIT_COMMITTER_DATE=f"{old} +0900", GIT_AUTHOR_DATE=f"{old} +0900")
git("checkout", "-q", "main")
MARK = TMP / "fsmonitor-ran"
hook = TMP / "fsmon.sh"
hook.write_text(f"#!/bin/sh\ntouch {MARK}\n")
hook.chmod(0o755)
git("config", "core.fsmonitor", str(hook))
fresh()
r = S.sent(PROJ, project="repo", since=at(2026, 9, 1, 0, 0))
check("枝: 作業木（main）の行に、最近の枝にだけある行と控えのファイルを足す。古い枝（14 日より前）は読まない",
      [(x["at_text"], x["what"], x.get("copy")) for x in r["rows"]],
      [("11/19 18:00", "", "枝の控え"), ("11/19 17:59", "枝にだけある行", None), ("11/18 09:00", "main にある行", None)])
check("枝: 案件のリポジトリの fsmonitor の設定を動かさない", MARK.exists(), False)
check("枝: git は作業木を変えない（枝を移らない・ファイルを書き換えない）",
      (git("branch", "--show-current").stdout.strip(), "枝にだけある行" in (G / "doc" / S.LOG_NAME).read_text(encoding="utf-8")), ("main", False))

# 探したあとに、途中のフォルダを置き場所の外への symlink に替えても、外は読まない
write(PROJ / "swap" / "doc" / S.LOG_NAME, "| 2026-11-19 10:00 | 入れ替え前 | 11/19 09:30・当方 | 開いた | 中の控え | ― |\n")
fresh()
S.logs(PROJ)
os.rename(PROJ / "swap" / "doc", PROJ / "swap" / "doc-moved")
(PROJ / "swap" / "doc").symlink_to(OUTSIDE, target_is_directory=True)
r = S.sent(PROJ, project="swap", since=0)
check("入れ替え: 探したあとに途中のフォルダを外への symlink に替えても、外の控えを読まない", [x["what"] for x in r["rows"]], [])
os.rename(PROJ / "swap", TMP / "swap-gone")
# 案件のフォルダが自分のリポジトリを持たないときは、親のリポジトリ（置き場所など）の枝を読まない
P2 = TMP / "parent-repo"
(P2 / "proj" / "doc").mkdir(parents=True)
subprocess.run(["git", "-C", str(P2), "init", "-q", "-b", "main"], check=True)
write(P2 / "proj" / "doc" / S.LOG_NAME, "| 2026-11-19 10:00 | 親 | 11/19 09:40・当方 | 開いた | 作業木の行 | ― |\n")
pe = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
subprocess.run(["git", "-C", str(P2), "add", "-A"], check=True, env=pe)
subprocess.run(["git", "-C", str(P2), "commit", "-q", "-m", "main"], check=True, env=pe, capture_output=True)
subprocess.run(["git", "-C", str(P2), "checkout", "-q", "-b", "side"], check=True)
(P2 / "proj" / "doc" / S.LOG_NAME).write_text(HEAD + "| 2026-11-19 11:00 | 親 | 11/19 10:50・当方 | 開いた | 親の枝の行 | ― |\n", encoding="utf-8")
subprocess.run(["git", "-C", str(P2), "add", "-A"], check=True, env=pe)
subprocess.run(["git", "-C", str(P2), "commit", "-q", "-m", "side"], check=True, env=pe, capture_output=True)
subprocess.run(["git", "-C", str(P2), "checkout", "-q", "main"], check=True, capture_output=True)
fresh()
check("枝: 案件のフォルダが自分のリポジトリでなければ、親のリポジトリの枝を読まない（作業木の控えだけ）",
      [x["what"] for x in S.sent(P2, since=0)["rows"]], ["作業木の行"])
fresh()

# ---------- 本体の口 ----------
c = TestClient(s.app, base_url="http://127.0.0.1:8790")
s.PROJECTS_DIR = PROJ
fresh()
res = c.get("/api/hub/sent", params={"since": f"{at(2026, 11, 19, 0, 0):.0f}"})
check("口: 当方の投稿を新しい順に返す", (res.status_code, [(x["project"], x["at_text"]) for x in res.json()["rows"]][:2]),
      (200, [("repo", "11/19 18:00"), ("repo", "11/19 17:59")]))
check("口: 時刻が数でない・範囲の外なら絞らない（読めない値で全部を隠さない）",
      [c.get("/api/hub/sent", params={"since": v, "limit": 200}).status_code for v in ("nan", "inf", "-5", "1e20")], [200, 200, 200, 200])
check("口: 数の上限は 200（大きな数・負の数でも 1〜200）",
      (len(c.get("/api/hub/sent", params={"since": "0", "limit": 99999}).json()["rows"]) <= 200, len(c.get("/api/hub/sent", params={"since": "0", "limit": -3}).json()["rows"])),
      (True, 1))
res = c.get("/api/hub/sent", params={"project": "../outside"})
check("口: 控えの無い案件・場所の形は断り、控えのある案件を返す", (res.status_code, res.json()["error"], "outside" in str(res.json())), (404, "no_project", False))
s.PROJECTS_DIR = None
check("口: 置き場所が無ければ断る", c.get("/api/hub/sent").status_code, 404)
s.PROJECTS_DIR = PROJ
ok_key = {"host": "host.docker.internal:8790", "x-vl-key": "k1"}
check("守り: コンテナから鍵つきの GET /api/hub/sent は通す", problem(ok_key, "GET", "/api/hub/sent", container=True), None)
check("守り: コンテナから POST・鍵なし・違う鍵は断る",
      [problem(ok_key, "POST", "/api/hub/sent", container=True) is not None,
       problem({"host": "host.docker.internal:8790"}, "GET", "/api/hub/sent", container=True) is not None,
       problem({"host": "host.docker.internal:8790", "x-vl-key": "k2"}, "GET", "/api/hub/sent", container=True) is not None], [True, True, True])

# ---------- hubctl sent（127.0.0.1:8792 に本体の写しを立てる） ----------
import uvicorn  # noqa: E402

with socket.socket() as probe:
    busy = probe.connect_ex(("127.0.0.1", 8792)) == 0
if busy:
    check("8792 が空いている（hubctl の試験を立てられる）", busy, False)
else:
    srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=8792, log_level="warning", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    end = time.time() + 10
    while not srv.started and time.time() < end:
        time.sleep(0.05)
    env = {**os.environ, "VL_HUB_URL": "http://127.0.0.1:8792/api/hub", "VL_HUB_KEY_FILE": ""}
    hub = lambda *a: subprocess.run(["uv", "run", "--quiet", "--script", str(ROOT / "hubctl"), *a], capture_output=True, text=True, env=env, timeout=120)  # noqa: E731
    r = hub("sent", "--since", "2026-11-19 00:00")
    lines = r.stdout.splitlines()
    check("hubctl sent: 引用の注意・見出し・案件と時刻と題・控えの記録・控えのある案件と、出ないものの注意",
          (r.returncode, lines[0].startswith("※ 以下は案件のフォルダの Teams の控え"), "[repo] 11/19 17:59｜本番の件" in lines,
           "    控えの記録: 枝にだけある行" in lines, "[repo] 11/19 18:00｜（スレッドは控えのファイルの名前だけ）" in lines,
           lines[-1].startswith("控えのある案件: big, repo, shop, shop-old。控えに書かれない投稿")),
          (0, True, True, True, True, True))
    r = hub("sent", "--project", "shop", "--since", "11/18 00:00", "-n", "1")
    check("hubctl sent: 案件で絞る・数を切って残りを言う", (r.returncode, "[shop] 11/19 09:58｜見積もりの件" in r.stdout, "（ほかに 1 件。-n で増やせます）" in r.stdout), (0, True, True))
    r = hub("sent", "--project", "../outside")
    check("hubctl sent: 控えの無い案件は断り、控えのある案件を言う", (r.returncode != 0, "控えのある案件: big, repo, shop, shop-old" in r.stderr), (True, True))
    r = hub("sent", "--since", "きのう")
    check("hubctl sent: 読めない --since は断る", (r.returncode != 0, "--since は" in r.stderr), (True, True))
    r = hub("sent", "--hours", "1")
    check("hubctl sent: 既定は時間で区切り、無ければ（ありません）", (r.returncode, "（ありません）" in r.stdout), (0, True))
    check("hubctl の説明に sent がある", "hubctl sent [--project P]" in hub("--help").stdout, True)
    srv.should_exit = True
    th.join(5)

ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
