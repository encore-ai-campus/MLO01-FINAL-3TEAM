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
from .events import COMPANY, DISCORD_CHANNEL, GMAIL_LABEL, ROOT, make_event, status
from .spool import CaptureStore, Checkpoint, Scope


def human_message(msg):
    author = msg.get("author") or {}
    return bool(author.get("id") and not author.get("bot") and not msg.get("webhook_id")
                and msg.get("type", 0) in (0, 19))


def discord_event(msg, raw, origin, scope, thread, channel_name=None):
    if msg.get("channel_id") != scope.source or not human_message(msg):
        return None
    return make_event("discord", scope.connection, msg["id"], origin, raw,
                      channel_id=scope.source, is_thread=thread, channel_name=channel_name)


def init_discord_checkpoint(store, root, scope, starting_position):
    current = store.checkpoint(scope)
    if current is not None:
        return current
    legacy = root / "cursors" / f"discord-{scope.company}-{scope.source}.json"
    position = str(int(json.loads(legacy.read_bytes()))) if legacy.exists() else starting_position
    return store.initialize(scope, Checkpoint(position))


def save_discord_history(store, scope, checkpoint, page, thread, channel_name=None):
    messages = sorted(page, key=lambda msg: int(msg["id"]))
    events = (discord_event(msg, msg, "discord_history", scope, thread, channel_name) for msg in messages)
    position = messages[-1]["id"]
    if int(position) <= int(checkpoint.position):
        raise ValueError("Discord history did not advance")
    return store.save_page(scope, checkpoint, (e for e in events if e is not None),
                           advance=Checkpoint(position))


async def run_discord():
    channel_id = int(DISCORD_CHANNEL)
    token = Path("/run/secrets/discord_token").read_text().strip()
    intents = discord.Intents.none()
    intents.guilds = intents.guild_messages = intents.message_content = True
    client = discord.Client(intents=intents, enable_debug_events=True, max_messages=None)
    fail = asyncio.get_running_loop().create_future()
    store = CaptureStore(ROOT)
    starting_position = str(discord.utils.time_snowflake(datetime.now(timezone.utc)))
    channel = None
    history_task = None

    async def history():
        scope = Scope(COMPANY, "discord", str(channel.guild.id), str(channel_id))
        init_discord_checkpoint(store, ROOT, scope, starting_position)
        while True:
            if client.is_ready():
                try:
                    can_read = channel.permissions_for(channel.guild.me).read_message_history
                    checkpoint = store.checkpoint(scope)
                    if not can_read:
                        store.save_page(scope, checkpoint, issues=[("history_permission_missing", checkpoint.position)])
                    while can_read:
                        page = await client.http.logs_from(channel_id, 100, after=int(checkpoint.position))
                        if not page:
                            break
                        saved = await asyncio.to_thread(save_discord_history, store, scope, checkpoint, page,
                                                        isinstance(channel, discord.Thread), channel.name)
                        if not saved or len(page) < 100:
                            break
                        checkpoint = store.checkpoint(scope)
                    status("discord", "ready", channel_id=str(channel_id), history=bool(can_read))
                except discord.HTTPException as exc:
                    status("discord", "live_history_retry", channel_id=str(channel_id), http=exc.status)
            await asyncio.sleep(15)

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
                    scope = Scope(COMPANY, "discord", str(msg["guild_id"]), str(channel_id))
                    event = discord_event(msg, raw, "discord_gateway", scope, thread, getattr(channel, "name", None))
                    if event and await asyncio.to_thread(store.stage, event):
                        print(f"수집 discord event={event['event_id']} discord_gateway", flush=True)
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


def gmail_candidate(session, store, mailbox, label_id, ident):
    event = make_event("gmail", mailbox, ident, "gmail_api", {}, label=GMAIL_LABEL)
    if store.contains(event["event_id"]):
        return None, None
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/" + ident
    for fmt in ("minimal", "full"):
        response = session.get(url, params={"format": fmt}, timeout=15)
        if response.status_code == 404:
            return None, ("message_unavailable", ident)
        response.raise_for_status()
        raw = response.json()
        if label_id not in raw.get("labelIds", []):
            return None, ("label_removed", ident)
    if raw["id"] != ident:
        raise ValueError("Gmail source ID mismatch")
    event["raw"] = raw
    return event, None


