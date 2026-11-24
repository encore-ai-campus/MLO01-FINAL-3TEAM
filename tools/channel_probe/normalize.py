"""Normalize one new test inquiry. Provider payloads stay unchanged on disk."""

import base64
import binascii
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.headerregistry import Address
from email.errors import HeaderParseError
from email.message import Message
from email.utils import getaddresses
from html.parser import HTMLParser

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


def header_values(part, name):
    for h in part.get("headers") or []:
        if isinstance(h, dict) and str(h.get("name", "")).casefold() == name.casefold() and isinstance(h.get("value"), str):
            yield re.sub(r"\r?\n(?=[ \t])", "", h["value"])


def header(part, name):
    return next(header_values(part, name), "")


def optional_text(value):
    return value if isinstance(value, str) and value.strip() else None


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
    # Block tags used by the production JDK HTML parser, including each table cell.
    BLOCKS = {"blockquote", "body", "dd", "dir", "div", "dl", "dt", "h1", "h2", "h3", "h4", "h5", "h6", "li",
              "menu", "noframes", "ol", "p", "pre", "table", "td", "th", "tr", "ul"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces = []
        self.suppressed = 0
        self.pre = 0

    def block_break(self):
        if self.pieces and not self.pre:
            self.pieces[-1] = self.pieces[-1].rstrip(" ")
        self.pieces.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.suppressed += 1
        elif not self.suppressed and (tag in self.BLOCKS or tag == "br"):
            self.block_break()
            self.pre += tag == "pre"

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.suppressed = max(0, self.suppressed - 1)
        elif not self.suppressed and tag in self.BLOCKS:
            self.block_break()
            if tag == "pre":
                self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if not self.suppressed:
            if not self.pre:
                data = re.sub(r"[ \t\r\n\f]+", " ", data)
                if not self.pieces or self.pieces[-1].endswith((" ", "\n")):
                    data = data.lstrip(" ")
            if data:
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
    return "html", re.sub(r"\n\s*\n", "\n", "".join(parser.pieces)).strip()


def decoded_header(value):
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError) as error:
        raise ProbeError("Gmail MIME 헤더를 해석할 수 없습니다.") from error


def addresses(part, key):
    result = []
    for value in header_values(part, key):
        for name, addr in getaddresses([value]):
            try:
                mailbox = Address(addr_spec=addr)
            except (HeaderParseError, ValueError, IndexError):
                continue
            if mailbox.username and mailbox.domain:
                result.append((optional_text(decoded_header(name)), addr))
    return result


def gmail_fields(raw, run, connection):
    part = raw.get("payload") or {}
    title = decoded_header(header(part, "Subject"))
    labels = [label for label in raw.get("labelIds") or [] if optional_text(label)]
    if run is not None:
        if marked_text(title, run) is None or "INBOX" not in labels:
            raise ProbeError("테스트 제목 표식과 INBOX 라벨이 일치하지 않습니다.")
    occurred = None
    millis = raw.get("internalDate")
    if millis is not None:
        try:
            if not re.fullmatch(r"[+-]?[0-9]+", str(millis)):
                raise ValueError
            occurred = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(millis))
        except (ValueError, OverflowError) as error:
            raise ProbeError("Gmail internalDate가 올바르지 않습니다.") from error
    senders = addresses(part, "From")
    name, sender = senders[0] if len(senders) == 1 else (None, None)
    automated = any((token := re.match(r"^\s*([\w-]+)(?:[;\s(]|$)", value, re.ASCII)) and token[1].lower() != "no"
                    for value in header_values(part, "Auto-Submitted"))
    if "DRAFT" in labels or part.get("mimeType", "").lower() == "multipart/report" or automated:
        kind = "other"
    elif "SENT" in labels or (sender and sender.casefold() == connection.casefold()):
        kind = "staff_reply"
    else:
        kind = "unknown"
    return {
        "source_message_id": source_id(raw.get("id"), "id"),
        "source_conversation_id": source_id(raw.get("threadId"), "threadId"),
        "source_channel_id": None, "source_channel_name": None, "reply_to_message_id": None,
        "sender_ref": sender, "sender_display_name": name, "message_kind": kind,
        "title": title, "body_text": mail_text(part)[1], "occurred_at": occurred,
        "source_metadata": {"label_ids": labels, "to_addresses": [address for _, address in addresses(part, "To")]},
    }


