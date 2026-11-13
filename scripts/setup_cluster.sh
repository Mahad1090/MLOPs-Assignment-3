#!/usr/bin/env bash
# Create the kind cluster, build + load the prediction API image and deploy it.
source "$(dirname "$0")/common.sh"
VERSION="${1:-1.0.0}"
kind get clusters | grep -qx mlops || kind create cluster --name mlops --wait 120s
[ -f serving_model/model.pkl ] || .venv/bin/python -m scripts.export_model --alias candidate
docker build -t "prediction-api:$VERSION" .
kind load docker-image "prediction-api:$VERSION" --name mlops
kubectl --context kind-mlops apply -f kubernetes/namespace.yaml
kubectl --context kind-mlops apply -f kubernetes/
kubectl --context kind-mlops -n mlops rollout status deployment/prediction-api --timeout=180s
kubectl --context kind-mlops -n mlops get deploy,pods,svc -o wide
