"""判断待ちの受け箱（画面の 💡）。Hermes の見張りの記録（<置き場>/voice-loop/items.jsonl）を読み、片づいていない判断待ちを出す（0.31.0）。

押すと中身（要点・返信案・元を開く）が開き、ハブとの会話は、その件への「↩ 返信」の状態になる。「済んだ」で片づけ、「あとで」で決めた時刻まで隠す。

守り（README「守り」）。相手は外の文面に乗せられた Hermes で、置き場（ホストの ~/.hermes = コンテナの /opt/data）をいつでも書ける:
  - ホストの側は置き場を読むだけ。記録は置き場の根から 1 段ずつ symlink をたどらずに開き、通常のファイルで大きさの上限内のものだけを読む
    （hermes_skills と同じ）。中身は文字として出すだけで、字数で切り、制御文字・向きを変える文字は空白にする
  - 「元を開く」のリンクは、本体が記録の口（mail・teams）と id の形から作る。Hermes が書いた文の中のリンクは使わない
  - 「済んだ」は、コンテナの中で Hermes の利用者として、記録の道具（vl_items.py done --by user）に付けさせる。ホストは置き場に書かない。
    押してから UNDO 秒は本体の手元に置くだけで（「戻す」なら何もしない）、そのあとで付ける。付けられなければ一覧に戻し、画面に知らせる
  - 「不要だった」（0.33.0）は、「済んだ」と同じ道に --unneeded と理由を足す。その記録は見張りの手本になる（見張りの回は、同じ種類の知らせを
    判断待ちにせず fyi にする。下げるのは fyi まで）。理由は本体が決めた選び方だけ。手本は「学んだこと」に並べ、「外す」で外す（forget。同じ道）。
    置き場は Hermes が書けるので、手本の行は偽れる。画面で押して付いた印（番号と、その印の時刻と、手本の中身（口・id・案件・題・鍵・理由）の
    指紋）は本体の状態フォルダに覚え、それと合わない手本には「ハブから」と出す（画面で押していない手本・あとで中身を書き換えた手本に気づけるように）。
    記録が大きくて新しい側だけを読んだときは、手本も新しい側の分だけになる（見張りは全部を使う）。そのことを画面に出し、「外す」は記録の道具に任せる
  - 「あとで」は本体の状態フォルダ（Hermes が書けない側）に覚える
  - 件への返信は、ハブへのふつうの発言として送る（hub_hermes が返信の前置きを付ける）。返信の台帳に載るので、承認に答える・閉じるには使えない
記録の読み方（片づけた印・戻した印・3 日の区切り）は、見張りの道具（templates/hermes/watch/vl_items.py）の関数をそのまま使う。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import hermes_skills  # 置き場を安全に読む部品と、コンテナで動かすときの決まり（docker の場所・利用者・python）
from places import STATE_DIR

def _template(name: str):
    """見張りの台本（templates/hermes/watch/<name>.py）を、そのまま読み込む（読み方の決まりを 1 か所にする）。"""
    spec = importlib.util.spec_from_file_location(f"{name}_tpl", Path(__file__).resolve().parent.parent / f"templates/hermes/watch/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vl_items = _template("vl_items")
vl_watch = _template("vl_watch")  # Teams の止まり方の決まり（TEAMS_STALE・TEAMS_REASON）

HOME = Path(os.environ.get("VL_HERMES_HOME") or Path.home() / ".hermes").expanduser()  # hub_hermes が同じ値を入れ直す
CONTAINER = os.environ.get("VL_HERMES_CONTAINER", "hermes")
ITEMS_MAX = 4_000_000  # 記録を読む上限。これより大きい記録は、新しい側（末尾）のこの大きさだけを読む（記録は済んだ分も足していくので、いつか超える）
STATUS_MAX = 64 * 1024  # 見張りの状態のファイルの上限
UNDO = 5.0  # 「済んだ」を押してから、付けるまでの秒（その間は「戻す」で取り消せる）
DONE_WAIT = 40  # コンテナの中の道具を待つ上限（秒）
TOOL = "/opt/data/scripts/vl_items.py"  # コンテナの中の記録の道具（見張りの手順と同じもの）
DONE_NOTE = "画面の「済んだ」を押した"
UNNEEDED_NOTE = "画面の「不要だった」を押した"
# 「不要だった」の理由の選び方（key → (画面の文, 記録に残す理由)）。画面から来るのは key だけで、記録に書く文は本体が決める
UNNEEDED = {"notice": ("周知・お知らせだった", "周知・お知らせ"), "test": ("試験・dev の通知だった", "試験・dev の通知"),
            "alarm": ("既知のアラームだった", "既知のアラーム"), "notmine": ("自分の件ではなかった", "自分の件ではない"), "none": ("理由は選ばない", "")}
FYI_MAX, LIST_MAX = 30, 200
AI_SHOW = 86400  # AI が片づけた件を受け箱に出す時間（秒）。その間は「戻す」で判断待ちに戻せる
REOPEN_NOTE = "画面の「戻す」（AI の片づけを取り消した）"
JST = vl_items.JST
STATE_FILE = STATE_DIR / "inbox.json"
LESSON_FILE = STATE_DIR / "inbox-lessons.json"  # 画面の「不要だった」で付いた印: 番号 → その印の時刻（Hermes が書けない側）
LESSON_KEEP = 500
_MAIL_ID = re.compile(r"[0-9a-f]{12,24}")  # Gmail の記録の id（16 進）。この形のときだけ Gmail のリンクを作る
_lock = threading.Lock()
router = APIRouter()

# 「済んだ」を押して、まだ付けていないもの: 番号 → {"until": 付ける時刻, "handle": 付ける予約, "state": "wait"|"run", "gen": 予約の世代}
PENDING: dict[int, dict] = {}
FAIL: dict = {}  # 付けられなかった最後の知らせ {"n", "why", "at"}
_done_lock: asyncio.Lock | None = None  # コンテナの中の道具を 1 つずつ動かす
_op_lock: asyncio.Lock | None = None  # 済んだ・戻すを 1 つずつ受ける（記録を読む間に、別の画面の戻すと入れ違わないように）


# ---------- 記録を読む ----------
def _read_tail(dfd: int, name: str, cap: int) -> tuple[bytes, bool]:
    """name を symlink をたどらずに開き、通常のファイルでほかの名前から指されていないときだけ、末尾の cap バイトを読む（行の途中から始まる頭の 1 行は捨てる）。
    (中身, 頭を捨てたか)。置き場の安全な読み方（hermes_skills._read_at）と同じ確かめをして、大きさだけは断らずに新しい側を読む。"""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("通常のファイルではありません")
        if st.st_nlink != 1:
            raise ValueError("ほかの名前からも指されています")
        start = max(0, st.st_size - cap)
        os.lseek(fd, start, os.SEEK_SET)
        data = b""
        while len(data) < cap:
            chunk = os.read(fd, cap - len(data))
            if not chunk:
                break
            data += chunk
        if start > 0:
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else b""
        return data, start > 0
    finally:
        os.close(fd)


def _read(name: str, cap: int, tail: bool = False) -> tuple[bytes | None, str]:
    """置き場の voice-loop/<name> を読む。(中身, 読めない理由)。ファイルが無ければ (b"", "missing")（空のファイルは (b"", "")）。
    tail なら、大きすぎても断らずに末尾の cap バイトだけを読み、理由に "tail" を返す。"""
    try:
        dfd = hermes_skills._open_dir(HOME, ("voice-loop",))
    except FileNotFoundError:
        return b"", "missing"
    except (OSError, ValueError) as e:
        return None, f"記録のフォルダを開けません（{type(e).__name__}）"
    try:
        if tail:
            data, cut = _read_tail(dfd, name, cap)
            return data, "tail" if cut else ""
        return hermes_skills._read_at(dfd, name, cap), ""
    except FileNotFoundError:
        return b"", "missing"
    except ValueError as e:
        return None, str(e)
    except OSError as e:
        return None, type(e).__name__
    finally:
        os.close(dfd)


def records(info: dict | None = None) -> tuple[dict, dict] | None:
    """(番号→出来事, 番号→片づけた印)。読めなければ None。形の合わない行は飛ばす（vl_items と同じ読み方）。"""
    data, why = _read("items.jsonl", ITEMS_MAX, tail=True)
    if data is None:
        return None
    if info is not None:  # 新しい側だけを読んだか。読み込みごとに返す（同時に来た別の読み込みと混ぜない）
        info["trunc"] = why == "tail"
    rows = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            r = json.loads(line)
        except (ValueError, RecursionError):  # 入れ子が深すぎる行も、形の違う行として飛ばす
            continue
        if isinstance(r, dict) and _num_ok(r.get("n")):  # 番号は口と同じ範囲だけ（画面の数で扱えない番号で、返信の印が消えないように）
            rows.append(r)
    if info is not None:
        info["rows"] = rows  # 手本（vl_items.lessons）は、手本から外した行も見る
    return vl_items._state(rows)


def _num_ok(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 0 < n < 2**31


def records_problem() -> str:
    data, why = _read("items.jsonl", ITEMS_MAX, tail=True)  # 無い（missing）・空・大きい（tail）は問題にしない
    return "" if data is not None else f"判断待ちの記録を読めませんでした（{why}）"


def link(r: dict) -> dict | None:
    """元を開くリンク。本体が口と id の形から作る（Hermes の文の中のリンクは使わない）。作れなければ None。"""
    src, rid = r.get("source"), r.get("id")
    if src == "mail" and isinstance(rid, str) and _MAIL_ID.fullmatch(rid):
        return {"url": f"https://mail.google.com/mail/u/0/#all/{rid}", "label": "Gmail で開く"}
    if src == "teams":
        return {"url": "https://teams.microsoft.com/v2/", "label": "Teams を開く"}
    return None


def shape(r: dict) -> dict:
    """画面に出す形。文字は 1 行にそろえて字数で切る（vl_items.clip）。"""
    src = r.get("source") if r.get("source") in vl_items.SOURCES else ""
    return {"n": r["n"], "source": src, "project": vl_items.clip(r.get("project"), "project") or "未振り分け",
            "title": vl_items.clip(r.get("title"), "title") or "（題なし）", "note": vl_items.clip(r.get("note"), "note"),
            "prepared": vl_items.clip(r.get("prepared"), "prepared"), "when": float(vl_items._when(r)), "link": link(r)}


def item(n) -> dict | None:
    """件への返信の先（判断待ち・宛ての知らせの記録）。片づいていても返す。無い・読めなければ None。"""
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        return None
    got = records()
    if not got:
        return None
    r = got[0].get(n)
    return shape(r) if r and r.get("kind") in ("judge", "fyi") else None


# ---------- あとで ----------
def _later() -> dict[int, tuple[float, float]]:
    """番号 → (戻す時刻, あとでにした時刻)。"""
    try:
        d = json.loads(STATE_FILE.read_text()) if STATE_FILE.is_file() else {}
    except (OSError, ValueError, RecursionError):
        d = {}
    raw = d.get("later") if isinstance(d, dict) and isinstance(d.get("later"), dict) else {}
    out = {}
    for k, v in raw.items():
        if isinstance(k, str) and k.isdigit() and isinstance(v, dict) and vl_items._num(v.get("until")) and vl_items._num(v.get("set")):
            out[int(k)] = (float(v["until"]), float(v["set"]))
    return out


def _back(m: dict, r: dict) -> float | None:
    """その件の「あとで」の戻す時刻。あとでにしたあとで判断待ちに戻された（reopen）件には効かせない（古いあとでで隠さない）。"""
    e = m.get(r["n"])
    if not e:
        return None
    moved = r.get("moved")
    return None if vl_items._num(moved) and moved > e[1] else e[0]


def _save_later(m: dict[int, tuple[float, float]]):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps({"later": {str(k): {"until": v[0], "set": v[1]} for k, v in m.items()}}))
    tmp.chmod(0o600)
    tmp.replace(STATE_FILE)


# ---------- 画面で付けた手本 ----------
def lesson_sig(r: dict, d: dict) -> str:
    """手本の中身の指紋（見張りが照らすのに使うもの: 口・id・案件・題・照合の鍵と、不要の理由）。記録の値のまま（切らずに）数える。"""
    keep = [r.get(k) for k in ("source", "id", "project", "title", "key")] + [d.get("why")]
    return hashlib.sha256(json.dumps(keep, ensure_ascii=True, default=str).encode()).hexdigest()  # ASCII にしてから数える（記録に壊れた文字があっても止まらない）


def _ui_lessons() -> dict[int, tuple[float, str]]:
    """番号 → (その印の時刻, 手本の中身の指紋)。"""
    try:
        d = json.loads(LESSON_FILE.read_text()) if LESSON_FILE.is_file() else {}
    except (OSError, ValueError, RecursionError):
        d = {}
    raw = d.get("ui") if isinstance(d, dict) and isinstance(d.get("ui"), dict) else {}
    return {int(k): (float(v["at"]), v["sig"]) for k, v in raw.items()
            if isinstance(k, str) and k.isdigit() and isinstance(v, dict) and vl_items._num(v.get("at")) and isinstance(v.get("sig"), str)}


def _mark_ui_lesson(n: int, at: float, sig: str):
    """画面で押して付いた不要の印を覚える（同じ番号は新しい印で上書き。古い順に LESSON_KEEP 件を超えた分は捨てる）。"""
    with _lock:
        m = _ui_lessons()
        m[n] = (at, sig)
        m = dict(sorted(m.items(), key=lambda p: p[1][0])[-LESSON_KEEP:])
        LESSON_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LESSON_FILE.with_name(LESSON_FILE.name + ".tmp")
        tmp.write_text(json.dumps({"ui": {str(k): {"at": v[0], "sig": v[1]} for k, v in m.items()}}))
        tmp.chmod(0o600)
        tmp.replace(LESSON_FILE)


def later_options(now: float | None = None) -> list[dict]:
    """「あとで」の選び方と、戻す時刻（日本の時刻）。今夜は 19 時より前だけ。"""
    t = dt.datetime.fromtimestamp(now if now is not None else time.time(), JST)
    out = []
    if t.hour < 19:
        out.append({"key": "tonight", "label": "今夜（20:00）", "at": t.replace(hour=20, minute=0, second=0, microsecond=0).timestamp()})
    morning = (t + dt.timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    out.append({"key": "morning", "label": "明日の朝（8:00）", "at": morning.timestamp()})
    monday = (t + dt.timedelta(days=7 - t.weekday())).replace(hour=8, minute=0, second=0, microsecond=0)
    out.append({"key": "week", "label": f"来週の月曜（{monday.month}/{monday.day} 8:00）", "at": monday.timestamp()})
    return out


# ---------- 一覧 ----------
def _watch(now: float | None = None) -> list[dict]:
    """見張りが止まっている口（いまは Teams）。見張りの台本（vl_watch.teams）と同じ決まりで読む: 状態の時刻が 2 時間より古ければ止まっている、
    組織が読めなければその理由（決まった言葉に直す。ファイルの文は出さない）。状態のファイルが無ければ、Teams の見張りを使っていない。"""
    now = time.time() if now is None else now
    data, why = _read("teams-status.json", STATUS_MAX)
    title = "Teams の見張りが止まっています"
    if data is None:  # 有るのに読めない（symlink・大きすぎる・権限など）。無い（Teams の見張りを使っていない）とは分ける
        return [{"source": "teams", "title": title, "reason": "見張りの状態を読めません", "fix": ""}]
    if why == "missing":  # 無い = Teams の見張りを使っていない（空のファイルは、下で読めない形として帯を出す）
        return []
    try:
        d = json.loads(data)
    except (ValueError, RecursionError):
        d = None
    if not isinstance(d, dict) or not vl_items._num(d.get("at")) or not isinstance(d.get("orgs"), dict):
        return [{"source": "teams", "title": title, "reason": "見張りの状態を読めません", "fix": ""}]
    reasons = []
    if now - d["at"] > vl_watch.TEAMS_STALE:
        reasons.append("Mac の見張りが 2 時間以上動いていない（Mac が止まっている・眠っている）")
    for o in d["orgs"].values():
        if isinstance(o, dict) and not o.get("ok"):
            code = o.get("reason")  # 文字でなければ（配列・物など）辞書を引かない
            reasons.append(vl_watch.TEAMS_REASON.get(code, vl_watch.TEAMS_REASON["run"]) if isinstance(code, str) else vl_watch.TEAMS_REASON["run"])
    reasons = list(dict.fromkeys(reasons))
    return [{"source": "teams", "title": title, "reason": "／".join(reasons), "fix": ""}] if reasons else []


def inbox(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with _lock:
        later = _later()
    read: dict = {}
    got = records(read)
    try:  # 見張りの状態のどんな形でも、判断待ちの一覧は止めない
        watch = _watch(now)
    except Exception:
        watch = [{"source": "teams", "title": "Teams の見張りが止まっています", "reason": "見張りの状態を読めません", "fix": ""}]
    out = {"on": True, "problem": "", "items": [], "fyi": [], "ai_closed": [], "later": 0, "pending": [], "watch": watch,
           "days": vl_items.OPEN_DAYS, "later_options": later_options(now), "undo": UNDO,
           "unneeded_options": [{"key": k, "label": v[0]} for k, v in UNNEEDED.items()], "lessons": [], "lessons_more": 0}
    f = dict(FAIL)  # 一覧は別のスレッドで作るので、写してから見る（その間に付け直しが成功して消しても落ちない）
    if f.get("n") and now - f.get("at", 0) < 600:
        out["fail"] = {"n": f["n"], "why": f.get("why", "")}
    if got is None:
        out["problem"] = records_problem() or "判断待ちの記録を読めませんでした"
        return out
    items, done = got
    if read.get("trunc"):
        out["note"] = "判断待ちの記録が大きいので、新しい側だけを読んでいます（それより前に記録した判断待ちは、ここには出ていません。ハブに聞けば全部を確かめます）"
    today = dt.datetime.fromtimestamp(now, JST).date()
    rows = []
    for r in items.values():
        n = r["n"]
        if n in done:
            continue
        if r.get("kind") == "fyi" and dt.datetime.fromtimestamp(r["at"], JST).date() == today:
            out["fyi"].append(shape(r))
            continue
        if r.get("kind") != "judge":
            continue
        p = PENDING.get(n)  # 一覧は別のスレッドで作るので、1 回だけ取って同じものを見る（その間に戻す・付けるが外しても落ちない）
        if p:
            out["pending"].append({"n": n, "left": _left(p), "state": p["state"]})
            continue
        back = _back(later, r)
        if back and back > now:
            out["later"] += 1
            continue
        fresh = max(vl_items._fresh(r), back or 0)
        rows.append((fresh, {**shape(r), "fresh": fresh, "old": now - fresh >= vl_items.OPEN_DAYS * 86400, "back": bool(back)}))
    out["items"] = [x for _, x in sorted(rows, key=lambda p: -p[0])][:LIST_MAX]
    ls = vl_items.lessons(read.get("rows") or [], items, done)  # 見張りが使うのと同じ数だけ並べる（押し出された古い手本は数だけ）
    with _lock:
        ui = _ui_lessons()
    out["lessons"] = [{k: v for k, v in shape(r).items() if k in ("n", "source", "project", "title")}
                      | {"why": vl_items.why_of(d), "at": float(d["at"]), "ui": ui.get(r["n"]) == (d["at"], lesson_sig(r, d))}  # 画面で押して付いた、その印と中身か
                      for r, d in ls[:vl_items.LESSONS_MAX]]
    out["lessons_more"] = max(0, len(ls) - vl_items.LESSONS_MAX)
    out["lessons_partial"] = bool(read.get("trunc"))  # 新しい側だけを読んだ（それより前に付けた手本は、ここに出ていない。見張りは使う）
    out["fyi"] = sorted(out["fyi"], key=lambda x: -x["when"])[:FYI_MAX]
    # AI が根拠を書いて片づけた判断待ち（本体の確かめ・ハブの会話）。1 日は出して、「戻す」で判断待ちに戻せるようにする
    ai = [(d["at"], {**{k: v for k, v in shape(items[n]).items() if k in ("n", "source", "project", "title")},
                     "why": vl_items.clip(d.get("note"), "note"), "closed_at": float(d["at"])})
          for n, d in done.items() if n in items and items[n].get("kind") == "judge" and d.get("by") == "ai"
          and vl_items._num(d.get("at")) and now - d["at"] < AI_SHOW]
    out["ai_closed"] = [x for _, x in sorted(ai, key=lambda p: -p[0])][:FYI_MAX]
    return out


def one(n, now: float | None = None) -> dict | None:
    """通知・リンクの /#i=<番号> で開く 1 件。一覧の隠す条件（あとで・数の上限）に関係なく、片づいていない判断待ちなら返す。"""
    now = time.time() if now is None else now
    got = records()
    if not got or n in PENDING:
        return None
    items, done = got
    r = items.get(n)
    if not r or r.get("kind") != "judge" or n in done:
        return None
    with _lock:
        back = _back(_later(), r)
    fresh = max(vl_items._fresh(r), back if back and back <= now else 0)
    return {**shape(r), "fresh": fresh, "old": now - fresh >= vl_items.OPEN_DAYS * 86400, "back": bool(back and back <= now)}


