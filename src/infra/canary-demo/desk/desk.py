"""Run the existing Django desk on the isolated, local canary demo database."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from demo import KUBE, ROOT, kube


def inside(command):
    source = (HERE / "in_pod.py").read_text()
    print(kube("exec", "-i", "deployment/canary-desk", "--", "python", "-", command, data=source))


def setup():
    kube("get", "secret/voc-classification-db", "-o", "name")
    architecture = kube("get", "nodes", "-o", "jsonpath={.items[0].status.nodeInfo.architecture}")
    platform = f"linux/{architecture}"
    digest = subprocess.check_output(["docker", "build", "--platform", platform, "-q",
                                      "-f", str(ROOT / "src/backend/Dockerfile"), str(ROOT / "src")], text=True).strip()
    tag = "voc-canary-desk:" + digest.removeprefix("sha256:")[:16]
    subprocess.run(["docker", "tag", digest, tag], check=True)
    with TemporaryDirectory(prefix="voc-canary-desk-") as directory:
        archive = str(Path(directory) / "desk.tar")
        subprocess.run(["docker", "image", "save", "--platform", platform, "-o", archive, tag], check=True)
        subprocess.run(["k3d", "image", "import", archive, "--cluster", "voc-canary-demo"], check=True)
    if not kube("get", "secret/canary-desk", "--ignore-not-found", "-o", "name"):
        kube("create", "-f", "-", data=json.dumps({
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "canary-desk"},
            "stringData": {"staff-password": secrets.token_urlsafe(32), "django-key": secrets.token_urlsafe(64)},
        }))
    manifest = (HERE / "desk.yaml").read_text().replace("__DESK_IMAGE__", tag)
    kube("apply", "-f", "-", data=manifest)
    kube("rollout", "status", "deployment/canary-desk", "--timeout=120s")
    inside("seed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "verify", "open"])
    command = parser.parse_args().command
    if command == "setup":
        setup()
    elif command == "verify":
        inside("verify")
    else:
        # One foreground process; terminating the command closes the listener.
        import os
        os.execvp(KUBE[0], [*KUBE, "-n", "voc-ml", "port-forward", "--address", "127.0.0.1",
                            "service/canary-desk", "18086:8000"])
