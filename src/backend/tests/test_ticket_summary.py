import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from apps.tickets import summary

URL = "http://ollama.test:11434"


def reply(title):
    return BytesIO(json.dumps({"done_reason": "stop", "message": {
        "content": json.dumps({"summary": title}),
    }}).encode())


class TicketSummaryTests(TestCase):
    @patch.object(summary.OLLAMA, "open")
    def test_short_text_can_be_reworded_and_mail_history_is_omitted(self, send):
        body = "카드에 핑구 말고 다른 캐릭터가 들어가면 좋을 것 같아요\n> 예전 메일"
        send.return_value = reply("카드 캐릭터 교체 요청")
        self.assertEqual(summary.summarize_inquiry(body, "gmail", URL),
                         {"mode": "summary", "text": "카드 캐릭터 교체 요청", "partial": False})
        request = json.loads(send.call_args.args[0].data)
        self.assertEqual(request["model"], summary.MODEL)
        self.assertEqual(request["messages"][0]["content"], body.splitlines()[0])

    @patch.object(summary.OLLAMA, "open")
    def test_retries_invented_amount_without_returning_raw_input(self, send):
        send.side_effect = [reply("5,000원 환불 요청"), reply("반품 가능 여부 문의")]
        data = summary.summarize_inquiry("반품이 가능한가요?", "discord", URL)
        self.assertEqual(data["text"], "반품 가능 여부 문의")
        self.assertEqual(send.call_count, 2)

    @patch.object(summary, "request_title", return_value="강의 자막 크기 조절 요청")
    def test_how_question_keeps_its_topic_and_intent(self, generate):
        data = summary.summarize_inquiry("강의 자막을 더 크게 보는 방법이 있을까요?", "discord", URL)
        self.assertEqual(data["text"], "강의 자막 크기 조절 방법 문의")
        generate.assert_called_once()

    @patch.object(summary, "request_title")
    def test_long_inquiry_includes_the_middle_and_yields_one_title(self, generate):
        background = [f"접수 경과 {n}번을 첨부합니다." for n in range(230)]
        background.insert(114, "교통카드 환불 방법을 알려 주세요.")
        body = " ".join(background)
        generate.return_value = "교통카드 환불 방법 문의"
        data = summary.summarize_inquiry(body, "discord", URL)
        self.assertEqual(data, {"mode": "summary", "text": "교통카드 환불 방법 문의", "partial": False})
        self.assertGreater(generate.call_count, 2)
        self.assertIn("교통카드 환불 방법을 알려 주세요.", generate.call_args.args[0][0]["content"])

    @patch.object(summary, "request_title", return_value="배송 현황 문의")
    def test_exceptionally_large_text_discloses_partial_coverage(self, generate):
        data = summary.summarize_inquiry("배송 현황 " * 3000, "discord", URL)
        self.assertTrue(data["partial"])
        self.assertEqual(generate.call_count, summary.MAX_CHUNKS + 1)
