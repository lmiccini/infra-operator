#!/bin/bash
#
# Quick load test runner for RabbitMQ proxy
# Automatically gets credentials and runs the test
#

set -e

NAMESPACE="${NAMESPACE:-openstack}"
POD_NAME="${POD_NAME:-rabbitmq-server-0}"
CLIENTS="${CLIENTS:-100}"
MESSAGES="${MESSAGES:-10}"
RAMP_UP="${RAMP_UP:-0}"

echo "RabbitMQ Proxy Load Test Runner"
echo "================================"
echo ""
echo "Configuration:"
echo "  Namespace: $NAMESPACE"
echo "  Clients:   $CLIENTS"
echo "  Messages:  $MESSAGES"
echo "  Ramp-up:   ${RAMP_UP}s"
echo ""

# Get RabbitMQ credentials
echo "Getting RabbitMQ credentials..."
SECRET_NAME="rabbitmq-default-user"

if ! kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" &>/dev/null; then
    echo "Error: Secret $SECRET_NAME not found in namespace $NAMESPACE"
    exit 1
fi

USERNAME=$(kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" -o jsonpath='{.data.username}' | base64 -d)
PASSWORD=$(kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" -o jsonpath='{.data.password}' | base64 -d)

if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
    echo "Error: Could not retrieve credentials from secret"
    exit 1
fi

echo "Credentials retrieved: $USERNAME / ***"
echo ""

# Check if pika is installed
if ! python3 -c "import pika" 2>/dev/null; then
    echo "Installing required Python packages..."
    pip install pika psutil
    echo ""
fi

# Determine RabbitMQ service host and port
SERVICE_NAME="rabbitmq"
HOST="${HOST:-${SERVICE_NAME}.${NAMESPACE}.svc}"
PORT="${PORT:-5671}"

echo "Target: $HOST:$PORT"
echo ""

# Check if we should run in-cluster or spawn a pod
if kubectl get pod -n "$NAMESPACE" 2>/dev/null | grep -q "^NAME"; then
    # We're running locally, need to create a pod
    echo "Running test from a temporary pod..."
    echo ""

    # Create a test pod
    kubectl run rabbitmq-load-test \
        -n "$NAMESPACE" \
        -it --rm \
        --image=python:3.11-slim \
        --restart=Never \
        --command -- bash -c "
            pip install pika psutil > /dev/null 2>&1
            cat > /tmp/load_test.py << 'EOFPY'
$(cat "$(dirname "$0")/rabbitmq_proxy_load_test.py")
EOFPY
            python3 /tmp/load_test.py \
                --host '$HOST' \
                --port '$PORT' \
                --username '$USERNAME' \
                --password '$PASSWORD' \
                --clients '$CLIENTS' \
                --messages '$MESSAGES' \
                --ramp-up '$RAMP_UP' \
                --max-workers 100
        "
else
    # We're inside a pod already, run directly
    echo "Running test locally..."
    echo ""

    SCRIPT_DIR="$(dirname "$0")"
    python3 "$SCRIPT_DIR/rabbitmq_proxy_load_test.py" \
        --host "$HOST" \
        --port "$PORT" \
        --username "$USERNAME" \
        --password "$PASSWORD" \
        --clients "$CLIENTS" \
        --messages "$MESSAGES" \
        --ramp-up "$RAMP_UP" \
        --max-workers 100
fi

echo ""
echo "Test complete!"
echo ""
echo "To monitor proxy during tests, run in another terminal:"
echo "  ./monitor_proxy.sh $POD_NAME $NAMESPACE"
