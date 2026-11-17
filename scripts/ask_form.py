"""選択肢の問い（Claude Code の AskUserQuestion）に、画面から 1 問ずつ答えるための部品（2026-11-17 本人の要望・決定）。

いまターミナルに出ている 1 問（または最後の確認の画面）だけを、画面の文字から読んでボタンにする。1 回押すごとに、確かめながら
キーを送り、画面を読み直して次の 1 問を出す。正しいのはいつもターミナルの画面で、こちらに答えの控えを持たない。
（Claude Code は、答えるまで問いをセッションの記録に書かないので、記録からは組めない。2026-11-17 に確かめた。
  この形は、herdr のプラグイン Collie（MIT、Altan Sarisin）の「いまの 1 問を写し、1 回押すごとに 1 キー」の考え方に倣った。
  Collie の測った結果（web/src/lib/grammar/WIZARD_NOTES.md・ASK_FIELD_NOTES.md）も参考にした）

キーの動き（2026-11-17 に Claude Code 2.1.326 の実画面で確かめた）:
- 1 つ選ぶ（見本なし）: 数字キーで選ばれ、次の問いへ進む（問いが 1 つだけなら、そのまま送られる）
- 1 つ選ぶ（見本つき）: 数字キーはカーソルを動かすだけ。Enter で選ばれて進む
- 自由記入（「Type something.」。見本なしの 1 つ選ぶ問いだけ）: 空の欄の番号で入力欄に入り、文字を打って Enter で進む
- 複数選ぶ: 数字キーで印を付け外しする。Tab で次の問い（最後なら確認の画面）へ
- 確認の画面（問いが 2 つ以上か、複数選ぶ問いのとき）: 「1」で送る
"""
from __future__ import annotations

import asyncio
import hashlib
import re

OTHER_MAX = 300  # 自由記入の字数の上限
WAIT, STEP = 4.0, 0.2  # 1 手ごとに画面が変わるのを待つ秒数（読み取りの時間も含めた実の時間）と、見直す間隔
READ_MAX = 5.0  # 1 回の画面の読み取り・キーを送る呼び出しの上限

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿\ud800-\udfff]")
_BAR = re.compile(r"^\s*(?:←\s+)?[☐☒] ")  # 見出しの行（問いが 1 つなら「 ☐ 朝」、複数なら「←  ☐ 朝  ☒ 色  ✔ Submit  →」）
_OPT = re.compile(r"^(❯ | {2}|↓ |↑ )(\d+)\. (.*)$")  # 選択肢の行（↓↑ は、選択肢が画面に収まらないときの印）
_BOX = re.compile(r"\s{2,}[┌│└].*$")  # 見本の枠（選択肢の行の右に出る）
_MARK = re.compile(r"^\[([ ✔✓x])\] ")  # 複数選ぶ問いの印
_RULE = re.compile(r"^\s*[─╌]{8,}\s*$")
FREE = ("Type something.", "Type something")  # 自由記入の欄の空のときの文字


def _flat(s: str) -> str:
    """比べるための形（空白をすべて外す。折り返しと字下げを問わない）。"""
    return "".join(str(s).split())


def _line(s, n: int) -> str:
    """1 行にそろえ、制御文字・向きを変える文字を空白にして n 字で切る。"""
    v = " ".join(_CTRL.sub(" ", str(s or "")).split())
    return v if len(v) <= n else v[: n - 1] + "…"


class Stuck(Exception):
    pass


