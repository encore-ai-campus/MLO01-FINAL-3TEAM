"""Normalize one new test inquiry. Provider payloads stay unchanged on disk."""

import base64
import binascii
import json
from datetime import datetime, timezone
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from uuid import NAMESPACE_URL, uuid5


SAMPLE_TEXT = "결제 버튼을 누르면 오류가 납니다."


class ProbeError(Exception):
    pass


def marked_text(value, run):
    """Return the text following the exact test prefix, or None for other messages."""
    marker = f"[{run}]"
    value = (value or "").strip()
    if value == marker:
        return ""
    if value.startswith(marker) and value[len(marker):len(marker) + 1].isspace():
        return value[len(marker):].strip()
    return None


def header(part, name):
    return next((h["value"] for h in part.get("headers", [])
                 if h.get("name", "").casefold() == name.casefold()), "")


def clean(value):
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


class HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.suppressed += 1
        elif not self.suppressed and tag in {"p", "br", "div", "li", "tr"}:
            self.pieces.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.suppressed = max(0, self.suppressed - 1)
        elif not self.suppressed and tag in {"p", "div", "li", "tr"}:
            self.pieces.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.pieces.append(data)


def mail_text(part):
    """Return (representation, text) without exporting attachments or duplicate alternatives."""
    mime = part.get("mimeType", "").split(";")[0].strip().lower()
    if part.get("filename") or header(part, "Content-Disposition").lower().startswith("attachment"):
        return "", ""
    if mime.startswith("multipart/"):
        children = part.get("parts", [])
        if mime == "multipart/alternative":
            # A plain part can satisfy the request even when a separate HTML alternative is large.
            children = sorted(children, key=lambda p: p.get("mimeType") != "text/plain")
            choices = []
            for child in children:
                kind, text = mail_text(child)
                if kind == "plain" and text:
                    return kind, text
                if text:
                    choices.append((kind, text))
            return choices[0] if choices else ("", "")
        parts = [pair for child in children if (pair := mail_text(child))[1]]
        kind = "plain" if any(k == "plain" for k, _ in parts) else "html"
        return kind, "\n".join(t for _, t in parts)
    if mime not in {"text/plain", "text/html"}:
        return "", ""
    body = part.get("body", {})
    data = body.get("data", "")
    if not data and body.get("attachmentId"):
        raise ProbeError("텍스트 본문이 별도 attachmentId로 저장됨. raw.json을 확인하세요.")
    content_type = Message()
    content_type["Content-Type"] = header(part, "Content-Type") or mime
    try:
        binary = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
        decoded = binary.decode(content_type.get_content_charset() or "utf-8")
    except (binascii.Error, ValueError, LookupError) as error:
        raise ProbeError("Gmail 본문의 인코딩이나 charset을 확인하세요.") from error
    if mime == "text/plain":
        return "plain", clean(decoded)
    parser = HtmlText()
    parser.feed(decoded)
    parser.close()
    return "html", "\n".join(line.strip() for line in "".join(parser.pieces).splitlines() if line.strip())


def gmail_fields(raw, run):
    part = raw.get("payload", {})
    title = marked_text(header(part, "Subject"), run)
    if title is None or "INBOX" not in raw.get("labelIds", []):
        raise ProbeError("테스트 제목 표식과 INBOX 라벨이 일치하지 않습니다.")
    if not raw.get("id") or not raw.get("threadId"):
        raise ProbeError("Gmail Message의 id와 threadId가 필요합니다.")
    occurred = None
    try:
        if raw.get("internalDate"):
            occurred = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc).isoformat()
        elif date_header := header(part, "Date"):
            date = parsedate_to_datetime(date_header)
            if date.tzinfo is not None:
                occurred = date.astimezone(timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError, TypeError):
        pass
    return {
        "source_message_id": raw["id"], "source_conversation_id": raw["threadId"],
        "sender_ref": parseaddr(header(part, "From"))[1] or None,
        "title": title, "body_text": mail_text(part)[1], "occurred_at": occurred,
    }


def discord_message_status(raw, run, channel_id):
    """Return the capture decision for a new message in the requested channel."""
    if raw.get("t") != "MESSAGE_CREATE" or raw.get("op") != 0:
        return None
    message = raw.get("d") or {}
    if message.get("channel_id") != channel_id:
        return None
    if message.get("webhook_id") or (message.get("author") or {}).get("bot"):
        return "automated"
    if not (message.get("content") or "").strip():
        return "empty"
    if marked_text(message["content"], run) is None:
        return "unmarked"
    return "match"


def is_discord_test(raw, run, channel_id):
    return discord_message_status(raw, run, channel_id) == "match"


def discord_fields(raw, run, channel_id, is_thread):
    if not is_discord_test(raw, run, channel_id):
        raise ProbeError("지정 채널에서 사람이 보낸 테스트 MESSAGE_CREATE가 필요합니다.")
    message = raw["d"]
    if not message.get("id") or not message.get("guild_id") or not message.get("author", {}).get("id"):
        raise ProbeError("Discord 원본의 메시지·서버·작성자 ID를 확인하세요.")
    if message.get("type") not in (0, None) and not is_thread:
        raise ProbeError("일반 채널의 답글은 원문의 티켓 조회가 필요합니다. 첫 문의로 양 채널을 비교하세요.")
    conversation = channel_id if is_thread else message["id"]
    return {
        "source_message_id": message["id"], "source_conversation_id": conversation,
        "sender_ref": message["author"]["id"], "title": "",
        "body_text": marked_text(message["content"], run), "occurred_at": message.get("timestamp"),
    }


def transform(channel, raw, capture):
    run = capture["run"]
    if channel == "discord":
        fields = discord_fields(raw, run, capture["channel_id"], capture["is_thread"])
    elif channel == "gmail":
        fields = gmail_fields(raw, run)
    else:
        raise ProbeError("지원하는 수신 채널은 discord와 gmail입니다.")
    company = "local-test-company"
    connection = capture["connection_id"]
    ticket_key = json.dumps([company, channel, connection, fields["source_conversation_id"]])
    # Each run compares the first captured inquiry per channel, so its sample body is revision 1.
    ticket_id = "VOC-" + uuid5(NAMESPACE_URL, ticket_key).hex
    common = {
        "schema_version": 1, "company_id": company, "connection_id": connection,
        "channel": channel, "ticket_id": ticket_id, "body_version": 1,
        "received_at": capture["received_at"], "raw_ref": "raw.json", **fields,
    }
    title, body = clean(fields["title"]), clean(fields["body_text"])
    if not body:
        raise ProbeError("분류할 고객 본문이 없습니다. 원본은 raw.json에서 확인하세요.")
    if title and " ".join(title.split()) != " ".join(body.split()):
        body = title + "\n" + body
    classifier = {"schema_version": 1, "ticket_id": ticket_id, "body_version": 1, "analysis_text": body}
    return common, classifier
