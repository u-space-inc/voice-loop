"""画面の 💡 の「自動で当てていない手順の書き足し（Hermes）」（旧「ハブが手順に書き足したいこと」）の通し。本体の写しをこの試験の中で 127.0.0.1:8791 に立て、本物のブラウザ（Chromium と、
iPhone を名乗る WebKit）で開く。Hermes の置き場は一時のフォルダ、コンテナの台本は差し替える（docker・常駐の 8790 には触れない）。
見ること: 欄が出る（要約・いつ・どこから）、扱えない形は採るを出さない、差分は開いたときに取り、描き直しても開いたまま、
文は文字として出す（タグを解釈しない）、「採る」は2段（1回目では当てない・描き直しで確かめが消えない）、採った・捨てたの案内、
分からないの赤い案内と採るが消えること、コンテナが使えないときの理由、承認待ちが無ければ欄を出さない。
差分は行ごとに色を分ける（文字として入れる）。学んだこと（Chromium・WebKit）: 自動で当てた手順と記憶の跡を出す（文字として。30 件を超えたら「さらに見る」）・中身を開く・
「戻す」は 2 段（1 回目では戻さない）・戻すと「戻しました」・戻せない跡には「戻す」を出さない。PC の幅（Chromium）では ⤢ で広げて読む（差分を高さで切らず字を大きく・開いた差分と確かめは残る・Esc で戻る）。
使い方: uv run spike/hermes_skills_ui_test.py"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
HOME = Path(TMP) / "hermes"
PEND = HOME / "pending" / "skills"
PEND.mkdir(parents=True)
os.environ.update(VL_CONFIG_DIR=f"{TMP}/cfg", VL_STATE_DIR=f"{TMP}/state", VL_HERMES_KEY="k1", VL_HERMES_URL="http://127.0.0.1:9", VL_HERMES_HOME=str(HOME), VL_INTAKE="1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import server as s  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
results = []


def check(name, got, want):
    results.append((name, got == want, got, want))


async def nothing(*a, **k):
    return None


s.boot_ensure_hub = s.keep_gateway_token = s.watch_blocked = s.watch_delegation = s.hermes.watch_hermes_skills = nothing
s.list_agents = lambda: []
CALLS = []
OUT = {}


def fake_run(container, mode, pid, raw, wait=90, env=None, script=None):
    CALLS.append((mode, pid))
    r = OUT.get(mode)
    if r is None:
        r = {"check": {"result": "ok"}, "diff": {"result": "diff", "diff": f"--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1,2 +1,2 @@\n-消す行\n+<b>足す行</b> {pid}\n 残る行\n"},
             "approve": {"result": "applied", "discarded": True}, "reject": {"result": "rejected", "discarded": True}}[mode]
    if r.get("result") in ("applied", "rejected") and r.get("discarded"):
        (PEND / f"{pid}.json").unlink(missing_ok=True)  # 台本が承認待ちから消すのにあたる
    return dict(r)


s.hermes.hermes_skills.run_script = fake_run
s.hermes.hermes_skills.still_running = lambda c: False


def record(pid, payload, summary, origin="background_review"):
    d = {"id": pid, "subsystem": "skills", "action": payload["action"], "summary": summary, "origin": origin, "created_at": time.time(), "payload": payload}
    (PEND / f"{pid}.json").write_text(json.dumps(d, ensure_ascii=False))


PATCH = {"action": "batch", "operations": [{"action": "patch", "name": "voice-loop-hub", "old_string": "a", "new_string": "a\nb"}]}


def seed():
    for p in PEND.iterdir():
        p.unlink()
    s.hermes.HSK_FILE.unlink(missing_ok=True)
    for f in s.hermes.HLEARN_DIR.glob("*.json"):  # 学んだことの跡も空にする（節ごとに始め直す）
        f.unlink()
    s.hermes.HSK_SHOWN.clear()
    s.hermes.HSK_DIFFS.clear()
    s._probes.pop("hermes-skills", None)
    CALLS.clear()
    OUT.clear()
    record("aaaaaaaa", PATCH, "読み替えに「山だ」を足す")
    record("bbbbbbbb", {"action": "delete", "name": "voice-loop-hub"}, "<img src=x onerror=\"window.__xss=1\">手順を消す", origin="assistant_tool")


srv = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=PORT, log_level="warning"))
th = threading.Thread(target=srv.run, daemon=True)
th.start()
for _ in range(200):
    if srv.started:
        break
    time.sleep(0.05)


def wait(fn, limit=8.0):
    end = time.time() + limit
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def body(page):
    return page.text_content("#sitBody") or ""


def open_panel(page):
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody > *")


def heads(page):
    return page.eval_on_selector_all("#sitBody h4", "els => els.map(e => e.textContent)")


def run(page, who):
    seed()
    open_panel(page)
    page.wait_for_selector("#sitBody .prop.hsk:not(.learn)")
    h = heads(page)
    check(f"{who} 欄が出る", h[0].startswith("自動で当てていない手順の書き足し（Hermes・2）"), True)
    cards = page.locator("#sitBody .prop.hsk:not(.learn)")
    texts = [cards.nth(i).text_content() for i in range(cards.count())]
    check(f"{who} 要約・どこから（見直し・会話の中）・扱えない形の理由", (cards.count(), "読み替えに「山だ」を足す" in texts[0], "会話のあとの見直しから" in texts[0],
                                                       "会話の中から" in texts[1], "この形は画面では扱えません（delete は扱えません）" in texts[1]), (2, True, True, True, True))
    check(f"{who} 要約は文字として出す（タグを解釈しない）", ("<img src=x" in texts[1], page.evaluate("window.__xss || 0")), (True, 0))
    btns = lambda i: [b.text_content() for b in page.locator("#sitBody .prop.hsk:not(.learn)").nth(i).locator(".acts button").all()]
    check(f"{who} 扱える形には採ると捨てる、扱えない形には捨てるだけ", (btns(0), btns(1)), (["採る", "捨てる"], ["捨てる"]))
    check(f"{who} 似た書き足しなら捨ててよい、の一言", "似た書き足しがもう手順にあれば、捨ててかまいません" in body(page), True)

    first = page.locator("#sitBody .prop.hsk:not(.learn)").nth(0)
    first.locator("summary").click()
    check(f"{who} 差分は開いたときに取り、文字として出す", wait(lambda: "+<b>足す行</b> aaaaaaaa" in (first.locator("pre.diff").text_content() or "")), True)
    lines = first.locator("pre.diff span").evaluate_all("els => els.map(e => [e.className, e.textContent])")
    check(f"{who} 差分は行ごとに色を分ける（足した行・消した行・位置の行に印、ファイル名の行と変わらない行には付けない）。タグは要素にしない",
          (lines, first.locator("pre.diff b").count()),
          ([["", "--- a/SKILL.md"], ["", "+++ b/SKILL.md"], ["hunk", "@@ -1,2 +1,2 @@"], ["del", "-消す行"], ["add", "+<b>足す行</b> aaaaaaaa"], ["", " 残る行"]], 0))
    page.evaluate("loadSituation()")
    time.sleep(0.6)
    first = page.locator("#sitBody .prop.hsk:not(.learn)").nth(0)
    check(f"{who} 描き直しても差分は開いたまま（取り直さない）",
          (first.locator("details").get_attribute("open") is not None, "+<b>足す行</b>" in (first.locator("pre.diff").text_content() or ""),
           [c for c in CALLS if c[0] == "diff"]), (True, True, [("diff", "aaaaaaaa")]))

    first.locator(".acts button").nth(0).click()
    check(f"{who} 「採る」の1回目は確かめを出すだけ（当てない）",
          (first.locator(".acts button").nth(0).text_content(), [c for c in CALLS if c[0] == "approve"]), ("Hermes の手順に書き足す", []))
    page.evaluate("loadSituation()")
    time.sleep(0.5)
    check(f"{who} 描き直しで確かめが消えない", page.locator("#sitBody .prop.hsk:not(.learn)").nth(0).locator(".acts button").nth(0).text_content(), "Hermes の手順に書き足す")
    page.locator("#sitBody .prop.hsk:not(.learn)").nth(0).locator(".acts button").nth(0).click()
    check(f"{who} 2回目で当て、案内を出し、欄から消える",
          (wait(lambda: "手順に書き足しました。次からの会話で使われます" in body(page)), wait(lambda: page.locator("#sitBody .prop.hsk:not(.learn)").count() == 1),
           [c for c in CALLS if c[0] == "approve"]), (True, True, [("approve", "aaaaaaaa")]))
    page.locator("#sitBody .prop.hsk:not(.learn)").nth(0).locator(".acts button").nth(0).click()
    check(f"{who} 捨てると案内を出し、承認待ちが無くなれば欄の見出しも出さない",
          (wait(lambda: "書き足しを捨てました" in body(page)), wait(lambda: not any(x.startswith("ハブが手順に") for x in heads(page)))), (True, True))

    seed()
    OUT["approve"] = {"result": "failed", "why": "patch would fail"}
    page.click("#sitClose")
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .prop.hsk:not(.learn)")
    b = page.locator("#sitBody .prop.hsk:not(.learn)").nth(0).locator(".acts button").nth(0)
    b.click()
    b.click()
    check(f"{who} 当たったか分からないときは赤で出し、その書き足しは採るを出さない（捨てるは出す）",
          (wait(lambda: page.locator("#sitNote.bad").count() == 1 and "当たったか分かりません" in (page.text_content("#sitNote") or "")),
           wait(lambda: btns(0) == ["捨てる"] and "当たったか分かりません" in (page.locator("#sitBody .prop.hsk:not(.learn)").nth(0).text_content() or ""))), (True, True))

    seed()
    OUT["check"] = {"result": "error", "why": "Error: container hermes is not running"}
    page.click("#sitClose")
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody .prop.hsk:not(.learn)")
    check(f"{who} コンテナが使えないときは理由を出し、採るを出さない",
          (wait(lambda: "Hermes のコンテナ（hermes）で台本を動かせません" in body(page)), btns(0)), (True, ["捨てる"]))
    page.click("#sitClose")

    # ハブの会話を新しくする（2段。1回目は確かめだけ）
    page.click("#sitBtn")
    page.wait_for_selector("#sitBody > *")
    row = lambda: page.locator("#sitBody .sitHead").nth(0)  # ハブの会話の行（拾う役の見出しの行は 0.20.0 で外した）
    before = s.hermes.hermes_session()
    check(f"{who} ハブの会話の欄に、いまの会話の名前と、毎朝4時に新しくなることが出る",
          wait(lambda: before in (row().text_content() or "") and "毎朝4時に新しくなります" in (row().text_content() or "")), True)
    row().locator("button").click()
    check(f"{who} 「ハブの会話を新しくする」の1回目は確かめを出すだけ（新しくしない）", (row().locator("button").text_content(), s.hermes.hermes_session()),
          ("前の会話を持ち越さずに始める", before))
    row().locator("button").click()
    check(f"{who} 2回目で新しくし、案内と新しい会話の名前を出す",
          (wait(lambda: "ハブの会話を新しくしました" in body(page)), wait(lambda: s.hermes.hermes_session() != before and s.hermes.hermes_session() in (row().text_content() or ""))),
          (True, True))
    page.click("#sitClose")


LCALLS = []


def fake_learn(container, mode, req, wait=90, env=None):
    LCALLS.append(mode)
    return {"result": "restored"}


s.hermes.hermes_learn.run_script = fake_learn


def run_learned(page, who):
    """学んだこと: 跡を出し、中身を開き、「戻す」は 2 段"""
    seed()
    LCALLS.clear()
    s.hermes._hlearn_add({"kind": "skill", "auto": True, "pid": "cccccccc", "summary": "<b>返事</b>を短くする手順", "origin": "background_review",
                          "places": ["voice-loop/hub"], "can_undo": True},
                         {"diff": "--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1 +1 @@\n-長い\n+短い\n", "before": {"voice-loop/hub": {"exists": True, "files": {}}},
                          "expect": {"voice-loop/hub": "x"}})
    s.hermes._hlearn_add({"kind": "memory", "target": "user", "added": 1, "removed": 0, "summary": "朝は 7 時に起きる", "can_undo": True},
                         {"added": ["朝は 7 時に起きる"], "removed": []})
    s.hermes._hlearn_add({"kind": "skill", "auto": True, "pid": "dddddddd", "summary": "戻せない手順", "can_undo": False, "note": "当てたあとの中身を読めなかったので、戻せません"},
                         {"diff": "", "before": {}, "expect": {}})
    s.hermes._hlearn_add({"kind": "skill", "auto": True, "pid": "eeeeeeee", "summary": "分からない手順", "state": "unknown", "can_undo": False}, {"diff": ""})
    s.hermes._hlearn_add({"kind": "skill", "auto": True, "pid": "ffffffff", "summary": "当てられなかった手順", "state": "failed", "can_undo": False}, {"diff": ""})
    st = json.loads(s.hermes.HLEARN_FILE.read_text())
    st["days"] = {s.hermes._hlearn_day(time.time()): 2}  # 今日自動で当てた（当てようとした）のは 2 件
    s.hermes.HLEARN_FILE.write_text(json.dumps(st))
    open_panel(page)
    page.wait_for_selector("#sitBody .learn")
    cards = page.locator("#sitBody .learn")
    texts = cards.evaluate_all("els => els.map(e => e.querySelector('.text').textContent)")
    check(f"{who} 学んだことの欄に、手順と記憶の跡を出す（文字として。今日の数と上限）",
          (any(h.startswith("学んだこと（Hermes。今日自動で当てた手順 2/20）") for h in heads(page)), sorted(texts),
           page.locator("#sitBody .learn b").count()),
          (True, sorted(["手順｜<b>返事</b>を短くする手順", "記憶（あなたについて）｜足した 1・消した 0：朝は 7 時に起きる", "手順｜戻せない手順", "手順｜分からない手順", "手順｜当てられなかった手順"]), 0))
    meta = lambda t: cards.filter(has_text=t).first.locator(".sitMeta").text_content() or ""
    check(f"{who} 状態ごとに出し分ける（当てた・分からない・当てられなかった・記憶）",
          [w in meta(t) for t, w in (("返事", "自動で当てました"), ("分からない手順", "当たったか分かりません"), ("当てられなかった", "当てられませんでした"), ("朝は", "Hermes が書きました"))],
          [True] * 4)
    skill = cards.filter(has_text="返事").first
    skill.locator("summary").click()
    check(f"{who} 中身を開くと、手順は差分", wait(lambda: "+短い" in (skill.locator("pre.diff").text_content() or "")), True)
    mem = cards.filter(has_text="朝は").first
    mem.locator("summary").click()
    check(f"{who} 中身を開くと、記憶は足した項目（+）と消した項目（-）", wait(lambda: (mem.locator("pre.diff").text_content() or "") == "+ 朝は 7 時に起きる"), True)
    check(f"{who} 戻せない跡には「戻す」を出さず、理由を出す",
          (cards.filter(has_text="戻せない手順").first.locator(".acts").count(), "当てたあとの中身を読めなかった" in (cards.filter(has_text="戻せない手順").first.text_content() or "")), (0, True))
    back = skill.locator(".acts button")
    back.click()
    check(f"{who} 「戻す」の 1 回目は確かめを出すだけ（戻さない）", (back.text_content(), LCALLS), ("当てる前の手順に戻す", []))
    back.click()
    check(f"{who} 2 回目で戻し、案内と「戻しました」を出す", wait(lambda: "戻しました" in body(page) and LCALLS == ["restore"])
          and cards.filter(has_text="返事").first.locator(".acts").count(), 0)
    # 跡が 30 件を超えても、古いものに画面から届く（「さらに見る」）
    for i in range(30):
        s.hermes._hlearn_add({"kind": "memory", "target": "memory", "added": 1, "removed": 0, "summary": f"足した{i:02d}", "can_undo": True},
                             {"added": [f"足した{i:02d}"], "removed": []})
    page.evaluate("loadSituation()")
    wait(lambda: page.locator("#sitBody .learnMore").count() == 1)
    more = page.locator("#sitBody .learnMore")
    first_n = cards.count()
    check(f"{who} 跡が 30 件を超えたら、新しい 30 件と「さらに見る」（残りの数）を出す", (first_n, (more.text_content() or "") if more.count() else ""),
          (30, "さらに 5 件を見る（残り 5 件）"))
    if more.count():
        more.click()
    check(f"{who} 「さらに見る」で、古い跡（最初の手順の跡）も出す", wait(lambda: cards.count() == 35 and page.locator("#sitBody .learnMore").count() == 0)
          and cards.filter(has_text="当てられなかった手順").count(), 1)


def run_wide(page, who):
    """PC の幅: 右の「状況」で差分を開き、⤢ で広げて読む（差分は高さで切らず、字を大きく。広げても、開いた差分と採るの確かめは残る）"""
    seed()
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('conn').classList.contains('on')")
    page.click("#tabSit")
    page.wait_for_selector("#sitBody .prop.hsk:not(.learn)")
    first = page.locator("#sitBody .prop.hsk:not(.learn)").nth(0)
    first.locator("summary").click()
    wait(lambda: "+<b>足す行</b>" in (first.locator("pre.diff").text_content() or ""))
    style = lambda: first.locator("pre.diff").evaluate("e => [getComputedStyle(e).maxHeight, getComputedStyle(e).fontSize]")
    narrow = style()
    first.locator(".acts button").nth(0).click()  # 採るの 1 段目（確かめを出すだけ）
    page.click("#readBtn")
    rec = page.locator("#rec").bounding_box()
    check(f"{who} ⤢ で右の列が画面いっぱいになり、差分は高さで切らず字を大きくする（3 列のときは高さで切り、小さい字）",
          (page.evaluate("document.body.classList.contains('reading')"), [round(rec["x"]), round(rec["width"])], narrow[0] != "none", narrow[1], style()),
          (True, [0, 1400], True, "11px", ["none", "13px"]))
    check(f"{who} 広げても、開いた差分と採るの確かめはそのまま（当ててはいない）",
          (first.locator("details").get_attribute("open") is not None, first.locator(".acts button").nth(0).text_content(), [c for c in CALLS if c[0] == "approve"]),
          (True, "Hermes の手順に書き足す", []))
    page.keyboard.press("Escape")
    check(f"{who} Esc で 3 列に戻る（確かめはまだ残る）",
          (page.evaluate("document.body.classList.contains('reading')"), first.locator(".acts button").nth(0).text_content()), (False, "Hermes の手順に書き足す"))


with sync_playwright() as pw:
    b = pw.chromium.launch()
    run(b.new_page(viewport={"width": 390, "height": 844}), "Chromium:")
    run_learned(b.new_page(viewport={"width": 390, "height": 844}), "Chromium（学んだこと）:")
    run_wide(b.new_page(viewport={"width": 1400, "height": 900}), "Chromium（PC）:")
    b.close()
    b = pw.webkit.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
    run(ctx.new_page(), "WebKit:")
    run_learned(ctx.new_page(), "WebKit（学んだこと）:")
    b.close()

srv.should_exit = True
th.join(5)
ok = sum(r[1] for r in results)
for name, passed, got, want in results:
    print(("✓ " if passed else "✗ ") + name + ("" if passed else f"\n    得た値: {got!r}\n    期待:   {want!r}"))
print(f"\n{ok}/{len(results)} 件が期待どおり")
sys.exit(0 if ok == len(results) else 1)
