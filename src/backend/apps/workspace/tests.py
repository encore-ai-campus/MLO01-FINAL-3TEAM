from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase
from django.urls import reverse


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

    @patch("apps.workspace.presentation.views.current_actor", return_value={"name": "상담사", "email": "staff@example.org"})
    def test_settings_navigation_follows_analytics_and_links_to_django(self, actor):
        response = self.client.get("/home/")
        self.assertContains(response, '<details class="desk-settings-menu">')
        self.assertContains(response, 'href="/settings/#personal"')
        self.assertNotContains(response, "07-SettingsPage.html")
        html = response.content.decode()
        followup_link = f'href="{reverse("followup:page")}"'
        self.assertLess(html.index(followup_link), html.index('href="/analytics/"'))
        self.assertLess(html.index('href="/analytics/"'), html.index('<details class="desk-settings-menu">'))
