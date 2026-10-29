#!/bin/sh
# CI (the ci-dispatch runner, which may call the Kubernetes API): sign the release package's checksum manifest with
# Notation and the apt repository's Release file with the OpenPGP repository key, both through a Kubernetes Job in
# the ci namespace (scripts/ci/deb-sign-pod.sh) so no signing material reaches the runner. Modelled on the
# launch-sequence python-sign job (same trust root, same secret, same pinned tool versions).
#
#   scripts/ci/deb-sign.sh MANIFEST RELEASE OUTDIR
#
# MANIFEST is DEB-SHA256SUMS, RELEASE the apt Release file; OUTDIR receives DEB-SHA256SUMS.cose.sig, InRelease and
# Release.gpg. Variables (all have defaults except the ones CI provides):
#   SIGN_NAMESPACE (ci)  SIGN_TIMEOUT (600)  SIGN_IMAGE  POD_IMAGE (a Debian image with gpg, curl and python3)
#   PULL_TIMEOUT (900): how long the pod may take to pull its images and start the sign container. A cold node pulls
#   the ~1 GB POD_IMAGE through the Harbor proxy in minutes, so that wait is budgeted apart from SIGN_TIMEOUT.
#   NOTATION_VERSION  NOTATION_AZUREKV_VERSION  CI_SERVICE_ACCOUNT (builder)  HARBOR_PULL_SECRET
#   KEY_VAULT_NAME (omlab-secrets)  APT_KEY_SECRET (streamstack-apt-signing-key)  APT_KEY_FINGERPRINT
#   (default: packaging/apt-public.fingerprint, the committed public key's fingerprint)
set -eu

[ "$#" -eq 3 ] || { echo "usage: deb-sign.sh MANIFEST RELEASE OUTDIR" >&2; exit 2; }
MANIFEST="$1"
RELEASE="$2"
OUTDIR="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -s "${MANIFEST}" ] || { echo "ERROR: ${MANIFEST} is missing or empty" >&2; exit 1; }
[ -s "${RELEASE}" ] || { echo "ERROR: ${RELEASE} is missing or empty" >&2; exit 1; }
# The fingerprint lives in a file, not in .gitlab-ci.yml: a `KEY: <hex>` line there trips gitleaks' generic-api-key rule
# on the tag pipeline (v0.0.18), and a waiver for it would also hide a real key.
APT_KEY_FINGERPRINT="${APT_KEY_FINGERPRINT:-$(cat "${HERE}/../../packaging/apt-public.fingerprint" 2>/dev/null || true)}"
: "${APT_KEY_FINGERPRINT:?APT_KEY_FINGERPRINT is not set and packaging/apt-public.fingerprint is missing}"
: "${CI_PROJECT_ID:?}" "${CI_PIPELINE_IID:?}" "${CI_JOB_ID:?}"

SIGN_NAMESPACE="${SIGN_NAMESPACE:-ci}"
SIGN_TIMEOUT="${SIGN_TIMEOUT:-600}"
PULL_TIMEOUT="${PULL_TIMEOUT:-900}"
REGISTRY="${IMAGE_REGISTRY:-harbor.focuscell.org}"
SIGN_IMAGE="${SIGN_IMAGE:-${REGISTRY}/infra/ci-scanner:1.0.0}"
POD_IMAGE="${POD_IMAGE:-${REGISTRY}/dockerhub-proxy/library/python:3.13-bookworm}"
NOTATION_VERSION="${NOTATION_VERSION:-2.0.0-alpha.1}"
NOTATION_AZUREKV_VERSION="${NOTATION_AZUREKV_VERSION:-1.2.0}"
CI_SERVICE_ACCOUNT="${CI_SERVICE_ACCOUNT:-builder}"
HARBOR_PULL_SECRET="${HARBOR_PULL_SECRET:-harbor-registry-creds}"
KEY_VAULT_NAME="${KEY_VAULT_NAME:-omlab-secrets}"
APT_KEY_SECRET="${APT_KEY_SECRET:-streamstack-apt-signing-key}"
JOB_NAME="debsign-${CI_PROJECT_ID}-p${CI_PIPELINE_IID}-j${CI_JOB_ID}"
WORK=""

