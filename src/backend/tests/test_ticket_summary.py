import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from apps.tickets import summary
from config.http import DeskError


class TicketSummaryTests(TestCase):
    @patch.object(summary.OLLAMA, "open")
    def test_verifies_model_selection_and_keeps_conditions_and_current_mail(self, send):
        body = "안녕하세요. 영수증이 오지 않았습니다. 금요일까지 보내 주세요. 감사합니다.\n> 이전 상담 내용"
        def result(issue):
            return BytesIO(json.dumps({
                "done_reason": "stop",
                "message": {"content": json.dumps({"issue": issue, "request": ""})},
            }).encode())
        send.return_value = result("영수증이 오지 않았습니다.")
        data = summary.summarize(body, "gmail")
        self.assertEqual(data["text"], "영수증이 오지 않았습니다. 금요일까지 보내 주세요.")
        request = json.loads(send.call_args.args[0].data)
        self.assertEqual(request["model"], summary.MODEL)
        self.assertNotIn("이전 상담", request["messages"][0]["content"])
        send.return_value = result("영수증을 전송했습니다.")
        with self.assertRaises(DeskError) as error:
            summary.summarize(body, "gmail")
        self.assertEqual(error.exception.status, 502)

    @patch.object(summary.OLLAMA, "open", side_effect=TimeoutError())
    def test_offline_model_returns_retryable_error(self, _send):
        with self.assertRaises(DeskError) as error:
            summary.summarize("결제일이 언제인가요?", "discord")
        self.assertEqual(error.exception.status, 503)
