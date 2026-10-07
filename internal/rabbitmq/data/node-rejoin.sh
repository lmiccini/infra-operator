#!/bin/sh
# node-rejoin.sh - rejoin a blank-booted seed node to the surviving cluster.
#
# RabbitMQ 4.1+ makes the lowest StatefulSet ordinal (server-0) the peer
# discovery seed. If server-0 is recreated with a blank data directory (e.g. its
# PVC was replaced during node remediation) it forms its own standalone cluster
# instead of rejoining server-1/server-2. This PostStart hook detects that case
# (via a marker written by the init container only when the data dir was absent)
# and joins the node back to a surviving peer, then re-adds it to the quorum
# queues it was removed from.
#
# It is survivor-safe: it only ever acts on server-0's own node, never touches
# the surviving quorum.
#
# Exposure safety: once we know this is a blank server-0 that must rejoin, every
# path that does NOT achieve membership exits non-zero. A failing PostStart hook
# makes the kubelet kill the container before it reaches Running, so the TCP
# readiness probe never passes and a still-standalone node is never added to the
# Service. The container then restarts and the hook retries. Only a confirmed
# cluster member exits 0 and is allowed to become Ready.
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
# If it never boots we have not rejoined, so fail (restart) rather than expose.
rabbitmqctl await_startup -t 300 || exit 1

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
        echo "node-rejoin: server-0 rejoined via $P"
        # Rejoining the cluster does NOT restore quorum-queue replicas:
        # forget_cluster_node dropped server-0 from every quorum queue's member
        # list, and a rejoined node only hosts a queue's replica once it is added
        # back. `grow <self> all` re-adds this node to all quorum queues now,
        # instead of waiting for continuous membership reconciliation (CMR) to do
        # it on its own interval. Best-effort: CMR (enabled in the operator
        # config) is the backstop, and strict replica-count verification belongs
        # in an e2e test, not this hook.
        SELF="rabbit@${MY_POD_NAME}.${K8S_SERVICE_NAME}.${MY_POD_NAMESPACE}"
        rabbitmq-queues grow "$SELF" all ||
          echo "node-rejoin: quorum-queue grow failed; leaving replica regrowth to CMR"
        rm -f "$MARKER"
        exit 0
      fi
    fi
    i=$(( i + 1 ))
  done

  sleep 5
  attempt=$(( attempt + 1 ))
done

echo "node-rejoin: could not rejoin a surviving cluster; failing so the container restarts and retries (never exposing a standalone node)"
exit 1