cleanup() {
  kubectl delete job "${JOB_NAME}" -n "${SIGN_NAMESPACE}" --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl delete configmap "${JOB_NAME}" "${JOB_NAME}-script" -n "${SIGN_NAMESPACE}" --ignore-not-found=true >/dev/null 2>&1 || true
  [ -z "${WORK}" ] || rm -rf "${WORK}"
}
trap cleanup EXIT
cleanup
WORK="$(mktemp -d)"
LOG="${WORK}/pod.log"

echo "--- manifest to sign ---"
cat "${MANIFEST}"

# The inputs are a few kilobytes: a short-lived ConfigMap carries them, the signatures come back through the pod log.
kubectl create configmap "${JOB_NAME}" -n "${SIGN_NAMESPACE}" \
  --from-file=DEB-SHA256SUMS="${MANIFEST}" --from-file=Release="${RELEASE}" \
  --dry-run=client -o yaml | kubectl apply --validate=false -f -

cat >"${WORK}/job.yaml" <<EOJOB
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${SIGN_NAMESPACE}
  labels:
    app: deb-release-sign
    ci-commit: "${CI_COMMIT_SHORT_SHA:-unknown}"
spec:
  ttlSecondsAfterFinished: 300
  activeDeadlineSeconds: $((PULL_TIMEOUT + SIGN_TIMEOUT))
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: deb-release-sign
    spec:
      serviceAccountName: ${CI_SERVICE_ACCOUNT}
      restartPolicy: Never
      imagePullSecrets:
        - name: ${HARBOR_PULL_SECRET}
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      initContainers:
        - name: prepare-tools
          image: ${SIGN_IMAGE}
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            runAsGroup: 1000
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
            seccompProfile:
              type: RuntimeDefault
          command: ["sh", "-c"]
          args:
            - |
              set -eu
              curl -fsSL --retry 3 "https://github.com/notaryproject/notation/releases/download/v${NOTATION_VERSION}/notation_${NOTATION_VERSION}_linux_amd64.tar.gz" -o /tmp/notation.tar.gz
              tar --no-same-owner -xzf /tmp/notation.tar.gz -C /tools notation
              chmod +x /tools/notation
              mkdir -p /tools/notation-plugins/azure-kv
              curl -fsSL --retry 3 "https://github.com/Azure/notation-azure-kv/releases/download/v${NOTATION_AZUREKV_VERSION}/notation-azure-kv_${NOTATION_AZUREKV_VERSION}_linux_amd64.tar.gz" -o /tmp/plugin.tar.gz
              tar --no-same-owner -xzf /tmp/plugin.tar.gz -C /tools/notation-plugins/azure-kv notation-azure-kv
              chmod +x /tools/notation-plugins/azure-kv/notation-azure-kv
              /tools/notation version | head -3
          resources:
            requests: {cpu: 125m, memory: 128Mi}
            limits: {cpu: 250m, memory: 256Mi}
          volumeMounts:
            - {name: tools, mountPath: /tools}
      containers:
        - name: sign
          image: ${POD_IMAGE}
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            runAsGroup: 1000
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
            seccompProfile:
              type: RuntimeDefault
          command: ["sh", "/scripts/deb-sign-pod.sh"]
          env:
            - name: NOTATION_KEY_ID
              valueFrom: {secretKeyRef: {name: notation-signing, key: key-id}}
            - name: AZURE_CLIENT_ID
              valueFrom: {secretKeyRef: {name: notation-signing, key: client-id}}
            - name: AZURE_CLIENT_SECRET
              valueFrom: {secretKeyRef: {name: notation-signing, key: client-secret}}
            - name: AZURE_TENANT_ID
              valueFrom: {secretKeyRef: {name: notation-signing, key: tenant-id}}
            - {name: KEY_VAULT_NAME, value: "${KEY_VAULT_NAME}"}
            - {name: APT_KEY_SECRET, value: "${APT_KEY_SECRET}"}
            - {name: APT_KEY_FINGERPRINT, value: "${APT_KEY_FINGERPRINT}"}
          resources:
            requests: {cpu: 250m, memory: 256Mi}
            limits: {cpu: 500m, memory: 512Mi}
          volumeMounts:
            - {name: tools, mountPath: /tools}
            - {name: inputs, mountPath: /work, readOnly: true}
            - {name: scripts, mountPath: /scripts, readOnly: true}
      volumes:
        - name: tools
          emptyDir: {}
        - name: inputs
          configMap:
            name: ${JOB_NAME}
        - name: scripts
          configMap:
            name: ${JOB_NAME}-script
