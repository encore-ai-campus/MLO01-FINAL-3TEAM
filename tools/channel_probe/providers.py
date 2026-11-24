"""Opt-in provider calls used by the local, tagged probe."""

import asyncio
from getpass import getpass
from http.client import HTTPSConnection, HTTPException
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .artifacts import read_json, write_json
from .normalize import ProbeError, SAMPLE_TEXT, discord_message_status, header, marked_text


def secret(name):
    value = os.environ.get(name)
    if not value and sys.stdin.isatty():
        value = getpass(f"{name} (화면에 표시되지 않음): ")
    if not value or not value.strip():
        raise ProbeError(f"터미널에서 {name}을 입력하거나 환경변수에 설정하세요.")
    return value.strip()


def webhook_target(url):
    parsed = urlsplit(url)
    allowed = {"discord.com", "ptb.discord.com", "canary.discord.com", "discordapp.com"}
    if (parsed.scheme != "https" or parsed.netloc not in allowed
            or not re.fullmatch(r"/api/(?:v\d+/)?webhooks/\d+/[A-Za-z0-9_.-]+/?", parsed.path)):
        raise ProbeError("Discord 채널에서 복사한 HTTPS 웹훅 URL을 확인하세요.")
    query = {"wait": "true"}
    thread = parse_qs(parsed.query).get("thread_id", [])
    if thread:
        if len(thread) != 1 or not thread[0].isdigit():
            raise ProbeError("웹훅 URL의 thread_id를 확인하세요.")
        query["thread_id"] = thread[0]
    return parsed.netloc, parsed.path + "?" + urlencode(query)


def post_webhook(base, run):
    directory = base / "runs" / run / "discord-webhook"
    if (directory / "raw.json").is_file():
        return directory
    host, target = webhook_target(secret("DISCORD_WEBHOOK_URL"))
    request = {"content": f"[{run}] {SAMPLE_TEXT}", "allowed_mentions": {"parse": []}}
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise ProbeError("이 RUN의 게시 시도가 이미 있습니다. 채널에서 게시 여부를 확인한 뒤 새 RUN으로 시도하세요.") from error
    write_json(directory / "request.json", request)
    conn = HTTPSConnection(host, timeout=15)
    try:
        conn.request("POST", target, body=json.dumps(request).encode(),
                     headers={"Content-Type": "application/json", "User-Agent": "Team3-VoC-Format-Probe/1"})
        response = conn.getresponse()
        if response.status != 200:
            raise ProbeError(f"웹훅 HTTP {response.status}. URL·채널·호출 제한을 확인하세요.")
        message = json.loads(response.read())
        if not message.get("id") or not message.get("channel_id") or message.get("content") != request["content"]:
            raise ProbeError("게시 응답에서 메시지 ID, 채널 ID 또는 보낸 본문을 확인할 수 없습니다.")
        write_json(directory / "raw.json", message)
    except (OSError, HTTPException, ValueError) as error:
        raise ProbeError("게시 응답을 확인할 수 없습니다. 중복 전송 전에 채널 화면을 확인하세요.") from error
    finally:
        conn.close()
    return directory


