"""Exercise the registration fence in a rollback-only transaction."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from .datasets import database
from .workflow import register_candidate


@unittest.skipUnless(os.environ.get("VOC_RETRAINING_TEST_DB") == "1", "Requires the local TiCats training database")
class RegistrationStopTests(unittest.TestCase):
    def test_previously_accepted_stop_prevents_any_registry_call(self):
        company, staff, uid = (str(uuid4()) for _ in range(3))
        with database() as cn, cn.transaction(force_rollback=True):
            cn.execute("INSERT INTO public.company(company_id,email_domain) VALUES (%s,'stop.test')", (company,))
            cn.execute("""INSERT INTO public.staff_user(staff_user_id,company_id,email,password_hash)
                       VALUES (%s,%s,'test@stop.test','!')""", (staff,company))
            cn.execute("""INSERT INTO voc.training_stop_request(workflow_name,workflow_uid,company_id,requested_by,cutoff)
                       VALUES (%s,%s,%s,%s,now())""", ("voc-training-"+uuid4().hex,uid,company,staff))
            with patch("ml.training.workflow.mlflow.MlflowClient") as registry:
                with self.assertRaisesRegex(ValueError, "registration was stopped"):
                    register_candidate(cn, uid)
                registry.assert_not_called()
