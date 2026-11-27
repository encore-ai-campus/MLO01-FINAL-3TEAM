from unittest import TestCase
from watch import REPO, WORKFLOW, eligible


class ReleaseGate(TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.run = dict(id=10, run_attempt=1, path=WORKFLOW, event="push", head_branch="develop",
                        head_sha=self.sha, status="completed", conclusion="success", head_repository={"full_name": REPO})
        self.pull = dict(merged_at="2026-11-01", merge_commit_sha=self.sha,
                         base={"ref": "develop", "repo": {"full_name": REPO}})

    def test_verified_merge(self):
        self.assertTrue(eligible(self.sha, [self.run], [self.pull]))

    def test_direct_push_unmerged_or_other_target(self):
        for pulls in ([], [dict(self.pull, merged_at=None)], [dict(self.pull, merge_commit_sha="b"*40)]):
            self.assertFalse(eligible(self.sha, [self.run], pulls))

    def test_untrusted_or_stale_run(self):
        for change in (dict(event="pull_request"), dict(head_sha="b"*40), dict(head_branch="feature"),
                       dict(path="unrelated.yml"), dict(head_repository={"full_name":"fork/repo"}),
                       dict(conclusion="failure"), dict(status="in_progress")):
            self.assertFalse(eligible(self.sha, [dict(self.run, **change)], [self.pull]))
        self.assertFalse(eligible(self.sha, [self.run, dict(self.run, id=11, conclusion="failure")], [self.pull]))
