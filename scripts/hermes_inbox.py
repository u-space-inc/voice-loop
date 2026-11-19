"""判断待ちの受け箱（画面の 💡）。Hermes の見張りの記録（<置き場>/voice-loop/items.jsonl）を読み、片づいていない判断待ちを出す（0.31.0）。

押すと中身（要点・返信案・元を開く）が開き、ハブとの会話は、その件への「↩ 返信」の状態になる。「済んだ」で片づけ、「あとで」で決めた時刻まで隠す。

守り（README「守り」）。相手は外の文面に乗せられた Hermes で、置き場（ホストの ~/.hermes = コンテナの /opt/data）をいつでも書ける:
  - ホストの側は置き場を読むだけ。記録は置き場の根から 1 段ずつ symlink をたどらずに開き、通常のファイルで大きさの上限内のものだけを読む
    （hermes_skills と同じ）。中身は文字として出すだけで、字数で切り、制御文字・向きを変える文字は空白にする
  - 「元を開く」のリンクは、本体が記録の口（mail・teams）と id の形から作る。Hermes が書いた文の中のリンクは使わない
  - 「済んだ」は、コンテナの中で Hermes の利用者として、記録の道具（vl_items.py done --by user）に付けさせる。ホストは置き場に書かない。
    押してから UNDO 秒は本体の手元に置くだけで（「戻す」なら何もしない）、そのあとで付ける。付けられなければ一覧に戻し、画面に知らせる
  - 「あとで」は本体の状態フォルダ（Hermes が書けない側）に覚える
  - 件への返信は、ハブへのふつうの発言として送る（hub_hermes が返信の前置きを付ける）。返信の台帳に載るので、承認に答える・閉じるには使えない
記録の読み方（片づけた印・戻した印・3 日の区切り）は、見張りの道具（templates/hermes/watch/vl_items.py）の関数をそのまま使う。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import os
import re
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
ITEMS_MAX = 4_000_000  # 記録のファイルの上限（これより大きければ読まない）
STATUS_MAX = 64 * 1024  # 見張りの状態のファイルの上限
UNDO = 5.0  # 「済んだ」を押してから、付けるまでの秒（その間は「戻す」で取り消せる）
DONE_WAIT = 40  # コンテナの中の道具を待つ上限（秒）
TOOL = "/opt/data/scripts/vl_items.py"  # コンテナの中の記録の道具（見張りの手順と同じもの）
DONE_NOTE = "画面の「済んだ」を押した"
FYI_MAX, LIST_MAX = 30, 200
JST = vl_items.JST
STATE_FILE = STATE_DIR / "inbox.json"
_MAIL_ID = re.compile(r"[0-9a-f]{12,24}")  # Gmail の記録の id（16 進）。この形のときだけ Gmail のリンクを作る
_lock = threading.Lock()
router = APIRouter()

# 「済んだ」を押して、まだ付けていないもの: 番号 → {"until": 付ける時刻, "handle": 付ける予約, "state": "wait"|"run", "gen": 予約の世代}
PENDING: dict[int, dict] = {}
FAIL: dict = {}  # 付けられなかった最後の知らせ {"n", "why", "at"}
_done_lock: asyncio.Lock | None = None  # コンテナの中の道具を 1 つずつ動かす
_op_lock: asyncio.Lock | None = None  # 済んだ・戻すを 1 つずつ受ける（記録を読む間に、別の画面の戻すと入れ違わないように）


# ---------- 記録を読む ----------
def _read(name: str, cap: int) -> tuple[bytes | None, str]:
    """置き場の voice-loop/<name> を読む。(中身, 読めない理由)。ファイルが無ければ (b"", "missing")（空のファイルは (b"", "")）。"""
    try:
        dfd = hermes_skills._open_dir(HOME, ("voice-loop",))
    except FileNotFoundError:
        return b"", "missing"
    except (OSError, ValueError) as e:
        return None, f"記録のフォルダを開けません（{type(e).__name__}）"
    try:
        return hermes_skills._read_at(dfd, name, cap), ""
    except FileNotFoundError:
        return b"", "missing"
    except ValueError as e:
        return None, str(e)
    except OSError as e:
        return None, type(e).__name__
    finally:
        os.close(dfd)


def records() -> tuple[dict, dict] | None:
    """(番号→出来事, 番号→片づけた印)。読めなければ None。形の合わない行は飛ばす（vl_items と同じ読み方）。"""
    data, why = _read("items.jsonl", ITEMS_MAX)
    if data is None:
        return None
    rows = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            r = json.loads(line)
        except (ValueError, RecursionError):  # 入れ子が深すぎる行も、形の違う行として飛ばす
            continue
        if isinstance(r, dict):
            rows.append(r)
    return vl_items._state(rows)


def records_problem() -> str:
    data, why = _read("items.jsonl", ITEMS_MAX)  # 無い（missing）・空は問題にしない
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
def _later() -> dict[int, float]:
    try:
        d = json.loads(STATE_FILE.read_text()) if STATE_FILE.is_file() else {}
    except (OSError, ValueError):
        d = {}
    raw = d.get("later") if isinstance(d, dict) and isinstance(d.get("later"), dict) else {}
    out = {}
    for k, v in raw.items():
        if isinstance(k, str) and k.isdigit() and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 1e11:
            out[int(k)] = float(v)
    return out


def _save_later(m: dict[int, float]):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps({"later": {str(k): v for k, v in m.items()}}))
    tmp.chmod(0o600)
    tmp.replace(STATE_FILE)


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
    got = records()
    try:  # 見張りの状態のどんな形でも、判断待ちの一覧は止めない
        watch = _watch(now)
    except Exception:
        watch = [{"source": "teams", "title": "Teams の見張りが止まっています", "reason": "見張りの状態を読めません", "fix": ""}]
    out = {"on": True, "problem": "", "items": [], "fyi": [], "later": 0, "pending": [], "watch": watch,
           "days": vl_items.OPEN_DAYS, "later_options": later_options(now), "undo": UNDO}
    f = dict(FAIL)  # 一覧は別のスレッドで作るので、写してから見る（その間に付け直しが成功して消しても落ちない）
    if f.get("n") and now - f.get("at", 0) < 600:
        out["fail"] = {"n": f["n"], "why": f.get("why", "")}
    if got is None:
        out["problem"] = records_problem() or "判断待ちの記録を読めませんでした"
        return out
    items, done = got
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
        back = later.get(n)
        if back and back > now:
            out["later"] += 1
            continue
        fresh = max(vl_items._fresh(r), back or 0)
        rows.append((fresh, {**shape(r), "fresh": fresh, "old": now - fresh >= vl_items.OPEN_DAYS * 86400, "back": bool(back)}))
    out["items"] = [x for _, x in sorted(rows, key=lambda p: -p[0])][:LIST_MAX]
    out["fyi"] = sorted(out["fyi"], key=lambda x: -x["when"])[:FYI_MAX]
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
        back = _later().get(n)
    fresh = max(vl_items._fresh(r), back if back and back <= now else 0)
    return {**shape(r), "fresh": fresh, "old": now - fresh >= vl_items.OPEN_DAYS * 86400, "back": bool(back and back <= now)}


# ---------- 済んだ ----------
def done_command(n: int) -> list[str] | None:
    exe = hermes_skills.docker_bin()
    if not exe:
        return None
    return [exe, "exec", "-u", hermes_skills.USER, "-e", "HERMES_HOME=/opt/data", CONTAINER,
            "timeout", "--kill-after=5", str(DONE_WAIT - 10), hermes_skills.PY, TOOL, "done", str(n), "--by", "user", "--note", DONE_NOTE]


def run_done(n: int) -> str:
    """コンテナの中で片づけた印を付ける。付いたら ""、だめならその理由。"""
    cmd = done_command(n)
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
    got = records()  # 付いたことを記録で確かめる（道具の答えの文には頼らない）
    return "" if got and n in got[1] else "記録に印が見当たりません"


async def _commit(n: int, gen: str):
    global _done_lock
    p = PENDING.get(n)
    if not p or p["state"] != "wait" or p["gen"] != gen:  # 戻した・押し直した予約は、古い予約では付けない
        return
    p["state"] = "run"
    _done_lock = _done_lock or asyncio.Lock()
    async with _done_lock:
        why = await asyncio.to_thread(run_done, n)
    PENDING.pop(n, None)
    if why:
        FAIL.update(n=n, why=why, at=time.time())
        print(f"[inbox] 済んだを付けられませんでした [{n}]: {why}", flush=True)
    else:
        if FAIL.get("n") == n:
            FAIL.clear()
        with _lock:
            m = _later()
            if m.pop(n, None) is not None:
                _save_later(m)
        print(f"[inbox] 済んだ [{n}]（画面から）", flush=True)


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
    """「済んだ」。UNDO 秒あとに、コンテナの中で印を付ける（それまでは「戻す」で取り消せる）。コンテナからは使えない口。
    済んだ・戻すは 1 つずつ受ける。予約には世代を付け、付けるときに同じ世代かを確かめる。"""
    n = _num_of(body)
    if n is None:
        return JSONResponse({"ok": False, "reason": "bad_request"}, status_code=400)
    async with _ops():
        if n in PENDING:
            return JSONResponse({"ok": True, "n": n, "left": _left(PENDING[n])})
        got = await asyncio.to_thread(records)
        if got is None:
            return JSONResponse({"ok": False, "reason": "unreadable"})
        items, done = got
        if n not in items or items[n].get("kind") != "judge" or n in done:
            return JSONResponse({"ok": False, "reason": "gone"})
        loop, gen = asyncio.get_running_loop(), os.urandom(6).hex()
        PENDING[n] = {"until": time.time() + UNDO, "state": "wait", "gen": gen,
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
        m[n] = opt["at"]
        _save_later(m)
    return JSONResponse({"ok": True, "n": n, "at": opt["at"], "label": opt["label"]})
