#!/usr/bin/env bash
# Bootstrap the dedicated local demo and return sentiment routing to 0% canary.
set -euo pipefail
umask 077
cd "$(dirname "$0")/../../.."
root=$(pwd -P)
demo="$root/src/infra/canary-demo"
state="$root/.local/canary"
cluster=voc-canary-demo
node="k3d-$cluster-server-0"
argo_version=v3.6.3
argo_sha=22b404352546b3bd000cd8354de793e8ca748fc8f3b2798cefe397dc4cafef27

for tool in docker k3d kubectl python3 curl shasum; do
  command -v "$tool" >/dev/null || { echo "Required command missing: $tool" >&2; exit 1; }
done
case "$(docker info --format '{{.Architecture}}')" in
  aarch64|arm64) platform=linux/arm64 ;;
  amd64|x86_64) platform=linux/amd64 ;;
  *) echo 'A running arm64 or amd64 Linux Docker engine is required.' >&2; exit 1 ;;
esac

mkdir -p "$state"
chmod 700 "$state"
tmp=$(mktemp -d "$state/up.XXXXXXXX")
trap 'rm -rf -- "$tmp"' EXIT
export KUBECONFIG="$state/kubeconfig"

clusters=$(k3d cluster list --no-headers)
if ! awk -v wanted="$cluster" '$1 == wanted {found=1} END {exit !found}' <<< "$clusters"; then
  k3d cluster create "$cluster" --servers 1 --agents 0 --image rancher/k3s:v1.35.5-k3s1 \
    --api-port 127.0.0.1:16553 --k3s-arg '--disable=traefik@server:0' \
    --kubeconfig-update-default=false --kubeconfig-switch-context=false --wait --timeout 240s
elif [[ "$(docker inspect --format '{{.State.Running}}' "$node")" != true ||
        "$(docker inspect --format '{{.State.Running}}' "k3d-$cluster-serverlb")" != true ]]; then
  k3d cluster start "$cluster" --wait --timeout 240s
fi

binding=$(docker port "k3d-$cluster-serverlb" 6443/tcp)
if [[ ! "$binding" =~ ^127\.0\.0\.1:[0-9]+$ ]]; then
  echo "Expected a loopback-only API port on $cluster, found $binding" >&2
  exit 1
fi
k3d kubeconfig get "$cluster" > "$tmp/kubeconfig"
kubectl --kubeconfig "$tmp/kubeconfig" config set-cluster "k3d-$cluster" --server="https://$binding" >/dev/null
mv "$tmp/kubeconfig" "$KUBECONFIG"
kube() { kubectl --kubeconfig "$KUBECONFIG" --context "k3d-$cluster" "$@"; }
kube wait --for=condition=Ready "node/$node" --timeout=120s
[[ "$(kube get nodes -o name)" == "node/$node" ]] || { echo 'Expected the single-node demo cluster.' >&2; exit 1; }
for ns in voc-ml argocd; do
  kube create namespace "$ns" --dry-run=client -o yaml | kube apply -f -
done

argo_yaml="$state/argocd-$argo_version-install.yaml"
verify_argo() { [[ "$(shasum -a 256 "$1" | cut -d' ' -f1)" == "$argo_sha" ]]; }
if [[ ! -s "$argo_yaml" ]] || ! verify_argo "$argo_yaml"; then
  curl --fail --location --silent --show-error --retry 3 \
    "https://raw.githubusercontent.com/argoproj/argo-cd/$argo_version/manifests/install.yaml" \
    --output "$tmp/argocd.yaml"
  verify_argo "$tmp/argocd.yaml" || { echo 'Argo CD manifest checksum mismatch.' >&2; exit 1; }
  mv "$tmp/argocd.yaml" "$argo_yaml"
fi
kube -n argocd apply --server-side -f "$argo_yaml"
kube wait --for=condition=Established crd/applications.argoproj.io --timeout=90s

