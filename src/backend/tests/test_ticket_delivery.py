import base64
import email
import json
import unittest
from email.policy import default
from http.client import IncompleteRead
from unittest.mock import Mock, patch


from apps.tickets import delivery


class ChannelReplyTests(unittest.TestCase):
    def setUp(self):
        config = {
            'company-a': {
                'gmail': {'mailbox@example.com': {
                    'client_id': 'client', 'client_secret': 'secret', 'refresh_token': 'refresh',
                }},
                'discord': {'guild-a': {'bot_token': 'bot-placeholder'}},
            },
        }
        env = patch.dict('os.environ', {'TASK3_REPLY_CONNECTIONS': json.dumps(config)})
        env.start()
        self.addCleanup(env.stop)

    def test_gmail_reply_uses_original_thread_subject_and_rfc_message_id(self):
        source = {'company_uuid': 'company-a', 'source_kind': 'gmail',
                  'connection_id': 'mailbox@example.com', 'source_conversation_id': 'thread-1',
                  'rfc_message_id': '<original@example.com>', 'original_subject': '배송 문의',
                  'reply_address': 'customer@example.com'}
        with patch.object(delivery, 'post', side_effect=[
            {'access_token': 'token-placeholder'}, {'id': 'reply-1', 'threadId': 'thread-1'},
        ]) as post:
            self.assertEqual(delivery.send_reply(source, '답변입니다 🙂'), 'reply-1')
        url, raw, content_type, auth = post.call_args.args
        self.assertEqual(url, 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send')
        self.assertEqual(content_type, 'application/json')
        self.assertEqual(auth, 'Bearer token-placeholder')
        payload = json.loads(raw)
        self.assertEqual(payload['threadId'], 'thread-1')
        message = email.message_from_bytes(base64.urlsafe_b64decode(payload['raw']), policy=default)
        self.assertEqual(message['Subject'], '배송 문의')
        self.assertEqual(message['To'], 'customer@example.com')
        self.assertEqual(message['In-Reply-To'], '<original@example.com>')
        self.assertEqual(message['References'], '<original@example.com>')
        self.assertEqual(message.get_content().strip(), '답변입니다 🙂')

    def test_discord_reply_references_original_message_without_mentions(self):
        source = {'company_uuid': 'company-a', 'source_kind': 'discord', 'connection_id': 'guild-a',
                  'source_channel_id': '123456789', 'source_message_id': '987654321'}
        with patch.object(delivery, 'post', return_value={'id': 'reply-2'}) as post:
            self.assertEqual(delivery.send_reply(source, '상담 답변 @everyone'), 'reply-2')
        url, raw, _, auth = post.call_args.args
        self.assertEqual(url, 'https://discord.com/api/v10/channels/123456789/messages')
        self.assertEqual(auth, 'Bot bot-placeholder')
        self.assertEqual(json.loads(raw), {
            'content': '상담 답변 @everyone',
            'allowed_mentions': {'parse': [], 'replied_user': False},
            'message_reference': {'message_id': '987654321', 'fail_if_not_exists': True},
        })

    def test_incomplete_provider_response_is_an_unknown_delivery(self):
        opener = Mock()
        opener.open.side_effect = IncompleteRead(b'{"id":')
        with patch.object(delivery, 'build_opener', return_value=opener):
            with self.assertRaises(delivery.DeliveryError) as failure:
                delivery.post('https://discord.com/api/v10/channels/123/messages', b'{}', 'application/json')
        self.assertEqual(failure.exception.status, 'unknown')
