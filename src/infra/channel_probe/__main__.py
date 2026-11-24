"""Run from the repository root with PYTHONPATH=src python -m infra.channel_probe."""

import argparse
import asyncio
import json
import re
import sys

from .artifacts import BASE, regenerate, save_inbound, verify
from .normalize import ProbeError
from .providers import post_webhook, receive_discord, receive_gmail, secret


def run_tag(value):
    if not re.fullmatch(r"VOC-[a-zA-Z0-9][a-zA-Z0-9_-]{0,56}", value):
        raise argparse.ArgumentTypeError("VOC-로 시작하는 영문·숫자·하이픈·밑줄 테스트 표식을 입력하세요.")
    return value


def snowflake(value):
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise argparse.ArgumentTypeError("숫자로 된 Discord 채널 ID를 입력하세요.")
    return value


def seconds(value):
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise argparse.ArgumentTypeError("대기 시간은 양의 정수(초)입니다.")
    return int(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Discord·Gmail 실제 원본 수신과 공통 분류 입력 검증")
    sub = parser.add_subparsers(dest="command", required=True)
    modes = [
        ("discord-webhook", "웹훅으로 테스트 메시지 1건을 게시하고 Discord 응답 저장"),
        ("discord-receive", "지정 채널에서 사람이 작성한 Gateway 이벤트 1건 수신"),
        ("gmail", "OAuth로 승인한 사서함에서 표식이 일치하는 원본 1건 수신"),
        ("regenerate", "저장된 두 채널 원본으로 표준 메시지와 분류 입력 예시 다시 생성"),
        ("verify", "두 수신 원본으로 표준화 결과와 공통 입력 재검증"),
    ]
    for name, help_text in modes:
        cmd = sub.add_parser(name, help=help_text, description=help_text)
        cmd.add_argument("--run", required=True, type=run_tag, help="두 채널에 같이 쓰는 VOC-... 표식")
        if name in {"discord-receive", "gmail"}:
            cmd.add_argument("--timeout", type=seconds, default=120, help="수신 대기 시간(초), 기본 120")
        if name == "discord-receive":
            cmd.add_argument("--channel-id", required=True, type=snowflake, help="테스트 채널 또는 스레드 ID")
        if name == "verify":
            cmd.add_argument("--expect", help="Discord의 RUN 표식을 제외한 양쪽 본문이 이 문장과 같은지도 검사")
    args = parser.parse_args(argv)
    try:
        if args.command == "regenerate":
            regenerate(BASE, args.run)
            print("Discord·Gmail의 standard.json과 classifier-input.json을 Silver v4에 맞게 다시 생성했습니다.")
            return 0
        if args.command == "verify":
            requests = verify(BASE, args.run, args.expect)
            print("PASS · Discord Gateway와 Gmail 원본 재변환 및 공통 키·자료형 일치")
            for channel, value in requests.items():
                print(f"{channel} {value['event_id']} / 본문 {len(value['model_text'])}자")
            if args.expect is not None:
                print("PASS · 두 채널의 수신 본문이 기대 문장과 일치")
            return 0
        if args.command == "discord-webhook":
            folder = post_webhook(BASE, args.run)
            print(f"웹훅 게시 확인 · 요청 request.json / Discord 응답 raw.json\n{folder}")
            return 0
        if args.command == "discord-receive":
            raw, is_thread, channel_name = asyncio.run(receive_discord(args.run, args.channel_id, secret("DISCORD_BOT_TOKEN"), args.timeout))
            folder = save_inbound(BASE, args.run, "discord", raw, raw["d"]["guild_id"],
                                  channel_id=args.channel_id, is_thread=is_thread, channel_name=channel_name)
        else:
            raw, mailbox = receive_gmail(BASE, args.run, args.timeout)
            folder = save_inbound(BASE, args.run, "gmail", raw, mailbox)
        print(f"수신 확인 · 원본 raw.json / 공통 레코드 standard.json / 분류 입력 classifier-input.json\n{folder}")
        return 0
    except ModuleNotFoundError as error:
        print(f"필요 패키지 {error.name}. 현재 가상환경에서 requirements.txt를 설치하세요.", file=sys.stderr)
    except (ProbeError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"확인 필요 · {error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("수신을 중단했습니다.", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
