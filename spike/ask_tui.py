"""選択肢の問い（AskUserQuestion）の画面の偽物と、本物の画面の写し。ask_form_test.py と answer_form_test.py が共有する（これ自体は試験ではない）。

偽物の動き（FakeTUI）は、2026-11-17 に Claude Code 2.1.326 の実画面で確かめた動きに合わせた:
- 1 つ選ぶ（見本なし）: 数字キーで選ばれて次へ。最後の問いなら、問いが 2 つ以上は確認の画面へ、1 つだけならそのまま送られる
- 1 つ選ぶ（見本つき）: 数字キーはカーソルを動かすだけ。Enter で選ばれて次へ
- 自由記入: 「Type something.」の番号で入力欄に入る（操作案内に「ctrl+g to edit in Zed」）。文字は入力欄に入り、Enter で次へ
- 複数選ぶ: 数字キーで印を付け外し。Tab で次へ（最後なら確認の画面）
- 確認の画面: 「1」で送る、「2」で取り消す
入力欄にいないときに文字を送ると、1 字ずつキーとして読む（数字なら選んでしまう。本物と同じく危ない）。
"""

# 2026-11-17 に本物の Claude Code（2.1.326）で取った画面（read_dialog を通した形。罫線の行は残す。state が外す）
REAL_TWO_FIRST = """❯ voice-loop の画面の試験です。
──────────────────────────────────────────────────────────────────────────────────────────────────
←  ☐ 朝  ☐ 色  ✔ Submit  →
朝はどれ？
❯ 1. パン
     朝はパン
  2. ごはん
     朝はごはん
  3. Type something.
──────────────────────────────────────────────────────────────────────────────────────────────────
  4. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""
REAL_TWO_MULTI_CHECKED = """──────────────────────────────────────────────────────────────────────────────────────────────────
←  ☒ 朝  ☒ 色  ✔ Submit  →
好きな色は？（複数可）
❯ 1. [✔] 赤
         赤色
  2. [ ] 青
         青色
  3. [✔] 緑
         緑色
  4. [ ] Type something
     Submit
──────────────────────────────────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""
REAL_TWO_REVIEW = """──────────────────────────────────────────────────────────────────────────────────────────────────
←  ☒ 朝  ☒ 色  ✔ Submit  →
Review your answers
 ● 朝はどれ？
   → ごはん
 ● 好きな色は？（複数可）
   → 赤, 緑
Ready to submit your answers?
❯ 1. Submit answers
  2. Cancel"""
REAL_OTHER_TYPED = """←  ☐ 店  ☐ 形  ✔ Submit  →
どの店？
  1. A店
     A店にする
  2. B店
     B店にする
❯ 3. 駅前の店
──────────────────────────────────────────────────────────────────────────────────────────────────
  4. Chat about this
Enter to select · Tab/Arrow keys to navigate · ctrl+g to edit in Zed · Esc to cancel"""
REAL_PREVIEW_CURSOR = """←  ☒ 店  ☐ 形  ✔ Submit  →
どの形？
  1. 横長                         ┌──────────────────────────────────────────┐
❯ 2. 縦長                         │ +----+                                   │
                                  │ |    |                                   │
                                  │ |    |                                   │
                                  │ +----+                                   │
                                  └──────────────────────────────────────────┘
                                  Notes: press n to add notes
──────────────────────────────────────────────────────────────────────────────────────────────────
  Chat about this
Enter to select · ↑/↓ to navigate · n to add notes · Tab to switch questions · Esc to cancel"""
# 小さいペインで、選択肢が画面に収まらないとき（↓ の印。自由記入の欄「3. Type something.」は画面の外）。2026-11-17 に取った
REAL_SCROLLED = """←  ☐ 店  ☐ 色  ☐ 形  ✔ Submit  →
どの店にしますか？
❯ 1. A店
     A店にする
↓ 2. B店
     B店にする
──────────────────────────────────────────────────────────────────────
  4. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""
# 小さいペインの複数選ぶ問い（見出しの行は画面の上にはみ出し、緑と自由記入の欄は画面の外。問いが続くので「Next」）。2026-11-17 に取った
REAL_SMALL_MULTI = """  B店にする（このあたりは会話の続き）
好きな色は？（複数可）
❯ 1. [ ] 赤
         赤を選ぶ
↓ 2. [ ] 青
         青を選ぶ
     Next
──────────────────────────────────────────────────────────────────────
  5. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""
