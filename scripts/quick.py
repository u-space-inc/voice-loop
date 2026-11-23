"""本体がその場で答える決まった問い（設計書の R4・R11 の段 0、S2）。LLM も頭（Hermes）も使わず、本体が持っている状態だけで答える。

決まり（2026-11-23 本人「迷ったら Hermes」）:
- 本体が受けるのは、発言の全体が下の形のときだけ。前後に別のこと（セッションの名前・頼みごと・はい・止めて など）があれば None を返し、頭に渡す。
- 答えは本体の状態（セッションの一覧・承認待ち・頼んだ件・判断待ち）から作る。作り話をしない。問いの形の文にしない
  （承認に答える・閉じるの守りは、利用者が直前に聞いた文が問いかを見る。本体の答えは問いにならず、呼ぶ側は聞いた文を空にする）。
- 承認・選択肢には答えない（どの形にも入れない）。
"""
import re
import unicodedata

KINDS = ("next", "status", "waiting")
NAMES_MAX = 3  # 声で読む名前の数（それより多ければ「ほか N 件」）
NEXT_HOLD = 180.0  # 「次」を続けて言ったとき、前に読んだ件の次へ進む秒数

# 見分ける前に外すもの: 空白・句読点・記号（声の聞き取りは「？」「、」が揺れる）
_DROP = re.compile(r"[\s、。，．,.!！?？…・「」『』（）()〜~ー－―-]+")
_HEAD = r"(?:ねえ|ねぇ|ね|あの|えっと|じゃあ|では|で)?"  # 文の頭の呼びかけ・つなぎ（呼び名は match で足す）
_NOW = r"(?:今|いま)?(?:なんか|何か|なにか)?"
_EXIST = r"(?:(?:は|が)?(?:ある|あります|ありますか|ありますかね|あるかな|ない|ないかな)|は)"  # 有無を聞く終わり方
_TELL = r"(?:は|を教えて|教えて|を教えてください|教えてください)?"
_FORMS = {
    # 次は？ 次は何？ 次ある？
    "next": r"次(?:は|に)?(?:何|なに|なん)?(?:が|か)?(?:ある|あります|ありますか|する|やる|ですか)?",
    # どうなってる？ 状況は？ 進行中のある？ 動いてるセッションある？
    "status": rf"{_NOW}(?:全体(?:の|は|で|として)?)?(?:(?:どうなってる|どうなっている|どうなってますか|どうなっていますか)"
              rf"|(?:全体の状況|状況|様子){_TELL}"
              rf"|(?:進行中|稼働中|作業中|動いてる|動いている)(?:の|になってる|になっている)?(?:セッション|もの)?{_EXIST}?)",
    # 待ってるのある？ 承認待ちある？ 止まってるセッションある？（「承認」だけ・「待ってる」だけは受けない。許可や返事のつもりの発言は頭へ）
    "waiting": rf"(?:他の|ほかの)?(?:セッション)?(?:で)?{_NOW}"
               rf"(?:(?:承認待ち|入力待ち)(?:の|になってる|になっている)?(?:セッション|もの)?{_EXIST}?"
               rf"|(?:待機中|止まってる|止まっている|待ってる|待っている)(?:の|になってる|になっている)?(?:の)?(?:セッション|もの)?(?:の)?{_EXIST})",
}


def normalize(text: str) -> str:
    return _DROP.sub("", unicodedata.normalize("NFKC", text or ""))


def match(text: str, agent: str = "") -> str | None:
    """発言の全体が決まった問いの形なら、その種類（KINDS のどれか）。違えば None（頭に渡す）。agent は呼び名（文の頭の呼びかけを外す）。"""
    t = normalize(text)
    if not t or len(t) > 40:
        return None
    names = [re.escape(normalize(n)) for n in (agent, "ハブ", "ヴィカ") if n and normalize(n)]
    head = rf"{_HEAD}(?:{'|'.join(names)})?{_HEAD}" if names else _HEAD
    for kind in KINDS:
        if re.fullmatch(head + _FORMS[kind], t):
            return kind
    return None


