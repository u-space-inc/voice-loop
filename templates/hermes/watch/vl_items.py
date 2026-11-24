"""voice-loop の見張りの記録（出来事ごとに 1 行）。見張りの回（cron）が書き、ふだんの会話が読む。

  vl_items.py seen <source> <id>        記録にあれば 0 で終わる（無ければ 1）。同じ出来事を二度扱わないために使う
  vl_items.py unseen --current         見張りの台本の今の出力（Hermes が回の前に書く monitor_last_output.txt）を自分で読み、
                                       まだ記録に無い出来事の行だけを出す（見張りの回はこれを使う。行を手で写さない）
  vl_items.py unseen ["<行>" ...]       見張りの行（引数か標準入力。先頭の + は外す）から、まだ記録に無い出来事の行だけを出す（--current を読めないときの代わり）
  vl_items.py add --source mail --id X --project P --kind judge|fyi|info|baseline --title T [--note N] [--prepared 準備したこと] [--key 照合の鍵] [--sent 届いた時刻]
                                       （--sent はメールなどが届いた時刻の UNIX 秒。find の --before と並びに使う）
                                       （fyi は、人が利用者に宛てて書いたが対応の要らないもの。見張りの回が DM に 1 行で知らせる）
  vl_items.py open [--project P] [--all]
                                       判断待ち（kind=judge で、まだ片づいていないもの）を新しい順に。並べるのは記録してから（戻したものは戻してから）
                                       OPEN_DAYS 日以内のものだけで、それより前のものは数だけ出す（動きの無いものは一覧から薄れる。消さない）。--all で全部
  vl_items.py recent [--hours 24]      直近の記録（種類を問わず）を新しい順に
  vl_items.py find --key K [--before T] [--limit 20]
                                       照合の鍵が K と一字一句同じ記録を、古さを問わず新しい順に（既読扱いは出さない。アラームの照合に使う）。
                                       --before は UNIX 秒で、それより前に届いたもの（--sent。無い記録は記録した時刻）だけ（絞ってから数を切る。切ったら「ほかに N 件」と出す）
  vl_items.py done <番号> [--note N] [--by user|ai] [--unneeded [--why 理由]]
                                       片づけた印を付ける。user は利用者が「済んだ」と言ったとき。ai は、あなたが元の記録（利用者が送ったメール・
                                       Slack の投稿、セッションの記録）で済んだと確かめたときで、根拠を --note に書く（無ければ断る）
                                       --unneeded は、利用者が「そもそも判断待ちでなかった（不要だった）」と言ったとき（--by user だけ）。
                                       その記録は見張りの手本になる（lessons）。既に済の印がある件にも付けられる
  vl_items.py reopen <番号> [--note N] 判断待ちに戻す（利用者が「まだだった」「判断が要る」と言ったとき）。片づけた印を外し、
                                       info・fyi と分けたものも判断待ちにする（不要の手本からも外れる）
  vl_items.py lessons                  見張りの手本（利用者が不要だったとした記録）を新しい順に LESSONS_MAX 件まで。見張りの回が、
                                       判断待ちにする前に照らす（同じ種類なら fyi にする）
  vl_items.py forget <番号>            手本から外す（利用者が「その手本は外して」と言ったとき）。済の印はそのまま
  vl_items.py undo <番号> [--note N]   あなた（AI）が付けた済の印（--by ai）を取り消す（誤りに気づいたとき）。利用者の訂正（reopen）とは分け、
                                       判断待ちに戻した印（moved）を付けない。利用者の印・不要の印は取り消せない
  vl_items.py checked <番号> --result open|unknown [--note N] [--expect-moved T|none]
                                       本体が新しい会話で確かめた結果（まだ・確かめきれない）を残す。一覧に「確かめた:」の行で出る（本体だけが使う）
  --expect-moved（done・checked）・--expect-ai-at（reopen）は、本体が読んだときの状態を書き込みの錠の中で照らす前提。
                                       合わなければ書かずに終わりの数 3（確かめている間に利用者が片づけた・戻した・印が替わった）
  vl_items.py projects                 これまでに使った案件の名前と件数

記録は HERMES_HOME/voice-loop/items.jsonl。書くのはこの道具だけ（手で書き換えない）。中身はメール・Slack・録音からの要約（引用データ）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import math
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or "/opt/data")
FILE = HOME / "voice-loop" / "items.jsonl"
SOURCES = {"mail", "slack", "plaud", "cal", "teams"}
KINDS = {"judge", "fyi", "info", "baseline"}
CHECKED = {"open": "まだ", "unknown": "確かめきれない"}  # 本体の確かめの結果（済んだものは、本体が --by ai で片づける）
LIMITS = {"title": 80, "note": 600, "prepared": 600, "project": 40, "id": 200}
JST = dt.timezone(dt.timedelta(hours=9))  # 表示の時刻。コンテナの時計は UTC なので、決めて付ける
LESSONS_MAX = 40  # 見張りの手本を出す数（新しい側から）。見張りの回が毎回読むので、多くしすぎない。押し出された古い手本は使わない
OPEN_DAYS = 3  # 判断待ちの一覧に並べる日数（記録してから。戻したものは戻してから）。これより前のものは数だけ出す（2026-11-17。人が閉じなくても一覧が古くならないように）
# 見張りの行の形（vl_watch.py と同じ。変えるときは両方を変える）。2 つ目の語（括弧の中）が記録の id
_ID = r"[A-Za-z0-9_.@:+=-]{1,200}"
_LINE = {
    "mail": re.compile(rf"mail ({_ID})"),
    "slack": re.compile(r"slack ([A-Z0-9]{1,40}/[0-9]{1,12}\.[0-9]{1,12}) (?:[A-Z0-9]{1,40}|-)"),
    "cal": re.compile(rf"cal ({_ID})"),
    "plaud": re.compile(rf"plaud ({_ID}) \| {_ID}"),
    "teams": re.compile(r"teams ([0-9a-f]{16})"),
}
# unseen --current が読む、見張りの台本の今の出力（Hermes が cron の回の前に、ジョブごとの出力の置き場に書く）。
# 2026-11-24: 見張りの回が差分の行を手で写して渡し、Plaud の行の後半を落とす・Teams の id を 2 字落とす・末尾に「;」を付けることがあった。
# 写させず、ここで読む。古すぎる出力（回の前に書かれていない）は使わない（見張りが止まっているのに「新しい出来事 0 件」と言わないため）
WATCH_JOB = "voice-loop-watch"
CURRENT_MAX_AGE = 30 * 60
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _current_lines() -> tuple[list[str] | None, str]:
    """見張りの台本の今の出力の行。読めなければ (None, 理由)。"""
    try:
        jobs = json.loads((HOME / "cron" / "jobs.json").read_text(encoding="utf-8")).get("jobs")
    except (OSError, ValueError, AttributeError) as e:
        return None, f"cron の設定を読めません（{type(e).__name__}）"
    ids = [j.get("id") for j in (jobs if isinstance(jobs, list) else []) if isinstance(j, dict) and j.get("name") == WATCH_JOB]
    if len(ids) != 1 or not isinstance(ids[0], str) or not _JOB_ID.fullmatch(ids[0]):
        return None, f"cron のジョブ {WATCH_JOB} が 1 つに決まりません（{len(ids)} 件）"
    path = HOME / "cron" / "output" / ids[0] / "monitor_last_output.txt"
    try:
        age = time.time() - path.stat().st_mtime
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"台本の出力を読めません（{type(e).__name__}）"
    if age > CURRENT_MAX_AGE:
        return None, f"台本の出力が古すぎます（{int(age // 60)} 分前に書かれたもの）"
    return text.splitlines(), ""


# 記録の id。切らずに保存し（切ると、同じ出来事が記録済みにならない）、形が合わなければ断る
_RECORD_ID = re.compile(r"[A-Za-z0-9_.@:+=/-]{1,200}")


def _rows() -> list[dict]:
    if not FILE.exists():
        return []
    out = []
    for line in FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _num(v) -> bool:
    """時刻として読める数か（1970 年より後で、日時として出せる範囲）。比べるだけで確かめる（巨大な整数を float に直すと例外になる。NaN は比べると偽）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e11


