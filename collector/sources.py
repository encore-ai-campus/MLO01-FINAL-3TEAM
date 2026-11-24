import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from tools.channel_probe.providers import gmail_get
from .events import COMPANY, DISCORD_CHANNEL, GMAIL_LABEL, ROOT, already_received, atomic_json, make_event, stage, status


def human_message(msg):
    author = msg.get("author") or {}
    return bool(author.get("id") and not author.get("bot") and not msg.get("webhook_id")
                and msg.get("type", 0) in (0, 19))


async def run_discord():
    channel_id = int(DISCORD_CHANNEL)
    token = Path("/run/secrets/discord_token").read_text().strip()
    intents = discord.Intents.none()
    intents.guilds = intents.guild_messages = intents.message_content = True
    client = discord.Client(intents=intents, enable_debug_events=True, max_messages=None)
    fail = asyncio.get_running_loop().create_future()
    cursor_file = ROOT / "cursors" / f"discord-{COMPANY}-{channel_id}.json"
    if not cursor_file.exists():
        atomic_json(cursor_file, str(discord.utils.time_snowflake(datetime.now(timezone.utc))))
    channel = None
    history_task = None

    async def record(msg, raw, origin, guild, thread):
        if msg.get("channel_id") != str(channel_id) or not human_message(msg):
            return
        event = make_event("discord", str(guild), msg["id"], origin, raw,
                           channel_id=str(channel_id), is_thread=thread)
        if await asyncio.to_thread(stage, ROOT, event):
            print(f"수집 discord event={event['event_id']} {origin}", flush=True)

    async def history():
        # Only REST-confirmed pages advance this cursor. A later live event cannot skip a gap.
        cursor = int(json.loads(cursor_file.read_text()))
        while True:
            await asyncio.sleep(15)
            if not client.is_ready() or channel is None:
                continue
            try:
                can_read = channel.permissions_for(channel.guild.me).read_message_history
                if can_read:
                    while True:
                        page = await client.http.logs_from(channel_id, 100, after=cursor)
                        if not page:
                            break
                        for msg in sorted(page, key=lambda m: int(m["id"])):
                            await record(msg, msg, "discord_history", channel.guild.id, isinstance(channel, discord.Thread))
                        cursor = max(int(m["id"]) for m in page)
                        await asyncio.to_thread(atomic_json, cursor_file, str(cursor), replace=True)
                        if len(page) < 100:
                            break
                status("discord", "ready", channel_id=str(channel_id), history=bool(can_read))
            except discord.HTTPException as exc:
                status("discord", "live_history_retry", channel_id=str(channel_id), http=exc.status)

    def history_done(task):
        if not task.cancelled() and task.exception() and not fail.done():
            fail.set_exception(task.exception())

    @client.event
    async def on_ready():
        nonlocal channel, history_task
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)) or not channel.permissions_for(channel.guild.me).view_channel:
                raise RuntimeError("설정한 텍스트 채널에 채널 보기 권한이 필요합니다")
            can_read = channel.permissions_for(channel.guild.me).read_message_history
            status("discord", "ready", channel_id=str(channel_id), history=bool(can_read))
            print(f"READY discord history={can_read}", flush=True)
            if history_task is None:
                history_task = asyncio.create_task(history())
                history_task.add_done_callback(history_done)
        except Exception as exc:
            if not fail.done():
                fail.set_exception(exc)

    @client.event
    async def on_disconnect():
        status("discord", "reconnecting", channel_id=str(channel_id))

    @client.event
    async def on_socket_raw_receive(frame):
        try:
            raw = frame if isinstance(frame, dict) else json.loads(frame)
            if raw.get("op") == 0 and raw.get("t") == "MESSAGE_CREATE":
                msg = raw["d"]
                if msg.get("guild_id") and msg.get("channel_id") == str(channel_id):
                    thread = msg.get("channel_type") in (10, 11, 12) or isinstance(channel, discord.Thread)
                    await record(msg, raw, "discord_gateway", msg["guild_id"], thread)
        except Exception as exc:
            if not fail.done():
                fail.set_exception(exc)

    async with client:
        connection = asyncio.create_task(client.start(token))
        try:
            done, _ = await asyncio.wait({connection, fail}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            if history_task:
                history_task.cancel()
                await asyncio.gather(history_task, return_exceptions=True)
            await client.close()
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)


def poll_gmail(session, mailbox, label_id):
    page_token = None
    while True:
        params = {"labelIds": label_id, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        page = gmail_get(session, "messages", **params)
        for entry in page.get("messages", []):
            event = make_event("gmail", mailbox, entry["id"], "gmail_api", {}, label=GMAIL_LABEL)
            if already_received(ROOT, event):
                continue
            # Labels may change between listing and fetching. Never fetch full content
            # for a message that is already known to have lost the selected label.
            item = "messages/" + entry["id"]
            meta = session.get("https://gmail.googleapis.com/gmail/v1/users/me/" + item,
                               params={"format":"minimal"}, timeout=15)
            if meta.status_code == 404:
                continue
            meta.raise_for_status()
            if label_id not in meta.json().get("labelIds", []):
                continue
            resp = session.get("https://gmail.googleapis.com/gmail/v1/users/me/" + item,
                               params={"format":"full"}, timeout=15)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            event["raw"] = resp.json()
            if label_id in event["raw"].get("labelIds", []) and stage(ROOT, event):
                print(f"수집 gmail event={event['event_id']}", flush=True)
        page_token = page.get("nextPageToken")
        if not page_token:
            break


def run_gmail():
    saved = json.loads(Path("/run/secrets/gmail_token").read_text())
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    if set(saved.get("scopes", [])) != set(scopes):
        raise ValueError("기존 gmail.readonly 테스트 계정의 token.json을 사용하세요")
    creds = Credentials.from_authorized_user_info(saved, scopes)
    with AuthorizedSession(creds) as session:
        mailbox = gmail_get(session, "profile")["emailAddress"].casefold()
        last_state = None
        while True:
            labels = gmail_get(session, "labels").get("labels", [])
            label = next((x for x in labels if x["name"] == GMAIL_LABEL and x.get("type") == "user"), None)
            state = "waiting_label" if label is None else "ready"
            if label:
                poll_gmail(session, mailbox, label["id"])
            status("gmail", state, label=GMAIL_LABEL)
            if last_state != state:
                print(f"Gmail {state} label={GMAIL_LABEL}", flush=True)
                last_state = state
            time.sleep(int(os.environ.get("GMAIL_POLL_SECONDS", "15")))
