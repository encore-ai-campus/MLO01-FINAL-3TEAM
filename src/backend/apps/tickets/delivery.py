import base64
import json
import os
import re
from email.message import EmailMessage
from email.utils import parseaddr
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DeliveryError(Exception):
    def __init__(self, status):
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(url, data, content_type, authorization=None):
    headers = {'Content-Type': content_type, 'User-Agent': 'VoC-Flow/1.0'}
    if authorization:
        headers['Authorization'] = authorization
    try:
        with build_opener(NoRedirect()).open(Request(url, data, headers), timeout=10) as response:
            result = json.load(response)
            if not isinstance(result, dict):
                raise DeliveryError('unknown')
            return result
    except HTTPError as exc:
        rejected = 400 <= exc.code < 500 and exc.code != 408
        raise DeliveryError('failed' if rejected else 'unknown') from None
    except (URLError, OSError, ValueError, HTTPException):
        raise DeliveryError('unknown') from None


def send_reply(source, body):
    kind = source['source_kind']
    try:
        credentials = json.loads(os.getenv('TASK3_REPLY_CONNECTIONS', '{}'))[
            str(source['company_uuid'])][kind][source['connection_id']]
        if kind == 'gmail':
            mailbox = source['connection_id']
            address = source['reply_address']
            message_id = source['rfc_message_id']
            if (parseaddr(address)[1] != address or '@' not in address or
                    not re.fullmatch(r'<[^<>\s@]+@[^<>\s@]+>', message_id)):
                raise DeliveryError('failed')
            message = EmailMessage()
            message['From'] = mailbox
            message['To'] = address
            message['Subject'] = source['original_subject']
            message['In-Reply-To'] = message_id
            message['References'] = message_id
            message.set_content(body)
            token = post(
                'https://oauth2.googleapis.com/token',
                urlencode({'grant_type': 'refresh_token', 'client_id': credentials['client_id'],
                           'client_secret': credentials['client_secret'],
                           'refresh_token': credentials['refresh_token']}).encode(),
                'application/x-www-form-urlencoded',
            )['access_token']
            url = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
            payload = {'threadId': source['source_conversation_id'],
                       'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()}
            authorization = f'Bearer {token}'
        else:
            channel = source['source_channel_id']
            message_id = source['source_message_id']
            if not all(re.fullmatch(r'[0-9]{1,20}', value) for value in (channel, message_id)):
                raise DeliveryError('failed')
            url = f'https://discord.com/api/v10/channels/{channel}/messages'
            payload = {'content': body, 'allowed_mentions': {'parse': [], 'replied_user': False},
                       'message_reference': {'message_id': message_id, 'fail_if_not_exists': True}}
            authorization = f'Bot {credentials["bot_token"]}'
    except (KeyError, TypeError, ValueError, DeliveryError):
        raise DeliveryError('failed') from None

    result = post(url, json.dumps(payload).encode(), 'application/json', authorization)
    if not isinstance(result.get('id'), str):
        raise DeliveryError('unknown')
    if kind == 'gmail' and result.get('threadId') != source['source_conversation_id']:
        raise DeliveryError('unknown')
    return result['id']
