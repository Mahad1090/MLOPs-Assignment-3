#!/usr/bin/env bash
# Injected failure 1: point the ConfigMap at a model file that does not exist and
# roll the Deployment. New Pods start but never become Ready.
#   scripts/inject_failure_k8s.sh          # break
#   scripts/inject_failure_k8s.sh restore  # fix
source "$(dirname "$0")/common.sh"
K="kubectl --context kind-mlops -n mlops"
if [ "${1:-break}" = "restore" ]; then
  $K apply -f kubernetes/configmap.yaml
else
  $K patch configmap prediction-api-config --type merge \
    -p '{"data":{"MODEL_PATH":"/app/model/model-v2-missing.pkl"}}'
fi
# Replace all Pods at once so the outage is real (bypasses the safe rolling update).
$K delete pods -l app.kubernetes.io/name=prediction-api --wait=false
sleep 25
$K get pods