# ---------- 済んだ・不要だった・手本から外す ----------
def tool_command(*args: str) -> list[str] | None:
    exe = hermes_skills.docker_bin()
    if not exe:
        return None
    return [exe, "exec", "-u", hermes_skills.USER, "-e", "HERMES_HOME=/opt/data", CONTAINER,
            "timeout", "--kill-after=5", str(DONE_WAIT - 10), hermes_skills.PY, TOOL, *args]


def done_command(n: int, why: str | None = None) -> list[str] | None:
    """why が None なら「済んだ」、文字（空も）なら「不要だった」（理由は UNNEEDED の記録に残す文だけ）。"""
    if why is None:
        return tool_command("done", str(n), "--by", "user", "--note", DONE_NOTE)
    return tool_command("done", str(n), "--by", "user", "--note", UNNEEDED_NOTE, "--unneeded", "--why", why)


def _run(cmd: list[str] | None) -> str:
    """コンテナの中で記録の道具を動かす。終わったら ""、だめならその理由（付いたかは、呼んだ側が記録で確かめる）。"""
    if not cmd:
        return "docker が見つかりません"
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=DONE_WAIT)
    except subprocess.TimeoutExpired:
        return "時間切れになりました"
    except OSError as e:
        return f"docker を動かせません（{type(e).__name__}）"
    if p.returncode != 0:
        tail = (p.stdout or p.stderr).decode("utf-8", "replace").strip().splitlines()[-1:] or [f"終了コード {p.returncode}"]
        return vl_items.clip(tail[0], "title")
    return ""