def discord_message_status(raw, run, channel_id):
    """Return the capture decision for a new message in the requested channel."""
    if raw.get("t") != "MESSAGE_CREATE" or raw.get("op") != 0:
        return None
    message = raw.get("d") or {}
    if message.get("channel_id") != channel_id:
        return None
    author = message.get("author") or {}
    if optional_text(message.get("webhook_id")) or author.get("bot") or author.get("system") or message.get("type") not in (None, 0, 19):
        return "automated"
    if not optional_text(message.get("content")):
        return "empty"
    if marked_text(message["content"], run) is None:
        return "unmarked"
    return "match"


def is_discord_test(raw, run, channel_id):
    return discord_message_status(raw, run, channel_id) == "match"


def discord_fields(raw, run, capture):
    channel_id = source_id(capture.get("channel_id"), "channel_id")
    message = raw.get("d") or {}
    if raw.get("op") != 0 or raw.get("t") != "MESSAGE_CREATE" or message.get("channel_id") != channel_id:
        raise ProbeError("지정 채널에서 수신한 MESSAGE_CREATE가 필요합니다.")
    body = (optional_text(message.get("content")) or "").strip()
    if run is not None:
        if not is_discord_test(raw, run, channel_id):
            raise ProbeError("지정 채널에서 사람이 보낸 테스트 MESSAGE_CREATE가 필요합니다.")
    message_id = source_id(message.get("id"), "id")
    author = message.get("author") or {}
    reference = message.get("message_reference") or {}
    message_type = message.get("type")
    if message_type is not None and (type(message_type) is not int or not -2**31 <= message_type < 2**31):
        raise ProbeError("Discord 메시지 유형이 올바르지 않습니다.")
    ref_type = reference.get("type")
    default_ref = ref_type is None or (type(ref_type) is int and ref_type == 0)
    reply = message_type == 19 or (message_type is None and reference.get("message_id") is not None and default_ref)
    parent = optional_text(reference.get("message_id")) if reply and default_ref else None
    thread = capture.get("is_thread", False)
    if parent == message_id or (reply and not thread and parent is None):
        raise ProbeError("Discord 답글의 직접 부모 메시지 ID가 올바르지 않습니다.")
    # The standalone probe has no verified parent lookup. A normal-channel reply stays unresolved.
    conversation = channel_id if thread else (None if reply else message_id)
    automated = author.get("bot") or author.get("system") or optional_text(message.get("webhook_id")) or message_type not in (None, 0, 19)
    name = optional_text((message.get("member") or {}).get("nick")) or optional_text(author.get("global_name")) or optional_text(author.get("username"))
    return {
        "source_message_id": message_id, "source_conversation_id": conversation,
        "source_channel_id": channel_id, "source_channel_name": optional_text(capture.get("channel_name")),
        "reply_to_message_id": parent,
        "sender_ref": optional_text(author.get("id")),
        "sender_display_name": name, "message_kind": "other" if automated else "unknown", "title": None,
        "body_text": body, "occurred_at": message.get("timestamp"),
        "source_metadata": {},
    }


def transform(channel, raw, capture):
    run = capture.get("run")
    company = capture.get("company_id", "local-test-company" if run is not None else None)
    company = source_id(company, "company_id")
    connection = source_id(capture.get("connection_id"), "connection_id")
    if channel == "discord":
        fields = discord_fields(raw, run, capture)
        if raw["d"].get("guild_id") is not None and raw["d"]["guild_id"] != connection:
            raise ProbeError("Discord 서버 ID와 수신 연결 ID가 다릅니다.")
    elif channel == "gmail":
        fields = gmail_fields(raw, run, connection)
    else:
        raise ProbeError("지원하는 수신 채널은 discord와 gmail입니다.")
    received = utc_timestamp(capture.get("received_at"))
    if received is None:
        raise ProbeError("received_at에 시간대가 포함된 수신 시각이 필요합니다.")
    fields["title"] = (fields["title"] or "").strip() or None
    if fields["occurred_at"] is not None:
        fields["occurred_at"] = utc_timestamp(fields["occurred_at"])
        if fields["occurred_at"] is None:
            raise ProbeError("원천 메시지의 occurred_at을 해석할 수 없습니다.")
    if not fields["body_text"]:
        raise ProbeError("분류할 고객 본문이 없습니다. 원본은 raw.json에서 확인하세요.")
    common = {
        "schema_version": 4, "event_id": bronze_event_id(company, channel, connection, fields["source_message_id"]),
        "company_id": company, "channel": channel, "connection_id": connection,
        "body_version": 1, "received_at": received, **fields,
    }
    classifier = {"event_id": common["event_id"], "body_version": 1, "preprocess_version": 1,
                  "model_text": fields["body_text"]}
    return common, classifier
