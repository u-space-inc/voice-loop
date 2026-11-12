"""Gmail の下書きを作る（送らない）。同梱の google-workspace の台本と同じ鍵（HERMES_HOME/google_token.json）を使う。

  vl_gmail_draft.py --reply-to <メールの id> --body "本文"        そのメールへの返信の下書き（宛先・件名・スレッドは元のメールから）
  vl_gmail_draft.py --to a@example.com --subject "件名" --body "本文"   新しいメールの下書き

作るのは下書きだけで、送信の API は呼ばない。返信の宛先は 1 人だけ（Reply-To が 1 つのアドレスならそれ、でなければ From）。
結果は {"draft_id", "message_id", "thread_id", "to", "cc", "reply_to_ignored"} を 1 行の JSON で出す。宛先は利用者に伝える。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or "/opt/data")
sys.path.insert(0, str(HOME / "skills/productivity/google-workspace/scripts"))
import google_api  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reply-to"); p.add_argument("--to"); p.add_argument("--cc"); p.add_argument("--subject")
    p.add_argument("--body", required=True)
    p.add_argument("--dry-run", action="store_true", help="下書きを作らず、組み立てた見出しの形だけを出す（試し用）")
    a = p.parse_args()
    svc = google_api.build_service("gmail", "v1")
    msg = EmailMessage()
    thread, reply_to_ignored = None, False
    if a.reply_to:
        orig = svc.users().messages().get(userId="me", id=a.reply_to, format="metadata",
                                          metadataHeaders=["From", "Reply-To", "Subject", "Message-ID", "References", "Cc"]).execute()
        h = {x["name"].lower(): x["value"] for x in orig.get("payload", {}).get("headers", [])}
        thread = orig.get("threadId")
        subj = h.get("subject", "")
        # 宛先は 1 人だけ。Reply-To が 1 つのアドレスなら採り、複数・空・形の崩れなら From にする（相手の見出しで宛先を足させない）。
        # Reply-To が差出人と別のアドレスでも採るので、下書きの宛先は、送る前に利用者が確かめる（結果の JSON に出す）。
        rt = [x for x in getaddresses([h.get("reply-to", "")]) if x[1]]
        fr = [x for x in getaddresses([h.get("from", "")]) if x[1]]
        pick = rt if len(rt) == 1 else fr[:1]
        if not pick:
            sys.exit("元のメールから宛先を決められません（--to で指定してください）")
        reply_to_ignored = bool(h.get("reply-to")) and len(rt) != 1
        msg["To"] = a.to or formataddr(pick[0])
        msg["Subject"] = a.subject or (subj if subj.lower().startswith("re:") else f"Re: {subj}")
        if h.get("message-id"):
            msg["In-Reply-To"] = h["message-id"]
            msg["References"] = (h.get("references", "") + " " + h["message-id"]).strip()
    else:
        if not a.to or not a.subject:
            sys.exit("--reply-to か、--to と --subject を指定してください")
        msg["To"], msg["Subject"] = a.to, a.subject
    if a.cc:
        msg["Cc"] = a.cc
    msg.set_content(a.body)
    if a.dry_run:
        print(json.dumps({"to_set": bool(msg["To"]), "subject_re": str(msg["Subject"] or "").lower().startswith("re:"),
                          "in_reply_to": bool(msg["In-Reply-To"]), "thread": bool(thread)}))
        return 0
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"message": {"raw": raw, **({"threadId": thread} if thread else {})}}
    d = svc.users().drafts().create(userId="me", body=body).execute()
    print(json.dumps({"draft_id": d.get("id"), "message_id": d.get("message", {}).get("id"), "thread_id": d.get("message", {}).get("threadId"),
                      "to": str(msg["To"]), "cc": str(msg["Cc"] or ""), "reply_to_ignored": reply_to_ignored}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