def save_gmail_page(session, store, scope, state, ids, advance):
    # Large MIME messages are persisted individually. Commit the last item and
    # the final cursor together; a crash earlier will re-read this range.
    ids = list(dict.fromkeys(ids))
    for index, ident in enumerate(ids):
        event, issue = gmail_candidate(session, store, scope.connection, scope.source, ident)
        if not store.save_page(scope, state, [event] if event else (),
                               issues=[issue] if issue else (),
                               advance=advance if index == len(ids) - 1 else None):
            return False
    return True if ids else store.save_page(scope, state, advance=advance)


def gmail_baseline(session):
    return Checkpoint(str(int(gmail_get(session, "profile")["historyId"])), "bootstrap")


def poll_gmail(session, mailbox, label_id, store):
    scope = Scope(COMPANY, "gmail", mailbox, label_id)
    state = store.checkpoint(scope)
    if state is None:
        state = store.initialize(scope, gmail_baseline(session))
    if state.phase == "bootstrap":
        params = {"labelIds": label_id, "maxResults": 100}
        while True:
            page = gmail_get(session, "messages", **params)
            token = page.get("nextPageToken")
            ids = [entry["id"] for entry in page.get("messages", [])]
            active = Checkpoint(state.position)
            if not save_gmail_page(session, store, scope, state, ids, None if token else active):
                return "sync_retry"
            if not token:
                state = active
                break
            params["pageToken"] = token

    params = {"startHistoryId": state.position, "labelId": label_id, "maxResults": 100,
              "historyTypes": ["messageAdded", "labelAdded"]}
    while True:
        response = session.get("https://gmail.googleapis.com/gmail/v1/users/me/history",
                               params=params, timeout=15)
        if response.status_code == 404:
            store.save_page(scope, state, advance=gmail_baseline(session),
                            issues=[("history_expired", state.position)])
            return "resync_pending"
        response.raise_for_status()
        page = response.json()
        ids = []
        for change in page.get("history", []):
            ids.extend(item["message"]["id"] for item in change.get("messagesAdded", []))
            ids.extend(item["message"]["id"] for item in change.get("labelsAdded", [])
                       if label_id in item["labelIds"])
        token = page.get("nextPageToken")
        advance = None
        if not token:
            position = int(page["historyId"])
            if position < int(state.position):
                raise ValueError("Gmail history position regressed")
            advance = Checkpoint(str(position))
        if not save_gmail_page(session, store, scope, state, ids, advance):
            return "sync_retry"
        if not token:
            return "ready"
        params["pageToken"] = token


def run_gmail():
    saved = json.loads(Path("/run/secrets/gmail_token").read_text())
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    if set(saved.get("scopes", [])) != set(scopes):
        raise ValueError("기존 gmail.readonly 테스트 계정의 token.json을 사용하세요")
    creds = Credentials.from_authorized_user_info(saved, scopes)
    store = CaptureStore(ROOT)
    with AuthorizedSession(creds) as session:
        mailbox = gmail_get(session, "profile")["emailAddress"].casefold()
        last_state = None
        while True:
            labels = gmail_get(session, "labels").get("labels", [])
            label = next((x for x in labels if x["name"] == GMAIL_LABEL and x.get("type") == "user"), None)
            state = "waiting_label" if label is None else "ready"
            if label:
                state = poll_gmail(session, mailbox, label["id"], store)
            status("gmail", state, label=GMAIL_LABEL)
            if last_state != state:
                print(f"Gmail {state} label={GMAIL_LABEL}", flush=True)
                last_state = state
            time.sleep(int(os.environ.get("GMAIL_POLL_SECONDS", "15")))