def run_done(n: int, why: str | None = None, snap: str | None = None) -> str:
    """コンテナの中で片づけた印（不要だったなら、不要の印）を付ける。付いたら ""、だめならその理由。
    画面で付けた手本として覚えるのは、この操作で新しく付いた印（前と違う行で、利用者の印・画面の文・選んだ理由が合うもの）で、
    手本の中身が押した時点の指紋（snap）と同じときだけ（待つ間に件の中身を書き換えられていたら覚えない）。
    待つ間にハブが先に不要の印を付けていた（道具は付け足さずに終わる）ときは、片づいたとはするが、画面で付けたとは覚えない。"""
    before, known = None, False  # known: 押す前の印を読めたか（読めなければ、画面で付けたとは覚えない）
    if why is not None:
        got0 = records()
        if got0 is not None:
            before, known = got0[1].get(n), True
    err = _run(done_command(n, why))
    if err:
        return err
    got = records()  # 付いたことを記録で確かめる（道具の答えの文には頼らない）
    if not got or n not in got[1]:
        return "記録に印が見当たりません"
    if why is not None:
        d = got[1][n]
        if d.get("unneeded") is not True:
            return "記録に不要の印が見当たりません（Hermes の置き場の vl_items.py が古いかもしれません）"
        mine = (known and d != before and d.get("by") == "user" and d.get("note") == UNNEEDED_NOTE and d.get("why") == why
                and (snap is None or lesson_sig(got[0][n], d) == snap))
        if mine and vl_items._num(d.get("at")):
            try:  # 覚えられなくても、付いた印はそのまま（その手本は「ハブから」と出る）。片づけの成否には混ぜない
                _mark_ui_lesson(n, d["at"], lesson_sig(got[0][n], d))
            except (OSError, ValueError, TypeError) as e:
                print(f"[inbox] 画面で付けた手本を覚えられませんでした [{n}]: {type(e).__name__}", flush=True)
    return ""


