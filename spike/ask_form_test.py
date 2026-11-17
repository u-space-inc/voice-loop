"""選択肢の問い（AskUserQuestion）に 1 問ずつ答える部品（scripts/ask_form.py）の試験。herdr・本物のセッションには触れない（画面は偽物の FakeTUI と、本物の画面の写し）。
確かめること:
- 画面を読む: 本物の画面の写し（2.1.318 と 2.1.326。問いが 1 つ・複数・複数選ぶ・見本つき・自由記入の入力中・確認の画面・選択肢が収まらない印）から、
  見出し・答えたか・いまの問い・選択肢（説明・印）・自由記入の欄・カーソル・入力中・メモを読む。操作案内の無い画面は問いとみない
- 1 回押したぶん: 1 つ選ぶ（数字）・見本つき（数字でカーソル → Enter）・自由記入（空の欄の番号 → 文字 → Enter）・複数選ぶ（数字で印・Tab で次へ）・
  確認の画面（1）。偽物の画面で、問いが 1 つ・複数を通して答えが届く。送ったあと画面が変わったのを見届ける
- 相手の位置・揺れ: 端末で書きかけている（入力欄・メモ・欄に文）なら送らない・本物と違う動き（見本つきでも数字で進む・自由記入の欄に入らない・
  画面が変わらない）では止まり、それ以上送らない・画面に無い番号・押し方の取り違えは送らない
使い方: uv run spike/ask_form_test.py"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask_form as af  # noqa: E402
from ask_tui import (REAL_OTHER_TYPED, REAL_PREVIEW_CURSOR, REAL_SHOP_INPUT, REAL_TWO_FIRST, REAL_TWO_INPUT,  # noqa: E402
                     REAL_TWO_MULTI_CHECKED, REAL_TWO_REVIEW, FakeTUI)
from question_screens import REAL_CHOICE, REAL_MULTI, REAL_PREVIEW, REAL_TWO  # noqa: E402

af.WAIT, af.STEP = 0.3, 0.01
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


labels = lambda st: [(o["n"], o["label"]) for o in st["options"]]  # noqa: E731

# ---------- 画面を読む（本物の画面の写し） ----------
st = af.step(REAL_TWO_FIRST)
check("2.1.326: 2 つの問いの最初（見出し・答えたか・問い・選択肢と説明・自由記入の欄・カーソル）",
      (st["stage"], st["chips"], st["question"], labels(st), [o["desc"] for o in st["options"]], st["free"], st["free_text"], st["cursor"], st["multi"], st["preview"]),
      ("question", [("朝", False), ("色", False)], "朝はどれ？", [(1, "パン"), (2, "ごはん")], ["朝はパン", "朝はごはん"], 3, "", 1, False, False))
st = af.step(REAL_TWO_MULTI_CHECKED)
check("2.1.326: 複数選ぶ問いの印（自由記入の欄の印と「Submit」の行は選択肢に混ぜない）",
      (st["multi"], labels(st), [o["checked"] for o in st["options"]], st["free"], st["chips"]),
      (True, [(1, "赤"), (2, "青"), (3, "緑")], [True, False, True], 4, [("朝", True), ("色", True)]))
st = af.step(REAL_TWO_REVIEW)
check("2.1.326: 確認の画面の答えと「送る」（操作案内が無くても読む）",
      (st["stage"], st["answers"], st["submit"]), ("review", [("朝はどれ？", "ごはん"), ("好きな色は？（複数可）", "赤, 緑")], True))
st = af.step(REAL_OTHER_TYPED)
check("2.1.326: 自由記入の入力中（欄の文・カーソル・入力中）", (st["free"], st["free_text"], st["cursor"], st["typing"], labels(st)),
      (3, "駅前の店", 3, True, [(1, "A店"), (2, "B店")]))
st = af.step(REAL_PREVIEW_CURSOR)
check("2.1.326: 見本つきの問い（枠の文字を選択肢に混ぜない・自由記入の欄は無い・カーソル・見本の中身は行のまま、枠線と選択肢の名前を外す）",
      (st["preview"], labels(st), st["free"], st["cursor"], [l.rstrip() for l in st["box"].split("\n")], st["notes"]),
      (True, [(1, "横長"), (2, "縦長")], None, 2, ["+----+", "|    |", "|    |", "+----+"], ""))
st = af.step(REAL_CHOICE)
check("2.1.318（罫線を外した写し）: 問いが 1 つ・「Type something.」は欄・「Chat about this」は外す",
      (st["chips"], labels(st), st["free"]), ([("試験", False)], [(1, "りんご"), (2, "みかん"), (3, "ぶどう")], 4))
check("2.1.318（罫線を外した写し）: 複数選ぶ・2 問・見本つき",
      (labels(af.step(REAL_MULTI)), af.step(REAL_MULTI)["multi"], af.step(REAL_TWO)["chips"], labels(af.step(REAL_PREVIEW)), af.step(REAL_PREVIEW)["free"]),
      ([(1, "赤"), (2, "青"), (3, "緑")], True, [("朝", False), ("夜", False)], [(1, "横長"), (2, "縦長")], None))
check("画面: 操作案内の無い画面・番号の飛ぶ画面・問いの無い画面・読めない文字",
      (af.step("\n".join(REAL_TWO_FIRST.split("\n")[:-1]))["stage"], af.step(REAL_TWO_FIRST.replace("2. ごはん", "5. ごはん")).get("why"),
       af.step("❯ ")["stage"], af.step(REAL_TWO_FIRST + "�")["stage"]),
      ("unknown", "numbers", "gone", "unknown"))
scroll = REAL_TWO_FIRST.replace("  2. ごはん", "↓ 2. ごはん")
check("画面: 選択肢が収まらない印（↓）の行も選択肢として読み、収まらないことを知らせる", (labels(af.step(scroll)), af.step(scroll)["more"]), ([(1, "パン"), (2, "ごはん")], True))
check("指紋: 見出しの行から下だけで決まる（上の会話が変わっても同じ）・中身が変われば変わる",
      (af.fp("会話A\n" + REAL_TWO_FIRST) == af.fp("会話B\n" + REAL_TWO_FIRST), af.fp(REAL_TWO_FIRST) == af.fp(REAL_TWO_MULTI_CHECKED)), (True, False))


# ---------- 1 回押したぶんを送る（偽物の画面で） ----------
def go(tui, kind, arg=None):
    async def read():
        return tui.screen()

    async def keys(k):
        tui.keys(k)

    async def text(t):
        tui.text(t)
    try:
        return asyncio.run(af.act(kind, arg, read, keys, text)), None
    except af.Stuck as e:
        return None, str(e)


tui = FakeTUI(REAL_TWO_INPUT)
seq = [go(tui, "pick", 2), go(tui, "toggle", 1), go(tui, "toggle", 3), go(tui, "next"), go(tui, "submit")]
check("通し: 1 つ選ぶ → 複数選ぶ（印・次へ）→ 確認の画面で送る。1 回ずつ 1 手",
      ([e for _, e in seq], tui.result, tui.log, [s for s, _ in seq][0]),
      ([None] * 5, ["ごはん", "赤, 緑"], ["2", "1", "3", "tab", "1"], "2番「ごはん」"))
tui = FakeTUI(REAL_SHOP_INPUT)
seq = [go(tui, "free", "駅前の店"), go(tui, "pick", 2), go(tui, "submit")]
check("通し: 自由記入（番号 → 入力欄で打つ → Enter）→ 見本つき（数字でカーソル → Enter）→ 確認",
      ([e for _, e in seq], tui.result, tui.log), ([None] * 3, ["駅前の店", "縦長"], ["3", "text:駅前の店", "enter", "2", "enter", "1"]))
one = {"questions": [REAL_TWO_INPUT["questions"][0]]}
tui = FakeTUI(one)
check("通し: 問いが 1 つで 1 つ選ぶ形は、番号だけで送られる", (go(tui, "pick", 1), tui.result, tui.log), (("1番「パン」", None), ["パン"], ["1"]))
tui = FakeTUI({"questions": [REAL_SHOP_INPUT["questions"][1]]})
tui.keys("2")  # カーソルが既に 2 番にある
tui.log.clear()
check("通し: 見本つきで、カーソルが既にその番号なら Enter だけ", (go(tui, "pick", 2)[1], tui.result, tui.log), (None, ["縦長"], ["enter"]))
tui = FakeTUI(one)
long = "駅前の大きな交差点の角にある、青い看板のパン屋さんの二階の窓際の席で、九時に待ち合わせ" * 2
check("通し: 端末の幅で折り返す長い自由記入も、つないで照らして送る", (go(tui, "free", long)[1], tui.result), (None, [long]))

# ---------- 書きかけ・揺れ・取り違え ----------
tui = FakeTUI(one)
tui.keys("3")
tui.text("おに")
tui.log.clear()
check("書きかけ: 端末で自由記入の欄に書きかけていたら、何も送らない（番号が書きかけの文に足されない）",
      (bool(go(tui, "pick", 1)[1]), bool(go(tui, "free", "パン")[1]), tui.log, tui.other[0]), (True, True, [], "おに"))


class Filled(FakeTUI):
    """自由記入の欄に文が入ったまま、カーソルは選択肢の上（端末で書いて外れた）。"""

    def __init__(self, raw):
        super().__init__(raw)
        self.other[0] = "おにぎり"


tui = Filled(one)
check("書きかけ: 自由記入の欄に文が入っていたら、欄の番号で入らない（その文で確定させない）", (bool(go(tui, "free", "パン")[1]), tui.log, tui.result), (True, [], None))
noted = REAL_PREVIEW_CURSOR.replace("Notes: press n to add notes", "Notes: 大きめで")
check("書きかけ: 見本つきの問いのメモ欄に書かれていたら、読み取りで分かる（送らない）", af.step(noted)["notes"], "大きめで")
tui, err = FakeTUI(REAL_SHOP_INPUT, ["preview_number_selects"]), None
go(tui, "pick", 1)
s2, err = go(tui, "pick", 2)
check("揺れ: 見本つきでも数字で先へ進む版では、Enter を送らずに止まる（次の問いを勝手に選ばない）", (bool(err), tui.log), (True, ["1", "2"]))
tui = FakeTUI(one, ["no_typing"])
check("揺れ: 自由記入の欄に入れなければ、文字を打たずに止まる", (bool(go(tui, "free", "3番の店")[1]), tui.log, tui.result), (True, ["3"], None))
tui = FakeTUI(REAL_TWO_INPUT, ["submit_ignored"])
for k in ("2", "1", "tab"):
    tui.keys(k)
check("揺れ: 送ったあとも画面が変わらなければ、届いたか分からないとして止まる", (bool(go(tui, "submit")[1]), tui.log.count("1")), (True, 2))
tui = FakeTUI(REAL_TWO_INPUT, ["ignore_tab"])
tui.keys("2")
tui.keys("1")
check("揺れ: 次へ（Tab）が効かなければ止まる", bool(go(tui, "next")[1]), True)
tui = FakeTUI(REAL_TWO_INPUT)
check("取り違え: 画面に無い番号・1 つ選ぶ問いに印・複数選ぶ問いに 1 つ選ぶ・何も選ばずに次へ・確認の画面でない「送る」は送らない",
      (bool(go(tui, "pick", 9)[1]), bool(go(tui, "toggle", 1)[1]), bool(go(tui, "pick", True)[1]), bool(go(tui, "submit")[1]), tui.log),
      (True, True, True, True, []))
tui.keys("2")
tui.log.clear()
check("取り違え: 複数選ぶ問いに 1 つ選ぶ・何も選ばずに次へ・自由記入は送らない",
      (bool(go(tui, "pick", 1)[1]), bool(go(tui, "next")[1]), bool(go(tui, "free", "黄")[1]), tui.log), (True, True, True, []))
check("取り違え: 空・長すぎる自由記入は送らない", (bool(go(FakeTUI(one), "free", "  ")[1]), bool(go(FakeTUI(one), "free", "あ" * 301)[1])), (True, True))
tui = FakeTUI(REAL_SHOP_INPUT)
tui.keys("1")  # 1 つ目に答えて、見本つきの問いへ
check("取り違え: 見本つきの問いに自由記入は送らない", (bool(go(tui, "free", "丸")[1]), tui.log), (True, ["1"]))

ok = sum(r[1] for r in results)
for name, passed, got_, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got_!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
