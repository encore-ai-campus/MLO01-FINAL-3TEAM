"""Commit the classification chart to the disposable, in-cluster Git repository."""

import argparse
import io
import re
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
CHART = Path("src/infra/helm/voc-classification")
CONTEXT = "k3d-voc-canary-demo"


def publish(percent=None):
    chart = ROOT / CHART
    files = [chart / name for name in ("Chart.yaml", "values.yaml", "values-local.yaml")]
    if (chart / "values.schema.json").is_file():
        files.append(chart / "values.schema.json")
    files.extend(sorted(path for path in (chart / "templates").rglob("*")
                        if path.is_file() and path.suffix in {".tpl", ".yaml", ".yml", ".txt"}))
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for path in files:
            if path.is_symlink() or not path.resolve().is_relative_to(chart.resolve()):
                raise ValueError(f"Unexpected chart symlink: {path}")
            data = path.read_bytes()
            if path.name == "values-local.yaml" and percent is not None:
                data, count = re.subn(rb"(?m)^([ \t]*canaryPercent:)[ \t]+[0-9]+[ \t]*$",
                                      rb"\g<1> " + str(percent).encode(), data)
                if count != 1:
                    raise ValueError("Expected one sentiment canaryPercent field in values-local.yaml")
            member = tarfile.TarInfo(path.relative_to(ROOT).as_posix())
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    message = "로컬 카나리 차트 적용" if percent is None else f"로컬 감성 모델 카나리 비율을 {percent}%로 설정"
    subprocess.run(["kubectl", "--context", CONTEXT, "-n", "voc-ml", "exec", "-i", "deployment/canary-git",
                    "-c", "publisher", "--", "publish-chart", message],
                   input=archive.getvalue(), check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--percent", type=int, help="override only the demo copy's sentiment weight (0..100)")
    args = parser.parse_args()
    if args.percent is not None and not 0 <= args.percent <= 100:
        parser.error("--percent must be within 0..100")
    publish(args.percent)