def _lesson_ns() -> tuple[set[int] | None, bool]:
    """(手本の番号, 新しい側だけを読んだか)。読めなければ (None, False)。新しい側だけなら、手本は一部（見張りの道具は全部を読む）。"""
    info: dict = {}
    got = records(info)
    if got is None:
        return None, False
    return {r["n"] for r, _ in vl_items.lessons(info.get("rows") or [], *got)}, bool(info.get("trunc"))


def _forget_marks(n: int) -> tuple[set[int] | None, set[float]]:
    """(手本の番号, その番号の手本から外した行の時刻)。読めなければ (None, 空)。"""
    info: dict = {}
    got = records(info)
    if got is None:
        return None, set()
    rows = info.get("rows") or []
    ats = {r["at"] for r in rows if r.get("op") == "forget" and r.get("n") == n and vl_items._num(r.get("at"))}
    return {r["n"] for r, _ in vl_items.lessons(rows, *got)}, ats


def run_forget(n: int) -> str:
    """コンテナの中で手本から外す。外れたら ""、だめならその理由。
    手本に見えなくなっただけでは外れたとしない（新しい側だけを読んだときは、もともと見えていない）。道具が書いた外した行が、新しく増えたかも見る。"""
    _, before = _forget_marks(n)
    err = _run(tool_command("forget", str(n)))
    if err:
        return err
    ls, after = _forget_marks(n)
    if ls is None or n in ls:
        return "手本から外れたことを確かめられませんでした"
    if not after - before:
        return "手本から外した記録が見当たりません"
    return ""


