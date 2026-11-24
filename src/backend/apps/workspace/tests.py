from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase


class WorkspacePageTests(SimpleTestCase):
    def test_workspace_requires_staff_session(self):
        for url in ("/home/", "/inbox/", "/analytics/?preview=administrator"):
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                target = urlsplit(response.redirect_chain[-1][0])
                self.assertEqual(target.path, "/login/")
                self.assertEqual(parse_qs(target.query).get("next"), [url])

    @patch("apps.workspace.presentation.views.current_actor", return_value={"name": "상담사", "email": "staff@example.org"})
    def test_previews_are_rendered_server_side_and_scoped_to_sample_role(self, actor):
        self.assertContains(self.client.get("/home/"), "상담사님의 오늘 업무")
        drawer = self.client.get("/inbox/", {"tab": "review", "request": "review-1048"})
        self.assertContains(drawer, "받은 검토 요청 상세")
        limited = self.client.get("/analytics/", {"preview": "team-lead", "scope": "tech", "tab": "agents"})
        self.assertContains(limited, "고객지원팀")
        self.assertNotContains(limited, "김하늘")
        overview = self.client.get("/analytics/", {"preview": "overview-only", "tab": "agents", "agent": "한서윤"})
        self.assertContains(overview, "현재 미처리 상태별 구성")
        self.assertNotContains(overview, "한서윤 상담사 상세")
