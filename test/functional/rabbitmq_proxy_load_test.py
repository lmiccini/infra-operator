#!/usr/bin/env python3
"""
RabbitMQ Proxy Load Test

Simulates a large number of concurrent compute nodes connecting to RabbitMQ
through the AMQP proxy to test scalability and performance.

Usage:
    # Basic test with 100 clients
    python3 rabbitmq_proxy_load_test.py --clients 100 --host rabbitmq.openstack.svc --port 5671

    # Full stress test with 1000 clients
    python3 rabbitmq_proxy_load_test.py --clients 1000 --host rabbitmq.openstack.svc --port 5671 --messages 100

    # Run from within a Kubernetes pod
    kubectl run load-test -it --rm --image=python:3.11 -- bash
    pip install pika psutil
    python3 rabbitmq_proxy_load_test.py --clients 1000 --host rabbitmq.openstack.svc
"""

import argparse
import asyncio
import ssl
import time
import sys
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional
import pika
import threading
import psutil
import os


@dataclass
class ConnectionMetrics:
    """Metrics for a single connection"""
    client_id: int
    connect_time: float
    success: bool
    error: Optional[str] = None
    messages_sent: int = 0
    messages_received: int = 0


@dataclass
class TestResults:
    """Overall test results"""
    total_clients: int
    successful_connections: int
    failed_connections: int
    total_time: float
    connect_times: List[float]
    errors: List[str]
    messages_sent: int = 0
    messages_received: int = 0
    peak_memory_mb: float = 0
    avg_cpu_percent: float = 0