async def _commit(n: int, gen: str):
    global _done_lock
    p = PENDING.get(n)
    if not p or p["state"] != "wait" or p["gen"] != gen:  # 戻した・押し直した予約は、古い予約では付けない
        return
    p["state"] = "run"
    _done_lock = _done_lock or asyncio.Lock()
    async with _done_lock:
        try:
            why = await asyncio.to_thread(run_done, n, p.get("why"), p.get("snap"))
        except Exception as e:  # どんな止まり方でも予約は外す（実行中のまま残すと、あとで判断待ちに戻した件が一覧に出なくなる）
            why = f"印を付ける途中で止まりました（{type(e).__name__}）"
    PENDING.pop(n, None)
    if why:
        FAIL.update(n=n, why=why, at=time.time())
        print(f"[inbox] {'不要だった' if p.get('why') is not None else '済んだ'}を付けられませんでした [{n}]: {why}", flush=True)
    else:
        if FAIL.get("n") == n:
            FAIL.clear()
        with _lock:
            m = _later()
            if m.pop(n, None) is not None:
                _save_later(m)
        print(f"[inbox] {'不要だった' if p.get('why') is not None else '済んだ'} [{n}]（画面から）", flush=True)


def _num_of(body: dict):
    n = body.get("n") if isinstance(body, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) and 0 < n < 2**31 else None


