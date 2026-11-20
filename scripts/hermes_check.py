"""判断待ちを 1 件ずつ、Hermes の新しい会話で確かめる（層 3。0.34.0）。

見張りの回が記録した判断待ちは、記録したあとに利用者が Teams・メールで返したり、セッションが片づけたりして、済んでいることが多い。
これまではハブとの会話の中で全件を確かめていて、会話があふれていた（2026-11-20 の総ざらい）。ここでは本体が 1 件ずつ、
新しい会話（X-Hermes-Session-Id を毎回変える）で Hermes に確かめさせ、決まった形の答え（判定・根拠の場所・根拠の時刻・引用）を受け取る。

  - きっかけ: 出ていく側（案件の Teams の控え。sent_log）に新しい当方の投稿が出た案件の判断待ちと、毎朝 MORNING_HOUR 時に、
    まだ確かめていない（RECHECK_GAP より前に確かめた）判断待ち。動くのは HOURS の間だけ
  - 上限: 1 日 CAP 件（Hermes に頼んだ数。失敗も数える）。越えた分は次の日に回す（2026-11-20 利用者が 10 件に決めた）
  - 片づけ: 答えが done で、根拠の場所と要約があり、根拠の時刻が元の時刻（--sent、Slack は記録の id の ts、ほかは Hermes が答えた
    元の時刻を記録の時刻で挟んだもの。判断待ちに戻した件は戻した時刻）より後で、いまより先でないときだけ、本体がコンテナの中の
    記録の道具に --by ai と根拠で印を付けさせる。頼む直前に記録を読み直し、そのときの「戻した印」を --expect-moved で渡して、
    道具が書き込みの錠の中で照らす（確かめている間に利用者が片づけた・戻した件には付けない）。まだ・確かめきれない件は checked で残す
  - 元の時刻を Hermes の答えに頼る件があるのは、勘違いへの備えで、悪意への守りではない（Hermes はいまも記録の道具で --by ai の印を
    付けられる。見張りの回と同じく、決まりで守る。2026-11-13 利用者の決定）
  - 守り: Hermes の答えは引用データ（外の文面に乗せられているかもしれない）。本体がするのは、決めた形を機械で確かめたうえでの
    片づけ（受け箱に「AI が片づけた」として 1 日出て、利用者が「戻す」で戻せる）と、結果のメモだけ。文は字数で切り、argv で渡す
  - 動かすのは config.env の VL_HERMES_CHECK=on のときだけ（答え合わせのあとで本番に入れる）

答え合わせ: `python scripts/hermes_check.py eval --records <記録の控え> --answers <答えの JSON> --out <結果の JSONL>`
（記録の控えの件を、片づけずに確かめるだけ。答えは {"done": [...], "unneeded": [...], "open": [...], "ask": [...]}）。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import hermes_inbox
from places import CONFIG_DIR, STATE_DIR

vl_items = hermes_inbox.vl_items
JST = vl_items.JST
ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "templates/hermes/check.md"
STATE_FILE = STATE_DIR / "hermes-check.json"
CAP = 10                  # 1 日に Hermes に頼む数の上限（日本時間の日付で数える。2026-11-20 利用者の決定）
HOURS = (7, 21)           # 動く時間（日本時間。見張りの回と同じ 7〜20 時台）
MORNING_HOUR = 8          # 毎朝のきっかけ（見張りの朝の回のあと）
RECHECK_GAP = 20 * 3600   # 朝のきっかけでは、これより最近に確かめた件は飛ばす
TICK = 300                # 見回りの間隔（秒）
PER_TICK = 2              # 1 回の見回りで確かめる数の上限（Hermes を続けて塞がない）
CALL_TIMEOUT = 600        # Hermes の答えを待つ上限（秒）
ORIG_BEFORE = 7 * 86400   # Hermes が答えた元の時刻として受け入れる幅（記録の時刻より前、この秒まで）
LOG_MAX = 200
LIM = {"where": 100, "quote": 120, "reason": 80}
VERDICTS = {"done", "open", "unknown"}
_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})")
_lock = threading.Lock()   # 状態のファイル
_run_lock = threading.Lock()  # 確かめは 1 つずつ
sent_reader = None         # 本体が入れる: since → sent_log.sent の結果（本体の読み取りの作業 1 本で順に読む）


def on() -> bool:
    return os.environ.get("VL_HERMES_CHECK", "").strip().lower() in ("1", "on", "true", "yes")


# ---------- 状態 ----------
def _load() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text()) if STATE_FILE.is_file() else {}
    except (OSError, ValueError, RecursionError):
        d = {}
    d = d if isinstance(d, dict) else {}
    d["items"] = {k: v for k, v in (d.get("items") or {}).items() if isinstance(k, str) and k.isdigit() and isinstance(v, dict)} \
        if isinstance(d.get("items"), dict) else {}
    d["queue"] = [n for n in d.get("queue") or [] if isinstance(n, int) and not isinstance(n, bool) and n > 0] if isinstance(d.get("queue"), list) else []
    d["log"] = [e for e in d.get("log") or [] if isinstance(e, dict)][-LOG_MAX:] if isinstance(d.get("log"), list) else []
    for k in ("day", "morning"):
        d[k] = d.get(k) if isinstance(d.get(k), str) else ""
    d["count"] = d.get("count") if isinstance(d.get("count"), int) and not isinstance(d.get("count"), bool) else 0
    d["sent_seen"] = float(d["sent_seen"]) if vl_items._num(d.get("sent_seen")) else 0.0
    return d


def _save(d: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    tmp.chmod(0o600)
    tmp.replace(STATE_FILE)


def _day(t: float) -> str:
    return dt.datetime.fromtimestamp(t, JST).strftime("%Y-%m-%d")


def _jst(t: float) -> str:
    return dt.datetime.fromtimestamp(t, JST).strftime("%Y-%m-%d %H:%M")


def _ts(s) -> float | None:
    """日本時間の「YYYY-MM-DD HH:MM」を UNIX 秒に。形が違えば None。"""
    m = _TIME.fullmatch(s.strip()) if isinstance(s, str) else None
    if not m:
        return None
    try:
        return dt.datetime(*map(int, m.groups()), tzinfo=JST).timestamp()
    except ValueError:
        return None


def _clip(v, n: int) -> str:
    v = " ".join(vl_items._CTRL.sub(" ", str(v or "")).split())
    return v if len(v) <= n else v[: n - 1] + "…"


# ---------- Hermes に頼む ----------
def prompt_for(r: dict) -> str:
    """1 件の依頼の文。記録の文は字数で切り、1 行にそろえる（中身は引用データ）。"""
    def c(k):
        return vl_items.clip(r.get(k), k if k in vl_items.LIMITS else "note")
    lines = [f"判断待ち [{r['n']}] が、いまも済んでいないかを確かめてください（指示文の「確かめ方」と「答えの形」のとおり）。",
             f"- 口: {c('source')}　id: {vl_items.clip(r.get('id'), 'id')}　案件: {c('project') or '未振り分け'}",
             f"- 題: {c('title')}"]
    if r.get("note"):
        lines.append(f"- 要ること: {c('note')}")
    if r.get("prepared"):
        lines.append(f"- 準備したこと: {c('prepared')}")
    lines.append(f"- 記録した時刻: {_jst(r['at'])}" + (f"　届いた時刻: {_jst(r['sent'])}" if vl_items._num(r.get("sent")) else ""))
    if vl_items._num(r.get("moved")):
        lines.append(f"- 判断待ちに戻した時刻: {_jst(r['moved'])}（利用者の訂正。これより後の根拠だけを使う）")
    return "\n".join(lines)


def ask(text: str, session: str) -> str:
    """Hermes の API サーバーに、確かめの指示文（system）と 1 件の依頼を、新しい会話の名前で送る。"""
    url = os.environ.get("VL_HERMES_URL", "http://127.0.0.1:8642").rstrip("/")
    key = os.environ.get("VL_HERMES_KEY", "")
    if not key:
        raise RuntimeError("VL_HERMES_KEY がありません")
    guide = GUIDE.read_text().strip()
    req = urllib.request.Request(
        f"{url}/v1/chat/completions",
        data=json.dumps({"model": "hermes-agent", "stream": False,
                         "messages": [{"role": "system", "content": guide}, {"role": "user", "content": text}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "X-Hermes-Session-Id": session}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Hermes が {e.code} を返しました") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Hermes に接続できません（{e.reason}）") from None
    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


def parse(text: str) -> dict | None:
    """答えの最後の VL_CHECK の行を読む。形が違えば None。文は字数で切り、1 行にそろえる。"""
    for line in reversed(text.splitlines() if isinstance(text, str) else []):
        line = line.strip().strip("`").strip()
        if not line.startswith("VL_CHECK "):
            continue
        try:
            d = json.loads(line[len("VL_CHECK "):])
        except (ValueError, RecursionError):
            return None
        if not isinstance(d, dict) or not isinstance(d.get("verdict"), str) or d["verdict"] not in VERDICTS:
            return None
        return {"verdict": d["verdict"], "orig_at": d.get("orig_at") if isinstance(d.get("orig_at"), str) else "",
                "where": _clip(d.get("where"), LIM["where"]), "at": d.get("at") if isinstance(d.get("at"), str) else "",
                "quote": _clip(d.get("quote"), LIM["quote"])}
    return None


def judge(r: dict, ans: dict | None, now: float) -> tuple[str, str]:
    """(本体の判定, 理由)。done は、根拠の時刻を機械で確かめられたときだけ。ほかは open・unknown。"""
    if ans is None:
        return "unknown", "答えの形が合わない"
    if ans["verdict"] != "done":
        return ans["verdict"], ""
    if not ans["where"] or not ans["quote"]:
        return "unknown", "根拠の場所か要約が無い"
    ev = _ts(ans["at"])
    if ev is None:
        return "unknown", "根拠の時刻が読めない"
    slack = _slack_ts(r)
    if vl_items._num(r.get("sent")):
        base = r["sent"]
    elif slack is not None:
        base = slack
    else:
        orig = _ts(ans["orig_at"])
        if orig is None or not (r["at"] - ORIG_BEFORE <= orig <= r["at"] + 300):
            return "unknown", "元の時刻が分からない"
        base = orig
    if vl_items._num(r.get("moved")):
        base = max(base, r["moved"])
    if ev <= base:
        return "unknown", "根拠が元の時刻（戻した件は戻した時刻）より前"
    if ev > now:
        return "unknown", "根拠の時刻がいまより先"
    return "done", ""


def _slack_ts(r: dict) -> float | None:
    """Slack の記録の id（<チャンネル>/<ts>）の ts。発言の時刻そのもの（Hermes の答えに頼らない）。"""
    if r.get("source") != "slack" or not isinstance(r.get("id"), str):
        return None
    m = re.fullmatch(r"[A-Z0-9]{1,40}/([0-9]{1,12})\.[0-9]{1,12}", r["id"])
    t = float(m.group(1)) if m else None
    return t if t is not None and vl_items._num(t) else None


def _note(ans: dict | None, reason: str) -> str:
    if not ans:
        return reason
    s = "／".join(x for x in (ans["where"], f"根拠の時刻 {ans['at']}" if ans["at"] else "", ans["quote"]) if x)
    return (s + (f"（{reason}）" if reason else ""))[:vl_items.LIMITS["note"]]


def _records():
    info: dict = {}
    got = hermes_inbox.records(info)
    return got


def check_one(r: dict, now: float | None = None, dry: bool = False) -> dict:
    """1 件を確かめる。dry なら、渡された記録のまま確かめ、印を付けない（答え合わせ）。
    そうでなければ、頼む直前に記録を読み直し、まだ判断待ちの件だけを、そのときの中身と「戻した印」で確かめる。
    印・メモは --expect-moved で、そのときの「戻した印」を書き込みの錠の中で照らさせる。結果の 1 件（日誌の形）を返す。"""
    n, started = r["n"], time.time()
    e = {"n": n, "at": started, "verdict": "unknown", "said": "", "where": "", "ev_at": "", "quote": "", "reason": "", "closed": False}
    if not dry:
        got = _records()
        cur = got[0].get(n) if got else None
        if not got or not cur or n in got[1] or cur.get("kind") != "judge":
            e.update(verdict="skip", reason="もう判断待ちではない・読めない")
            return e
        r = cur
    expect = repr(float(r["moved"])) if vl_items._num(r.get("moved")) else "none"
    try:
        text = ask(prompt_for(r), f"vl-check-{n}-{os.urandom(3).hex()}")
    except Exception as x:  # 接続・時間切れ・形の違う応答
        e.update(verdict="error", reason=_clip(str(x), LIM["reason"]))
        return e
    try:
        ans = parse(text)
        v, reason = judge(r, ans, time.time())
    except Exception as x:  # 答えの形が思いがけないものでも、確かめきれないとして残す
        ans, v, reason = None, "unknown", _clip(f"答えを読めない（{type(x).__name__}）", LIM["reason"])
    e.update(verdict=v, reason=reason, said=ans["verdict"] if ans else "",
             where=ans["where"] if ans else "", ev_at=ans["at"] if ans else "", quote=ans["quote"] if ans else "")
    if dry:
        return e
    if v == "done":
        why = hermes_inbox._run(hermes_inbox.tool_command("done", str(n), "--by", "ai", "--note", "本体の確かめ（新しい会話）: " + _note(ans, ""),
                                                          "--expect-moved", expect))
        after = _records()
        e["closed"] = not why and bool(after) and n in after[1] and after[1][n].get("by") == "ai"
        if not e["closed"]:
            e["reason"] = _clip(why or "記録に印が見当たりません", LIM["reason"])
    else:
        why = hermes_inbox._run(hermes_inbox.tool_command("checked", str(n), "--result", v, "--note", _note(ans, reason), "--expect-moved", expect))
        if why:
            e["reason"] = _clip(why, LIM["reason"])
    return e


# ---------- 見回り ----------
def tick(now: float | None = None) -> list[dict]:
    """1 回の見回り。きっかけを集めて並びに足し、上限の内で確かめる。確かめた結果を返す。"""
    now = time.time() if now is None else now
    hour = dt.datetime.fromtimestamp(now, JST).hour
    if not (HOURS[0] <= hour < HOURS[1]) or not _run_lock.acquire(blocking=False):
        return []
    try:
        got = _records()
        if not got:
            return []
        items, done = got
        open_ = {n: r for n, r in items.items() if r.get("kind") == "judge" and n not in done}
        with _lock:
            st = _load()
        if st["day"] != _day(now):
            st.update(day=_day(now), count=0)
        last = {int(k): v.get("at", 0) for k, v in st["items"].items()}
        queue = [n for n in st["queue"] if n in open_]
        # 出ていく側に新しい当方の投稿が出た案件の判断待ち（その投稿より前に確かめた件だけ）
        if sent_reader is not None:
            since = st["sent_seen"] or now - 86400
            try:
                res = sent_reader(since)
                if isinstance(res, dict) and res.get("more"):  # 上限で切られた: 案件ごとに、いちばん新しい 1 件を読み直す（切られた案件を落とさない）
                    per = [sent_reader(since, p, 1) for p in res.get("projects") or [] if isinstance(p, str)]
                    res = {"rows": [x for q in per if isinstance(q, dict) for x in (q.get("rows") or [])],
                           "partial": any(isinstance(q, dict) and (q.get("partial") or q.get("error")) for q in per) or res.get("partial")}
            except Exception:
                res = None
            rows = res.get("rows") if isinstance(res, dict) and isinstance(res.get("rows"), list) else []
            newest: dict[str, float] = {}
            for x in rows:
                if isinstance(x, dict) and vl_items._num(x.get("at")) and x["at"] > st["sent_seen"] and isinstance(x.get("project"), str):
                    newest[x["project"]] = max(newest.get(x["project"], 0.0), x["at"])
            for n, r in sorted(open_.items()):
                p = r.get("project")
                if p in newest and last.get(n, 0) < newest[p] and n not in queue:
                    queue.append(n)
            if rows and not (isinstance(res, dict) and res.get("partial")):  # 読み残しがあれば、次の回にもう一度同じ時刻から読む
                st["sent_seen"] = max([st["sent_seen"]] + [x["at"] for x in rows if isinstance(x, dict) and vl_items._num(x.get("at"))])
        # 毎朝: まだ確かめていない件（古い順）
        if hour >= MORNING_HOUR and st["morning"] != _day(now):
            for n, r in sorted(open_.items(), key=lambda p: vl_items._fresh(p[1])):
                if now - last.get(n, 0) >= RECHECK_GAP and n not in queue:
                    queue.append(n)
            st["morning"] = _day(now)
        out = []
        while queue and st["count"] < CAP and len(out) < PER_TICK:
            n = queue.pop(0)
            st["count"] += 1
            with _lock:  # 頼む前に数を残す（途中で止まっても、上限を越えて頼まない）
                _save({**st, "queue": queue})
            e = check_one(open_[n], now)
            if e["verdict"] == "skip":  # 頼む前に読み直したら、もう判断待ちではなかった（頼んでいないので数に入れない）
                st["count"] -= 1
            out.append(e)
            st["items"][str(n)] = {"at": e["at"], "verdict": e["verdict"]}
            st["log"] = (st["log"] + [e])[-LOG_MAX:]
            print(f"[check] [{n}] {e['verdict']}{'（片づけた）' if e['closed'] else ''} {e['reason']}".rstrip(), flush=True)
        st["queue"] = queue
        with _lock:
            _save(st)
        return out
    finally:
        _run_lock.release()


async def watch():
    """TICK 秒おきに見回る（VL_HERMES_CHECK=on のときだけ）。Hermes の答えを待つ間も本体を止めない。"""
    while True:
        await asyncio.sleep(TICK)
        if not on():
            continue
        try:
            await asyncio.to_thread(tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()


def status() -> dict:
    with _lock:
        st = _load()
    today = _day(time.time())
    return {"on": on(), "today": st["count"] if st["day"] == today else 0, "cap": CAP, "queue": len(st["queue"]), "log": st["log"][-20:]}


# ---------- 答え合わせ ----------
def _config_env():
    """CONFIG_DIR/config.env の VL_HERMES_URL・VL_HERMES_KEY を、まだ無い環境変数として読む（値は出さない）。"""
    try:
        lines = (CONFIG_DIR / "config.env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        k, sep, v = line.strip().partition("=")
        if sep and k in ("VL_HERMES_URL", "VL_HERMES_KEY") and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


def evaluate(records_file: Path, answers: dict, out: Path) -> dict:
    rows = []
    for line in records_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    items, _ = vl_items._state(rows)
    key = {n: k for k, ns in answers.items() for n in ns}
    summary: dict = {}
    with out.open("a", encoding="utf-8") as f:
        for n in sorted(key):
            if n not in items:
                continue
            e = check_one(items[n], dry=True)
            e["answer"] = key[n]
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
            f.flush()
            summary.setdefault(key[n], {}).setdefault(e["verdict"], []).append(n)
            print(f"[{n}] 答え {key[n]} → {e['verdict']}（Hermes: {e['said'] or '-'}）{e['reason']}", flush=True)
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description="判断待ちの確かめ（層 3）の答え合わせ")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("eval")
    s.add_argument("--records", required=True, type=Path)
    s.add_argument("--answers", required=True, type=Path)
    s.add_argument("--out", required=True, type=Path)
    a = p.parse_args()
    _config_env()
    answers = json.loads(a.answers.read_text())
    summary = evaluate(a.records, {k: [int(x) for x in v] for k, v in answers.items()}, a.out)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
