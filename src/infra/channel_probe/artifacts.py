"""Save raw captures locally and verify the exact requests a classifier would receive."""

import errno
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from shutil import rmtree

from .normalize import ProbeError, marked_text, transform


BASE = Path(__file__).resolve().parents[3] / ".local" / "channel-probe"
ORIGINS = {"discord": "discord_gateway", "gmail": "gmail_api"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_inbound(base, run, channel, raw, connection_id, **details):
    directory = base / "runs" / run
    directory.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="capture-", dir=directory))
    capture = {"run": run, "origin": ORIGINS[channel], "received_at": utc_now(),
               "connection_id": connection_id, **details}
    destination = directory / channel
    try:
        write_json(temp / "raw.json", raw)
        write_json(temp / "capture.json", capture)
        try:
            common, request = transform(channel, raw, capture)
        except ProbeError as error:
            review = directory / ("review-" + channel + "-" + temp.name)
            temp.rename(review)
            raise ProbeError(f"{error} 검토할 원본: {review / 'raw.json'}") from error
        write_json(temp / "standard.json", common)
        write_json(temp / "classifier-input.json", request)
        try:
            temp.rename(destination)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY, errno.EACCES) or not destination.is_dir():
                raise
            existing = read_json(destination / "standard.json")
            key = ("connection_id", "source_message_id")
            if any(existing[k] != common[k] for k in key):
                raise ProbeError("이 RUN에는 다른 메시지가 저장돼 있습니다. 새 쌍은 새 RUN으로 수신하세요.") from error
        return destination
    finally:
        if temp.exists():
            rmtree(temp)


def replay_capture(base, run, channel):
    directory = base / "runs" / run / channel
    if not directory.is_dir():
        raise ProbeError(f"{channel} 수신 기록이 없습니다. 웹훅 게시 결과만으로는 수신 검증이 되지 않습니다.")
    capture = read_json(directory / "capture.json")
    if capture.get("origin") != ORIGINS[channel] or capture.get("run") != run:
        raise ProbeError(f"{channel} 원본의 수신 방식 또는 RUN이 다릅니다.")
    common, request = transform(channel, read_json(directory / "raw.json"), capture)
    return directory, common, request


def regenerate(base, run):
    # Replay both original captures before upgrading their derived artifacts.
    pair = [replay_capture(base, run, channel) for channel in ORIGINS]
    for directory, common, request in pair:
        write_json(directory / "standard.json", common)
        write_json(directory / "classifier-input.json", request)


def verify(base, run, expected=None):
    requests = {}
    for channel in ORIGINS:
        directory, rebuilt_common, rebuilt_request = replay_capture(base, run, channel)
        common = read_json(directory / "standard.json")
        request = read_json(directory / "classifier-input.json")
        if (common != rebuilt_common or request != rebuilt_request
                or type(common["schema_version"]) is not int or type(common["body_version"]) is not int):
            raise ProbeError(f"{channel} 저장본과 원본 재변환 결과가 다릅니다.")
        types = {"event_id": str, "body_version": int, "preprocess_version": int, "model_text": str}
        if set(request) != set(types) or any(type(request[k]) is not t for k, t in types.items()):
            raise ProbeError(f"{channel} 분류 입력의 키 또는 자료형이 다릅니다.")
        text = request["model_text"]
        comparable = marked_text(text, run) if channel == "discord" else text
        if expected is not None and comparable != expected:
            raise ProbeError(f"{channel} 수신 본문이 기대 문장과 다릅니다. {directory / 'classifier-input.json'}")
        requests[channel] = request
    if requests["discord"]["event_id"] == requests["gmail"]["event_id"]:
        raise ProbeError("서로 다른 채널의 이벤트 ID가 충돌했습니다.")
    return requests
