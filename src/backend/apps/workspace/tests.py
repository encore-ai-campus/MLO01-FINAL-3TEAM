from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase


class WorkspacePageTests(SimpleTestCase):
    def test_workspace_requires_staff_session(self):
        for url in (
            "/home/", "/inbox/", "/inbox/?request=9020aca7-1794-430f-b1b5-8173b1b353ea",
            "/analytics/?preview=administrator", "/analytics/?preview=team-lead",
            "/analytics/?preview=overview-only",
        ):
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                target = urlsplit(response.redirect_chain[-1][0])
                self.assertEqual(target.path, "/login/")
                self.assertEqual(parse_qs(target.query).get("next"), [url])

    @patch("apps.workspace.presentation.views.current_actor", return_value={"name": "상담사", "email": "staff@example.org"})
    def test_home_is_rendered_server_side(self, actor):
        self.assertContains(self.client.get("/home/"), "상담사님의 오늘 업무")
