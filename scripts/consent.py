"""利用者の発言が、許す（承認の「はい」・閉じる）か選ぶ（選択肢の番号）の答えになっているか。ハブの「承認に答える」「閉じる」の守りで使う（server.py）。

ハブは利用者の発言を作れないが、発言のどこかに「いい」があれば通る、のような緩い判定だと、関係のない発言や断る発言を答えに使える。
そこで、発言全体がこの文法に収まるときだけ答えとして読む（README「守り」）。保証は文法との照合まで（何への返事かは見ない）。
  - 断る（先に見る）: 打ち消し・保留の語（構わない・問題ない などを除いてから）、禁止形（閉じるな）、問いの形で終わる発言
  - 許す: すべての節が「前置き＊ ＋（許す句＋結び＊）＋」
  - 選ぶ: すべての節が「選ぶ節」か「許す節」で、選びうる選択肢がちょうど一つ
"""
import re
import unicodedata

KEEP_NAI = ("構わない", "かまわない", "問題ない", "問題なし", "差し支えない", "差支えない")  # 「ない」を含むが、許す語
NEG_JA = re.compile(r"いいえ|ううん|いや|だめ|ダメ|駄目|やめ|止め|待って|まって|ない|なく|ません|拒否|却下|キャンセル|取り消|取消|残して|まだ|後で|あとで"
                    r"|今度に|今度で|また今度|違う|ちがう|別の")
NEG_EN = re.compile(r"(?<![a-z])(?:no|not|don't|dont|stop|wait|later|cancel)(?![a-z])")  # 語の境目で見る（notes の no は数えない）
PROHIBIT = re.compile(r"[るうくぐすつぬぶむ]な[よーっ]*$")  # 禁止形（閉じるな・許可するなよ・頼むな）。節ごとに見る
ASKING = re.compile(r"(?:[?？]|か|かな|かね|っけ)[。．.!！\s]*$")
QUESTION = re.compile(r"[?？]|か(?:[。」』）)\s*]|$)", re.M)  # 聞いた文が問いの形か

PRE = ["じゃあ", "じゃ", "では", "それでは", "それじゃあ", "それじゃ", "あー", "あ", "えっと", "ええと", "うーん", "んー", "まあ", "そうですね", "そうだね", "そうね",
       "今度は", "今度こそ", "今は", "それは", "これは"]  # 「もう」は入れない（「もういいよ」は「やめて」の意味にもなる）
YES = ["はい", "うん", "ええ", "いい", "オーケー", "オッケー", "okay", "ok", "yes", "sure", "どうぞ", "お願い", "おねがい", "大丈夫", "構わない", "かまわない",
       "問題ない", "問題なし", "了解", "りょうかい", "承知", "よろしく", "頼みます", "頼む", "そうして", "それで"]
YES_FOR = {"close": ["閉じちゃってもいい", "閉じちゃっていい", "閉じてもいい", "閉じていい", "閉じちゃって", "閉じて", "とじて", "閉じる"],
           "answer": ["許可して", "許可する", "許可", "通して", "進めて", "実行して"]}
TAIL = ["してください", "ください", "下さい", "します", "です", "ます", "して", "だよ", "よ", "ね", "わ", "ぞ", "ー", "っ", "〜", "~"]  # 「な」は入れない（禁止形）
PICK = ["を選んで", "を選ぶ", "にします", "にする", "にして", "でお願い", "でおねがい", "がいい", "でいい", "で", "を"]
KANJI = {c: i for i, c in enumerate("〇一二三四五六七八九")}


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", _norm(text))


def _alt(words) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def clauses(text: str) -> list[str]:
    """節（「、。,.!！?？」と改行で分け、節の中の空白は除く）。"""
    return [re.sub(r"\s+", "", c) for c in re.split(r"[、。,.!！?？\n]+", _norm(text)) if c.strip()]


def refuses(text: str) -> bool:
    """断る・保留する・問い返す発言か（許す・選ぶより先に見る）。"""
    flat = _flat(text)
    for k in KEEP_NAI:
        flat = flat.replace(k, "")
    if NEG_JA.search(flat) or NEG_EN.search(_norm(text)) or ASKING.search(_norm(text).strip()):
        return True
    return any(PROHIBIT.search(c) for c in clauses(text))


def _pre(project: str) -> str:
    p = _flat(project)
    return f"(?:{_alt(PRE)}" + (f"|{re.escape(p)}(?:のセッション)?(?:は|も|を)?" if p else "") + ")*"


def _yes_clause(action: str | None, project: str):
    yes = _alt(YES + (YES_FOR[action] if action else []))
    return re.compile(f"{_pre(project)}(?:(?:{yes})(?:{_alt(TAIL)})*)+")


# 聞いた問いの「〇〇して（も）よいですか？」の〇〇（漢字・カタカナの語）。利用者がその語で答えたら（「削除して」「送信してください」）、許す句として読む。
# 読むのは、文の終わりがこの問いの形そのもの（「…削除してよいですか？」）の文だけ。説明の中（「削除してよいか確認中です」）・引用（「『削除してよいですか』と
# 聞かれています」）・打ち消し（「削除しなくてよいですか」）・ほかの問いと並んだ説明は読まない（codex-review）
ASKED_ACT = re.compile(r"([一-龥々ァ-ヶー]{1,12})しても?(?:よい|いい|良い|大丈夫|構わない|かまわない|構いません|かまいません)(?:です|でしょう)?か[?？]?$")
_SENTENCE = re.compile(r"[^。．.!！?？\n]*[?？]?")
# 引用・括弧のひとまとまり（中の文は、ハブが利用者に聞いた問いとして読まない。中の「？」で文を切らない）
_QUOTE = re.compile(r"「[^「」]*」|『[^『』]*』|“[^“”]*”|\"[^\"]*\"|（[^（）]*）|\([^()]*\)")