# 小さいペインの見本つきの問い（見出しの行も問いの文も画面の上にはみ出し、見本の枠は「✂ … hidden」で縮む）。2026-11-17 に取った
REAL_SMALL_PREVIEW = """❯ 1. 横長                         ┌─────────────────────────────────────
  2. 縦長                         │ +----------------+                  │
                                  ├─── ✂ ─── 2 lines hidden ──────────
                                  └───────────────────────────────────
                                  Notes: press n to add notes
──────────────────────────────────────────────────────────────────────
  Chat about this
Enter to select · ↑/↓ to navigate · n to add notes · Tab to switch questions · Esc to cancel"""
# 小さいペインの確認の画面（見出しの行も「Review your answers」も画面の上にはみ出している）。2026-11-17 に取った
REAL_SMALL_REVIEW = """ ● どの店にしますか？
   → B店
 ● 好きな色は？（複数可）
   → 赤
 ● どの形？
   → 縦長
Ready to submit your answers?
❯ 1. Submit answers
  2. Cancel"""
# 問い・説明に改行と、見出し・選択肢をまねた行を入れた問いの、本物の描き方（2026-11-17、小さいペインで取った）。
# 問いの文が複数行だと各行の頭に「│ 」が付き、説明の改行は「�」になって 1 行に描かれる（偽の見出し・選択肢の行にはならない）
REAL_MALICIOUS = """│ 本当の問い
│  ☐ 偽
│ 中止しますか？
❯ 1. 処理を実行する
     説明\ufffd ☐ 偽\ufffd❯ 1. 中止する
↓ 2. やめる
     やめる
──────────────────────────────────────────────────────────────────────────────────────────
  4. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""
# 上の画面の問いの形（セッションの記録にあった AskUserQuestion の入力）
REAL_TWO_INPUT = {"questions": [
    {"question": "朝はどれ？", "header": "朝", "multiSelect": False,
     "options": [{"label": "パン", "description": "朝はパン"}, {"label": "ごはん", "description": "朝はごはん"}]},
    {"question": "好きな色は？（複数可）", "header": "色", "multiSelect": True,
     "options": [{"label": "赤", "description": "赤色"}, {"label": "青", "description": "青色"}, {"label": "緑", "description": "緑色"}]}]}
REAL_SHOP_INPUT = {"questions": [
    {"question": "どの店？", "header": "店", "multiSelect": False,
     "options": [{"label": "A店", "description": "A店にする"}, {"label": "B店", "description": "B店にする"}]},
    {"question": "どの形？", "header": "形", "multiSelect": False,
     "options": [{"label": "横長", "description": "横に長い形", "preview": "+----------------+\n|                |\n+----------------+"},
                 {"label": "縦長", "description": "縦に長い形", "preview": "+----+\n|    |\n|    |\n+----+"}]}]}


class FakeTUI:
    """Claude Code の選択肢の画面の偽物。quirks で、本物と違う動き（版の違い・取り違え）を入れられる:
    preview_number_selects（見本つきでも数字で選ばれる）・ignore_tab（Tab が効かない）・no_typing（自由記入の欄に入らない）・
    review_wrong（確認の画面に違う答えを出す）・submit_ignored（「1」で送られない）・garble_after（「1」のあと画面に読めない文字が混じる）・
    next_question（送ったあと、すぐ次の別の問いが画面に出る）・wrap_labels（選択肢の名前を 6 字で折り返す）。recorded() は、送られた答えを記録の形（{問いの文: 答え}）で返す。
    自由記入の文は 30 字で折り返す（続きの行は 5 字下げ）。"""

    def __init__(self, raw: dict, quirks=()):
        self.qs = raw["questions"]
        self.q = set(quirks)
        self.i, self.cursor, self.typing, self.stage = 0, 1, False, "question"
        self.ans = [None] * len(self.qs)
        self.checked = [set() for _ in self.qs]
        self.other = [""] * len(self.qs)
        self.log: list[str] = []
        self.result = None
        self.garbled = False

    def review_needed(self):
        return len(self.qs) > 1 or self.qs[0].get("multiSelect")

    def advance(self):
        self.typing = False
        if self.i + 1 < len(self.qs):
            self.i, self.cursor = self.i + 1, 1
        elif self.review_needed():
            self.stage = "review"
        else:
            self.submit()

    def submit(self):
        self.stage = "done"
        self.result = [self.answer_text(k) for k in range(len(self.qs))]

    def answer_text(self, k):
        q = self.qs[k]
        if q.get("multiSelect"):
            return ", ".join(q["options"][n - 1]["label"] for n in sorted(self.checked[k]))
        a = self.ans[k]
        return a if isinstance(a, str) else (q["options"][a - 1]["label"] if a else "")

    def keys(self, k: str):
        self.log.append(k)
        if self.stage == "review":
            if k == "1" and "garble_after" in self.q:
                self.garbled = True
            elif k == "1" and "submit_ignored" not in self.q:
                self.submit()
            elif k == "2":
                self.stage = "cancelled"
            return
        if self.stage != "question":
            return
        q = self.qs[self.i]
        n = len(q["options"])
        preview = any(o.get("preview") for o in q["options"])
        if self.typing:
            if k == "enter":
                self.ans[self.i] = self.other[self.i]
                self.advance()
            elif len(k) == 1:
                self.other[self.i] += k
            return
        if k.isdigit():
            d = int(k)
            if q.get("multiSelect"):
                if 1 <= d <= n:
                    self.checked[self.i] ^= {d}
                    self.cursor = d
            elif preview and "preview_number_selects" not in self.q:
                if 1 <= d <= n:
                    self.cursor = d
            elif 1 <= d <= n:
                self.ans[self.i] = d
                self.advance()
            elif d == n + 1 and not preview:
                self.cursor = d
                self.typing = "no_typing" not in self.q
        elif k == "enter":
            if preview and 1 <= self.cursor <= n:
                self.ans[self.i] = self.cursor
                self.advance()
        elif k == "tab":
            if "ignore_tab" not in self.q:
                self.advance()

    def text(self, t: str):
        if self.typing:
            self.log.append(f"text:{t}")
            self.other[self.i] += t
        else:  # 入力欄にいなければ、1 字ずつキーとして読まれる
            for ch in t:
                self.keys(ch)

    def bar(self):
        if not self.review_needed():
            return f" ☐ {self.qs[0]['header']}"
        chips = "  ".join(("☒" if (self.ans[k] is not None or self.checked[k]) else "☐") + " " + q["header"] for k, q in enumerate(self.qs))
        return f"←  {chips}  ✔ Submit  →"

    def recorded(self):
        return None if self.result is None else {q["question"]: a for q, a in zip(self.qs, self.result)}

    def screen(self) -> str:
        if self.stage == "done" and "next_question" in self.q:
            return ("←  ☐ 別  ☐ 次  ✔ Submit  →\n別の問いです\n❯ 1. はい\n     する\n  2. いいえ\n     しない\n  3. Type something.\n"
                    "  4. Chat about this\nEnter to select · Tab/Arrow keys to navigate · Esc to cancel")
        if self.stage in ("done", "cancelled"):
            return "⏺ 受け取りました\n❯ "
        rule = "─" * 60
        if self.stage == "review":
            rows = [rule, self.bar(), "Review your answers"]
            for k, q in enumerate(self.qs):
                got = self.answer_text(k)
                if "review_wrong" in self.q and k == 0:
                    got = "ちがう答え"
                rows += [f" ● {q['question']}", f"   → {got}"]
            out = "\n".join(rows + ["Ready to submit your answers?", "❯ 1. Submit answers", "  2. Cancel"])
            return out.replace("Review", "Re\ufffdview") if self.garbled else out
        q = self.qs[self.i]
        n = len(q["options"])
        preview = any(o.get("preview") for o in q["options"])
        ql = q["question"].split("\n")  # 本物と同じく、問いの文が複数行なら各行の頭に「│ 」
        rows = [rule, self.bar()] + (["│ " + x for x in ql] if len(ql) > 1 else ql)
        cur = lambda k: "❯ " if self.cursor == k else "  "  # noqa: E731
        for k, o in enumerate(q["options"], 1):
            o = {**o, "label": o["label"].replace("\n", "\ufffd"), "description": o.get("description", "").replace("\n", "\ufffd")}  # 本物と同じく、改行は「�」
            lab, more = o["label"], []
            if "wrap_labels" in self.q and len(lab) > 6:
                lab, more = lab[:6], [lab[j:j + 6] for j in range(6, len(lab), 6)]
            if q.get("multiSelect"):
                rows.append(f"{cur(k)}{k}. [{'✔' if k in self.checked[self.i] else ' '}] {lab}")
                rows += ["         " + x for x in more]
                rows.append("         " + o.get("description", ""))
            elif preview:
                rows.append(f"{cur(k)}{k}. {o['label']}" + ("                         ┌──────────┐" if k == 1 else "                         │ " + "x" * k + " │"))
            else:
                rows.append(f"{cur(k)}{k}. {lab}")
                rows += ["     " + x for x in more]
                rows.append("     " + o.get("description", ""))
        if preview:
            rows += ["                                  └──────────┘", "                                  Notes: press n to add notes", rule, "  Chat about this",
                     "Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel"]
        elif q.get("multiSelect"):
            rows += [f"{cur(n + 1)}{n + 1}. [ ] Type something", "     Submit", rule, f"  {n + 2}. Chat about this",
                     "Enter to select · Tab/Arrow keys to navigate · " + ("ctrl+g to edit in Zed · " if self.typing else "") + "Esc to cancel"]
        else:
            o = self.other[self.i] or "Type something."
            rows += [f"{cur(n + 1)}{n + 1}. {o[:30]}"] + ["     " + o[j:j + 30] for j in range(30, len(o), 30)]
            rows += [rule, f"  {n + 2}. Chat about this",
                     "Enter to select · " + ("Tab/Arrow keys" if self.review_needed() else "↑/↓") + " to navigate · "
                     + ("ctrl+g to edit in Zed · " if self.typing else "") + "Esc to cancel"]
        return "\n".join(rows)
