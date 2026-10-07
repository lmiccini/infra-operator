#!/bin/sh
# node-rejoin.sh - rejoin a blank-booted seed node to the surviving cluster.
#
# RabbitMQ 4.1+ makes the lowest StatefulSet ordinal (server-0) the peer
# discovery seed. If server-0 is recreated with a blank data directory (e.g. its
# PVC was replaced during node remediation) it forms its own standalone cluster
# instead of rejoining server-1/server-2. This PostStart hook detects that case
# (via a marker written by the init container only when the data dir was absent)
# and joins the node back to a surviving peer.
#
# It is survivor-safe: it only ever acts on server-0's own node, never touches
# the surviving quorum, and always exits 0 so a failed attempt simply retries on
# the next restart instead of crash-looping the container.
#
# Delivered to /operator (a shared emptyDir) by the setup init container. Runs
# only for 4.1+ multi-replica clusters. Required env: MY_POD_NAME,
# MY_POD_NAMESPACE, K8S_SERVICE_NAME, RABBITMQ_REPLICAS.
set -u

MARKER=/var/lib/rabbitmq/mnesia/.operator-fresh-node
[ -f "$MARKER" ] || exit 0                               # only a blank-PVC boot
case "$MY_POD_NAME" in *-server-0) ;; *) exit 0 ;; esac   # only the seed node

BASE="${MY_POD_NAME%-server-0}"
QUORUM=$(( RABBITMQ_REPLICAS / 2 + 1 ))
peer() { echo "rabbit@${BASE}-server-$1.${K8S_SERVICE_NAME}.${MY_POD_NAMESPACE}"; }

# Wait for the local node to finish booting so rabbitmqctl can talk to it.
rabbitmqctl await_startup -t 300 || exit 0

attempt=1
while [ "$attempt" -le 60 ]; do
  # Already a member of a quorum cluster? Nothing to do.
  if rabbitmqctl await_online_nodes "$QUORUM" -t 5 >/dev/null 2>&1; then
    rm -f "$MARKER"
    echo "node-rejoin: server-0 already in a cluster of >= $QUORUM nodes"
    exit 0
  fi

  # Find a surviving peer that itself reports quorum and join it. On 4.1+
  # join_cluster self-prepares (stop/reset); it is destructive to this blank
  # node's local state and bounces the app, dropping it from the Service.
  i=1
  while [ "$i" -lt "$RABBITMQ_REPLICAS" ]; do
    P="$(peer "$i")"
    if rabbitmqctl -n "$P" await_online_nodes "$QUORUM" -t 5 >/dev/null 2>&1; then
      echo "node-rejoin: joining server-0 to surviving cluster via $P"
      if rabbitmqctl join_cluster "$P"; then
        rm -f "$MARKER"
        echo "node-rejoin: server-0 rejoined via $P"
        exit 0
      fi
    fi
    i=$(( i + 1 ))
  done

  sleep 5
  attempt=$(( attempt + 1 ))
done

echo "node-rejoin: no surviving peer with quorum yet; keeping marker for next restart"
exit 0
