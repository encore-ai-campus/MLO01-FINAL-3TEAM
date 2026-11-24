"""Normalize one new test inquiry. Provider payloads stay unchanged on disk."""

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from uuid import NAMESPACE_URL, uuid5

from collector.events import event_id as bronze_event_id


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


def source_id(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ProbeError(f"{field}에는 비어 있지 않은 원천 문자열 ID가 필요합니다.")
    return value


def utc_timestamp(value):
    """Return an unambiguous UTC instant at PostgreSQL microsecond precision."""
    try:
        if isinstance(value, str):
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


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
    title = header(part, "Subject")
    if run is not None:
        title = marked_text(title, run)
        if title is None or "INBOX" not in raw.get("labelIds", []):
            raise ProbeError("테스트 제목 표식과 INBOX 라벨이 일치하지 않습니다.")
    occurred = None
    millis = raw.get("internalDate")
    try:
        if type(millis) in (int, str):
            occurred = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(millis))
    except (ValueError, OverflowError):
        pass
    if occurred is None and (date_header := header(part, "Date")):
        try:
            occurred = parsedate_to_datetime(date_header)
        except (ValueError, TypeError, OverflowError):
            pass
    return {
        "source_message_id": source_id(raw.get("id"), "id"),
        "source_conversation_id": source_id(raw.get("threadId"), "threadId"),
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
    message = raw.get("d") or {}
    if raw.get("op") != 0 or raw.get("t") != "MESSAGE_CREATE" or message.get("channel_id") != channel_id:
        raise ProbeError("지정 채널에서 수신한 MESSAGE_CREATE가 필요합니다.")
    body = message.get("content")
    if run is not None:
        if not is_discord_test(raw, run, channel_id):
            raise ProbeError("지정 채널에서 사람이 보낸 테스트 MESSAGE_CREATE가 필요합니다.")
        body = marked_text(body, run)
    message_id = source_id(message.get("id"), "id")
    author_id = (message.get("author") or {}).get("id")
    if message.get("type") not in (0, None) and not is_thread:
        raise ProbeError("일반 채널의 답글은 원문의 티켓 조회가 필요합니다. 첫 문의로 양 채널을 비교하세요.")
    conversation = source_id(channel_id, "channel_id") if is_thread else message_id
    return {
        "source_message_id": message_id, "source_conversation_id": conversation,
        "sender_ref": source_id(author_id, "author.id") if author_id is not None else None, "title": None,
        "body_text": body, "occurred_at": message.get("timestamp"),
    }


def transform(channel, raw, capture):
    run = capture.get("run")
    company = capture.get("company_id", "local-test-company" if run is not None else None)
    company = source_id(company, "company_id")
    connection = source_id(capture.get("connection_id"), "connection_id")
    if channel == "discord":
        fields = discord_fields(raw, run, capture["channel_id"], capture["is_thread"])
        if raw["d"].get("guild_id") != connection:
            raise ProbeError("Discord 서버 ID와 수신 연결 ID가 다릅니다.")
    elif channel == "gmail":
        fields = gmail_fields(raw, run)
    else:
        raise ProbeError("지원하는 수신 채널은 discord와 gmail입니다.")
    ticket_key = json.dumps([company, channel, connection, fields["source_conversation_id"]])
    # Each run compares the first captured inquiry per channel, so its sample body is revision 1.
    ticket_id = "VOC-" + uuid5(NAMESPACE_URL, ticket_key).hex
    received = utc_timestamp(capture.get("received_at"))
    if received is None:
        raise ProbeError("received_at에 시간대가 포함된 수신 시각이 필요합니다.")
    fields["title"] = clean(fields["title"]) or None
    fields["body_text"] = clean(fields["body_text"])
    fields["occurred_at"] = utc_timestamp(fields["occurred_at"])
    title, body = fields["title"], fields["body_text"]
    if not body:
        raise ProbeError("분류할 고객 본문이 없습니다. 원본은 raw.json에서 확인하세요.")
    common = {
        "schema_version": 2, "company_id": company, "connection_id": connection,
        "channel": channel, "ticket_id": ticket_id, "body_version": 1,
        "received_at": received,
        "raw_ref": bronze_event_id(company, channel, connection, fields["source_message_id"]), **fields,
    }
    if title and " ".join(title.split()) != " ".join(body.split()):
        body = title + "\n" + body
    classifier = {"schema_version": 1, "ticket_id": ticket_id, "body_version": 1, "analysis_text": body}
    return common, classifier
