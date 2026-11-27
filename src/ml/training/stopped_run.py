"""Finish an open MLflow run after Argo stops a training workflow at the user's request."""
import os
import sys

import psycopg


def interrupted_run_id(connection, workflow_uid):
    if not connection.execute("SELECT to_regclass('voc.training_stop_request')").fetchone()[0]:
        return None
    row = connection.execute("""
        SELECT r.mlflow_run_id FROM voc.training_stop_request s
        JOIN voc.training_run r ON r.workflow_ref=s.workflow_uid
        JOIN voc.training_dataset d ON d.dataset_id=r.train_dataset_id AND d.company_id=s.company_id
        WHERE s.workflow_uid=%s AND s.state IN ('requested','stopped') AND r.mlflow_run_id IS NOT NULL
          AND NOT EXISTS (SELECT FROM voc.model_version v WHERE v.workflow_ref=r.workflow_ref)
    """, (workflow_uid,)).fetchone()
    return row[0] if row else None


def finish_interrupted_run(workflow_uid):
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        run_id = interrupted_run_id(connection, workflow_uid)
    if not run_id:
        return
    from mlflow import MlflowClient

    client = MlflowClient()
    run = client.get_run(run_id)
    if run.data.tags.get("workflow_ref") == workflow_uid and run.info.status in {"RUNNING", "SCHEDULED"}:
        client.set_terminated(run_id, status="KILLED")


if __name__ == "__main__":
    finish_interrupted_run(sys.argv[1])
