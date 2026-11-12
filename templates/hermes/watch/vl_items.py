"""voice-loop の見張りの記録（出来事ごとに 1 行）。見張りの回（cron）が書き、ふだんの会話が読む。

  vl_items.py seen <source> <id>        記録にあれば 0 で終わる（無ければ 1）。同じ出来事を二度扱わないために使う
  vl_items.py unseen ["<行>" ...]       見張りの行（引数か標準入力。先頭の + は外す）から、まだ記録に無い出来事の行だけを出す
  vl_items.py add --source mail --id X --project P --kind judge|info|baseline --title T [--note N] [--prepared 準備したこと]
  vl_items.py open [--project P]       判断待ち（kind=judge で、まだ片づいていないもの）を新しい順に
  vl_items.py recent [--hours 24]      直近の記録（種類を問わず）を新しい順に
  vl_items.py done <番号> [--note N]   判断待ちを片づけた印を付ける（ユーザーが「済んだ」と言ったとき）
  vl_items.py projects                 これまでに使った案件の名前と件数

記録は HERMES_HOME/voice-loop/items.jsonl。書くのはこの道具だけ（手で書き換えない）。中身はメール・Slack・録音からの要約（引用データ）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or "/opt/data")
FILE = HOME / "voice-loop" / "items.jsonl"
SOURCES = {"mail", "slack", "plaud", "cal"}
KINDS = {"judge", "info", "baseline"}
LIMITS = {"title": 80, "note": 600, "prepared": 600, "project": 40, "id": 200}
JST = dt.timezone(dt.timedelta(hours=9))  # 表示の時刻。コンテナの時計は UTC なので、決めて付ける
# 見張りの行の形（vl_watch.py と同じ。変えるときは両方を変える）。2 つ目の語（括弧の中）が記録の id
_ID = r"[A-Za-z0-9_.@:+=-]{1,200}"
_LINE = {
    "mail": re.compile(rf"mail ({_ID})"),
    "slack": re.compile(r"slack ([A-Z0-9]{1,40}/[0-9]{1,12}\.[0-9]{1,12}) (?:[A-Z0-9]{1,40}|-)"),
    "cal": re.compile(rf"cal ({_ID})"),
    "plaud": re.compile(rf"plaud ({_ID}) \| {_ID}"),
}
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


def _state(rows: list[dict]) -> tuple[dict, dict]:
    """(番号→出来事, 番号→片づけた印)。"""
    items, done = {}, {}
    for r in rows:
        if r.get("op") == "add":
            items[r["n"]] = r
        elif r.get("op") == "done":
            done[r["n"]] = r
    return items, done


def _append(row: dict) -> dict:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FILE.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        rows = _rows()
        if row["op"] == "add":
            row["n"] = max([0] + [r.get("n", 0) for r in rows if r.get("op") == "add"]) + 1
        with open(FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ud800-\udfff]")


def clip(v: str | None, key: str) -> str:
    """1 行にそろえ、制御文字（端末の表示を書き換える ESC など）と向きを変える文字を空白にして、字数で切る。"""
    v = " ".join(_CTRL.sub(" ", str(v or "")).split())
    n = LIMITS[key]
    return v if len(v) <= n else v[: n - 1] + "…"


def show(r: dict, done: dict) -> str:
    t = dt.datetime.fromtimestamp(r["at"], JST).strftime("%m/%d %H:%M")
    mark = "済" if r["n"] in done else {"judge": "判断待ち", "info": "情報", "baseline": "既読扱い"}.get(r["kind"], r["kind"])
    s = f"[{r['n']}] {t} {mark}｜{clip(r['project'], 'project')}｜{clip(r['source'], 'project')}:{clip(r['id'], 'id')}｜{clip(r['title'], 'title')}"
    if r.get("note"):
        s += f"\n    要ること: {clip(r['note'], 'note')}"
    if r.get("prepared"):
        s += f"\n    準備したこと: {clip(r['prepared'], 'prepared')}"
    return s


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seen"); s.add_argument("source"); s.add_argument("id")
    s = sub.add_parser("unseen"); s.add_argument("lines", nargs="*")
    s = sub.add_parser("add")
    s.add_argument("--source", required=True, choices=sorted(SOURCES)); s.add_argument("--id", required=True)
    s.add_argument("--project", default="未振り分け"); s.add_argument("--kind", required=True, choices=sorted(KINDS))
    s.add_argument("--title", required=True); s.add_argument("--note", default=""); s.add_argument("--prepared", default="")
    s = sub.add_parser("open"); s.add_argument("--project")
    s = sub.add_parser("recent"); s.add_argument("--hours", type=float, default=24)
    s = sub.add_parser("done"); s.add_argument("n", type=int); s.add_argument("--note", default="")
    sub.add_parser("projects")
    a = p.parse_args()
    rows = _rows()
    items, done = _state(rows)
    if a.cmd == "seen":
        hit = any(r["source"] == a.source and r["id"] == a.id for r in items.values())
        print("seen" if hit else "new")
        return 0 if hit else 1
    if a.cmd == "unseen":
        known = {(r["source"], r["id"]) for r in items.values()}
        new, old, odd, picked = [], 0, 0, set()
        for raw in (a.lines or sys.stdin.read().splitlines()):
            line = raw.strip()
            line = line[1:].strip() if line.startswith("+") else line
            src, _, rest = line.partition(" ")
            if src not in _LINE or rest.startswith("読めない"):
                continue  # 差分の見出し・口の見出し・「読めない」の行
            m = _LINE[src].fullmatch(line)
            if not m:
                odd += 1  # 黙って捨てず、数を出す（見張りの台本と食い違っている）
                continue
            key = (src, m.group(1))
            if key in picked:
                continue
            picked.add(key)
            if key in known:
                old += 1
            else:
                new.append(line)
        print("\n".join(new + [f"新しい出来事 {len(new)} 件（扱い済み {old} 件・形の合わない行 {odd} 件）"]))
        return 0
    if a.cmd == "add":
        if not _RECORD_ID.fullmatch(a.id):
            print("id の形が合いません（見張りの行の 2 つ目の語を、そのまま渡してください）")
            return 2
        if any(r["source"] == a.source and r["id"] == a.id for r in items.values()):
            print("既に記録にあります（追加しません）")
            return 0
        r = _append({"op": "add", "at": time.time(), "source": a.source, "id": a.id, "project": clip(a.project, "project") or "未振り分け",
                     "kind": a.kind, "title": clip(a.title, "title"), "note": clip(a.note, "note"), "prepared": clip(a.prepared, "prepared")})
        print(f"記録しました [{r['n']}]")
        return 0
    if a.cmd == "open":
        rs = [r for r in items.values() if r["kind"] == "judge" and r["n"] not in done and (not a.project or r["project"] == a.project)]
        print("\n".join(show(r, done) for r in sorted(rs, key=lambda r: -r["at"])) or "判断待ちはありません")
        return 0
    if a.cmd == "recent":
        rs = [r for r in items.values() if time.time() - r["at"] < a.hours * 3600 and r["kind"] != "baseline"]
        print("\n".join(show(r, done) for r in sorted(rs, key=lambda r: -r["at"])) or "記録はありません")
        return 0
    if a.cmd == "done":
        if a.n not in items:
            print(f"{a.n} 番の記録はありません")
            return 1
        _append({"op": "done", "at": time.time(), "n": a.n, "note": clip(a.note, "note")})
        print(f"片づけた印を付けました [{a.n}]")
        return 0
    if a.cmd == "projects":
        c = Counter(r["project"] for r in items.values() if r["kind"] != "baseline")
        print("\n".join(f"{k}（{v}）" for k, v in c.most_common()) or "まだありません")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
