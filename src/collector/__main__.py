import argparse
import asyncio
import sys
from datetime import datetime, timezone
import json
from pathlib import Path
from .events import ROOT, status
from .pipeline import init, init_schema, migrate_capture, publish_spool, run_db
from .exporter import serve_metrics
from .inference import retry_embedding, run_worker
from .sources import run_discord, run_gmail


def health(role):
    try:
        row = json.loads((ROOT / "status" / (role + ".json")).read_text())
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["at"])
        return row["state"] == "ready" and age.total_seconds() < 150
    except (OSError, KeyError, ValueError):
        return False


def authorize_gmail():
    from google_auth_oauthlib.flow import InstalledAppFlow
    from infra.channel_probe.artifacts import write_json

    base = Path("/oauth")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(base / "credentials.json"), ["https://www.googleapis.com/auth/gmail.readonly"],
        autogenerate_code_verifier=True)
    creds = flow.run_local_server(host="127.0.0.1", bind_addr="0.0.0.0", port=18765,
                                  open_browser=False, timeout_seconds=300, prompt="consent",
                                  authorization_prompt_message="호스트 브라우저에서 승인하세요.\n{url}")
    write_json(base / "token.json", json.loads(creds.to_json()))
    print("Gmail 읽기 전용 token.json 저장 완료")


def main():
    parser = argparse.ArgumentParser(description="Encore VOC collection pipeline")
    parser.add_argument("role", choices=["discord","gmail","init","schema","publish","db","gmail-oauth",
                                         "capture-migrate","health","classify","embed","metrics","retry-embedding",
                                         "evaluate","replay"])
    parser.add_argument("source", nargs="?")
    args = parser.parse_args()
    role = args.role
    if role == "health":
        if args.source not in ("discord", "gmail"):
            parser.error("health requires discord or gmail")
        raise SystemExit(0 if health(args.source) else 1)
    try:
        if role == "discord":
            asyncio.run(run_discord())
        elif role in ("retry-embedding", "evaluate", "replay"):
            if not args.source:
                parser.error(f"{role} requires an input")
            from .evaluation import EvaluationError, evaluate, replay
            try:
                {"retry-embedding": retry_embedding, "evaluate": evaluate, "replay": replay}[role](args.source)
            except EvaluationError as exc:
                raise SystemExit(str(exc)) from None
        else:
            {"gmail": run_gmail, "gmail-oauth": authorize_gmail, "schema": init_schema,
             "init": init, "capture-migrate": migrate_capture, "publish": publish_spool, "db": run_db,
             "classify": lambda: run_worker("classification"), "embed": lambda: run_worker("embedding"),
             "metrics": serve_metrics}[role]()
    except Exception as exc:
        if role in ("discord","gmail"):
            status(role,"error",error=type(exc).__name__)
        # HTTP errors can contain message IDs or tokens. Log only the exception class.
        print(f"{role} stopped: {type(exc).__name__}",file=sys.stderr,flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
