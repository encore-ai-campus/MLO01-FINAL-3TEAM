import base64
import binascii
import json
from uuid import UUID

from config.http import DeskError

IMAGE_SIGNATURES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",), "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"), "image/webp": (b"RIFF",),
}
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def invalid(message="입력값을 확인해 주세요."):
    return DeskError(422, "INVALID_REQUEST", message)


def fields(request):
    if request.content_type != "application/json":
        if request.path.startswith("/api/"):
            raise invalid("JSON 본문을 보내 주세요.")
        return request.POST
    raw = request.read(3_000_001)
    if len(raw) > 3_000_000:
        raise DeskError(413, "REQUEST_TOO_LARGE", "요청 크기 제한을 초과했습니다.")
    try:
        body = json.loads(raw)
    except ValueError:
        raise invalid("JSON 본문을 확인해 주세요.") from None
    if not isinstance(body, dict):
        raise invalid()
    return body


def text(value, name, limit=2000):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value
            or any("\ud800" <= char <= "\udfff" for char in value)):
        raise invalid(f"{name}에 1~{limit:,}자를 입력해 주세요.")
    return value


def identifier(value):
    if not isinstance(value, str):
        raise invalid()
    try:
        return UUID(value)
    except ValueError:
        raise invalid() from None


def message_payload(data):
    kind = data.get("message_type")
    if kind not in ("reply", "memo", "internal_memo"):
        raise invalid("고객 답변 또는 내부 메모를 선택해 주세요.")
    return ("reply" if kind == "reply" else "memo", text(data.get("body"), "본문"),
            identifier(data.get("request_key")))


def review_attachment(request, data):
    value = data.get("attachment")
    upload = request.FILES.get("reference_image")
    note = data.get("reference_text")
    if sum(option is not None and option != "" for option in (value, upload, note)) > 1:
        raise invalid("참고 자료는 텍스트와 이미지 중 한 가지를 선택해 주세요.")
    if upload:
        if upload.size > MAX_IMAGE_BYTES:
            raise invalid("이미지는 2 MiB까지 첨부할 수 있습니다.")
        value = {"content_type": upload.content_type, "content": base64.b64encode(upload.read()).decode("ascii")}
    if note:
        value = {"content_type": "text/plain", "content": note}
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid("참고 자료 형식이 올바르지 않습니다.")
    content_type, content = value.get("content_type"), value.get("content")
    if content_type == "text/plain":
        return {"content_type": content_type, "content": text(content, "참고 자료", 10_000)}
    if not isinstance(content_type, str) or content_type not in IMAGE_SIGNATURES or not isinstance(content, str):
        raise invalid("PNG·JPEG·GIF·WebP 이미지를 첨부해 주세요.")
    try:
        image = base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error):
        raise invalid("이미지 형식이 올바르지 않습니다.") from None
    if (len(image) > MAX_IMAGE_BYTES or not image.startswith(IMAGE_SIGNATURES[content_type])
            or (content_type == "image/webp" and image[8:12] != b"WEBP")):
        raise invalid("이미지 형식과 2 MiB 크기 제한을 확인해 주세요.")
    return {"content_type": content_type, "content": content}


def uploaded_references(request):
    signatures = {**IMAGE_SIGNATURES, "application/pdf": (b"%PDF-",)}
    files = request.FILES.getlist("reference_files")
    if sum(file.size for file in files) > 10 * 1024 * 1024:
        raise invalid("첨부파일의 전체 크기는 10 MiB까지 허용됩니다.")
    attachments = []
    for file in files:
        content = file.read()
        content_type = file.content_type
        if (content_type not in signatures or not content.startswith(signatures[content_type])
                or (content_type == "image/webp" and content[8:12] != b"WEBP")):
            raise invalid("PNG·JPEG·GIF·WebP 이미지 또는 PDF를 첨부해 주세요.")
        attachments.append({"content_type": content_type, "filename": file.name,
                            "content": base64.b64encode(content).decode("ascii")})
    return attachments