@router.get("/api/inbox")
async def api_inbox():
    """画面の 💡 の判断待ち。コンテナからは使えない口。"""
    return JSONResponse(await asyncio.to_thread(inbox))


def _ops() -> asyncio.Lock:
    global _op_lock
    _op_lock = _op_lock or asyncio.Lock()
    return _op_lock


def _left(p: dict) -> float:
    """戻せる残りの秒（画面は、この秒で「戻す」を消す。端末の時計には頼らない）。"""
    return max(0.0, round(p["until"] - time.time(), 2)) if p["state"] == "wait" else 0.0


@router.get("/api/inbox/item")
async def api_inbox_item(n: str = ""):
    """通知・リンクで開く 1 件（あとでに回した件・一覧の上限から外れた件も開ける）。コンテナからは使えない口。"""
    k = int(n) if re.fullmatch(r"[0-9]{1,10}", n) else 0  # ASCII の数字だけ（isdigit は「²」なども真にする）
    if not 0 < k < 2**31:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    it = await asyncio.to_thread(one, k)
    return JSONResponse({"ok": True, "item": it} if it else {"ok": False, "reason": "gone"})


@router.post("/api/inbox/done")
async def api_inbox_done(body: dict):
    """「済んだ」（unneeded に理由の key があれば「不要だった」）。UNDO 秒あとに、コンテナの中で印を付ける（それまでは「戻す」で取り消せる）。
    コンテナからは使えない口。済んだ・戻すは 1 つずつ受ける。予約には世代を付け、付けるときに同じ世代かを確かめる。"""
    n = _num_of(body)
    key = body.get("unneeded") if isinstance(body, dict) else None
    if n is None or (key is not None and (not isinstance(key, str) or key not in UNNEEDED)):
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    why = UNNEEDED[key][1] if key is not None else None
    async with _ops():
        if n in PENDING:
            if PENDING[n].get("why") != why:  # ほかの画面で、違う方（済んだ／不要だった・理由）を押したところ。押し直しとして受けない
                return JSONResponse({"ok": False, "reason": "busy"})
            return JSONResponse({"ok": True, "n": n, "left": _left(PENDING[n])})
        got = await asyncio.to_thread(records)
        if got is None:
            return JSONResponse({"ok": False, "reason": "unreadable"})
        items, done = got
        if n not in items or items[n].get("kind") != "judge" or n in done:
            return JSONResponse({"ok": False, "reason": "gone"})
        loop, gen = asyncio.get_running_loop(), os.urandom(6).hex()
        # 不要だったなら、押した時点の件の中身と選んだ理由の指紋を持っておく（付けたあとの手本がこれと同じときだけ、画面で付けたと覚える）
        snap = lesson_sig(items[n], {"why": why}) if why is not None else None
        PENDING[n] = {"until": time.time() + UNDO, "state": "wait", "gen": gen, "why": why, "snap": snap,
                      "handle": loop.call_later(UNDO, lambda: asyncio.ensure_future(_commit(n, gen)))}
        return JSONResponse({"ok": True, "n": n, "left": _left(PENDING[n])})