def _names(rows: list[dict]) -> str:
    shown = [r["name"] for r in rows[:NAMES_MAX]]
    rest = len(rows) - len(shown)
    return "、".join(shown) + (f"、ほか {rest} 件" if rest > 0 else "")


def _ask_word(row: dict) -> str:
    return "承認待ち" if row.get("ask") == "permission" else "入力待ち"


def queue(snap: dict) -> list[dict]:
    """あなたを待っているものの並び（「次」で読む順）。承認・入力待ちのセッション → 判断待ち（新しい順）。key は並びの中で見分ける印。"""
    out = [{"key": "s:" + r["pane"], "say": f"{r['name']}が{_ask_word(r)}です"}
           for r in snap.get("sessions", []) if r.get("status") == "blocked"]
    out += [{"key": f"i:{it['n']}", "say": f"判断待ちの「{it['title']}」" + (f"（{it['project']}）" if it.get("project") else "") + "です"}
            for it in snap.get("inbox", [])]
    return out


def answer(kind: str, snap: dict, cursor: dict | None = None, now: float = 0.0) -> str:
    """決まった問いへの答え（声で読む文。問いの形にしない）。snap は本体の状態の写し:
    sessions: [{pane, name, status（idle・working・blocked・done）, ask（permission・question など。blocked のとき）}]、
    watching: 頼んだ件の見張り中の数、inbox: [{n, title, project}]（判断待ち。新しい順）、inbox_problem: 読めなかったら真。
    cursor は「次」をどこまで読んだか（呼ぶ側が持つ dict。{"key", "at"} を書き換える）。"""
    sessions = snap.get("sessions", [])
    working = [r for r in sessions if r.get("status") == "working"]
    blocked = [r for r in sessions if r.get("status") == "blocked"]
    if kind == "waiting":
        if not blocked:
            return "承認・入力待ちのセッションはありません。"
        return f"承認・入力待ちは {len(blocked)} 件です。{_names(blocked)}。答えは画面の上に出ています。"
    if kind == "status":
        parts = [f"作業中は {len(working)} 件です。{_names(working)}。" if working else "いま作業中のセッションはありません。"]
        if blocked:
            parts.append(f"承認・入力待ちが {len(blocked)} 件あります。{_names(blocked)}。")
        if snap.get("watching"):
            parts.append(f"頼んだ件の見張り中は {snap['watching']} 件です。")
        return "".join(parts)
    if kind == "next":
        items = queue(snap)
        cur = cursor if cursor is not None else {}
        start = 0
        if cur.get("key") and now - float(cur.get("at") or 0) < NEXT_HOLD:
            keys = [x["key"] for x in items]
            if cur["key"] in keys:
                start = keys.index(cur["key"]) + 1
        partial = "（判断待ちの記録が大きいので、新しい側だけを見ています）" if snap.get("inbox_partial") else ""
        if not items:
            cur.update(key="", at=now)
            if snap.get("inbox_problem"):
                return "承認・入力待ちのセッションはありません。判断待ちの記録は読めませんでした。"
            return f"見た範囲では、あなたを待っているものはありません{partial}。" if partial else "いま、あなたを待っているものはありません。"
        if start >= len(items):
            cur.update(key="", at=now)
            if snap.get("inbox_problem"):  # 承認・入力待ちは読み終えたが、判断待ちは確かめられていない（codex-review）
                return f"ほかの承認・入力待ちはありません（{len(items)} 件でした）。判断待ちの記録は読めませんでした。"
            return f"見た範囲では、ほかに待っているものはありません{partial}。" if partial else f"ほかに待っているものはありません（全部で {len(items)} 件でした）。"
        it = items[start]
        cur.update(key=it["key"], at=now)
        rest = len(items) - start - 1
        return f"次は、{it['say']}。" + (f"このあと {rest} 件あります。" if rest else "")
    raise ValueError(kind)
