import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import release


class TrainingServicesTest(TestCase):
    def test_scheduler_shares_web_training_inputs_and_refreshes_domain_mapping(self):
        with TemporaryDirectory() as workdir:
            root = Path(workdir)
            (root / "training-read.env").touch()
            web = {"environment": {"MLFLOW_S3_ENDPOINT_URL": "", "DATABASE_URL": "test-db"},
                   "volumes": [], "ports": ["8000:8000"], "container_name": "web-only"}
            profile = {"services": {"web": web, "assign": {}}}
            with patch.object(release, "LIVE", root), patch.object(release, "kube") as config:
                for code in ("commerce", "finance"):
                    directory = root / code
                    directory.mkdir()
                    config.return_value = {"data": {"domains.json": json.dumps({"task": code})}}
                    release.configure_training_services(profile, code, directory)
                    scheduler = profile["services"]["retraining-scheduler"]
                    self.assertEqual(scheduler["image"], code)
                    self.assertEqual(scheduler["command"], ["python", "manage.py", "run_retraining_scheduler"])
                    self.assertEqual(scheduler["environment"], web["environment"])
                    self.assertNotIn("ports", scheduler)
                    self.assertNotIn("container_name", scheduler)
                    self.assertNotIn("MLFLOW_S3_ENDPOINT_URL", web["environment"])
                    self.assertEqual(scheduler["env_file"], [str(root / "training-read.env")])
                    self.assertEqual(len(scheduler["volumes"]), 1)
                    bind = scheduler["volumes"][0]
                    self.assertTrue(bind["read_only"])
                    self.assertEqual(json.loads(Path(bind["source"]).read_text()), {"task": code})

    def test_preserves_explicit_domain_mount_and_existing_scheduler_options(self):
        web = {"environment": {"VOC_TRAINING_DOMAINS_FILE": "/installation/domains.json"},
               "volumes": [{"target": "/installation/domains.json", "source": "/existing", "read_only": True}]}
        profile = {"services": {"web": web, "assign": {}, "retraining-scheduler": {"cpus": 1}}}
        with TemporaryDirectory() as folder, patch.object(release, "LIVE", Path(folder)), patch.object(release, "kube") as config:
            release.configure_training_services(profile, "test", Path(folder))
        config.assert_not_called()
        scheduler = profile["services"]["retraining-scheduler"]
        self.assertEqual(scheduler["volumes"], web["volumes"])
        self.assertEqual(scheduler["cpus"], 1)