EOJOB

kubectl create configmap "${JOB_NAME}-script" -n "${SIGN_NAMESPACE}" \
  --from-file=deb-sign-pod.sh="${HERE}/deb-sign-pod.sh" \
  --dry-run=client -o yaml | kubectl apply --validate=false -f -

dump_state() {
  kubectl get job -n "${SIGN_NAMESPACE}" "${JOB_NAME}" -o yaml 2>/dev/null || true
  kubectl describe pod -n "${SIGN_NAMESPACE}" -l "job-name=${JOB_NAME}" 2>/dev/null | tail -40 || true
  kubectl logs -n "${SIGN_NAMESPACE}" -l "job-name=${JOB_NAME}" --all-containers --tail=60 --prefix=true 2>/dev/null |
    grep -v '^SIGN-' | grep -v '^[A-Za-z0-9+/=]\{60,\}$' || true
}

echo "Applying signing job ${JOB_NAME}"
kubectl apply --validate=false -f "${WORK}/job.yaml"
waited=0
started=""
limit="${PULL_TIMEOUT}"
types=""
while [ "${waited}" -lt "${limit}" ]; do
  types="$(kubectl get job -n "${SIGN_NAMESPACE}" "${JOB_NAME}" -o jsonpath='{.status.conditions[*].type}' 2>/dev/null || true)"
  case " ${types} " in
    *" Complete "*) break ;;
    *" Failed "*) echo "=== signing job failed ==="; dump_state; exit 1 ;;
  esac
  for reason in $(kubectl get pods -n "${SIGN_NAMESPACE}" -l "job-name=${JOB_NAME}" -o jsonpath='{.items[*].status.containerStatuses[*].state.waiting.reason} {.items[*].status.initContainerStatuses[*].state.waiting.reason}' 2>/dev/null || true); do
    case "${reason}" in
      ImagePullBackOff | ErrImagePull | InvalidImageName)
        echo "FATAL: signing pod image pull failed (${reason}); check SIGN_IMAGE, POD_IMAGE and the pull secret" >&2
        dump_state
        exit 1
        ;;
    esac
  done
  if [ -z "${started}" ]; then
    # the signing budget starts once the sign container runs; until then the pod is pulling images
    state="$(kubectl get pods -n "${SIGN_NAMESPACE}" -l "job-name=${JOB_NAME}" -o jsonpath='{.items[*].status.containerStatuses[?(@.name=="sign")].state}' 2>/dev/null || true)"
    case "${state}" in
      *running* | *terminated*)
        started=1
        echo "sign container started after ${waited}s"
        limit=$((waited + SIGN_TIMEOUT))
        ;;
      *) [ $((waited % 60)) -ne 0 ] || [ "${waited}" -eq 0 ] || echo "still starting the signing pod after ${waited}s (pulling images)" ;;
    esac
  fi
  sleep 5
  waited=$((waited + 5))
done
case " ${types} " in
  *" Complete "*) echo "signing job completed" ;;
  *)
    if [ -z "${started}" ]; then echo "=== signing pod did not start within ${PULL_TIMEOUT}s ==="; else echo "=== signing job timed out ==="; fi
    dump_state
    exit 1
    ;;
esac

kubectl logs -n "${SIGN_NAMESPACE}" -l "job-name=${JOB_NAME}" -c sign --tail=-1 >"${LOG}"
mkdir -p "${OUTDIR}"
for name in DEB-SHA256SUMS.cose.sig InRelease Release.gpg; do
  awk -v n="${name}" '$1=="SIGN-BEGIN" && $2==n {on=1; next} /^SIGN-END/ {on=0} on' "${LOG}" | base64 -d >"${OUTDIR}/${name}"
  [ -s "${OUTDIR}/${name}" ] || { echo "ERROR: no ${name} in the signing pod log" >&2; exit 1; }
  echo "received ${name} ($(wc -c <"${OUTDIR}/${name}") bytes)"
done