@router.post("/api/inbox/undo")
async def api_inbox_undo(body: dict):
    """「戻す」。まだ印を付けていなければ取り消す。付け始めていたら late。コンテナからは使えない口。"""
    n = _num_of(body)
    if n is None:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    async with _ops():
        p = PENDING.get(n)
        if not p or p["state"] != "wait":
            return JSONResponse({"ok": False, "reason": "late"})
        p["handle"].cancel()
        PENDING.pop(n, None)
        return JSONResponse({"ok": True, "n": n})


@router.post("/api/inbox/later")
async def api_inbox_later(body: dict):
    """「あとで」。選んだ時刻まで一覧から隠し、その時刻に新着へ戻す。覚えるのは本体の状態フォルダ。コンテナからは使えない口。"""
    n, key = _num_of(body), body.get("until") if isinstance(body, dict) else None
    opt = next((o for o in later_options() if o["key"] == key), None)
    if n is None or not opt:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    got = await asyncio.to_thread(records)
    if got is None:
        return JSONResponse({"ok": False, "reason": "unreadable"})
    items, done = got
    if n not in items or items[n].get("kind") != "judge" or n in done:
        return JSONResponse({"ok": False, "reason": "gone"})
    with _lock:  # 時刻を過ぎて一覧に戻った件の時刻も残す（戻った時刻で新しさを数え続ける）。片づいた・消えた件の分だけ外す
        m = {k: v for k, v in _later().items() if k in items and k not in done}
        m[n] = (opt["at"], time.time())
        _save_later(m)
    return JSONResponse({"ok": True, "n": n, "at": opt["at"], "label": opt["label"]})


