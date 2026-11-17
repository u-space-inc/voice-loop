"""選択肢の問い（AskUserQuestion）に答える部品（scripts/ask_form.py）の試験。herdr・本物のセッションには触れない（画面は偽物の FakeTUI と、本物の画面の写し）。
確かめること:
- 記録から問いを組む: 答えの無いいちばん新しい AskUserQuestion だけ・決まりの外の形（数・型・同じ問い）は使わない・壊れた行を飛ばす
- 画面を読む: 本物の画面の写し（2 つの問いの最初・複数選ぶ問いの印・確認の画面・自由記入の入力中・見本つきのカーソル）・見出しや選択肢が記録と違えば unknown
- 答えを確かめる: 問いの数・番号の範囲・複数選ぶ・自由記入は見本なしの 1 つ選ぶ問いだけ・字数と制御文字
- 送る: 形ごと（1 つ選ぶ・見本つき・自由記入・複数選ぶ・問いが 1 つ・複数）に、本物と同じ動きの偽物の画面で、思った答えが届く
- 相手の位置・揺れ: 本物と違う動き（見本つきでも数字で選ばれる・Tab が効かない・自由記入の欄に入らない・確認の画面の答えが違う・送られない）では、
  そこで止まり、それ以上のキーを送らない（違う答えを送らない・入力欄の外で文字を打たない・確認の画面で 1 を押さない）。端末で答え始めていたら答えない
使い方: uv run spike/ask_form_test.py"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask_form as af  # noqa: E402
from ask_tui import (REAL_OTHER_TYPED, REAL_PREVIEW_CURSOR, REAL_SHOP_INPUT, REAL_TWO_FIRST, REAL_TWO_INPUT,  # noqa: E402
                     REAL_TWO_MULTI_CHECKED, REAL_TWO_REVIEW, FakeTUI)

af.WAIT, af.STEP = 0.3, 0.01  # 偽物の画面はすぐ変わる
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


TWO, SHOP = af.form_of(REAL_TWO_INPUT), af.form_of(REAL_SHOP_INPUT)

# ---------- 記録から問いを組む ----------
check("問いの形: 複数選ぶ・見本・自由記入できるか", [(q["multi"], q["preview"], q["other"]) for q in TWO["questions"] + SHOP["questions"]],
      [(False, False, True), (True, False, False), (False, False, True), (False, True, False)])
bad = [{"questions": []}, {"questions": [{"question": "q", "options": [{"label": "a"}]}]},
       {"questions": [{"question": "q", "options": [{"label": str(i)} for i in range(5)]}]},
       {"questions": [{"question": "q", "options": [{"label": "a"}, {"label": "a"}]}]},
       {"questions": [{"question": "q", "options": [{"label": "a"}, {"label": "b"}]}] * 2},
       {"questions": [{"question": "q", "multiSelect": "yes", "options": [{"label": "a"}, {"label": "b"}]}]},
       {"questions": [{"question": "", "options": [{"label": "a"}, {"label": "b"}]}]},
       {"questions": [{"question": "q", "options": [{"label": "a", "preview": 3}, {"label": "b"}]}]},
       {"questions": [{"question": f"q{i}", "options": [{"label": "a"}, {"label": "b"}]} for i in range(5)]}, "x", None]
check("問いの形: 決まりの外（問い 0・5 つ・選択肢 1・5 つ・同じ選択肢・同じ問い・型の違い）は使わない", [af.form_of(b) for b in bad], [None] * len(bad))


def transcript(*rows):
    d = Path(tempfile.mkdtemp()) / "s.jsonl"
    d.write_text("\n".join(json.dumps(r, ensure_ascii=False) if not isinstance(r, str) else r for r in rows) + "\n")
    return d


use = lambda tid, inp: {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": inp}]}}  # noqa: E731
res = lambda tid: {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}  # noqa: E731
p1 = transcript(use("t1", REAL_SHOP_INPUT), res("t1"), "壊れた行", use("t2", REAL_TWO_INPUT))
check("記録: 答えの無いいちばん新しい問い（壊れた行は飛ばす）", (af.pending(p1)[0], af.pending(p1)[1] == TWO), ("t2", True))
check("記録: いちばん新しい問いに答えがあれば無し・記録が無ければ無し・形が決まりの外なら無し",
      (af.pending(transcript(use("t1", REAL_SHOP_INPUT), res("t1"))), af.pending(None), af.pending(Path("/nonexistent/x.jsonl")),
       af.pending(transcript(use("t3", {"questions": []})))), (None, None, None, None))
check("記録: 古い問いに答えが無くても、新しい問いに答えがあれば無し（古い問いの答えに使わない）",
      af.pending(transcript(use("t1", REAL_SHOP_INPUT), use("t2", REAL_TWO_INPUT), res("t2"))), None)
pub = af.public(SHOP)
check("画面に出す形: 番号は 1 から・見本は改行を残す・制御文字を外す",
      ([o["n"] for o in pub["questions"][0]["options"]], pub["questions"][1]["options"][1]["preview"].count("\n"),
       af.public(af.form_of({"questions": [{"question": "q\x1b[2K", "options": [{"label": "a‮b"}, {"label": "c"}]}]}))["questions"][0]["question"]),
      ([1, 2], 3, "q [2K"))

# ---------- 画面を読む（本物の画面の写し） ----------
st = af.state(REAL_TWO_FIRST, TWO)
check("本物の画面: 2 つの問いの最初（どの問いか・カーソル・答えたか）", (st["stage"], st["index"], st["cursor"], st["answered"], st["checked"], st["typing"]),
      ("question", 0, 1, [False, False], set(), False))
check("本物の画面: 答え始めてよい", af.ready(REAL_TWO_FIRST, TWO), "")
st = af.state(REAL_TWO_MULTI_CHECKED, TWO)
check("本物の画面: 複数選ぶ問いの印", (st["index"], st["checked"], st["answered"]), (1, {1, 3}, [True, True]))
check("本物の画面: 答え始めていたら答えない", af.ready(REAL_TWO_MULTI_CHECKED, TWO), "started")
M1 = af.form_of({"questions": [REAL_TWO_INPUT["questions"][1]]})
one_multi = REAL_TWO_MULTI_CHECKED.replace("←  ☒ 朝  ☒ 色  ✔ Submit  →", "←  ☐ 色  ✔ Submit  →")
check("画面: 最初の問いの中で印を付けていても・自由記入の欄にいても、答え始めたとみる",
      (af.state(one_multi, M1)["index"], af.ready(one_multi, M1), af.ready(REAL_OTHER_TYPED, SHOP)), (0, "started", "started"))
st = af.state(REAL_TWO_REVIEW, TWO)
check("本物の画面: 確認の画面の答えと送るボタン", (st["stage"], st["answers"], st["submit"]),
      ("review", [("朝はどれ？", "ごはん"), ("好きな色は？（複数可）", "赤, 緑")], True))
st = af.state(REAL_OTHER_TYPED, SHOP)
check("本物の画面: 自由記入の入力中（打った文・入力欄にいる）", (st["index"], st["cursor"], st["other"], st["typing"]), (0, 3, "駅前の店", True))
st = af.state(REAL_PREVIEW_CURSOR, SHOP)
check("本物の画面: 見本つきの問いのカーソル（枠の文字を選択肢に混ぜない）", (st["stage"], st["index"], st["cursor"]), ("question", 1, 2))
check("画面: 記録と見出しが違う・問いが違う・選択肢が違う・画面に問いが無いを見分ける",
      (af.state(REAL_TWO_FIRST.replace("☐ 色", "☐ 形"), TWO).get("why"), af.state(REAL_TWO_FIRST.replace("朝はどれ？", "夜はどれ？"), TWO).get("why"),
       af.state(REAL_TWO_FIRST.replace("2. ごはん", "2. めん"), TWO).get("why"), af.state("❯ ", TWO)["stage"],
       af.state(REAL_TWO_FIRST, SHOP).get("why")),
      ("headers", "question", "labels", "gone", "headers"))
check("画面: 1 つ選ぶ問いに印 [ ] があれば・複数選ぶ問いに印が無ければ unknown",
      (af.state(REAL_TWO_FIRST.replace("1. パン", "1. [ ] パン"), TWO).get("why"), af.state(REAL_TWO_MULTI_CHECKED.replace("[✔] 赤", "赤"), TWO).get("why")),
      ("marks", "marks"))

# ---------- 答えを確かめる ----------
ok_two = [{"pick": 2}, {"picks": [3, 1]}]
check("答え: そろえる（複数は番号の順）", af.check(TWO, ok_two), ([{"pick": 2}, {"picks": [1, 3]}], ""))
bad_two = [[{"pick": 2}], [{"pick": 3}, {"picks": [1]}], [{"pick": 0}, {"picks": [1]}], [{"pick": True}, {"picks": [1]}], [{"pick": "2"}, {"picks": [1]}],
           [{"pick": 1}, {"picks": []}], [{"pick": 1}, {"picks": [1, 1]}], [{"pick": 1}, {"picks": [4]}], [{"pick": 1}, {"pick": 1}],
           [{"pick": 1}, {"other": "x"}], [{"pick": 1, "other": "x"}, {"picks": [1]}], [{"other": ""}, {"picks": [1]}],
           [{"other": "あ" * 301}, {"picks": [1]}], "x", None, [{}, {"picks": [1]}]]
check("答え: 数・範囲・型・空・重なり・複数に自由記入・形の違いは断る", [af.check(TWO, b)[1] for b in bad_two], ["bad_form"] * len(bad_two))
check("答え: 見本つきの問いに自由記入は断る", af.check(SHOP, [{"pick": 1}, {"other": "丸"}])[1], "bad_form")
check("答え: 自由記入は 1 行にそろえ、制御文字を外す", af.check(SHOP, [{"other": " 駅前\n の\x1b[2K店 "}, {"pick": 1}])[0][0], {"other": "駅前 の [2K店"})
check("答えの言い方", af.said(TWO, [{"pick": 2}, {"picks": [1, 3]}]), "［朝］ごはん／［色］赤, 緑")


# ---------- 送る（偽物の画面で） ----------
def run(raw, answers, quirks=()):
    tui = FakeTUI(raw, quirks)
    form = af.form_of(raw)
    plan, why = af.check(form, answers)
    assert plan, why

    async def read():
        return tui.screen()

    async def keys(k, left=None):
        tui.keys(k)

    async def text(t, left=None):
        tui.text(t)
    err = None
    try:
        asyncio.run(af.drive(form, plan, read, keys, text))
    except af.Stuck as e:
        err = str(e)
    return tui, err


tui, err = run(REAL_TWO_INPUT, ok_two)
check("送る: 1 つ選ぶ → 複数選ぶ（印・Tab）→ 確認の画面で答えを照らして 1", (err, tui.result, tui.log), (None, ["ごはん", "赤, 緑"], ["2", "1", "3", "tab", "1"]))
tui, err = run(REAL_SHOP_INPUT, [{"other": "駅前の店"}, {"pick": 2}])
check("送る: 自由記入（番号 → 入力欄で打つ → Enter）→ 見本つき（番号でカーソル → Enter）→ 確認",
      (err, tui.result, tui.log), (None, ["駅前の店", "縦長"], ["3", "text:駅前の店", "enter", "2", "enter", "1"]))
one = {"questions": [REAL_TWO_INPUT["questions"][0]]}
tui, err = run(one, [{"pick": 1}])
check("送る: 問いが 1 つで 1 つ選ぶ形は、番号だけで送られる（確認の画面は無い）", (err, tui.result, tui.log), (None, ["パン"], ["1"]))
tui, err = run(one, [{"other": "おにぎり"}])
check("送る: 問いが 1 つの自由記入", (err, tui.result, tui.log), (None, ["おにぎり"], ["3", "text:おにぎり", "enter"]))
tui, err = run({"questions": [REAL_TWO_INPUT["questions"][1]]}, [{"picks": [2]}])
check("送る: 問いが 1 つで複数選ぶ形は、確認の画面を通る", (err, tui.result, tui.log), (None, ["青"], ["2", "tab", "1"]))
tui, err = run({"questions": [REAL_SHOP_INPUT["questions"][1]]}, [{"pick": 1}])
check("送る: 問いが 1 つで見本つき", (err, tui.result, tui.log), (None, ["横長"], ["1", "enter"]))

# ---------- 本物と違う動き・取り違え ----------
tui, err = run(REAL_SHOP_INPUT, [{"pick": 1}, {"pick": 2}], ["preview_number_selects"])
check("揺れ: 見本つきでも数字で選ばれて先へ進む版では、Enter を送らずに止まる（次の問いを勝手に選ばない）",
      (bool(err), tui.log, tui.result), (True, ["1", "2"], None))
tui, err = run(REAL_TWO_INPUT, ok_two, ["ignore_tab"])
check("揺れ: Tab が効かなければ止まり、確認の画面の 1 を押さない", (bool(err), tui.log, tui.result), (True, ["2", "1", "3", "tab"], None))
tui, err = run(REAL_SHOP_INPUT, [{"other": "3番の店"}, {"pick": 1}], ["no_typing"])
check("揺れ: 自由記入の欄に入れなければ、文字を打たずに止まる（数字の入った文が選択肢を選ばない）",
      (bool(err), tui.log, tui.result, tui.ans), (True, ["3"], None, [None, None]))
tui, err = run(REAL_TWO_INPUT, ok_two, ["review_wrong"])
check("揺れ: 確認の画面の答えが送るつもりと違えば、1 を押さずに止まる", (bool(err), tui.log[-1], tui.stage, tui.result), (True, "tab", "review", None))
tui, err = run(REAL_TWO_INPUT, ok_two, ["submit_ignored"])
check("揺れ: 1 を押しても確認の画面が残れば、止まったと伝える（1 は 1 回だけ）", (bool(err), tui.log.count("1"), tui.log), (True, 2, ["2", "1", "3", "tab", "1"]))


class Midway(FakeTUI):
    """送っている途中で、利用者が端末で別の答えを選んだ（2 つ目の問いの 2 番に印）。"""

    def keys(self, k):
        super().keys(k)
        if self.log == ["2"]:
            self.checked[1].add(2)


tui = Midway(REAL_TWO_INPUT)


async def _r():
    return tui.screen()


async def _k(k, left=None):
    tui.keys(k)


async def _t(t, left=None):
    tui.text(t)
try:
    asyncio.run(af.drive(TWO, af.check(TWO, ok_two)[0], _r, _k, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("揺れ: 途中で端末から印が足されたら、Tab を押さずに止まる", (bool(err), "tab" in tui.log, tui.result), (True, False, None))
tui = FakeTUI({"questions": [REAL_TWO_INPUT["questions"][1]]})
tui.keys("2")  # 確かめたあと、送り始めるまでに、端末で青に印が付いた
tui.log.clear()
try:
    asyncio.run(af.drive(M1, af.check(M1, [{"picks": [1]}])[0], _r, _k, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("揺れ: 送り始める前に答え始めていたら、キーを 1 本も送らない（印を変えない）", (bool(err), tui.log, tui.checked[0]), (True, [], {2}))


class LateMark(FakeTUI):
    """1 回目の読み取りでは未回答、2 回目の読み取りの前に、端末で青に印が付いた。"""
    reads = 0

    def screen(self):
        self.reads += 1
        if self.reads == 2:
            self.checked[0].add(2)
        return super().screen()


tui = LateMark({"questions": [REAL_TWO_INPUT["questions"][1]]})
try:
    asyncio.run(af.drive(M1, af.check(M1, [{"picks": [1]}])[0], _r, _k, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("揺れ: 最初のキーを決める画面で答え始めていたら、キーを 1 本も送らない（1 回目は未回答・2 回目で印）", (bool(err), tui.log, tui.checked[0]), (True, [], {2}))

long = "駅前の大きな交差点の角にある、青い看板のパン屋さんの二階の窓際の席で、九時に待ち合わせ" * 2
tui, err = run(one, [{"other": long}])
check("送る: 端末の幅で折り返す長い自由記入も、つないで照らして送る", (err, tui.result, tui.log[-1]), (None, [long], "enter"))
tui, err = run(REAL_TWO_INPUT, ok_two, ["garble_after"])
check("揺れ: 最後のキーのあと画面を読み取れなければ、送れたとしない（止まったと伝える）", (bool(err), tui.result), (True, None))
check("画面: 複数選ぶ問いの自由記入に印があれば答え始めたとみる・「Submit」の行を自由記入の文に混ぜない",
      (af.state(REAL_TWO_MULTI_CHECKED.replace("[✔]", "[ ]"), TWO)["other"],
       af.ready(one_multi.replace("[✔]", "[ ]").replace("4. [ ] Type something", "4. [✔] Type something"), M1)),
      (None, "started"))
TWO_SINGLE = {"questions": [REAL_SHOP_INPUT["questions"][0], REAL_TWO_INPUT["questions"][0]]}
TS = af.form_of(TWO_SINGLE)


class TypingSecond(FakeTUI):
    """1 つ目に答えた直後、端末で 2 つ目の自由記入の欄に入って打ち始めた。"""

    def keys(self, k):
        super().keys(k)
        if self.log == ["1"]:
            self.cursor, self.typing, self.other[1] = 3, True, "おに"


tui = TypingSecond(TWO_SINGLE)
try:
    asyncio.run(af.drive(TS, af.check(TS, [{"pick": 1}, {"pick": 2}])[0], _r, _k, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("揺れ: 2 つ目以降の問いでも、その問いの最初のキーの前に端末で答え始めていたら送らない（打ちかけの文に番号を足さない）",
      (bool(err), tui.log, tui.other[1]), (True, ["1"], "おに"))


class TypingInMulti(FakeTUI):
    """複数選ぶ問いで 1 つ目の印を付けた直後、端末で自由記入の欄に入った。"""

    def keys(self, k):
        super().keys(k)
        if self.log == ["2", "1"]:
            self.typing = True


tui = TypingInMulti(REAL_TWO_INPUT)
try:
    asyncio.run(af.drive(TWO, af.check(TWO, ok_two)[0], _r, _k, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("揺れ: 複数選ぶ問いの途中で端末の入力欄に入ったら、次の番号を送らない", (bool(err), tui.log), (True, ["2", "1"]))


class TypingAfterCheck(FakeTUI):
    """複数選ぶ問いで 1 つ目の印を確かめた読み取りのあと、次のキーの前に、端末で入力欄に入った。"""
    reads_after = 0

    def screen(self):
        if self.log == ["2", "1"]:
            self.reads_after += 1
            if self.reads_after == 2:
                self.typing = True
        return super().screen()


tui = TypingAfterCheck(REAL_TWO_INPUT)
try:
    asyncio.run(af.drive(TWO, af.check(TWO, ok_two)[0], _r, _k, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("揺れ: 印を確かめたあと次のキーまでに入力欄に入っても、次の番号を送らない", (bool(err), tui.log), (True, ["2", "1"]))
check("画面: いちばん下が質問の操作案内でなければ、質問の画面とみない（番号を送らない）",
      (af.state("\n".join(REAL_TWO_FIRST.split("\n")[:-1]), TWO).get("why"), af.ready("\n".join(REAL_TWO_FIRST.split("\n")[:-1]), TWO)), ("foot", "foot"))


class LeaveOther(FakeTUI):
    """自由記入の文を打った直後、端末でカーソルが 1 番へ移り、入力欄から外れた。"""

    def text(self, t):
        super().text(t)
        self.cursor, self.typing = 1, False


tui = LeaveOther(one)
try:
    asyncio.run(af.drive(af.form_of(one), [{"other": "おにぎり"}], _r, _k, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("揺れ: 自由記入の文を打ったあと入力欄から外れたら、Enter を送らない（別の選択肢を確定させない）", (bool(err), "enter" in tui.log, tui.result), (True, False, None))
noted = REAL_PREVIEW_CURSOR.replace("Notes: press n to add notes", "Notes: 大きめで").replace("☒ 店", "☐ 店").replace("❯ 2. 縦長", "  2. 縦長").replace("  1. 横長", "❯ 1. 横長")
SHOP2 = af.form_of({"questions": [REAL_SHOP_INPUT["questions"][1]]})
noted1 = noted.replace("←  ☐ 店  ☐ 形  ✔ Submit  →", " ☐ 形")
check("画面: 見本付きの問いのメモ欄（Notes）に書かれていたら、答え始めたとみる",
      (af.state(noted1, SHOP2).get("notes"), af.ready(noted1, SHOP2), af.ready(noted1.replace("Notes: 大きめで", "Notes: press n to add notes"), SHOP2)),
      ("大きめで", "started", ""))
DESC_NOTES = {"questions": [{"question": "朝はどれ？", "header": "朝", "options": [{"label": "パン", "description": "Notes: 説明を読む"}, {"label": "ごはん"}]},
                            {"question": "好きな色は？（複数可）", "header": "色", "multiSelect": True, "options": [{"label": "赤"}, {"label": "青"}]}]}
tui = FakeTUI(DESC_NOTES)
check("画面: 選択肢の説明文の「Notes:」は、メモ欄とみない（答え始めたとしない）", (af.state(tui.screen(), af.form_of(DESC_NOTES))["notes"], af.ready(tui.screen(), af.form_of(DESC_NOTES))), ("", ""))
box_notes = noted1.replace("│ |    |                                   │", "│ Notes: 枠の中の文                       │", 1).replace("Notes: 大きめで", "Notes: press n to add notes")
check("画面: 見本の枠の中の「Notes:」は、メモ欄とみない", (af.state(box_notes, SHOP2).get("notes"), af.ready(box_notes, SHOP2)), ("", ""))


async def _done():
    return tui.recorded()
tui = FakeTUI(REAL_TWO_INPUT, ["next_question"])
try:
    asyncio.run(af.drive(TWO, af.check(TWO, ok_two)[0], _r, _k, _t, _done))
    err = None
except af.Stuck as e:
    err = str(e)
check("送る: 送った直後に次の問いが出ていても、記録に残った答えで届いたと確かめる", (err, tui.result), (None, ["ごはん", "赤, 緑"]))


async def _wrong():
    return {"朝はどれ？": "パン", "好きな色は？（複数可）": "赤, 緑"} if tui.result else None
tui = FakeTUI(REAL_TWO_INPUT, ["next_question"])
try:
    asyncio.run(af.drive(TWO, af.check(TWO, ok_two)[0], _r, _k, _t, _wrong))
    err = None
except af.Stuck as e:
    err = str(e)
check("揺れ: 記録に残った答えが送るつもりと違えば、送れたとしない", bool(err) and "記録" in err, True)
p1 = transcript(use("t1", REAL_TWO_INPUT), {"type": "user", "toolUseResult": {"answers": {"朝はどれ？": "ごはん"}},
                                            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
check("記録の答え: tool_result と toolUseResult の answers から読む・無ければ None・答えの無い tool_result は {}",
      (af.recorded(p1, "t1"), af.recorded(p1, "t9"), af.recorded(transcript(use("t2", REAL_TWO_INPUT), res("t2")), "t2")),
      ({"朝はどれ？": "ごはん"}, None, {}))
import time as _time  # noqa: E402
tui = FakeTUI(REAL_TWO_INPUT, ["ignore_tab"])


async def _slow():  # 1 回の読み取りに 0.1 秒かかる
    await asyncio.sleep(0.1)
    return tui.screen()
t0 = _time.monotonic()
try:
    asyncio.run(af.drive(TWO, af.check(TWO, ok_two)[0], _slow, _k, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("待つ上限は、読み取りの時間も含めた実の時間（読み取りが遅くても、止まるまでが長くならない）", (bool(err), _time.monotonic() - t0 < 1.5), (True, True))
af.DRIVE_MAX = 0.2
tui = FakeTUI(REAL_TWO_INPUT)
t0 = _time.monotonic()
try:
    asyncio.run(af.drive(TWO, af.check(TWO, ok_two)[0], _slow, _k, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("送り始めてから終わるまでにも上限がある", (bool(err), _time.monotonic() - t0 < 1.0), (True, True))
tui = FakeTUI(REAL_TWO_INPUT)


lefts = []


async def _slow_keys(k, left):  # キーを送る処理が遅い。渡された残りの秒数で打ち切る（本物は herdr の呼び出しの時間切れ）
    lefts.append(left)
    await asyncio.sleep(min(2, left))
    if left < 2:
        raise af.Stuck("時間切れ")
    tui.keys(k)
t0 = _time.monotonic()
try:
    asyncio.run(af.drive(TWO, af.check(TWO, ok_two)[0], _r, _slow_keys, _t))
    err = None
except af.Stuck as e:
    err = str(e)
check("送る処理には残りの秒数を渡し、上限を過ぎて送り続けない（待ちだけを打ち切って、裏で送りが続く形にしない）",
      (bool(err), _time.monotonic() - t0 < 1.0, bool(lefts) and all(0 < x <= 0.2 for x in lefts), tui.log), (True, True, True, []))
af.DRIVE_MAX = 60.0

ok = sum(r[1] for r in results)
for name, passed, got_, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got_!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
