"""Build and import the two local arm64 releases. Arguments are the two model/ directories."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[2]
RELEASES = json.loads((HERE / "values.json").read_text())["models"]
WEIGHT_SHA = {
    "stable": "fdeda6b62ce0920f22da2c032e35e7575fcacbacb9ef33303c88e17a85594d7d",
    "canary": "71dfc105668710718c1bccf11ba87b9d0af4ea4fcd50b1359763821b84cfe7c0",
}

def run(*args):
    subprocess.run(args, check=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stable", type=Path)
    parser.add_argument("canary", type=Path)
    args = parser.parse_args()
    base = "voc-klue-api:eaf3b9c"
    run("docker", "buildx", "build", "--platform", "linux/arm64", "--load", "-t", base, str(SRC/"ml/serving"))
    images = []
    for slot, folder in vars(args).items():
        digest = hashlib.sha256()
        with (folder/"model.safetensors").open("rb") as f:
            for chunk in iter(lambda: f.read(1024*1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != WEIGHT_SHA[slot]:
            raise SystemExit(f"Unexpected {slot} checkpoint")
        img = RELEASES[slot]["image"]
        tag = f'{img["repository"]}:{img["tag"]}'
        kinds = "sentiment,category" if slot == "stable" else "sentiment"
        run("docker", "buildx", "build", "--platform", "linux/arm64", "--load", "-t", tag,
            "--build-arg", f"SERVER_IMAGE={base}", "--build-arg", f"ANALYSIS_KINDS={kinds}",
            "--build-context", f"mapping={HERE}", "-f", str(HERE/"Dockerfile"), str(folder))
        images.append(tag)
    with TemporaryDirectory() as tmp:
        archive = str(Path(tmp)/"klue.tar")
        run("docker", "image", "save", "--platform", "linux/arm64", "-o", archive, *images)
        run("k3d", "image", "import", archive, "--cluster", "voc-canary-demo")

if __name__ == "__main__":
    main()
