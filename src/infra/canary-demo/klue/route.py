"""Apply an uncommitted local Helm values override through the existing demo Argo CD Application."""
import argparse
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from demo import kube, deployed, MIRROR, MODEL_APP

def set_percent(percent):
    if not 0 <= percent <= 100:
        raise ValueError("Expected a percentage from 0 to 100")
    app = json.loads(kube("get", "application", MODEL_APP, "-o", "json", ns="argocd"))
    source = app["spec"]["source"]
    if source["repoURL"] != MIRROR or app["spec"]["destination"]["namespace"] != "voc-ml":
        raise RuntimeError("Expected the existing local demo deployment")
    values = json.loads((HERE/"values.json").read_text())
    values["routing"]["sentiment"]["canaryPercent"] = percent
    kube("patch", "application", MODEL_APP, "--type=merge", "-p",
         json.dumps({"spec":{"source":{"helm":{"valuesObject":values}}}}), ns="argocd")
    revision = app["status"]["sync"]["revision"]
    for _ in range(180):
        obj = json.loads(kube("get","deployment","voc-classification-worker","-o","json"))
        env = {v["name"]: v.get("value") for v in obj["spec"]["template"]["spec"]["containers"][0]["env"]}
        if env.get("STABLE_MODEL_VERSION") == values["models"]["stable"]["version"] and env.get("CANARY_MODEL_VERSION") == values["models"]["canary"]["version"]:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Argo CD did not reconcile the requested model releases")
    deployed(percent, revision)
    # A values-only change can reuse the same Git SHA and initial percentage.
    for model in values["models"].values():
        kube("rollout", "status", "deployment/voc-classification-"+model["version"], "--timeout=180s")
    return revision

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("percent", type=int, choices=range(101), metavar="0..100")
    print(set_percent(parser.parse_args().percent))