async def receive_discord(run, channel_id, token, timeout):
    import discord

    intents = discord.Intents.none()
    intents.guilds = intents.guild_messages = intents.message_content = True
    client = discord.Client(intents=intents, enable_debug_events=True, max_messages=None)
    result = asyncio.get_running_loop().create_future()
    target = None
    reported = set()
    skip_reasons = {
        "automated": "봇이나 웹훅이 작성한 글입니다. 사람 계정으로 새 메시지를 보내세요.",
        "empty": "수신한 글의 텍스트가 비어 있습니다. 아래 테스트 문장을 일반 텍스트로 보내세요.",
        "unmarked": f"사람이 쓴 글을 받았지만 [{run}] 표식이 일치하지 않습니다.",
    }

    @client.event
    async def on_ready():
        nonlocal target
        try:
            channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                raise ProbeError("테스트 서버의 텍스트 채널 또는 문의 스레드 ID를 입력하세요.")
            if not channel.permissions_for(channel.guild.me).view_channel:
                raise ProbeError("봇에 해당 채널의 View Channel 권한이 필요합니다.")
            target = channel
            print(f"READY · #{channel.name} ({channel_id}) {channel.jump_url}\n"
                  f"Discord에서 위 채널을 열고 사람 계정으로 아래 한 줄을 새 메시지로 보내세요.\n"
                  f"[{run}] {SAMPLE_TEXT}", flush=True)
        except ProbeError as error:
            if not result.done():
                result.set_exception(error)
        except discord.DiscordException:
            if not result.done():
                result.set_exception(ProbeError("지정 채널을 확인할 수 없습니다. 채널 ID와 봇의 접근 권한을 확인하세요."))

    @client.event
    async def on_socket_raw_receive(frame):
        if target is None or result.done():
            return
        event = frame if isinstance(frame, dict) else json.loads(frame)
        status = discord_message_status(event, run, channel_id)
        if status == "match":
            result.set_result((event, isinstance(target, discord.Thread)))
        elif status is not None and status not in reported:
            reported.add(status)
            print(f"건너뜀 · {skip_reasons[status]}\n[{run}] {SAMPLE_TEXT}", flush=True)

    async with client:
        connection = asyncio.create_task(client.start(token))
        try:
            done, _ = await asyncio.wait({connection, result}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if result in done:
                return result.result()
            if connection in done:
                await connection
                raise ProbeError("Discord가 메시지 수신 전에 연결을 닫았습니다.")
            if target is None:
                raise ProbeError("Discord 연결 준비 중 시간 초과. 네트워크와 봇의 채널 접근 권한을 확인하세요.")
            if reported:
                raise ProbeError("지정 채널의 새 글을 받았으나 테스트 조건을 충족한 문의는 0건입니다. 위의 건너뜀 사유를 확인하세요.")
            raise ProbeError("READY 이후 지정 채널에서 받은 새 메시지 이벤트가 0건입니다. "
                             "다시 실행하고 READY의 채널 링크를 열어 새 글을 보내세요. 스레드는 별도 채널 ID를 씁니다.")
        except discord.LoginFailure as error:
            raise ProbeError("Bot Token 인증에 실패했습니다. 웹훅 URL과 Bot Token은 별도 값입니다.") from error
        except discord.PrivilegedIntentsRequired as error:
            raise ProbeError("Developer Portal의 Bot에서 Message Content Intent를 켜세요.") from error
        except discord.DiscordException as error:
            raise ProbeError(f"Discord 연결 실패 ({type(error).__name__}).") from error
        finally:
            await client.close()
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
            if not result.done():
                result.cancel()


def gmail_credentials(base):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError

    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    token_path = base / "token.json"
    if token_path.is_file():
        saved = read_json(token_path)
        if set(saved.get("scopes") or []) != set(scopes):
            raise ProbeError("이 테스트의 토큰은 gmail.readonly 전용이어야 합니다. 기존 token.json을 따로 옮기고 다시 승인하세요.")
        creds = Credentials.from_authorized_user_info(saved, scopes)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
        if creds.valid:
            write_json(token_path, json.loads(creds.to_json()))
            return creds
    path = base / "credentials.json"
    if not path.is_file() or "installed" not in read_json(path):
        raise ProbeError(f"Desktop app OAuth 클라이언트 JSON을 {path}에 저장하세요.")
    flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes, autogenerate_code_verifier=True)
    try:
        creds = flow.run_local_server(
            host="127.0.0.1", port=0, timeout_seconds=180,
            authorization_prompt_message="테스트 Gmail을 승인하세요. 브라우저가 열리지 않으면 다음 주소를 여세요.\n{url}",
        )
    except WSGITimeoutError as error:
        raise ProbeError("180초 안에 Gmail 승인이 도착하지 않았습니다. 같은 RUN으로 다시 실행하세요.") from error
    write_json(token_path, json.loads(creds.to_json()))
    return creds


def gmail_get(session, resource, **params):
    response = session.get("https://gmail.googleapis.com/gmail/v1/users/me/" + resource,
                           params=params, timeout=15)
    if response.status_code != 200:
        raise ProbeError(f"Gmail API HTTP {response.status_code}. Gmail API 활성화·읽기 권한·호출 제한을 확인하세요.")
    return response.json()


def find_gmail(session, run):
    params = {"q": f'subject:"{run}"', "labelIds": "INBOX", "maxResults": 50}
    while True:
        page = gmail_get(session, "messages", **params)
        for entry in page.get("messages", []):
            resource = "messages/" + entry["id"]
            meta = gmail_get(session, resource, format="metadata", metadataHeaders="Subject")
            if marked_text(header(meta.get("payload", {}), "Subject"), run) is None:
                continue
            message = gmail_get(session, resource, format="full")
            if "INBOX" in message.get("labelIds", []):
                return message
        if not page.get("nextPageToken"):
            return None
        params["pageToken"] = page["nextPageToken"]


def receive_gmail(base, run, timeout):
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import AuthorizedSession
    from requests import RequestException
    from oauthlib.oauth2 import OAuth2Error

    try:
        creds = gmail_credentials(base)
        with AuthorizedSession(creds) as session:
            mailbox = gmail_get(session, "profile")["emailAddress"]
            print(f"수신 대기 · 승인한 사서함으로 제목 [{run}]인 메일을 보내세요.", flush=True)
            deadline = time.monotonic() + timeout
            while True:
                message = find_gmail(session, run)
                if message is not None:
                    return message, mailbox
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError("Gmail 수신 시간 초과. 승인한 계정의 받은편지함과 제목의 [RUN]을 확인하세요.")
                time.sleep(min(4, remaining))
    except (GoogleAuthError, OAuth2Error, RequestException) as error:
        raise ProbeError(f"Gmail 인증·연결 오류 ({type(error).__name__}). 테스트 계정과 OAuth 승인 상태를 확인하세요.") from error
