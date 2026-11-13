#!/usr/bin/env bash
# Injected failure 3: point the liveness probe at a path that does not exist (404). The
# kubelet then kills the container again and again, so Pods show restarts.
#   scripts/inject_failure_k8s_restarts.sh          # break, wait, show Pods
#   scripts/inject_failure_k8s_restarts.sh restore  # re-apply kubernetes/deployment.yaml
source "$(dirname "$0")/common.sh"
K="kubectl --context kind-mlops -n mlops"
if [ "${1:-break}" = "restore" ]; then
  $K apply -f kubernetes/deployment.yaml
  $K rollout status deployment/prediction-api --timeout=180s
else
  $K patch deployment prediction-api --type json \
    -p '[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/path","value":"/health/missing"}]'
  $K rollout status deployment/prediction-api --timeout=120s
  sleep "${WAIT:-150}"
fi
$K get pods
