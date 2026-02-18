# RabbitMQ Proxy Load Testing Guide

This guide explains how to load test the RabbitMQ AMQP proxy to validate performance with large numbers of concurrent connections (simulating 1000+ compute nodes).

## Overview

The load test simulates OpenStack compute nodes connecting to RabbitMQ through the proxy:
- Establishes concurrent TLS connections
- Declares exchanges and queues (testing proxy rewriting)
- Sends/receives AMQP messages
- Measures connection times, throughput, and resource usage

## Prerequisites

1. **Running RabbitMQ cluster with proxy enabled**
2. **Python 3.9+** with packages:
   ```bash
   pip install pika psutil
   ```

## Quick Start

### 1. Get RabbitMQ Credentials

```bash
# Get username and password
kubectl get secret rabbitmq-default-user -n openstack -o jsonpath='{.data.username}' | base64 -d
kubectl get secret rabbitmq-default-user -n openstack -o jsonpath='{.data.password}' | base64 -d
```

### 2. Run a Small Test (100 clients)

```bash
# From a pod inside the cluster
kubectl run load-test -n openstack -it --rm --image=python:3.11 -- bash

# Inside the pod
pip install pika psutil
python3 rabbitmq_proxy_load_test.py \
  --host rabbitmq.openstack.svc \
  --port 5671 \
  --username <username> \
  --password <password> \
  --clients 100 \
  --messages 10
```

### 3. Run Full Scale Test (1000 clients)

```bash
python3 rabbitmq_proxy_load_test.py \
  --host rabbitmq.openstack.svc \
  --port 5671 \
  --username <username> \
  --password <password> \
  --clients 1000 \
  --messages 50 \
  --ramp-up 10
```

## Monitoring During Tests

### Monitor Proxy Resources

In a separate terminal, watch the proxy's resource usage:

```bash
# Make script executable
chmod +x monitor_proxy.sh

# Run monitor
./monitor_proxy.sh rabbitmq-server-0 openstack 5

# Output: CSV with timestamp, memory, CPU, connections, etc.
# Redirect to file for analysis:
./monitor_proxy.sh rabbitmq-server-0 openstack 5 > proxy_metrics.csv
```

### Monitor Proxy Logs

```bash
# Watch proxy logs in real-time
kubectl logs -n openstack rabbitmq-server-0 -c amqp-proxy -f
```

### Monitor RabbitMQ

```bash
# Check RabbitMQ connections
kubectl exec -n openstack rabbitmq-server-0 -c rabbitmq -- \
  rabbitmqctl list_connections --formatter json | jq '.[] | .peer_host' | sort | uniq -c

# Check queue stats
kubectl exec -n openstack rabbitmq-server-0 -c rabbitmq -- \
  rabbitmqctl list_queues name messages consumers
```

## Test Scenarios

### Scenario 1: Connection Storm (Worst Case)

Simulate all compute nodes restarting simultaneously:

```bash
python3 rabbitmq_proxy_load_test.py \
  --clients 1000 \
  --messages 10 \
  --ramp-up 0 \
  --max-workers 200
```

**Expected:**
- All connections establish successfully
- Proxy memory stays under 1.5GB
- Connection times under 2s each

### Scenario 2: Gradual Ramp-Up (Typical)

Simulate compute nodes booting over 60 seconds:

```bash
python3 rabbitmq_proxy_load_test.py \
  --clients 1000 \
  --messages 50 \
  --ramp-up 60 \
  --max-workers 100
```

**Expected:**
- Smooth connection establishment
- Lower peak memory usage
- Consistent connection times

### Scenario 3: High Message Throughput

Simulate high messaging workload (e.g., Ceilometer):

```bash
python3 rabbitmq_proxy_load_test.py \
  --clients 500 \
  --messages 500 \
  --ramp-up 30
```

**Expected:**
- Tests proxy's message forwarding performance
- CPU usage will be higher (AMQP frame parsing)
- Validates queue/exchange rewriting at scale

### Scenario 4: Sustained Load

Keep connections open for extended period:

Modify the script to add `time.sleep(300)` before closing connections, then:

```bash
python3 rabbitmq_proxy_load_test.py \
  --clients 1000 \
  --messages 10
```

**Expected:**
- Tests long-lived connection stability
- Memory should stay stable (no leaks)
- Validates connection pooling behavior

## Interpreting Results

### Success Criteria

✅ **Pass:**
- Success rate > 95%
- Mean connection time < 1s
- Peak proxy memory < 1.5GB (with 2GB limit)
- No connection timeouts or errors