class RabbitMQLoadTest:
    """Load tester for RabbitMQ proxy"""

    def __init__(self, host: str, port: int, username: str, password: str,
                 use_tls: bool = True, ca_cert: Optional[str] = None,
                 verify_ssl: bool = False):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.ca_cert = ca_cert
        self.verify_ssl = verify_ssl
        self.metrics: List[ConnectionMetrics] = []
        self.metrics_lock = threading.Lock()

    def create_connection_params(self) -> pika.ConnectionParameters:
        """Create pika connection parameters"""
        credentials = pika.PlainCredentials(self.username, self.password)

        if self.use_tls:
            ssl_context = ssl.create_default_context()
            if not self.verify_ssl:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            elif self.ca_cert:
                ssl_context.load_verify_locations(self.ca_cert)

            ssl_options = pika.SSLOptions(ssl_context, self.host)

            return pika.ConnectionParameters(
                host=self.host,
                port=self.port,
                credentials=credentials,
                ssl_options=ssl_options,
                heartbeat=600,
                blocked_connection_timeout=300,
            )
        else:
            return pika.ConnectionParameters(
                host=self.host,
                port=self.port,
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300,
            )

    def simulate_client(self, client_id: int, num_messages: int = 10) -> ConnectionMetrics:
        """
        Simulate a single compute node client

        This mimics what Nova/Neutron/etc do:
        1. Connect to RabbitMQ
        2. Declare exchanges and queues
        3. Send/receive some messages
        4. Keep connection open
        """
        start_time = time.time()
        metric = ConnectionMetrics(
            client_id=client_id,
            connect_time=0,
            success=False
        )

        try:
            # Connect
            params = self.create_connection_params()
            connection = pika.BlockingConnection(params)
            channel = connection.channel()

            connect_time = time.time() - start_time
            metric.connect_time = connect_time

            # Simulate OpenStack service behavior
            # 1. Declare exchanges (with durable=False to test proxy rewriting)
            exchange_name = f'compute_{client_id}'
            channel.exchange_declare(
                exchange=exchange_name,
                exchange_type='topic',
                durable=False,  # Proxy should rewrite this to True
                auto_delete=False
            )

            # 2. Declare queue (with durable=False to test proxy rewriting)
            queue_name = f'compute_{client_id}_queue'
            result = channel.queue_declare(
                queue=queue_name,
                durable=False,  # Proxy should rewrite this to True
                exclusive=False,
                auto_delete=False
            )

            # 3. Bind queue
            channel.queue_bind(
                exchange=exchange_name,
                queue=queue_name,
                routing_key=f'compute.{client_id}.#'
            )

            # 4. Send some messages
            for i in range(num_messages):
                channel.basic_publish(
                    exchange=exchange_name,
                    routing_key=f'compute.{client_id}.test',
                    body=f'Test message {i} from client {client_id}'.encode(),
                    properties=pika.BasicProperties(
                        delivery_mode=1  # Non-persistent (proxy should NOT rewrite this)
                    )
                )
                metric.messages_sent += 1

            # 5. Consume one message to test bidirectional flow
            method_frame, header_frame, body = channel.basic_get(queue=queue_name, auto_ack=True)
            if method_frame:
                metric.messages_received += 1

            metric.success = True

            # Keep connection open briefly to simulate real usage
            time.sleep(0.1)

            # Clean up
            channel.queue_delete(queue=queue_name)
            channel.exchange_delete(exchange=exchange_name)
            connection.close()

        except Exception as e:
            metric.success = False
            metric.error = str(e)

        return metric

    def run_load_test(self, num_clients: int, num_messages: int = 10,
                     max_workers: int = 100, ramp_up_seconds: int = 0) -> TestResults:
        """
        Run load test with specified number of clients

        Args:
            num_clients: Number of concurrent clients to simulate
            num_messages: Messages per client to send
            max_workers: Max thread pool size
            ramp_up_seconds: Time to gradually ramp up connections (0 = all at once)
        """
        print(f"\n{'='*70}")
        print(f"Starting Load Test")
        print(f"{'='*70}")
        print(f"Clients: {num_clients}")
        print(f"Messages per client: {num_messages}")
        print(f"Max workers: {max_workers}")
        print(f"Ramp up: {ramp_up_seconds}s")
        print(f"Target: {self.host}:{self.port}")
        print(f"TLS: {self.use_tls}")
        print(f"{'='*70}\n")

        # Start resource monitoring
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss / 1024 / 1024  # MB
        cpu_samples = []

        start_time = time.time()

        # Create thread pool
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []

            # Submit client simulations
            for client_id in range(num_clients):
                # Ramp up gradually if specified
                if ramp_up_seconds > 0 and client_id > 0:
                    delay = (client_id / num_clients) * ramp_up_seconds
                    time.sleep(delay)

                future = executor.submit(self.simulate_client, client_id, num_messages)
                futures.append(future)

                # Print progress
                if (client_id + 1) % 50 == 0:
                    elapsed = time.time() - start_time
                    cpu_samples.append(process.cpu_percent(interval=0.1))
                    current_memory = process.memory_info().rss / 1024 / 1024
                    print(f"  Launched {client_id + 1}/{num_clients} clients "
                          f"({elapsed:.1f}s, {current_memory:.1f}MB)")

            # Wait for all clients to complete
            print(f"\nWaiting for all clients to complete...")
            for i, future in enumerate(futures):
                metric = future.result()
                with self.metrics_lock:
                    self.metrics.append(metric)

                # Sample CPU periodically
                if i % 10 == 0:
                    cpu_samples.append(process.cpu_percent(interval=0.1))

                if (i + 1) % 50 == 0:
                    completed = i + 1
                    success = sum(1 for m in self.metrics if m.success)
                    failed = completed - success
                    print(f"  Completed: {completed}/{num_clients} "
                          f"(Success: {success}, Failed: {failed})")

        total_time = time.time() - start_time

        # Final resource measurements
        final_memory = process.memory_info().rss / 1024 / 1024
        peak_memory = max(initial_memory, final_memory)
        avg_cpu = statistics.mean(cpu_samples) if cpu_samples else 0

        # Compile results
        successful = [m for m in self.metrics if m.success]
        failed = [m for m in self.metrics if not m.success]

        results = TestResults(
            total_clients=num_clients,
            successful_connections=len(successful),
            failed_connections=len(failed),
            total_time=total_time,
            connect_times=[m.connect_time for m in successful],
            errors=[m.error for m in failed if m.error],
            messages_sent=sum(m.messages_sent for m in successful),
            messages_received=sum(m.messages_received for m in successful),
            peak_memory_mb=peak_memory,
            avg_cpu_percent=avg_cpu
        )

        return results

    @staticmethod
    def print_results(results: TestResults):
        """Print formatted test results"""
        print(f"\n{'='*70}")
        print(f"Test Results")
        print(f"{'='*70}")
        print(f"Total clients:           {results.total_clients}")
        print(f"Successful connections:  {results.successful_connections}")
        print(f"Failed connections:      {results.failed_connections}")
        print(f"Success rate:            {results.successful_connections/results.total_clients*100:.1f}%")
        print(f"Total test time:         {results.total_time:.2f}s")
        print(f"\nConnection Times:")
        if results.connect_times:
            print(f"  Min:      {min(results.connect_times):.3f}s")
            print(f"  Max:      {max(results.connect_times):.3f}s")
            print(f"  Mean:     {statistics.mean(results.connect_times):.3f}s")
            print(f"  Median:   {statistics.median(results.connect_times):.3f}s")
            if len(results.connect_times) > 1:
                print(f"  Std Dev:  {statistics.stdev(results.connect_times):.3f}s")

        print(f"\nThroughput:")
        print(f"  Connections/sec:  {results.successful_connections/results.total_time:.1f}")
        print(f"  Messages sent:    {results.messages_sent}")
        print(f"  Messages recv:    {results.messages_received}")

        print(f"\nClient Resource Usage:")
        print(f"  Peak memory:      {results.peak_memory_mb:.1f} MB")
        print(f"  Avg CPU:          {results.avg_cpu_percent:.1f}%")

        if results.errors:
            print(f"\nErrors ({len(results.errors)} total):")
            # Show unique errors with counts
            from collections import Counter
            error_counts = Counter(results.errors)
            for error, count in error_counts.most_common(5):
                print(f"  [{count}x] {error[:80]}")

        print(f"{'='*70}\n")


