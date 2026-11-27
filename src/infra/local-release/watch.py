"""Poll GitHub from the Mac. Run only merged and verified develop commits."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import Request, urlopen

REPO = "encore-ai-campus/MLO01-FINAL-3TEAM"
WORKFLOW = ".github/workflows/develop-local.yml"
STATE = Path(os.environ.get("VOC_RELEASE_HOME", Path.home() / ".local/share/voc-develop-cd"))


def github(path):
    req = Request(f"https://api.github.com/repos/{REPO}/{path}", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "voc-local-release",
    })
    with urlopen(req, timeout=20) as response:
        return json.load(response)


def eligible(sha, runs, pulls):
    latest = max((r for r in runs if r["path"] == WORKFLOW and r["event"] == "push"
                  and r["head_branch"] == "develop" and r["head_sha"] == sha
                  and (r.get("head_repository") or {}).get("full_name") == REPO),
                 key=lambda r: (r["id"], r["run_attempt"]), default=None)
    merged = any(p["merged_at"] and p["base"]["ref"] == "develop" and p["merge_commit_sha"] == sha
                 and p["base"]["repo"]["full_name"] == REPO for p in pulls)
    return bool(merged and latest and latest["status"] == "completed" and latest["conclusion"] == "success")


def poll():
    sha = github("git/ref/heads/develop")["object"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid GitHub SHA")
    marker = STATE / "deployed"
    previous = marker.read_text().strip() if marker.exists() else (STATE / "initial").read_text().strip()
    if marker.exists() and sha == previous:
        return
    runs = github(f"actions/runs?branch=develop&event=push&head_sha={sha}&per_page=100")
    if not eligible(sha, runs["workflow_runs"], github(f"commits/{sha}/pulls")):
        return
    checkout = STATE / "checkout"
    if not checkout.exists():
        subprocess.run(["git", "clone", "--no-checkout", "--single-branch", "--branch", "develop",
                        f"https://github.com/{REPO}.git", checkout], check=True)
    git = ["git", "-C", str(checkout)]
    subprocess.run([*git, "fetch", "--quiet", "origin", "refs/heads/develop"], check=True)
    if subprocess.check_output([*git, "rev-parse", "FETCH_HEAD"], text=True).strip() != sha:
        return
    subprocess.run([*git, "merge-base", "--is-ancestor", previous, sha], check=True)
    subprocess.run([*git, "checkout", "--quiet", "--detach", sha], check=True)
    subprocess.run([sys.executable, checkout / "src/infra/local-release/release.py", previous, sha],
                   cwd=checkout, check=True)
    temp = marker.with_suffix(".tmp")
    temp.write_text(sha + "\n")
    os.replace(temp, marker)
    print(f"Deployed {sha}", flush=True)


if __name__ == "__main__":
    with (STATE / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(0)
        poll()