def _n(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _state(rows: list[dict]) -> tuple[dict, dict]:
    """(番号→出来事, 番号→片づけた印)。書いた順に読む。形の合わない行（番号・時刻・文字の欄の型が違う）は飛ばす。
    reopen は片づけた印を外し、種類を判断待ちにして、戻した時刻（一覧の古さはここから数える）を付ける。"""
    items, done = {}, {}
    for r in rows:
        op, n = r.get("op"), r.get("n")
        if not _n(n):
            continue
        if op == "add":
            if _num(r.get("at")) and all(isinstance(r.get(k), str) for k in ("source", "id", "kind", "title")):
                items[n] = {k: v for k, v in r.items() if k not in ("moved", "moved_note", "checked")}  # 戻した印・確かめた結果は、その操作の行からだけ作る（追加の行の値は使わない）
        elif op == "done":
            done[n] = r
        elif op == "undo" and n in done and done[n].get("by") == "ai":
            done.pop(n, None)  # AI が自分の印を取り消した。利用者の訂正ではないので、戻した印（moved）は付けない
        elif op == "checked" and n in items and n not in done and _num(r.get("at")) and isinstance(r.get("result"), str) and r["result"] in CHECKED:
            items[n] = {**items[n], "checked": {"at": r["at"], "result": r["result"], "note": r.get("note") if isinstance(r.get("note"), str) else ""}}
        elif op == "reopen" and n in items:
            done.pop(n, None)
            items[n] = {**items[n], "kind": "judge", "moved": r["at"] if _num(r.get("at")) else items[n]["at"], "moved_note": r.get("note")}
    return items, done


def lessons(rows: list[dict], items: dict, done: dict) -> list[tuple[dict, dict]]:
    """見張りの手本: 利用者が「不要だった」とした記録（[(出来事, 片づけた印)]、新しく付けた順）。
    いまの片づけた印が利用者の不要の印で、その行より後ろに手本から外した行（forget）が無いものだけ。判断待ちに戻した（reopen）ものは、印が外れるので入らない。
    前後は書いた順（行の並び）で決める（時刻で比べると、時計が戻ったときに、外したのに残る）。時刻は表示にだけ使う。"""
    last_done: dict[int, int] = {}
    last_forget: dict[int, int] = {}
    for i, r in enumerate(rows):
        n = r.get("n")
        if not _n(n):
            continue
        if r.get("op") == "done":
            last_done[n] = i  # _state の片づけた印（いちばん後ろの done の行）と同じ行
        elif r.get("op") == "forget":
            last_forget[n] = i
    out = [(last_done[n], items[n], d) for n, d in done.items()
           if n in items and n in last_done and d.get("unneeded") is True and d.get("by") == "user" and _num(d.get("at"))
           and last_done[n] > last_forget.get(n, -1)]
    return [(r, d) for _, r, d in sorted(out, key=lambda p: -p[0])]


def _float(v: str) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if _num(f) else None


def why_of(d: dict) -> str:
    return clip(d.get("why"), "title") if isinstance(d.get("why"), str) else ""


def _fresh(r: dict) -> float:
    """一覧の古さを数える起点（記録した時刻。戻したものは戻した時刻）。"""
    return max(r["at"], r.get("moved", 0))


def _when(r: dict) -> float:
    """届いた時刻（--sent。無ければ記録した時刻）。"""
    return r["sent"] if _num(r.get("sent")) else r["at"]


class Refused(Exception):
    """書き込みの錠の中で照らした前提が合わなかった（書いていない）。"""


def _append(row: dict, check=None) -> dict:
    """1 行を書き足す。check（行の一覧 → 断る理由か None）があれば、錠の中で読み直した記録で照らし、合わなければ Refused。"""
    FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FILE.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        rows = _rows()
        if check is not None:
            why = check(rows)
            if why:
                raise Refused(why)
        if row["op"] == "add":
            row["n"] = max([0] + [r["n"] for r in rows if r.get("op") == "add" and _n(r.get("n"))]) + 1
        with open(FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ud800-\udfff]")


KEY_MAX = 1000  # 照合の鍵の長さの上限。切ると末尾だけが違う鍵どうしが同じになるので、切らずに断る


def raw_key(v: str | None) -> str:
    """照合の鍵。空白はそのまま残し（一字一句で照らすため）、制御文字と向きを変える文字だけを外す（切らない。長さは呼び出し側で断る）。"""
    return _CTRL.sub("", str(v or ""))


def clip(v: str | None, key: str) -> str:
    """1 行にそろえ、制御文字（端末の表示を書き換える ESC など）と向きを変える文字を空白にして、字数で切る。"""
    v = " ".join(_CTRL.sub(" ", str(v or "")).split())
    n = LIMITS[key]
    return v if len(v) <= n else v[: n - 1] + "…"


def show(r: dict, done: dict) -> str:
    t = dt.datetime.fromtimestamp(r["at"], JST).strftime("%m/%d %H:%M")
    d = done.get(r["n"])
    mark = ("済（AI が根拠から）" if d.get("by") == "ai" else
            (f"済（不要だった{'：' + why_of(d) if why_of(d) else ''}）" if d.get("unneeded") is True else "済")) if d else \
        {"judge": "判断待ち", "fyi": "宛て・対応不要", "info": "情報", "baseline": "既読扱い"}.get(r["kind"], r["kind"])
    s = f"[{r['n']}] {t} {mark}｜{clip(r.get('project'), 'project')}｜{clip(r['source'], 'project')}:{clip(r['id'], 'id')}｜{clip(r['title'], 'title')}"
    if r.get("note"):
        s += f"\n    要ること: {clip(r['note'], 'note')}"
    if r.get("prepared"):
        s += f"\n    準備したこと: {clip(r['prepared'], 'prepared')}"
    if r.get("key"):
        s += f"\n    照合の鍵: {raw_key(r['key'])}"
    if d and d.get("note"):
        s += f"\n    片づけ: {clip(d['note'], 'note')}"
    c = r.get("checked")
    if not d and isinstance(c, dict):
        s += f"\n    確かめた: {dt.datetime.fromtimestamp(c['at'], JST).strftime('%m/%d %H:%M')} {CHECKED.get(c['result'], '')} {clip(c.get('note'), 'note')}".rstrip()
    if not d and r.get("moved") is not None:
        s += f"\n    判断待ちに戻した: {dt.datetime.fromtimestamp(r['moved'], JST).strftime('%m/%d %H:%M')} {clip(r.get('moved_note'), 'note')}".rstrip()
    return s


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seen"); s.add_argument("source"); s.add_argument("id")
    s = sub.add_parser("unseen"); s.add_argument("lines", nargs="*"); s.add_argument("--current", action="store_true")
    s = sub.add_parser("add")
    s.add_argument("--source", required=True, choices=sorted(SOURCES)); s.add_argument("--id", required=True)
    s.add_argument("--project", default="未振り分け"); s.add_argument("--kind", required=True, choices=sorted(KINDS))
    s.add_argument("--title", required=True); s.add_argument("--note", default=""); s.add_argument("--prepared", default=""); s.add_argument("--key", default="")
    s.add_argument("--sent", type=float)
    s = sub.add_parser("open"); s.add_argument("--project"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("recent"); s.add_argument("--hours", type=float, default=24)
    s = sub.add_parser("find"); s.add_argument("--key", required=True); s.add_argument("--before", type=float); s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("done"); s.add_argument("n", type=int); s.add_argument("--note", default=""); s.add_argument("--by", choices=("user", "ai"), default="user")
    s.add_argument("--unneeded", action="store_true"); s.add_argument("--why", default=""); s.add_argument("--expect-moved")
    s = sub.add_parser("reopen"); s.add_argument("n", type=int); s.add_argument("--note", default=""); s.add_argument("--expect-ai-at")
    sub.add_parser("lessons")
    s = sub.add_parser("forget"); s.add_argument("n", type=int)
    s = sub.add_parser("undo"); s.add_argument("n", type=int); s.add_argument("--note", default="")
    s = sub.add_parser("checked"); s.add_argument("n", type=int); s.add_argument("--result", required=True, choices=sorted(CHECKED)); s.add_argument("--note", default="")
    s.add_argument("--expect-moved")
    sub.add_parser("projects")
    a = p.parse_args()
    rows = _rows()
    items, done = _state(rows)
    if a.cmd == "seen":
        hit = any(r["source"] == a.source and r["id"] == a.id for r in items.values())
        print("seen" if hit else "new")
        return 0 if hit else 1
    if a.cmd == "unseen":
        if a.current and a.lines:
            print("--current と行は一緒に渡せません（--current だけを打ってください）")
            return 2
        if a.current:
            given, why = _current_lines()
            if given is None:
                print(f"見張りの台本の今の出力を読めません: {why}。差分の + の行を、そのまま vl_items.py unseen \"<行>\" … で渡してください")
                return 2
        else:
            given = a.lines or sys.stdin.read().splitlines()
        known = {(r["source"], r["id"]) for r in items.values()}
        new, old, odd, picked, odd_lines = [], 0, 0, set(), []
        for raw in given:
            line = raw.strip()
            line = line[1:].strip() if line.startswith("+") else line
            src, _, rest = line.partition(" ")
            if src not in _LINE or rest.startswith("読めない"):
                continue  # 差分の見出し・口の見出し・「読めない」の行
            m = _LINE[src].fullmatch(line)
            if not m:
                odd += 1  # 黙って捨てず、数と行の頭を出す（見張りの台本と食い違っている）
                odd_lines.append(f"形の合わない行: {src} {re.sub(r'[^A-Za-z0-9_.@:+=/|-]', '?', rest.split(' ')[0])[:40]}")
                continue
            key = (src, m.group(1))
            if key in picked:
                continue
            picked.add(key)
            if key in known:
                old += 1
            else:
                new.append(line)
        print("\n".join(new + odd_lines[:10] + [f"新しい出来事 {len(new)} 件（扱い済み {old} 件・形の合わない行 {odd} 件）"]))
        return 0
    if a.cmd in ("add", "find") and len(raw_key(a.key)) > KEY_MAX:
        print(f"照合の鍵が長すぎます（{KEY_MAX} 字まで。切らずに断ります）")
        return 2
    if a.cmd == "add":
        if not _RECORD_ID.fullmatch(a.id):
            print("id の形が合いません（見張りの行の 2 つ目の語を、そのまま渡してください）")
            return 2
        if any(r["source"] == a.source and r["id"] == a.id for r in items.values()):
            print("既に記録にあります（追加しません）")
            return 0
        r = _append({"op": "add", "at": time.time(), "source": a.source, "id": a.id, "project": clip(a.project, "project") or "未振り分け",
                     "kind": a.kind, "title": clip(a.title, "title"), "note": clip(a.note, "note"), "prepared": clip(a.prepared, "prepared"),
                     **({"key": raw_key(a.key)} if raw_key(a.key) else {}),
                     **({"sent": a.sent} if a.sent is not None and math.isfinite(a.sent) and a.sent > 0 else {})})
        print(f"記録しました [{r['n']}]")
        return 0
    if a.cmd == "open":
        rs = [r for r in items.values() if r["kind"] == "judge" and r["n"] not in done and (not a.project or r.get("project") == a.project)]
        near = rs if a.all else [r for r in rs if time.time() - _fresh(r) < OPEN_DAYS * 86400]
        out = [show(r, done) for r in sorted(near, key=lambda r: -_fresh(r))]
        if len(rs) > len(near):
            out.append(f"ほかに、記録してから {OPEN_DAYS} 日より前の判断待ちが {len(rs) - len(near)} 件"
                       "（済んでいるかは確かめていません。--all で並べます）")
        print("\n".join(out) or "判断待ちはありません")
        return 0
    if a.cmd == "recent":
        rs = [r for r in items.values() if time.time() - r["at"] < a.hours * 3600 and r["kind"] != "baseline"]
        print("\n".join(show(r, done) for r in sorted(rs, key=lambda r: -r["at"])) or "記録はありません")
        return 0
    if a.cmd == "find":  # 見張りの回が、アラームの記録を古さを問わず照らすため（手順 6）。鍵の一字一句の一致だけを見る（含む・正規表現にしない）
        k = raw_key(a.key)
        if not k.strip():
            print("照合の鍵が空です")
            return 2
        rs = sorted((r for r in items.values() if r["kind"] != "baseline" and r.get("key") == k and (a.before is None or _when(r) < a.before)),
                    key=lambda r: -_when(r))
        n = max(1, a.limit)
        print("\n".join([show(r, done) for r in rs[:n]] + ([f"ほかに {len(rs) - n} 件（--limit で増やせます）"] if len(rs) > n else [])) or "当たる記録はありません")
        return 0
    if a.cmd == "lessons":
        ls = lessons(rows, items, done)
        out = []
        for r, d in ls[:LESSONS_MAX]:
            t = dt.datetime.fromtimestamp(d["at"], JST).strftime("%m/%d")
            out.append(f"[{r['n']}] {t} 不要だった（{why_of(d) or '理由なし'}）｜{clip(r.get('project'), 'project')}｜{clip(r['source'], 'project')}:{clip(r['id'], 'id')}｜{clip(r['title'], 'title')}")
            if r.get("key"):
                out.append(f"    照合の鍵: {raw_key(r['key'])}")
        if len(ls) > LESSONS_MAX:
            out.append(f"ほかに、古い手本が {len(ls) - LESSONS_MAX} 件（使いません）")
        print("\n".join(out) or "手本はありません")
        return 0
    expect = None  # 本体が読んだときの状態（書き込みの錠の中で照らす）
    if getattr(a, "expect_moved", None) is not None:
        want = None if a.expect_moved == "none" else _float(a.expect_moved)
        if a.expect_moved != "none" and want is None:
            print("--expect-moved は時刻（数）か none です")
            return 2

        def expect(rs, n=a.n, want=want):
            it, dn = _state(rs)
            r = it.get(n)
            if not r or r.get("kind") != "judge" or n in dn:
                return "判断待ちではなくなっていました"
            return "" if r.get("moved") == want else "判断待ちに戻した印が替わっていました"
    elif getattr(a, "expect_ai_at", None) is not None:
        want = _float(a.expect_ai_at)
        if want is None:
            print("--expect-ai-at は時刻（数）です")
            return 2

        def expect(rs, n=a.n, want=want):
            d = _state(rs)[1].get(n)
            return "" if d and d.get("by") == "ai" and d.get("at") == want else "AI が付けた、その済の印ではなくなっていました"
    if a.cmd in ("done", "reopen", "forget", "undo", "checked") and a.n not in items:
        print(f"{a.n} 番の記録はありません")
        return 1
    if a.cmd == "done":
        if a.unneeded and a.by != "user":
            print("不要の印（見張りの手本）は、利用者が「不要だった」と言ったときだけ付けます（--by user）")
            return 2
        if expect is not None and a.n in done:  # 本体が読んだあとで片づいた（前提が合わない）
            print(f"書きませんでした [{a.n}]: 判断待ちではなくなっていました")
            return 3
        # 済の印がある件も、手本でなければ不要の印を付け直せる（「済んだ」を押したあとで「不要だった」と言われた・手本から外したのを戻す）
        if a.n in done and (not a.unneeded or any(r["n"] == a.n for r, _ in lessons(rows, items, done))):
            print(f"既に片づけた印があります [{a.n}]" + ("（手本にもなっています）" if a.unneeded else ""))
            return 0
        if a.by == "ai" and not clip(a.note, "note"):
            print("あなたが確かめて片づけるときは、根拠（何で済んだと確かめたか）を --note に書いてください")
            return 2
        try:
            _append({"op": "done", "at": time.time(), "n": a.n, "note": clip(a.note, "note"), "by": a.by,
                     **({"unneeded": True, "why": clip(a.why, "title")} if a.unneeded else {})}, expect)
        except Refused as e:
            print(f"書きませんでした [{a.n}]: {e}")
            return 3
        print(f"片づけた印を付けました [{a.n}]" + ("（不要だった。見張りの手本にします）" if a.unneeded else ""))
        return 0
    if a.cmd == "undo":
        d = done.get(a.n)
        if not d or d.get("by") != "ai":
            print(f"[{a.n}] には、あなた（AI）が付けた済の印がありません（利用者の印は取り消せません。利用者が「まだ」と言ったら reopen）")
            return 1
        _append({"op": "undo", "at": time.time(), "n": a.n, "note": clip(a.note, "note")})
        print(f"あなたが付けた済の印を取り消しました [{a.n}]（判断待ちに戻りました）")
        return 0
    if a.cmd == "checked":
        if a.n in done or items[a.n]["kind"] != "judge":
            print(f"[{a.n}] は判断待ちではありません")
            return 1
        try:
            _append({"op": "checked", "at": time.time(), "n": a.n, "result": a.result, "note": clip(a.note, "note")}, expect)
        except Refused as e:
            print(f"書きませんでした [{a.n}]: {e}")
            return 3
        print(f"確かめた結果を残しました [{a.n}]（{CHECKED[a.result]}）")
        return 0
    if a.cmd == "forget":
        if not any(r["n"] == a.n for r, _ in lessons(rows, items, done)):
            print(f"[{a.n}] は手本にありません")
            return 1
        _append({"op": "forget", "at": time.time(), "n": a.n})
        print(f"手本から外しました [{a.n}]（済の印はそのままです）")
        return 0
    if a.cmd == "reopen":
        if a.n not in done and items[a.n]["kind"] == "judge":
            print(f"[{a.n}] は既に判断待ちです")
            return 1
        try:
            _append({"op": "reopen", "at": time.time(), "n": a.n, "note": clip(a.note, "note")}, expect)
        except Refused as e:
            print(f"書きませんでした [{a.n}]: {e}")
            return 3
        print(f"判断待ちに戻しました [{a.n}]")
        return 0
    if a.cmd == "projects":
        c = Counter(clip(r.get("project"), "project") or "未振り分け" for r in items.values() if r["kind"] != "baseline")
        print("\n".join(f"{k}（{v}）" for k, v in c.most_common()) or "まだありません")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
