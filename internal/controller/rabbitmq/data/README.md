# RabbitMQ Controller Data Files

This directory contains embedded data files used by the RabbitMQ controller.

## Files

- **proxy.py** - AMQP durability proxy deployed as a sidecar during RabbitMQ upgrades. Rewrites AMQP frames to force `durable=True`, enabling non-durable clients to work with quorum queues. Embedded at build time via Go's `embed` directive.
- **test_proxy_unit.py** - Unit tests for proxy.py (AMQP frame parsing, rewriting logic). No RabbitMQ needed.
- **proxy_test.py** - Integration test simulating an Oslo messaging client through the proxy against a live RabbitMQ.

## Modifying the Proxy

1. Edit `proxy.py`
2. Rebuild: `make`
3. Run unit tests: `python3 -m pytest test_proxy_unit.py -v`
4. Run operator tests: `make test`

The proxy is embedded at compile time, so changes require a rebuild.

For complete documentation, see [docs/PROXY_INTEGRATION.md](../../../docs/PROXY_INTEGRATION.md).