mirror=voc-canary-git:demo-1
worker=voc-classification-worker:dev-1
model=voc-classifier:stub-1
database=pgvector/pgvector:pg16
docker build --platform "$platform" --load -t "$mirror" "$demo/git-mirror"
docker build --platform "$platform" --load -t "$worker" src/workers/classification
docker build --platform "$platform" --load -t "$model" "$demo/fixture"
external_images=("$database")
repo_part='[a-z0-9]+([._-]+[a-z0-9]+)*'
tag_pattern="^($repo_part(:[0-9]+)?/)?($repo_part/)*$repo_part:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
for component in prometheus grafana; do
  manifest="$demo/observability/$component.yaml"
  # A local empty merge patch reads the YAML documents without an API request.
  tag=$(kubectl patch --local --type=merge -p '{}' -f "$manifest" \
    -o "jsonpath={.spec.template.spec.containers[?(@.name==\"$component\")].image}")
  if [[ ! "$tag" =~ $tag_pattern || "$tag" == *:latest ]]; then
    echo "Expected one version-tagged $component image in $manifest, found '$tag'" >&2
    exit 1
  fi
  external_images+=("$tag")
done
for tag in "${external_images[@]}"; do
  if ! docker image inspect --platform "$platform" "$tag" >/dev/null 2>&1; then
    docker pull --platform "$platform" "$tag"
  fi
done

# CRI reports the native image config digest. Multi-platform Docker tag IDs
# may identify an OCI index instead. Compare the config from the exact archive.
tags=("$mirror" "$worker" "$model" "${external_images[@]}")
archive="$tmp/demo-images.tar"
docker image save --platform "$platform" -o "$archive" "${tags[@]}"
python3 - "$archive" "$platform" "${tags[@]}" > "$tmp/digests" <<'PY'
import hashlib
import json
import sys
import tarfile

archive, platform, *tags = sys.argv[1:]
expected = {}
with tarfile.open(archive) as tar:
    for manifest in json.load(tar.extractfile("manifest.json")):
        data = tar.extractfile(manifest["Config"]).read()
        config = json.loads(data)
        if f'{config["os"]}/{config["architecture"]}' != platform:
            raise SystemExit(f"Unexpected image platform in {archive}")
        for tag in manifest.get("RepoTags") or ():
            expected[tag] = "sha256:" + hashlib.sha256(data).hexdigest()
for tag in tags:
    print(tag, expected[tag])
PY

node_digest() {
  docker exec "$node" crictl inspecti --output go-template --template '{{.status.id}}' "$1"
}
import_needed=false
while read -r tag expected; do
  if [[ "$(node_digest "$tag" 2>/dev/null || true)" != "$expected" ]]; then import_needed=true; fi
done < "$tmp/digests"
if [[ "$import_needed" == true ]]; then k3d image import "$archive" --cluster "$cluster"; fi
while read -r tag expected; do
  [[ "$(node_digest "$tag")" == "$expected" ]] || { echo "Stale node image: $tag" >&2; exit 1; }
done < "$tmp/digests"

# Rebuilding a fixed local tag does not change a Deployment template. Recreate
# only owned demo Pods still running an older config, also after an interrupted run.
kube -n voc-ml get pods -o json > "$tmp/pods.json"
python3 - "$tmp/digests" "$tmp/pods.json" > "$tmp/stale-pods" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    expected = dict(line.split() for line in f)
with open(sys.argv[2]) as f:
    pods = json.load(f)["items"]
for pod in pods:
    meta = pod["metadata"]
    if not any(ref["kind"] == "ReplicaSet" for ref in meta.get("ownerReferences", [])):
        continue
    for container in pod.get("status", {}).get("containerStatuses", []):
        tag = container["image"].removeprefix("docker.io/").removeprefix("library/")
        if tag in expected and container.get("imageID") and container["imageID"] != expected[tag]:
            print(meta["name"])
            break
PY
while read -r pod; do kube -n voc-ml delete pod "$pod" --ignore-not-found --timeout=90s; done < "$tmp/stale-pods"

kube -n voc-ml apply -f "$demo/git-mirror/kubernetes.yaml"
kube -n voc-ml rollout status deployment/canary-git --timeout=180s
python3 "$demo/demo.py" prepare-db
kube -n argocd rollout status deployment/argocd-repo-server --timeout=300s
kube -n argocd rollout status statefulset/argocd-application-controller --timeout=300s
python3 "$demo/demo.py" set-weight 0
kube -n voc-ml rollout status deployment -l app.kubernetes.io/instance=voc-classification --timeout=180s
kube apply -k "$demo/observability"
kube -n voc-ml rollout status deployment/canary-prometheus --timeout=240s
kube -n voc-ml rollout status deployment/canary-grafana --timeout=240s
echo "Local demo ready. KUBECONFIG=$KUBECONFIG (API https://$binding)"
