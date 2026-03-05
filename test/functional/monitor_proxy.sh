#!/bin/bash
#
# Monitor RabbitMQ proxy resources during load testing
#
# Usage:
#   ./monitor_proxy.sh rabbitmq-server-0
#

POD_NAME="${1:-rabbitmq-server-0}"
NAMESPACE="${2:-openstack}"
INTERVAL="${3:-5}"

echo "Monitoring proxy in pod: $POD_NAME (namespace: $NAMESPACE)"
echo "Sample interval: ${INTERVAL}s"
echo "Press Ctrl+C to stop"
echo ""
echo "Timestamp,Memory(MB),CPU(%),Connections,QueueRewrites,ExchangeRewrites,BytesForwarded"

while true; do
    TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

    # Get memory usage from proxy container
    MEMORY=$(kubectl exec -n "$NAMESPACE" "$POD_NAME" -c amqp-proxy -- \
        sh -c 'cat /proc/1/status | grep VmRSS | awk "{print \$2/1024}"' 2>/dev/null)

    # Get CPU usage (from /proc/stat)
    CPU=$(kubectl exec -n "$NAMESPACE" "$POD_NAME" -c amqp-proxy -- \
        sh -c 'ps aux | grep "python3 /scripts/proxy.py" | grep -v grep | awk "{print \$3}"' 2>/dev/null)

    # Get proxy stats from logs
    STATS=$(kubectl logs -n "$NAMESPACE" "$POD_NAME" -c amqp-proxy --tail=10 2>/dev/null | \
        grep -A 4 "Proxy Statistics" | tail -4)

    CONNECTIONS=$(echo "$STATS" | grep "Total connections" | awk '{print $NF}')
    QUEUE_REWRITES=$(echo "$STATS" | grep "Queue rewrites" | awk '{print $NF}')
    EXCHANGE_REWRITES=$(echo "$STATS" | grep "Exchange rewrites" | awk '{print $NF}')
    BYTES=$(echo "$STATS" | grep "Bytes forwarded" | awk '{print $NF}')

    echo "$TIMESTAMP,$MEMORY,$CPU,$CONNECTIONS,$QUEUE_REWRITES,$EXCHANGE_REWRITES,$BYTES"

    sleep "$INTERVAL"
done