@router.post("/api/inbox/forget")
async def api_inbox_forget(body: dict):
    """「学んだこと」の見張りの手本を外す（済の印はそのまま）。コンテナの中の記録の道具で外し、外れたことを記録で確かめる。コンテナからは使えない口。
    済んだ・戻すの受け付け（_ops）は持たない（道具を待つ間も「戻す」を待たせない）。コンテナの道具は 1 つずつ（_done_lock）。"""
    global _done_lock
    n = _num_of(body)
    if n is None:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    ls, partial = await asyncio.to_thread(_lesson_ns)
    if ls is None:
        return JSONResponse({"ok": False, "reason": "unreadable"})
    if n not in ls and not partial:  # 新しい側だけを読んだときは、出ていない手本かもしれないので、記録の道具に任せる（手本でなければ道具が断る）
        return JSONResponse({"ok": False, "reason": "gone"})
    _done_lock = _done_lock or asyncio.Lock()
    async with _done_lock:
        why = await asyncio.to_thread(run_forget, n)
    if why:
        print(f"[inbox] 手本から外せませんでした [{n}]: {why}", flush=True)
        return JSONResponse({"ok": False, "reason": "failed", "why": why})
    print(f"[inbox] 手本から外した [{n}]（画面から）", flush=True)
    return JSONResponse({"ok": True, "n": n})


@router.post("/api/inbox/reopen")
async def api_inbox_reopen(body: dict):
    """「AI が片づけたもの」の「戻す」。AI の印（--by ai）の件だけを、コンテナの中の記録の道具で判断待ちに戻す（利用者の訂正の印が付く）。
    戻ったことを記録で確かめる。コンテナからは使えない口。"""
    global _done_lock
    n = _num_of(body)
    if n is None:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    async with _ops():
        got = await asyncio.to_thread(records)
        if got is None:
            return JSONResponse({"ok": False, "reason": "unreadable"})
        items, done = got
        if n not in items or n not in done or done[n].get("by") != "ai" or not vl_items._num(done[n].get("at")):
            return JSONResponse({"ok": False, "reason": "gone"})
        ai_at, moved_before = float(done[n]["at"]), items[n].get("moved")
    _done_lock = _done_lock or asyncio.Lock()
    async with _done_lock:  # 道具が、書き込みの錠の中で「AI が付けた、その済の印」のままかを照らす（途中で付いた利用者の印は外さない）
        why = await asyncio.to_thread(_run, tool_command("reopen", str(n), "--note", REOPEN_NOTE, "--expect-ai-at", repr(ai_at)))
        after = await asyncio.to_thread(records)
    cur = after[0].get(n) if after else None
    if not why and not (cur and cur.get("kind") == "judge" and n not in after[1] and cur.get("moved") != moved_before):
        why = "判断待ちに戻ったことを確かめられませんでした"
    if why:
        print(f"[inbox] AI の片づけを戻せませんでした [{n}]: {why}", flush=True)
        return JSONResponse({"ok": False, "reason": "failed", "why": why})
    print(f"[inbox] AI の片づけを戻した [{n}]（画面から）", flush=True)
    return JSONResponse({"ok": True, "n": n})
