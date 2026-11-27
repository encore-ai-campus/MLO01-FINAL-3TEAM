"""Feast reads the same immutable snapshot in PostgreSQL and Redis."""

import os
from datetime import timedelta
from functools import lru_cache

FEATURES = ("title", "body", "unresolved_count", "avg_first_reply_7d_seconds")
FEATURE_REFS = tuple(f"ticket_snapshot:{name}" for name in FEATURES)


@lru_cache(maxsize=1)
def store():
    from feast import FeatureStore, RepoConfig
    from psycopg.conninfo import conninfo_to_dict

    db = conninfo_to_dict(os.environ["DATABASE_URL"])
    return FeatureStore(config=RepoConfig(
        project="voc",
        provider="local",
        registry={"registry_type": "sql", "path": os.environ["FEAST_REGISTRY_URL"]},
        offline_store={
            "type": "postgres", "db_schema": os.environ.get("FEAST_OFFLINE_SCHEMA", "voc"), "host": db.get("host", "localhost"),
            "port": int(db.get("port", 5432)), "database": db["dbname"], "user": db["user"],
            "password": db.get("password", ""), "sslmode": db.get("sslmode", "prefer"),
        },
        online_store={
            "type": "redis", "connection_string": os.environ["FEAST_REDIS_CONNECTION"],
            "key_ttl_seconds": 86400,
        },
    ))


def register():
    from feast import Entity, FeatureView, Field, PushSource, ValueType
    from feast.infra.offline_stores.contrib.postgres_offline_store.postgres_source import PostgreSQLSource
    from feast.types import Float64, Int64, String

    snapshot = Entity(name="feature", join_keys=["feature_uuid"], value_type=ValueType.STRING)
    postgres = PostgreSQLSource(
        name="ticket_snapshot_history",
        query="""SELECT feature_snapshot_id::text AS feature_uuid, title, body,
                        unresolved_count, avg_first_reply_7d_ms / 1000 AS avg_first_reply_7d_seconds, available_at
                 FROM voc.feature_snapshot""",
        timestamp_field="available_at",
    )
    push = PushSource(name="ticket_snapshot_push", batch_source=postgres)
    view = FeatureView(
        name="ticket_snapshot", entities=[snapshot], ttl=timedelta(0), source=push,
        schema=[Field(name=name, dtype=dtype) for name, dtype in zip(FEATURES, (String, String, Int64, Float64))],
    )
    store().apply([snapshot, postgres, push, view])


def read_online(snapshot):
    import pandas as pd
    from feast.data_source import PushMode

    entity = {"feature_uuid": str(snapshot["feature_snapshot_id"])}
    values = {name: snapshot[name] for name in FEATURES}
    frame = pd.DataFrame([{**entity, **values, "available_at": snapshot["available_at"]}])
    frame["unresolved_count"] = frame["unresolved_count"].astype("Int64")
    frame["avg_first_reply_7d_seconds"] = frame["avg_first_reply_7d_seconds"].astype("Float64")
    features = store()
    features.push("ticket_snapshot_push", frame, to=PushMode.ONLINE)
    result = features.get_online_features(features=list(FEATURE_REFS), entity_rows=[entity]).to_dict()
    received = {name: result[name][0] for name in FEATURES}
    if received != values:
        raise ValueError("Redis did not return the selected snapshot")
    return received
