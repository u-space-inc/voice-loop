"""選択肢の問い（Claude Code の AskUserQuestion）に、画面と Slack（Hermes のボタン）から答えるための部品（2026-11-17 本人の要望・決定）。

問いの中身は、セッションの記録（transcript の JSONL）にある、まだ答えの無い AskUserQuestion の入力から組む（問い・見出し・選択肢・説明・
複数選ぶか・見本）。画面の文字は揺れるので、中身の読み取りには使わない。使うのは、「いまその問いが出ていて、まだ何も答えていない」ことと、
答えを送る途中の 1 手ごとの進み（いまどの問いか・カーソル・印・最後の確認の画面）の確かめだけ。

キーの動き（2026-11-17 に Claude Code 2.1.326 の実画面で確かめた）:
- 1 つ選ぶ（見本なし）: 数字キーで選ばれ、次の問いへ進む（問いが 1 つだけなら、そのまま送られる）
- 1 つ選ぶ（見本つき）: 数字キーはカーソルを動かすだけ。Enter で選ばれて進む
- 自由記入（「Type something.」。見本なしの 1 つ選ぶ問いだけ）: その番号で入力欄に入り、文字を打って Enter で進む
- 複数選ぶ: 数字キーで印を付け外しする。Tab で次の問い（最後なら確認の画面）へ
- 確認の画面（問いが 2 つ以上か、複数選ぶ問いのとき）: 「Review your answers」に問いごとの答え、「1. Submit answers」で送る

送る途中は 1 手ごとに画面を確かめ、思った形にならなければ、そこで止まる（それ以上キーを送らない。続きは端末で）。
最後の確認の画面では、問いごとの答えが送るつもりのものと一字一句同じか（空白は問わない）を確かめてから送る。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

TAIL = 4_000_000  # 記録の末尾から読む大きさ
MAX_Q, MIN_OPT, MAX_OPT = 4, 2, 4  # AskUserQuestion の決まり（問いは 1〜4、選択肢は 2〜4）
OTHER_MAX = 300  # 自由記入の字数の上限
WAIT, STEP = 5.0, 0.25  # 1 手ごとに画面が変わるのを待つ秒数（読み取りの時間も含めた実の時間）と、見直す間隔
READ_MAX = 5.0  # 1 回の画面の読み取りの上限（呼び出し側が読み取りに渡す）
DRIVE_MAX = 60.0  # 送り始めてから終わるまでの上限（実の時間。答えの鍵を持ったまま待ち続けない）

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿\ud800-\udfff]")
_BAR = re.compile(r"^\s*(?:←\s+)?[☐☒] ")  # 見出しの行（問いが 1 つなら「 ☐ 朝」、複数なら「←  ☐ 朝  ☒ 色  ✔ Submit  →」）
_OPT = re.compile(r"^(❯ | {2})(\d+)\. (.*)$")
_BOX = re.compile(r"\s{2,}[┌│└].*$")  # 見本の枠（選択肢の行の右に出る）
_MARK = re.compile(r"^\[([ ✔✓x])\] ")  # 複数選ぶ問いの印


def _flat(s: str) -> str:
    """比べるための形（空白をすべて外す。折り返しと字下げを問わない）。"""
    return "".join(str(s).split())


def _line(s, n: int) -> str:
    """1 行にそろえ、制御文字・向きを変える文字を空白にして n 字で切る。"""
    v = " ".join(_CTRL.sub(" ", str(s or "")).split())
    return v if len(v) <= n else v[: n - 1] + "…"


# ---------- 記録から問いを組む ----------
def form_of(raw) -> dict | None:
    """AskUserQuestion の入力を確かめて、画面と Hermes に渡す形にする。決まりの外の形（数・型・同じ問いが 2 つ）なら None。"""
    qs = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(qs, list) or not 1 <= len(qs) <= MAX_Q:
        return None
    out = []
    for q in qs:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not q["question"].strip():
            return None
        opts = q.get("options")
        if not isinstance(opts, list) or not MIN_OPT <= len(opts) <= MAX_OPT:
            return None
        o2 = []
        for o in opts:
            if not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o["label"].strip():
                return None
            desc, prev = o.get("description", ""), o.get("preview")
            if not isinstance(desc, str) or (prev is not None and not isinstance(prev, str)):
                return None
            o2.append({"label": o["label"], "desc": desc, "preview": prev or ""})
        if len({_flat(o["label"]) for o in o2}) != len(o2):
            return None
        multi = q.get("multiSelect", False)
        if not isinstance(multi, bool):
            return None
        preview = any(o["preview"] for o in o2)
        out.append({"question": q["question"], "header": q.get("header") if isinstance(q.get("header"), str) else "",
                    "multi": multi, "preview": preview, "other": not multi and not preview, "options": o2})
    if len({_flat(q["question"]) for q in out}) != len(out):
        return None
    return {"questions": out}


def pending(path: Path | None) -> tuple[str, dict] | None:
    """記録の中で、まだ答え（tool_result）の無い、いちばん新しい AskUserQuestion の (id, 問いの形)。無ければ None。"""
    if not path or not path.exists():
        return None
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - TAIL))
        data = f.read().decode("utf-8", "replace")
    asks, answered = [], set()
    for line in data.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        c = (d.get("message") or {}).get("content") if isinstance(d, dict) else None
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion" and isinstance(b.get("id"), str):
                asks.append((b["id"], b.get("input")))
            elif b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                answered.add(b["tool_use_id"])
    for tid, raw in reversed(asks):
        if tid not in answered:
            f2 = form_of(raw)
            return (tid, f2) if f2 else None
        return None  # いちばん新しいものに答えがある = 待っている問いは無い
    return None


def recorded(path: Path | None, tid: str) -> dict | None:
    """記録に、その問い（tool_use の id）への答え（tool_result）があれば、問いごとの答え {問いの文: 答え}。答えの中身が無ければ {}。
    まだ無ければ None。送ったあとに「届いた」を確かめるため（送った直後に次の問いが出ていても、記録で分かる）。"""
    if not path or not path.exists():
        return None
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - TAIL))
        data = f.read().decode("utf-8", "replace")
    for line in reversed(data.splitlines()):
        if tid not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        c = (d.get("message") or {}).get("content") if isinstance(d, dict) else None
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == tid for b in c):
            r = d.get("toolUseResult")
            ans = r.get("answers") if isinstance(r, dict) else None
            return {str(k): str(v) for k, v in ans.items()} if isinstance(ans, dict) else {}
    return None


def public(form: dict) -> dict:
    """画面と Hermes に出す形（字数で切り、1 行にそろえる。見本は改行を残して切る）。番号は 1 から。"""
    return {"questions": [{"header": _line(q["header"], 40), "question": _line(q["question"], 600), "multi": q["multi"],
                           "preview": q["preview"], "other": q["other"],
                           "options": [{"n": i + 1, "label": _line(o["label"], 120), "desc": _line(o["desc"], 300),
                                        **({"preview": "\n".join(_CTRL.sub(" ", l) for l in o["preview"].split("\n"))[:1200]} if o["preview"] else {})}
                                       for i, o in enumerate(q["options"])]} for q in form["questions"]]}


def form_text(form: dict) -> str:
    """取り消しにくい操作の印（RISKY）を探すための、問いの文の全体。"""
    return "\n".join([q["header"] + " " + q["question"] for q in form["questions"]] +
                     [o["label"] + " " + o["desc"] + " " + o["preview"] for q in form["questions"] for o in q["options"]])


# ---------- 答えを確かめる ----------
def check(form: dict, answers) -> tuple[list | None, str]:
    """画面・Hermes から来た答えを確かめて、(そろえた答え, "") か (None, 断る理由)。
    答えは問いごとに {"pick": 番号}（1 つ選ぶ）・{"picks": [番号, …]}（複数選ぶ）・{"other": 文}（自由記入。見本なしの 1 つ選ぶ問いだけ）。"""
    qs = form["questions"]
    if not isinstance(answers, list) or len(answers) != len(qs):
        return None, "bad_form"
    out = []
    for q, a in zip(qs, answers):
        if not isinstance(a, dict) or len(a) != 1:
            return None, "bad_form"
        n = len(q["options"])
        if q["multi"]:
            ps = a.get("picks")
            if not isinstance(ps, list) or not ps or any(type(p) is not int or not 1 <= p <= n for p in ps) or len(set(ps)) != len(ps):
                return None, "bad_form"
            out.append({"picks": sorted(ps)})
        elif "pick" in a:
            p = a["pick"]
            if type(p) is not int or not 1 <= p <= n:
                return None, "bad_form"
            out.append({"pick": p})
        elif "other" in a:
            t = _line(a["other"], OTHER_MAX + 1) if isinstance(a["other"], str) else ""
            if not q["other"] or not t or len(t) > OTHER_MAX:
                return None, "bad_form"
            out.append({"other": t})
        else:
            return None, "bad_form"
    return out, ""


def said(form: dict, answers: list) -> str:
    """送った答えの短い言い方（記録と知らせに使う）。"""
    parts = []
    for q, a in zip(form["questions"], answers):
        v = (", ".join(q["options"][p - 1]["label"] for p in a["picks"]) if "picks" in a else
             q["options"][a["pick"] - 1]["label"] if "pick" in a else f"（自由記入）{a['other']}")
        parts.append(f"［{_line(q['header'] or q['question'], 20)}］{_line(v, 80)}")
    return "／".join(parts)


def _expect(q: dict, a: dict) -> str:
    """確認の画面に出るはずの答え。"""
    if "picks" in a:
        return ", ".join(q["options"][p - 1]["label"] for p in a["picks"])
    return q["options"][a["pick"] - 1]["label"] if "pick" in a else a["other"]


# ---------- 画面を読む ----------
def state(text: str, form: dict) -> dict:
    """画面のいまの段。{"stage": "question", "index", "cursor", "checked", "answered", "other", "typing"} /
    {"stage": "review", "answers": [(問い, 答え)], "submit": bool} / {"stage": "gone"}（問いが画面に無い） / {"stage": "unknown", "why"}。"""
    if "�" in text:
        return {"stage": "unknown", "why": "odd"}
    lines = [l.rstrip() for l in text.split("\n") if l.strip() and not set(l.strip()) <= set("─╌")]
    bar_i = next((i for i in range(len(lines) - 1, -1, -1) if _BAR.match(lines[i])), None)
    if bar_i is None:
        return {"stage": "gone"}
    qs = form["questions"]
    items = [x for x in re.split(r"\s{2,}", lines[bar_i].strip().strip("←→").strip()) if x]
    chips = [x for x in items if x[:1] in "☐☒"]
    if [_flat(c[1:]) for c in chips] != [_flat(q["header"]) for q in qs]:
        return {"stage": "unknown", "why": "headers"}
    answered = [c[0] == "☒" for c in chips]
    body = lines[bar_i + 1:]
    if body and body[0].strip() == "Review your answers":
        got, cur = [], None
        for l in body[1:]:
            s = l.strip()
            if s.startswith("Ready to submit"):
                break
            if s.startswith("● "):
                cur = [s[2:], None]
                got.append(cur)
            elif s.startswith("→ ") and cur is not None and cur[1] is None:
                cur[1] = s[2:]
            elif cur is not None:  # 折り返した問い・答え
                cur[0 if cur[1] is None else 1] += s
        sub = [m for m in map(_OPT.match, body) if m]
        return {"stage": "review", "answers": [(a, b or "") for a, b in got],
                "submit": any(m.group(2) == "1" and m.group(3).strip() == "Submit answers" for m in sub)}
    first = next((i for i, l in enumerate(body) if _OPT.match(l)), None)
    if not first:
        return {"stage": "unknown", "why": "no_options"}
    asked = _flat("".join(body[:first]))
    idx = next((i for i, q in enumerate(qs) if _flat(q["question"]) == asked), None)
    if idx is None:
        return {"stage": "unknown", "why": "question"}
    q = qs[idx]
    opts = {}
    cursor = None
    for l in body[first:]:
        m = _OPT.match(l)
        if not m:
            continue
        n, label = int(m.group(2)), _BOX.sub("", m.group(3)).strip()
        if n in opts:
            continue
        opts[n] = label
        if m.group(1) == "❯ ":
            cursor = n
    checked = set()
    for i, o in enumerate(q["options"], 1):
        label = opts.get(i)
        if label is None:
            return {"stage": "unknown", "why": "options"}
        mk = _MARK.match(label)
        if q["multi"]:
            if not mk:
                return {"stage": "unknown", "why": "marks"}
            if mk.group(1) != " ":
                checked.add(i)
            label = label[mk.end():]
        elif mk:
            return {"stage": "unknown", "why": "marks"}
        if _flat(label) != _flat(o["label"]):
            return {"stage": "unknown", "why": "labels"}
    other = None
    k = len(q["options"]) + 1
    if q["other"] and k in opts and _flat(opts[k]) != "Typesomething.":  # 自由記入の文（端末の幅で折り返した続きの行もつなぐ）
        at = next(j for j in range(first, len(body)) if (m := _OPT.match(body[j])) and int(m.group(2)) == k)
        other = opts[k]
        for l in body[at + 1:]:
            if _OPT.match(l) or not l.startswith("  "):
                break
            other += l.strip()
    elif q["multi"] and k in opts and _flat(opts[k]) != "[]Typesomething":
        other = opts[k]  # 複数選ぶ問いで自由記入に印が付いている（答え始めている）
    foot = lines[-1]
    return {"stage": "question", "index": idx, "cursor": cursor, "checked": checked, "answered": answered,
            "other": other, "typing": "to edit in" in foot}


def ready(text: str, form: dict) -> str:
    """答え始めてよい画面か（最初の問いが出ていて、まだどの問いにも答えていない）。よければ ""、だめなら理由。"""
    st = state(text, form)
    if st["stage"] != "question":
        return st.get("why", st["stage"])
    if st["index"] != 0 or any(st["answered"]) or st["checked"] or st["other"] or st["typing"]:
        return "started"  # 端末で答え始めている。続きは端末で
    return ""


# ---------- 1 手ずつ確かめながら送る ----------
class Stuck(Exception):
    pass


async def drive(form: dict, answers: list, read, keys, text, done=None) -> None:
    """答えを送る。read() は画面の文字、keys(k, left) はキー、text(s, left) は文字を送る（どれも async。left は残りの秒数で、送る処理は
    それを超えて動き続けないこと）。done() は、記録に残った答え（recorded の形。無ければ None）。思った段にならなければ Stuck。"""
    qs = form["questions"]
    clock = asyncio.get_running_loop().time
    last = clock() + DRIVE_MAX

    send_keys, send_text = keys, text

    def left() -> float:  # 上限を過ぎたら、それ以上キーを送らない
        n = last - clock()
        if n <= 0:
            raise Stuck("送り始めてからの上限の時間を過ぎた")
        return n

    # 送る処理には残りの秒数を渡し、送る処理そのもの（herdr の呼び出し）をそこで打ち切ってもらう。
    # ここで待ちだけを打ち切ると、裏で送る処理が続き、止まったと返したあとにキーが届きうる（その間に答えの鍵が離れる）
    async def keys(k):  # noqa: F811
        await send_keys(k, left())

    async def text(t):  # noqa: F811
        await send_text(t, left())

    async def until(ok, what: str) -> dict:
        end = min(clock() + WAIT, last)  # 読み取りにかかった時間も数える
        while True:
            st = state(await read(), form)
            if ok(st):
                return st
            if clock() >= end:
                raise Stuck(f"{what}（画面: {st['stage']}{'・' + st['why'] if st.get('why') else ''}）")
            await asyncio.sleep(STEP)

    want_rec = {_flat(q["question"]): _flat(_expect(q, a)) for q, a in zip(qs, answers)}

    async def finished(what: str):
        """送れたと確かめる: 記録にこの問いへの答えが残り、送るつもりのものと同じ（送った直後に次の問いが出ていても分かる）か、
        問いが画面から消えた。記録の答えが違えば、送れたとしない。"""
        end = min(clock() + WAIT, last)
        while True:
            rec = await done() if done else None
            if rec is not None:
                if {_flat(k): _flat(v) for k, v in rec.items()} == want_rec:
                    return
                raise Stuck("セッションの記録に残った答えが、送るつもりのものと違う")
            if state(await read(), form)["stage"] == "gone":
                return
            if clock() >= end:
                raise Stuck(what)
            await asyncio.sleep(STEP)

    on = lambda i: (lambda st: st["stage"] == "question" and st["index"] == i)  # noqa: E731
    review = len(qs) > 1 or qs[0]["multi"]
    why = ready(await read(), form)
    if why:  # 送り始める前に、まだ何も答えていないことを確かめる（端末で答え始めていたら、キーを 1 本も送らない）
        raise Stuck(f"送り始める前の画面が、まだ答えていない最初の問いではない（{why}）")
    for i, (q, a) in enumerate(zip(qs, answers)):
        st = await until(on(i), f"{i + 1} つ目の問いが出ない")
        if (st["answered"][i] or st["checked"] or st["other"] or st["typing"]  # その問いの最初のキーを決める画面そのもので確かめる
                or not all(st["answered"][:i]) or any(st["answered"][i + 1:])):
            raise Stuck(f"{i + 1} つ目の問いを、送る前に端末で答え始めている")
        if "picks" in a:
            for p in a["picks"]:
                if p not in st["checked"]:
                    await keys(str(p))
                    st = await until(lambda s, p=p: on(i)(s) and p in s["checked"], f"{i + 1} つ目の {p} 番に印が付かない")
            if st["checked"] != set(a["picks"]):
                raise Stuck(f"{i + 1} つ目の印が答えと違う")
            await keys("tab")
        elif "other" in a:
            k = len(q["options"]) + 1
            await keys(str(k))
            await until(lambda s: on(i)(s) and s["cursor"] == k and s["typing"], f"{i + 1} つ目の自由記入の欄に入れない")
            await text(a["other"])
            await until(lambda s: on(i)(s) and s["other"] is not None and _flat(s["other"]) == _flat(a["other"]), f"{i + 1} つ目の自由記入の文が出ない")
            await keys("enter")
        else:
            p = a["pick"]
            await keys(str(p))
            if q["preview"]:
                await until(lambda s: on(i)(s) and s["cursor"] == p, f"{i + 1} つ目のカーソルが {p} 番に来ない")
                await keys("enter")
        if i + 1 < len(qs):
            continue
        if not review:  # 問いが 1 つで 1 つ選ぶ形は、ここで送られる
            await finished("送ったあとも問いが残っているか、画面を読み取れない")
            return
    st = await until(lambda s: s["stage"] == "review", "確認の画面が出ない")
    want = [(_flat(q["question"]), _flat(_expect(q, a))) for q, a in zip(qs, answers)]
    got = [(_flat(x), _flat(y)) for x, y in st["answers"]]
    if got != want or not st["submit"]:
        raise Stuck("確認の画面の答えが、送るつもりのものと違う")
    await keys("1")
    await finished("送ったあとも確認の画面が残っているか、画面を読み取れない")
