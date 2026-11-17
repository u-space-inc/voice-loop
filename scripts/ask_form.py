"""選択肢の問い（Claude Code の AskUserQuestion）に、画面から 1 問ずつ答えるための部品（2026-11-17 本人の要望・決定）。

いまターミナルに出ている 1 問（または最後の確認の画面）だけを、画面の文字から読んでボタンにする。1 回押すごとに、確かめながら
キーを送り、画面を読み直して次の 1 問を出す。正しいのはいつもターミナルの画面で、こちらに答えの控えを持たない。
（Claude Code は、答えるまで問いをセッションの記録に書かないので、記録からは組めない。2026-11-17 に確かめた。
  この形は、herdr のプラグイン Collie（MIT、Altan Sarisin）の「いまの 1 問を写し、1 回押すごとに 1 キー」の考え方に倣った。
  Collie の測った結果（web/src/lib/grammar/WIZARD_NOTES.md・ASK_FIELD_NOTES.md）も参考にした）

キーの動き（2026-11-17 に Claude Code 2.1.326 の実画面で確かめた）:
- 1 つ選ぶ（見本なし）: 数字キーで選ばれ、次の問いへ進む（問いが 1 つだけなら、そのまま送られる）
- 1 つ選ぶ（見本つき）: 数字キーはカーソルを動かすだけ。Enter で選ばれて進む
- 自由記入（「Type something.」。見本なしの 1 つ選ぶ問いだけ）: ↓↑ で欄まで移ると入力中になり、文字を打って Enter で進む
- 複数選ぶ: 数字キーで印を付け外しする。Tab で次の問い（最後なら確認の画面）へ
- 確認の画面（問いが 2 つ以上か、複数選ぶ問いのとき）: 「1」で送る
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata

OTHER_MAX = 300  # 自由記入の字数の上限
WAIT, STEP = 4.0, 0.2  # 1 手ごとに画面が変わるのを待つ秒数（読み取りの時間も含めた実の時間）と、見直す間隔
READ_MAX = 5.0  # 1 回の画面の読み取り・キーを送る呼び出しの上限

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿\ud800-\udfff]")
_BAR = re.compile(r"^(?:←\s+[☐☒] | [☐☒] )")  # 見出しの行。本物の形だけ（問いが 1 つなら行頭に空白 1 つで「 ☐ 朝」、複数なら行頭から「←  ☐ 朝  ☒ 色  ✔ Submit  →」）
_OPT = re.compile(r"^(❯ | {2}|↓ |↑ )(\d+)\. (.*)$")  # 選択肢の行（↓↑ は、選択肢が画面に収まらないときの印）
_BOX = re.compile(r"\s{2,}[┌│└].*$")  # 見本の枠（選択肢の行の右に出る）
_MARK = re.compile(r"^\[([ ✔✓x])\] ")  # 複数選ぶ問いの印
_RULE = re.compile(r"^\s*[─╌]{8,}\s*$")
FREE = ("Type something.", "Type something")  # 自由記入の欄の空のときの文字


def _flat(s: str) -> str:
    """比べるための形（空白をすべて外す。折り返しと字下げを問わない）。"""
    return "".join(str(s).split())


LIMITS = {"chip": 40, "question": 600, "label": 200, "desc": 1000, "answer": 300}  # 画面に出す字数の上限。超えたら省かずに、問いを読まない


def _fits(s, kind: str) -> bool:
    """省かずに出せる長さか（省いた名前のまま押すと、見えていない後半ごと確定してしまう）。"""
    return len(" ".join(_CTRL.sub(" ", str(s or "")).split())) <= LIMITS[kind]


def _line(s, n: int) -> str:
    """1 行にそろえ、制御文字・向きを変える文字を空白にして n 字で切る。"""
    v = " ".join(_CTRL.sub(" ", str(s or "")).split())
    return v if len(v) <= n else v[: n - 1] + "…"


def _cut(line: str, col: int) -> tuple[str, str]:
    """画面の列（全角は 2 列）で col の位置の左右に分ける。"""
    w = 0
    for i, ch in enumerate(line):
        if w >= col:
            return line[:i], line[i:]
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return line, ""


def _sure(ch: str) -> bool:
    """画面の列の幅がはっきりしている文字か。許すのは、ASCII の印字できる文字と、全角（W・F）か半角カナ（H）で、分類が文字・数字・
    句読点・記号のものだけ（組み合わせ記号 M*・制御 C*・幅のあいまいな文字・ほかの文字体系は許さない。列の数え方がずれうるため）。"""
    if ord(ch) < 0x80:
        return ch.isprintable()
    return unicodedata.east_asian_width(ch) in ("W", "F", "H") and unicodedata.category(ch)[0] in "LNPS"


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


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
    if not lines:
        return {"stage": "gone"}
    is_opt = lambda l, n, label: (m := _OPT.match(l)) is not None and int(m.group(2)) == n and m.group(3).strip() == label  # noqa: E731
    # 操作案内（いちばん下）。ペインの幅が狭いと 2〜3 行に折り返す（「Enter to select …」で始まる行から下）
    # 折り返しの続きの行は、操作案内の続きの形だけ（入力欄の ❯・選択肢・線の行でない）。つないだ文は「Esc to cancel」で終わること
    fstart = next((i for i in range(len(lines) - 1, max(-1, len(lines) - 4), -1) if lines[i].startswith("Enter to select")), None)
    foot = " ".join(lines[fstart:]) if fstart is not None else lines[-1]
    qfoot = (fstart is not None and foot.rstrip().endswith("Esc to cancel")
             and not any(l.lstrip().startswith(("❯", "↓", "↑")) or _OPT.match(l) or _RULE.match(l) for l in lines[fstart + 1:]))
    if qfoot:
        lines = lines[:fstart] + [foot]  # 折り返しを 1 行にそろえる
    review = len(lines) >= 3 and is_opt(lines[-2], 1, "Submit answers") and is_opt(lines[-1], 2, "Cancel")
    if not qfoot and not review:
        # いちばん下が、質問の操作案内でも確認の画面の終わりでもない。Claude の入力欄（「❯」だけ、か「❯ 文」の行）が下にあれば、問いは閉じた。
        # 無ければ読めない画面（描き途中など）で、問いが閉じたとは言い切らない（送ったあとに「届いた」と取り違えない）
        prompt = any(l.strip() == "❯" or (l.lstrip().startswith("❯ ") and not _OPT.match(l)) for l in lines[-6:])
        return {"stage": "gone"} if prompt else {"stage": "unknown", "why": "foot"}
    # 見出しの行。本物は画面に 1 つだけ。2 つ以上あれば、問い・選択肢・説明の文にまねた行がある（その下から読むと、本物の選択肢を捨てて
    # 偽の選択肢を読んでしまう）。見出しの行が画面に無い（小さいペインで上にはみ出した）ときは、作りの全体を確かめられないので読まない（端末で）
    bars = [i for i, l in enumerate(lines) if _BAR.match(l)]
    if len(bars) > 1:
        return {"stage": "unknown", "why": "suspicious"}
    if not bars:
        return {"stage": "unknown", "why": "partial"}
    bar_i = bars[0]
    items = [x for x in re.split(r"\s{2,}", lines[bar_i].strip().strip("←→").strip()) if x]
    if not all(_fits(x[1:], "chip") for x in items if x[:1] in "☐☒"):  # 省く前の見出しで確かめる
        return {"stage": "unknown", "why": "long"}
    chips = [(_line(x[1:], LIMITS["chip"]), x[0] == "☒") for x in items if x[:1] in "☐☒"]
    body = lines[bar_i + 1:]
    if not body or not chips:
        return {"stage": "unknown", "why": "empty"}
    if review:
        # 確認の画面（操作案内が無い）。決まった形で読む: 見出しのすぐ下に「Review your answers」（画面に 1 つだけ）…「Ready to submit…」
        # 「1. Submit answers」「2. Cancel」。答え（●）の数は問いの数と同じこと（隠れた答えがあるまま送らない）
        heads = [i for i, l in enumerate(body) if l.strip() == "Review your answers"]
        if heads == [0] and len(body) >= 4 and body[-3].strip().startswith("Ready to submit"):
            got, cur = [], None
            for l in body[1:-3]:
                s = l.strip()
                if _OPT.match(l) or _RULE.match(l) or _BAR.match(l) or s.startswith("❯"):
                    return {"stage": "unknown", "why": "suspicious"}  # 問い・答えの文に、選択肢・線・カーソルをまねた行がある
                if s.startswith("● "):
                    cur = [s[2:], None]
                    got.append(cur)
                elif s.startswith("→ ") and cur is not None and cur[1] is None:
                    cur[1] = s[2:]
                elif cur is not None:  # 折り返した問い・答え
                    cur[0 if cur[1] is None else 1] += s
            if len(got) != len(chips) or any(b is None for _, b in got):
                return {"stage": "unknown", "why": "answers"}
            if not all(_fits(a, "answer") and _fits(b, "answer") for a, b in got):
                return {"stage": "unknown", "why": "long"}
            return {"stage": "review", "chips": chips, "answers": [(_line(a, LIMITS["answer"]), _line(b or "", LIMITS["answer"])) for a, b in got], "submit": True}
        return {"stage": "unknown", "why": "review"}  # 確認の画面の終わりはあるが、頭（Review your answers）が決まった形で読めない
    # 問いの画面。下から決まった形で読む: 操作案内 → 「Chat about this」の行 → 線 → 選択肢のかたまり → 問いの文。
    # 問いの文（セッションのモデルが書く。外の文が入りうる）に、選択肢・線・見出し・カーソルをまねた行があれば読まない
    j = len(body) - 2
    if j < 1:
        return {"stage": "unknown", "why": "short"}
    chat = body[j].strip()
    mc = _OPT.match(body[j])
    if not (chat == "Chat about this" or (mc and mc.group(3).strip() == "Chat about this")):
        return {"stage": "unknown", "why": "chat"}
    chat_n = int(mc.group(2)) if mc else None  # 「Chat about this」の番号（見本つきの問いには番号が無い）
    rule = j - 1
    if not _RULE.match(body[rule]):
        return {"stage": "unknown", "why": "rule"}
    k = rule - 1
    while k >= 0 and (_OPT.match(body[k]) or (body[k].startswith(" ") and not _RULE.match(body[k]))):
        k -= 1
    first = k + 1
    if first >= rule or not _OPT.match(body[first]):
        return {"stage": "unknown", "why": "no_options"}
    asked = body[:first]
    if any(_OPT.match(l) or _RULE.match(l) or _BAR.match(l) or l.lstrip().startswith(("❯", "↓", "↑")) for l in asked):
        return {"stage": "unknown", "why": "suspicious"}
    question = " ".join(l.strip() for l in asked)
    if not question:  # 問いの文が無い（選択肢をまねた 1 行の問いが、選択肢のかたまりに混ざった形もここに来る）
        return {"stage": "unknown", "why": "question"}
    end = rule
    preview = "to add notes" in foot
    box_col = None
    if preview:  # 見本の枠の列（枠の終わり「└──」の行の位置）。選択肢の行は、この列より左だけが名前
        ends = [j for j in range(first, end) if re.match(r"^ {10,}└─", body[j])]
        if not ends:
            return {"stage": "unknown", "why": "notes"}
        e = body[ends[-1]]
        box_col = _width(e[:e.index("└")])
    rows = []  # [番号, 行の文字, 下の行（折り返し・説明）, カーソルか, 収まらない印か]
    after = []  # 選択肢の行より下の、選択肢でない字下げの行（Submit・Next を見る）
    for l in body[first:end]:
        m = _OPT.match(l)
        if m:
            if box_col is not None:
                head, tail = _cut(l, box_col)
                mh = _OPT.match(head.rstrip())
                label = mh.group(3).strip() if mh else ""
                if (not mh or set("│┌└┐┘├┤") & set(head) or not all(_sure(ch) for ch in mh.group(3))
                        or (tail and tail[0] not in "┌│└├")):
                    # 名前の側に枠の文字がある・名前に幅のはっきりしない文字がある（列の数え方がずれて、名前の途中で切りうる）・
                    # 枠の列にちょうど枠の文字が無い（列がずれていれば、名前の途中で切っている）
                    return {"stage": "unknown", "why": "label"}
            else:
                label = m.group(3).strip()  # 見本の無い問いでは、枠を外さない（名前に枠の文字があっても、そのまま名前）
            rows.append([int(m.group(2)), label, [], m.group(1) == "❯ ", m.group(1) in ("↓ ", "↑ ")])
            after = []
        elif rows and l.startswith(" ") and not _RULE.match(l):
            t = (_cut(l, box_col)[0] if box_col is not None else l).strip()
            after.append(t)
            if t and t[0] not in "│┌└┐┘├┤" and not t.startswith("Notes:") and t not in ("Submit", "Next"):
                rows[-1][2].append(t)
    if sum(1 for r in rows if r[3]) > 1:
        return {"stage": "unknown", "why": "cursors"}
    if not rows or [r[0] for r in rows] != list(range(rows[0][0], rows[0][0] + len(rows))) or (chat_n and rows[-1][0] >= chat_n):
        return {"stage": "unknown", "why": "numbers"}
    marked = [bool(_MARK.match(r[1])) for r in rows]
    multi = any(marked)
    if multi:
        # 複数選ぶ問いとみるのは、作りで確かめられるときだけ: どの行にも印があり、いちばん下が「[ ] Type something」の形の欄で
        # （Chat の番号の 1 つ前）、その下に「Submit」か「Next」の行がある。名前に [ ] をまねた 1 つ選ぶ問いと取り違えない
        if not (all(marked) and chat_n and rows[-1][0] == chat_n - 1 and _flat(_MARK.sub("", rows[-1][1])) == "Typesomething"
                and any(t in ("Submit", "Next") for t in after)):
            return {"stage": "unknown", "why": "marks"}
    free, free_hidden = None, False
    if not preview and chat_n:  # 自由記入の欄は「Chat about this」の 1 つ前の番号（見本つきの問いには無い）
        if rows[-1][0] == chat_n - 1:
            free = rows.pop()
        else:  # 選択肢が画面に収まらず、欄が画面の外にある（中に書きかけがあるか分からない）
            free_hidden = True
        if not rows:
            return {"stage": "unknown", "why": "no_options"}
        more = any(n not in {r[0] for r in rows} for n in range(1, chat_n - 1))  # 画面に収まっていない選択肢がある
    else:
        more = any(r[4] for r in rows) or rows[0][0] != 1
    notes = ""
    if preview:  # 見本の枠の終わり（字下げした「└──…」の行）のすぐ下の、字下げした「Notes: …」の行。この形で読めなければ読まない
        if ends[-1] + 1 >= end:
            return {"stage": "unknown", "why": "notes"}
        l = body[ends[-1] + 1]
        if not (l.startswith(" " * 10) and l.strip().startswith("Notes:")):
            return {"stage": "unknown", "why": "notes"}
        notes = l.strip()[len("Notes:"):].strip()
        notes = "" if notes == "press n to add notes" else notes
    if not _fits(question, "question"):
        return {"stage": "unknown", "why": "long"}
    options = []
    for n, label, below, _, _ in rows:
        mk0 = _MARK.match(label)
        if not _fits(label[mk0.end():] if mk0 else label, "label") or not _fits(" ".join(below), "desc"):
            return {"stage": "unknown", "why": "long"}  # 長すぎる名前・説明は省かずに、問いを読まない（端末で）
        mk = _MARK.match(label)
        options.append({"n": n, "label": _line(label[mk.end():] if mk else label, LIMITS["label"]), "desc": _line(" ".join(below), LIMITS["desc"]),
                        "checked": bool(mk and mk.group(1) != " ")})
    free_text = ""
    if free:
        ft = _MARK.sub("", free[1]) if multi else free[1]
        free_text = "" if ft in FREE else " ".join([ft] + free[2])
    cursor = next((r[0] for r in rows + ([free] if free else []) if r[3]), None)
    free_n = free[0] if free else (chat_n - 1 if free_hidden else None)
    box = []  # いまカーソルのある選択肢の見本（枠の中の文字。左右の枠線と、同じ行の選択肢の名前は外す）
    for l in body[first:end]:
        if l.count("│") >= 2:
            box.append(_CTRL.sub(" ", l.split("│", 1)[1].rsplit("│", 1)[0]).rstrip()[1:])
    return {"stage": "question", "chips": chips, "question": _line(question, LIMITS["question"]), "multi": multi, "preview": preview,
            "options": options, "free": free_n, "free_hidden": free_hidden, "free_text": _line(free_text, OTHER_MAX + 50), "cursor": cursor,
            "typing": "to edit in" in foot, "notes": notes, "more": more, "cut": not question,
            "box": "\n".join(box).strip("\n")[:600] if preview else ""}


def dialog(text: str) -> str:
    """問いの画面のうち、見出しの行から下（指紋と表示に使う）。見出しの行が画面の外なら、画面の全体。"""
    lines = [l.rstrip() for l in text.split("\n") if l.strip()]
    bar_i = next((i for i in range(len(lines) - 1, -1, -1) if _BAR.match(lines[i])), None)
    return "\n".join(lines[bar_i:] if bar_i is not None else lines)


def fp(text: str) -> str:
    """いまの 1 問の画面の指紋（見出しの行から下。空白をそろえる）。押したときに、見せたのと同じ画面かを確かめる。"""
    return hashlib.sha1(" ".join(dialog(text).split()).encode()).hexdigest()


def public(st: dict) -> dict:
    """画面に出す形。"""
    keep = ("stage", "chips", "question", "cut", "multi", "preview", "options", "free", "free_hidden", "free_text", "cursor", "typing", "notes", "more", "box",
            "answers", "submit")
    return {k: st[k] for k in keep if k in st}


# ---------- 1 回押したぶんを送る ----------
def sig(st: dict) -> tuple:
    """いまの 1 問の見分け（問いの文・選択肢の名前・種類・見出し）。送る途中で、別の問いに変わっていないかを照らす。"""
    return (_flat(st.get("question", "")), tuple(_flat(o["label"]) for o in st.get("options", [])), st.get("multi"), st.get("preview"),
            tuple(h for h, _ in st.get("chips", [])))


async def act(kind: str, arg, read, keys, text, expect: str | None = None) -> str:
    """押したぶんのキーを、画面を確かめながら送る。read() は画面の文字、keys(k) はキー、text(s) は文字を送る（どれも async）。
    kind: pick（1 つ選ぶ。arg は番号）・toggle（複数選ぶ問いの印。arg は番号）・next（複数選ぶ問いを決めて次へ）・
    free（自由記入。arg は文）・submit（確認の画面で送る）。expect は、利用者に見せた画面の指紋（最初の読み取りがこれと違えば送らない）。
    1 手ごとの読み取りは、その都度、同じ問い（sig）と期待する変化だけを確かめる。思った画面でなければ、キーを送らずに
    （途中なら、それ以上送らずに）Stuck。送ったあとは、読める次の画面（問いが消えた・次の問い・確認の画面）を見届ける。
    戻り値は、送ったことの短い言い方。"""
    clock = asyncio.get_running_loop().time

    async def until(ok, what: str) -> tuple[dict, str]:
        end = clock() + WAIT
        while True:
            raw_ = await read()
            st_ = step(raw_)
            if ok(st_):
                return st_, raw_
            if clock() >= end:
                raise Stuck(what)
            await asyncio.sleep(STEP)

    async def moved(said: str, before: str) -> str:
        """送ったあと、画面がいまの 1 問から、読める次の画面に変わったのを見届ける（読めない画面のままなら、届いたか分からないとして止める）。"""
        def ok(st_):
            if st_["stage"] in ("gone", "review"):
                return st_["stage"] != "review" or kind != "submit"
            # 次の問い。まだ何も答えていない形のときだけ（もう印・書きかけ・メモがあれば、送ったキーがずれて次の問いに届いたか、
            # 端末で同時に押された。届いたとしない）
            return (st_["stage"] == "question" and sig(st_) != sig0 and not any(o["checked"] for o in st_["options"])
                    and not st_["free_text"] and not st_["typing"] and not st_["notes"])
        end = clock() + WAIT
        while True:
            raw_ = await read()
            if fp(raw_) != before and ok(step(raw_)):
                return said
            if clock() >= end:
                raise Stuck("送ったあと、読める次の画面にならない（届いたか分からない）")
            await asyncio.sleep(STEP)

    raw = await read()
    if expect is not None and fp(raw) != expect:  # 見せたあとで画面が変わった
        raise Stuck("見せたときから画面が変わった")
    st = step(raw)
    sig0 = sig(st)
    if kind == "submit":
        if st["stage"] != "review" or not st["submit"]:
            raise Stuck("確認の画面ではない")
        await keys("1")
        return await moved("確認の画面で「送る」", fp(raw))
    if st["stage"] != "question":
        raise Stuck("問いの画面ではない")
    if st["typing"] or st["notes"]:  # 端末で入力欄・メモに書きかけている
        raise Stuck("端末で書きかけている")
    same = lambda s: s["stage"] == "question" and sig(s) == sig0  # noqa: E731
    labels = {o["n"]: o for o in st["options"]}
    if kind in ("pick", "toggle"):
        n = arg
        if type(n) is not int or n not in labels:
            raise Stuck("その番号の選択肢が画面に無い")
        if (kind == "toggle") != st["multi"]:
            raise Stuck("1 つ選ぶ問いと複数選ぶ問いを取り違えた")
        if kind == "toggle":
            was = {o["n"]: o["checked"] for o in st["options"]}
            want = {**was, n: not was[n]}
            await keys(str(n))
            await until(lambda s: same(s) and not s["typing"] and not s["free_text"] and {o["n"]: o["checked"] for o in s["options"]} == want,
                        f"{n} 番の印が思ったとおりに変わらない")
            return f"{n}番「{_line(labels[n]['label'], 40)}」の印を{'外した' if was[n] else '付けた'}"
        before = fp(raw)
        if st["preview"]:  # 見本つきは、数字でカーソルを動かしてから Enter（カーソルが既にそこなら Enter だけ）
            if st["cursor"] != n:
                await keys(str(n))
                _, raw2 = await until(lambda s: same(s) and s["cursor"] == n and not s["typing"] and not s["notes"], f"カーソルが {n} 番に来ない")
                before = fp(raw2)  # Enter の直前の画面（確かめた読み取りそのもの）
            await keys("enter")
        else:
            await keys(str(n))
        return await moved(f"{n}番「{_line(labels[n]['label'], 40)}」", before)
    if kind == "next":
        if not st["multi"] or not any(o["checked"] for o in st["options"]) or st["free_text"]:
            raise Stuck("複数選ぶ問いで、まだ何も選んでいないか、自由記入の欄に書きかけがある")
        await keys("tab")
        return await moved("選んだものを決めて次へ", fp(raw))
    if kind == "free":
        t = _line(arg, OTHER_MAX + 1) if isinstance(arg, str) else ""
        if not t or len(t) > OTHER_MAX:
            raise Stuck("自由記入の文が空か、長すぎる")
        k = st["free"]
        if k is None or st["multi"] or st["preview"]:
            raise Stuck("この問いには自由記入の欄が無い")
        if st["free_hidden"]:  # 欄が画面の外にあると、中に書きかけの文があるか分からない（番号がその文で答えを確定させうる）
            raise Stuck("自由記入の欄が画面に収まっていない（端末で）")
        if st["free_text"]:  # 欄に文が入っていたら、番号で入らない（番号がその文で答えを確定させてしまう。Collie の測った結果）
            raise Stuck("自由記入の欄に、もう文が入っている（端末で書きかけ）")
        # 欄へは番号ではなく ↓↑ で 1 段ずつ移る（文の入った欄の番号は、その文で答えを確定させる。欄の文が「Type something.」と同じだと
        # 空の欄と見分けられないので、番号は使わない。Collie の測った結果に倣った。2026-11-17 実画面で ↓ で欄に入ると入力中になるのを確かめた）
        c = st["cursor"]
        if type(c) is not int or abs(k - c) > 10:
            raise Stuck("カーソルの位置が分からない")
        while c != k:
            nxt = c + (1 if k > c else -1)
            await keys("down" if k > c else "up")
            last = nxt == k
            await until(lambda s, nxt=nxt, last=last: same(s) and s["cursor"] == nxt and (s["typing"] if last else not s["typing"]),
                        f"カーソルが {nxt} 番に来ない")
            c = nxt
        await until(lambda s: same(s) and s["cursor"] == k and s["typing"] and not s["free_text"], "自由記入の欄が空ではないか、入れない")
        await text(t)
        _, raw3 = await until(lambda s: same(s) and s["cursor"] == k and s["typing"] and _flat(s["free_text"]) == _flat(t),
                              "自由記入の文が出ないか、入力欄から外れた")
        await keys("enter")  # Enter の直前の画面は、確かめた読み取りそのもの（raw3）
        return await moved(f"（自由記入）{_line(t, 60)}", fp(raw3))
    raise Stuck("知らない押し方")