def main():
    parser = argparse.ArgumentParser(
        description='Load test for RabbitMQ AMQP proxy',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--host', default='rabbitmq.openstack.svc',
                       help='RabbitMQ host')
    parser.add_argument('--port', type=int, default=5671,
                       help='RabbitMQ port')
    parser.add_argument('--username', default='guest',
                       help='RabbitMQ username')
    parser.add_argument('--password', default='guest',
                       help='RabbitMQ password')
    parser.add_argument('--clients', type=int, default=100,
                       help='Number of concurrent clients to simulate')
    parser.add_argument('--messages', type=int, default=10,
                       help='Number of messages per client')
    parser.add_argument('--max-workers', type=int, default=100,
                       help='Thread pool size')
    parser.add_argument('--ramp-up', type=int, default=0,
                       help='Ramp up time in seconds (0 = all at once)')
    parser.add_argument('--no-tls', action='store_true',
                       help='Disable TLS')
    parser.add_argument('--ca-cert',
                       help='Path to CA certificate')
    parser.add_argument('--verify-ssl', action='store_true',
                       help='Verify SSL certificate')

    args = parser.parse_args()

    # Check dependencies
    try:
        import pika
        import psutil
    except ImportError as e:
        print(f"Error: Missing required package: {e}")
        print("\nInstall with: pip install pika psutil")
        sys.exit(1)

    # Create tester
    tester = RabbitMQLoadTest(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        use_tls=not args.no_tls,
        ca_cert=args.ca_cert,
        verify_ssl=args.verify_ssl
    )

    # Run test
    try:
        results = tester.run_load_test(
            num_clients=args.clients,
            num_messages=args.messages,
            max_workers=args.max_workers,
            ramp_up_seconds=args.ramp_up
        )

        # Print results
        tester.print_results(results)

        # Exit with error code if too many failures
        failure_rate = results.failed_connections / results.total_clients
        if failure_rate > 0.05:  # More than 5% failures
            print(f"ERROR: Failure rate too high ({failure_rate*100:.1f}%)")
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(130)


if __name__ == '__main__':
    main()