# ---------- 画面を読む ----------
def step(text: str) -> dict:
    """いまの 1 問（または確認の画面）。画面の文字だけから読む。
    {"stage": "question", "chips": [(見出し, 答えたか)], "question", "multi", "preview", "options": [{n, label, desc, checked}],
     "free": 自由記入の欄の番号か None, "free_text": 欄に入っている文か "", "cursor", "typing", "notes", "more": 画面に収まらない選択肢があるか}
    {"stage": "review", "chips", "answers": [(問い, 答え)], "submit": bool} / {"stage": "gone"} / {"stage": "unknown", "why"}"""
    if "�" in text:
        return {"stage": "unknown", "why": "odd"}
    lines = [l.rstrip() for l in text.split("\n") if l.strip()]
    bar_i = next((i for i in range(len(lines) - 1, -1, -1) if _BAR.match(lines[i])), None)
    if bar_i is None:
        return {"stage": "gone"}
    items = [x for x in re.split(r"\s{2,}", lines[bar_i].strip().strip("←→").strip()) if x]
    chips = [(_line(x[1:], 40), x[0] == "☒") for x in items if x[:1] in "☐☒"]
    if not chips:
        return {"stage": "unknown", "why": "chips"}
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
            elif cur is not None and not _RULE.match(l):  # 折り返した問い・答え
                cur[0 if cur[1] is None else 1] += s
        sub = [m for m in map(_OPT.match, body) if m]
        return {"stage": "review", "chips": chips, "answers": [(_line(a, 300), _line(b or "", 300)) for a, b in got],
                "submit": any(m.group(2) == "1" and m.group(3).strip() == "Submit answers" for m in sub)}
    foot = body[-1] if body else ""
    if not ("Enter to select" in foot and "Esc to cancel" in foot):  # いちばん下が質問の操作案内でなければ、質問の画面と言い切らない
        return {"stage": "unknown", "why": "foot"}
    first = next((i for i, l in enumerate(body) if _OPT.match(l)), None)
    if not first:
        return {"stage": "unknown", "why": "no_options"}
    question = " ".join(l.strip() for l in body[:first] if not _RULE.match(l))
    rule = next((i for i in range(first, len(body)) if _RULE.match(body[i])), None)  # 選択肢と「Chat about this」を分ける線
    end = rule if rule is not None else len(body) - 1
    rows = []  # [番号, 行の文字, 下の行（折り返し・説明）, カーソルか, 収まらない印か]
    for l in body[first:end]:
        m = _OPT.match(l)
        if m:
            rows.append([int(m.group(2)), _BOX.sub("", m.group(3)).strip(), [], m.group(1) == "❯ ", m.group(1) in ("↓ ", "↑ ")])
        elif rows and l.startswith(" ") and not _RULE.match(l):
            t = _BOX.sub("", l).strip()
            if t and t[0] not in "│┌└┐┘" and not t.startswith("Notes:") and t != "Submit":
                rows[-1][2].append(t)
    if not rows or [r[0] for r in rows] != list(range(rows[0][0], rows[0][0] + len(rows))):
        return {"stage": "unknown", "why": "numbers"}
    if rule is None:  # 線が無い（読み取りの形が違う）ときは、「Chat about this」の行を名前で外す
        rows = [r for r in rows if r[1] != "Chat about this"]
    preview = "to add notes" in foot
    multi = any(_MARK.match(r[1]) for r in rows)
    if multi and not all(_MARK.match(r[1]) for r in rows):
        return {"stage": "unknown", "why": "marks"}
    free = None
    if not preview and rows[0][0] == 1:  # 自由記入の欄は、線の上のいちばん下の行（見本つきの問いには無い）
        last = rows[-1]
        label = _MARK.sub("", last[1]) if multi else last[1]
        if rule is not None or label in FREE:
            free = rows.pop()
    notes = ""
    if preview:  # 見本の枠の下の、字下げした「Notes: …」の行だけ
        box_end = max((j for j in range(first, len(body)) if "└" in body[j]), default=None)
        if box_end is not None and box_end + 1 < len(body):
            l = body[box_end + 1]
            if l.startswith(" " * 10) and l.strip().startswith("Notes:"):
                notes = l.strip()[len("Notes:"):].strip()
                notes = "" if notes == "press n to add notes" else notes
    options = []
    for n, label, below, _, _ in rows:
        mk = _MARK.match(label)
        options.append({"n": n, "label": _line(label[mk.end():] if mk else label, 200), "desc": _line(" ".join(below), 300),
                        "checked": bool(mk and mk.group(1) != " ")})
    free_text = ""
    if free:
        ft = _MARK.sub("", free[1]) if multi else free[1]
        free_text = "" if ft in FREE else " ".join([ft] + free[2])
    cursor = next((r[0] for r in rows + ([free] if free else []) if r[3]), None)
    box = []  # いまカーソルのある選択肢の見本（枠の中の文字。左右の枠線と、同じ行の選択肢の名前は外す）
    for l in body[first:end]:
        if l.count("│") >= 2:
            box.append(_CTRL.sub(" ", l.split("│", 1)[1].rsplit("│", 1)[0]).rstrip()[1:])
    return {"stage": "question", "chips": chips, "question": _line(question, 600), "multi": multi, "preview": preview,
            "options": options, "free": free[0] if free else None, "free_text": _line(free_text, OTHER_MAX + 50), "cursor": cursor,
            "typing": "to edit in" in foot, "notes": notes, "more": any(r[4] for r in rows),
            "box": "\n".join(box).strip("\n")[:600] if preview else ""}


def dialog(text: str) -> str:
    """問いの画面のうち、見出しの行から下（指紋と表示に使う）。"""
    lines = [l.rstrip() for l in text.split("\n") if l.strip()]
    bar_i = next((i for i in range(len(lines) - 1, -1, -1) if _BAR.match(lines[i])), None)
    return "" if bar_i is None else "\n".join(lines[bar_i:])


def fp(text: str) -> str:
    """いまの 1 問の画面の指紋（見出しの行から下。空白をそろえる）。押したときに、見せたのと同じ画面かを確かめる。"""
    return hashlib.sha1(" ".join(dialog(text).split()).encode()).hexdigest()