def mentions(heard: str, name: str) -> bool:
    """聞いた文 heard に、名前 name がそのまま入っているか（大文字小文字・全角半角・空白の違いは数えない）。空の名前は入っていないとみなす。"""
    n = _flat(name)
    return bool(n) and n in _flat(heard)


_ASKS_END = re.compile(r"(?:[?？]|か|かな|かね|っけ)$")  # 問いで終わる文（引用・括弧を隠したあとの文の終わり）


def _sentences(heard: str) -> list[tuple[str, str]]:
    """聞いた文を、引用・括弧の外で文に分けたもの (隠した文, 引用・括弧を元に戻した文)。引用・括弧の中の「？」では切らない。
    隠すしるしには NUL を使うので、入力の NUL は先に取り除く（入力の文字と、しるしを取り違えない。codex-review）。"""
    flat, kept = _flat(heard).replace("\x00", ""), []

    def hide(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    while True:  # 入れ子の引用は内側から順に隠す（隠すたびに字が減るので、必ず終わる）
        hidden = _QUOTE.sub(hide, flat)
        if hidden == flat:
            break
        flat = hidden

    def show(s):
        for _ in range(len(kept) + 1):  # 入れ子の深さぶん戻せば足りる。戻せない形が残っても、回数で必ず終わる
            nxt = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))] if int(m.group(1)) < len(kept) else "", s)
            if nxt == s:
                break
            s = nxt
        return s

    return [(s, show(s)) for s in _SENTENCE.findall(flat) if s.strip()]


def question_count(heard: str) -> int:
    """聞いた文のうち、問いで終わる文（形を問わない。引用・括弧の中は数えない）の数。"""
    return sum(1 for s, _ in _sentences(heard) if _ASKS_END.search(s.rstrip("。．.!！ ")))


def asked_questions(heard: str) -> list[tuple[str, str]]:
    """聞いた文 heard のうち、文の終わりが「〇〇して（も）よいですか？」の形の問いの文ごとに (〇〇, その文)。文は引用・括弧を元に戻した形
    （『受注の記録』を削除してよいですか？ → ("削除", "『受注の記録』を削除してよいですか?")）。引用・括弧の中の問いは読まない（codex-review）。"""
    out = []
    for hidden, shown in _sentences(heard):
        m = ASKED_ACT.search(hidden)
        if m:
            out.append((m.group(1), shown))
    return out


def one_asked(heard: str) -> tuple[str, str] | None:
    """聞いた文の問いが、ちょうど 1 つで、それが「〇〇して（も）よいですか？」の形のときだけ (〇〇, その文)。ほかの形の問いが並ぶ・
    問いが 2 つ以上なら None（利用者の「はい」・操作の語が、どの問いへの答えか分からない。codex-review）。"""
    qs = asked_questions(heard)
    return qs[0] if len(qs) == 1 and question_count(heard) == 1 else None


def asked_acts(heard: str) -> list[str]:
    """聞いた問いが、文の終わりの「〇〇して（も）よいですか？」の形で名指しした操作の語（「削除してよいですか」の「削除」）。"""
    return list(dict.fromkeys(act for act, _ in asked_questions(heard)))


def affirms(text: str, action: str, project: str = "", heard: str = "") -> bool:
    """発言全体が「許す」か（action は close か answer）。answer では、聞いた問い heard の「〇〇してよいですか？」の問いが 1 つだけなら、
    その〇〇で答える（「削除して」「削除する」「削除してください」）のも許す句として読む（聞いていない操作の語、問いが 2 つ以上でどれへの答えか
    分からないときの操作の語は読まない。codex-review）。"""
    if refuses(text):
        return False
    acts = asked_acts(heard) if action == "answer" else []
    acts = acts if one_asked(heard) else []
    yes = _alt(YES + YES_FOR[action] + [w for a in acts for w in (a + "する", a)])
    clause = re.compile(f"{_pre(project)}(?:(?:{yes})(?:{_alt(TAIL)})*)+")
    cs = clauses(text)
    return bool(cs) and all(clause.fullmatch(c) for c in cs)


def chooses(text: str, n: int, options: dict, project: str = "") -> bool:
    """発言全体が、選択肢 n を選ぶ答えか。選びうる選択肢（選ぶ節の番号と、文言が合う選択肢）がちょうど {n} のときだけ。
    選ぶ発言では、プロジェクト名を前置きとして読まない（数字のプロジェクト名「2」で「22番」を「2＋2番」と読まないため）。"""
    if refuses(text):
        return False
    pre, tail, pick = _pre(""), f"(?:{_alt(TAIL)})*", f"(?:{_alt(PICK)})?"
    by_number = re.compile(f"{pre}(?P<n>[0-9]+|[〇一二三四五六七八九])番?目?{pick}{tail}")
    by_label = {k: re.compile(f"{pre}{re.escape(lab)}{pick}{tail}") for k, v in options.items() if len(lab := _flat(str(v))) >= 2}
    plain = _yes_clause(None, "")
    picked: set[int] = set()
    cs = clauses(text)
    for c in cs:
        hit = {k for k, rx in by_label.items() if rx.fullmatch(c)}
        m = by_number.fullmatch(c)
        if m:
            v = m["n"]
            hit.add(int(v) if v.isdigit() else KANJI[v])
        if hit:
            picked |= hit
        elif not plain.fullmatch(c):
            return False
    return bool(cs) and picked == {n}


def asks(heard: str, project: str) -> bool:
    """利用者が聞いたハブの返答が、プロジェクト名（フォルダ名）を挙げた問いか。"""
    return bool(project) and _flat(project) in _flat(heard) and bool(QUESTION.search(heard or ""))
