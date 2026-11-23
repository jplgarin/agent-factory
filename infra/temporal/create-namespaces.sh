#!/bin/sh
# One Temporal namespace per space (ADR 0001). Idempotent.
set -eu
: "${TEMPORAL_ADDRESS:?}" "${AF_NAMESPACES:?}"

attempt=0
until temporal operator cluster health --address "$TEMPORAL_ADDRESS" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -ge 30 ] && { echo 'temporal not healthy'; exit 1; }
  sleep 2
done

for ns in $AF_NAMESPACES; do
  if temporal operator namespace describe -n "$ns" --address "$TEMPORAL_ADDRESS" >/dev/null 2>&1; then
    echo "namespace $ns exists"
  else
    temporal operator namespace create -n "$ns" --retention 7d --address "$TEMPORAL_ADDRESS"
    echo "namespace $ns created"
  fi
done