⚠️ **Warning:**
- Success rate 90-95%
- Connection times 1-3s
- Memory 1.5-1.8GB
- Occasional timeout errors

❌ **Fail:**
- Success rate < 90%
- Connection times > 3s
- Memory approaching 2GB limit
- Frequent errors or crashes

### Common Issues

**High Connection Failures:**
- Check RabbitMQ is healthy: `kubectl exec rabbitmq-server-0 -c rabbitmq -- rabbitmqctl status`
- Verify proxy is running: `kubectl logs rabbitmq-server-0 -c amqp-proxy`
- Check for network issues

**High Memory Usage:**
- Each connection uses ~200KB
- 1000 connections = ~200MB baseline
- High memory suggests connection leaks - check proxy logs for errors

**Slow Connection Times:**
- TLS handshake overhead
- RabbitMQ backend overloaded
- Network latency

**CPU Saturation:**
- AMQP frame parsing is CPU-intensive
- Consider increasing proxy CPU limits
- Or reduce message throughput per connection

## Scaling Recommendations

Based on test results:

| Compute Nodes | RabbitMQ Pods | Proxy Memory | Proxy CPU |
|---------------|---------------|--------------|-----------|
| 100-300       | 3             | 512Mi        | 500m      |
| 300-600       | 3-5           | 1Gi          | 1000m     |
| 600-1000      | 5             | 2Gi          | 2000m     |
| 1000+         | 5-7           | 2Gi          | 2000m     |

## Troubleshooting

### Test Client Issues

**"Connection refused":**
- Check service exists: `kubectl get svc rabbitmq -n openstack`
- Verify port: Should be 5671 for TLS

**"SSL handshake failed":**
- Use `--no-tls` to test without TLS first
- Check certificate validity

**"Authentication failed":**
- Verify credentials are correct
- Check RabbitMQ user exists: `kubectl exec rabbitmq-server-0 -c rabbitmq -- rabbitmqctl list_users`

### Proxy Issues

**Proxy not logging connections:**
- Verify proxy is listening: `kubectl exec rabbitmq-server-0 -c amqp-proxy -- netstat -tlnp | grep 5671`
- Check for TLS cert issues in logs

**Proxy crashing (OOMKilled):**
- Increase memory limits in proxy.go
- Reduce concurrent clients in test

**Connection timeouts:**
- RabbitMQ backend might be overloaded
- Check RabbitMQ resource usage
- Scale RabbitMQ cluster

## Example Output

```
======================================================================
Starting Load Test
======================================================================
Clients: 1000
Messages per client: 50
Max workers: 100
Ramp up: 10s
Target: rabbitmq.openstack.svc:5671
TLS: True
======================================================================

  Launched 50/1000 clients (2.1s, 245.3MB)
  Launched 100/1000 clients (4.3s, 412.7MB)
  ...
  Completed: 1000/1000 (Success: 998, Failed: 2)

======================================================================
Test Results
======================================================================
Total clients:           1000
Successful connections:  998
Failed connections:      2
Success rate:            99.8%
Total test time:         45.23s

Connection Times:
  Min:      0.245s
  Max:      2.134s
  Mean:     0.687s
  Median:   0.612s
  Std Dev:  0.234s

Throughput:
  Connections/sec:  22.1
  Messages sent:    49900
  Messages recv:    998

Client Resource Usage:
  Peak memory:      856.3 MB
  Avg CPU:          45.2%

Errors (2 total):
  [2x] Connection timeout after 5s
======================================================================
```

## Advanced: Profile Proxy Memory

To get detailed memory profiling of the proxy script itself:

```bash
# Modify proxy container to use memory_profiler
kubectl exec -n openstack rabbitmq-server-0 -c amqp-proxy -- \
  pip install memory-profiler

# Add @profile decorator to handle_client method
# Run with: python -m memory_profiler /scripts/proxy.py
```

## Cleanup

After testing:

```bash
# Delete test queues/exchanges if any remain
kubectl exec rabbitmq-server-0 -c rabbitmq -- \
  rabbitmqctl list_queues | grep compute_ | awk '{print $1}' | \
  xargs -I {} kubectl exec rabbitmq-server-0 -c rabbitmq -- rabbitmqctl delete_queue {}
```

## Next Steps

After validating proxy performance:

1. **Document findings** - Record peak memory, CPU, connection limits
2. **Update resource limits** - Adjust based on actual usage
3. **Plan migration** - Schedule OpenStack client reconfiguration
4. **Monitor production** - Watch proxy metrics during real rollout
5. **Remove proxy** - Once all clients use durable queues, remove the proxy sidecar