def public(st: dict) -> dict:
    """画面に出す形。"""
    keep = ("stage", "chips", "question", "multi", "preview", "options", "free", "free_text", "cursor", "typing", "notes", "more", "box",
            "answers", "submit")
    return {k: st[k] for k in keep if k in st}


# ---------- 1 回押したぶんを送る ----------
async def act(kind: str, arg, read, keys, text) -> str:
    """押したぶんのキーを、画面を確かめながら送る。read() は画面の文字、keys(k) はキー、text(s) は文字を送る（どれも async）。
    kind: pick（1 つ選ぶ。arg は番号）・toggle（複数選ぶ問いの印。arg は番号）・next（複数選ぶ問いを決めて次へ）・
    free（自由記入。arg は文）・submit（確認の画面で送る）。思った画面でなければ、キーを送らずに（途中なら、それ以上送らずに）Stuck。
    戻り値は、送ったことの短い言い方。"""
    clock = asyncio.get_running_loop().time

    async def until(ok, what: str) -> dict:
        end = clock() + WAIT
        while True:
            st = step(await read())
            if ok(st):
                return st
            if clock() >= end:
                raise Stuck(what)
            await asyncio.sleep(STEP)

    async def moved(said: str) -> str:
        """送ったあと、画面がいまの 1 問から変わったのを見届ける（変わらなければ、届いたか分からないとして止める）。"""
        end = clock() + WAIT
        while fp(await read()) == fp0:
            if clock() >= end:
                raise Stuck("送ったあとも画面が変わらない（届いたか分からない）")
            await asyncio.sleep(STEP)
        return said

    raw = await read()
    fp0, st = fp(raw), step(raw)
    if kind == "submit":
        if st["stage"] != "review" or not st["submit"]:
            raise Stuck("確認の画面ではない")
        await keys("1")
        return await moved("確認の画面で「送る」")
    if st["stage"] != "question":
        raise Stuck("問いの画面ではない")
    if st["typing"] or st["notes"]:  # 端末で入力欄・メモに書きかけている
        raise Stuck("端末で書きかけている")
    q = _flat(st["question"])
    same = lambda s: s["stage"] == "question" and _flat(s["question"]) == q  # noqa: E731
    labels = {o["n"]: o for o in st["options"]}
    if kind in ("pick", "toggle"):
        n = arg
        if type(n) is not int or n not in labels:
            raise Stuck("その番号の選択肢が画面に無い")
        if (kind == "toggle") != st["multi"]:
            raise Stuck("1 つ選ぶ問いと複数選ぶ問いを取り違えた")
        if kind == "toggle":
            was = labels[n]["checked"]
            await keys(str(n))
            await until(lambda s: same(s) and not s["typing"] and {o["n"]: o["checked"] for o in s["options"]}.get(n) == (not was),
                        f"{n} 番の印が変わらない")
            return f"{n}番「{_line(labels[n]['label'], 40)}」の印を{'外した' if was else '付けた'}"
        if st["preview"]:  # 見本つきは、数字でカーソルを動かしてから Enter（カーソルが既にそこなら Enter だけ）
            if st["cursor"] != n:
                await keys(str(n))
                await until(lambda s: same(s) and s["cursor"] == n and not s["typing"] and not s["notes"], f"カーソルが {n} 番に来ない")
                fp0 = fp(await read())  # Enter のあとの「変わった」は、カーソルを動かしたあとの画面と比べる
            await keys("enter")
        else:
            await keys(str(n))
        return await moved(f"{n}番「{_line(labels[n]['label'], 40)}」")
    if kind == "next":
        if not st["multi"] or not any(o["checked"] for o in st["options"]):
            raise Stuck("複数選ぶ問いで、まだ何も選んでいない")
        await keys("tab")
        return await moved("選んだものを決めて次へ")
    if kind == "free":
        t = _line(arg, OTHER_MAX + 1) if isinstance(arg, str) else ""
        if not t or len(t) > OTHER_MAX:
            raise Stuck("自由記入の文が空か、長すぎる")
        k = st["free"]
        if k is None or st["multi"] or st["preview"]:
            raise Stuck("この問いには自由記入の欄が無い")
        if st["free_text"]:  # 欄に文が入っていたら、番号で入らない（番号がその文で答えを確定させてしまう。Collie の測った結果）
            raise Stuck("自由記入の欄に、もう文が入っている（端末で書きかけ）")
        await keys(str(k))
        await until(lambda s: same(s) and s["cursor"] == k and s["typing"] and not s["free_text"], "自由記入の欄に入れない")
        await text(t)
        await until(lambda s: same(s) and s["cursor"] == k and s["typing"] and _flat(s["free_text"]) == _flat(t),
                    "自由記入の文が出ないか、入力欄から外れた")
        fp0 = fp(await read())  # Enter のあとの「変わった」は、文を打ったあとの画面と比べる
        await keys("enter")
        return await moved(f"（自由記入）{_line(t, 60)}")
    raise Stuck("知らない押し方")
