"""選択式の質問（AskUserQuestion）の本物の画面の形。answer_test.py と question_ui_test.py が共有する（これ自体は試験ではない）。"""
# 2026-11-05 に本物の Claude Code（2.1.318）で取った選択式の質問の画面（read_prompt を通した形）。数字キーの 2 で「みかん」が選ばれた
REAL_CHOICE = """❯ 試験です。AskUserQuestion ツールで質問を 1 つだけしてください。
 ☐ 試験
試験の質問です。どれにしますか？
❯ 1. りんご
     赤い果物
  2. みかん
     橙の果物
  3. ぶどう
     紫の果物
  4. Type something.
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""
REAL_MULTI = """❯ 試験の 2 つ目です。
←  ☐ 複数  ✔ Submit  →
好きなものを選んでください
❯ 1. [ ] 赤
         色
  2. [ ] 青
         色
  3. [ ] 緑
         色
  4. [ ] Type something
     Submit
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""
REAL_TWO = """❯ 試験の 3 つ目です。
←  ☐ 朝  ☐ 夜  ✔ Submit  →
朝はどれ？
❯ 1. パン
     小麦
  2. ごはん
     米
  3. Type something.
  4. Chat about this
Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""
REAL_PREVIEW = """❯ 試験の 4 つ目です。
 ☐ 形
どの形にしますか？
❯ 1. 横長                         ┌──────────────────────────────────────────┐
  2. 縦長                         │ ┌────┐                                   │
                                  │ │ 横 │                                   │
                                  │ └────┘                                   │
                                  └──────────────────────────────────────────┘
                                  Notes: press n to add notes
  Chat about this
Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel"""
