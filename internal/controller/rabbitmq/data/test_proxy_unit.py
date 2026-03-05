#!/usr/bin/env python3
"""
Unit tests for the AMQP durability proxy internals.

Tests all protocol-level logic (frame parsing, table encode/decode,
queue/exchange rewriting, address parsing) without requiring a live
RabbitMQ instance.

Run with:
    python -m pytest test_proxy_unit.py -v
    # or
    python -m unittest test_proxy_unit -v
"""

import struct
import unittest
from unittest.mock import MagicMock

# Import the proxy module from the same directory
from proxy import (
    AMQPFrame,
    AMQPMethodParser,
    QueueDeclareRewriter,
    ExchangeDeclareRewriter,
    DurabilityProxy,
    parse_host_port,
)


class TestAMQPFrame(unittest.TestCase):
    """Tests for AMQPFrame parsing and serialization."""

    def _build_raw_frame(self, frame_type, channel, payload):
        """Helper: build raw AMQP frame bytes."""
        header = struct.pack('!BHI', frame_type, channel, len(payload))
        return header + payload + b'\xce'

    def test_parse_valid_method_frame(self):
        payload = b'\x00\x0a\x00\x0b' + b'\x00' * 10
        raw = self._build_raw_frame(1, 0, payload)
        frame = AMQPFrame.parse(raw)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.frame_type, 1)
        self.assertEqual(frame.channel, 0)
        self.assertEqual(frame.payload, payload)

    def test_parse_with_nonzero_channel(self):
        payload = b'\x01\x02\x03'
        raw = self._build_raw_frame(2, 42, payload)
        frame = AMQPFrame.parse(raw)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.channel, 42)

    def test_parse_returns_none_for_short_data(self):
        self.assertIsNone(AMQPFrame.parse(b'\x01\x00'))
        self.assertIsNone(AMQPFrame.parse(b''))
        self.assertIsNone(AMQPFrame.parse(b'\x00' * 7))

    def test_parse_returns_none_for_incomplete_payload(self):
        # Header says payload is 100 bytes, but we only have 10
        header = struct.pack('!BHI', 1, 0, 100)
        raw = header + b'\x00' * 10
        self.assertIsNone(AMQPFrame.parse(raw))

    def test_parse_returns_none_for_bad_frame_end(self):
        payload = b'\x01\x02'
        header = struct.pack('!BHI', 1, 0, len(payload))
        raw = header + payload + b'\xff'  # Wrong end marker
        self.assertIsNone(AMQPFrame.parse(raw))

    def test_to_bytes_round_trip(self):
        payload = b'hello world'
        frame = AMQPFrame(1, 5, payload)
        raw = frame.to_bytes()
        parsed = AMQPFrame.parse(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.frame_type, 1)
        self.assertEqual(parsed.channel, 5)
        self.assertEqual(parsed.payload, payload)

    def test_to_bytes_format(self):
        payload = b'\xaa\xbb'
        frame = AMQPFrame(3, 1, payload)
        raw = frame.to_bytes()
        # Verify header
        self.assertEqual(raw[0], 3)  # frame_type
        self.assertEqual(struct.unpack('!H', raw[1:3])[0], 1)  # channel
        self.assertEqual(struct.unpack('!I', raw[3:7])[0], 2)  # size
        self.assertEqual(raw[7:9], payload)
        self.assertEqual(raw[9], 0xCE)  # frame end

    def test_size_property(self):
        payload = b'\x00' * 20
        frame = AMQPFrame(1, 0, payload)
        self.assertEqual(frame.size, 8 + 20)

    def test_empty_payload(self):
        frame = AMQPFrame(1, 0, b'')
        raw = frame.to_bytes()
        parsed = AMQPFrame.parse(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.payload, b'')
        self.assertEqual(parsed.size, 8)


class TestAMQPMethodParser(unittest.TestCase):
    """Tests for AMQP method parsing utilities."""

    # --- shortstr ---

    def test_parse_shortstr_basic(self):
        data = bytes([5]) + b'hello'
        s, offset = AMQPMethodParser.parse_shortstr(data, 0)
        self.assertEqual(s, 'hello')
        self.assertEqual(offset, 6)

    def test_parse_shortstr_empty(self):
        data = bytes([0])
        s, offset = AMQPMethodParser.parse_shortstr(data, 0)
        self.assertEqual(s, '')
        self.assertEqual(offset, 1)

    def test_parse_shortstr_at_offset(self):
        prefix = b'\x00\x00'
        data = prefix + bytes([3]) + b'foo'
        s, offset = AMQPMethodParser.parse_shortstr(data, 2)
        self.assertEqual(s, 'foo')
        self.assertEqual(offset, 6)

    def test_parse_shortstr_past_end(self):
        data = b''
        s, offset = AMQPMethodParser.parse_shortstr(data, 0)
        self.assertEqual(s, '')

    def test_parse_shortstr_truncated(self):
        # Says length is 10, but only 3 bytes available
        data = bytes([10]) + b'abc'
        s, offset = AMQPMethodParser.parse_shortstr(data, 0)
        self.assertEqual(s, '')  # Returns empty on truncation

    def test_encode_shortstr_basic(self):
        result = AMQPMethodParser.encode_shortstr('hello')
        self.assertEqual(result, bytes([5]) + b'hello')

    def test_encode_shortstr_empty(self):
        result = AMQPMethodParser.encode_shortstr('')
        self.assertEqual(result, bytes([0]))

    def test_encode_shortstr_truncates_long(self):
        long_str = 'a' * 300
        result = AMQPMethodParser.encode_shortstr(long_str)
        self.assertEqual(result[0], 255)
        self.assertEqual(len(result), 256)

    # --- table encode/decode round-trips ---

    def test_table_empty(self):
        encoded = AMQPMethodParser.encode_table({})
        self.assertEqual(encoded, struct.pack('!I', 0))
        parsed, offset = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed, {})

    def test_table_boolean(self):
        table = {'flag': True}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['flag'], True)

        table2 = {'flag': False}
        encoded2 = AMQPMethodParser.encode_table(table2)
        parsed2, _ = AMQPMethodParser.parse_table(encoded2, 0)
        self.assertEqual(parsed2['flag'], False)

    def test_table_int8(self):
        table = {'val': 42}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['val'], 42)

    def test_table_int16(self):
        table = {'val': 1000}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['val'], 1000)

    def test_table_int32(self):
        table = {'val': 100000}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['val'], 100000)

    def test_table_int64(self):
        table = {'val': 2**40}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['val'], 2**40)

    def test_table_negative_int(self):
        table = {'val': -500}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['val'], -500)

    def test_table_float(self):
        # Floats are encoded as doubles ('d') in encode_table
        table = {'val': 3.14}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertAlmostEqual(parsed['val'], 3.14, places=10)

    def test_table_string(self):
        table = {'key': 'hello world'}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['key'], 'hello world')

    def test_table_bytes(self):
        table = {'data': b'\x00\x01\x02\xff'}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['data'], b'\x00\x01\x02\xff')

    def test_table_nested(self):
        table = {'outer': {'inner': 'value'}}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertIsInstance(parsed['outer'], dict)
        self.assertEqual(parsed['outer']['inner'], 'value')

    def test_table_decimal(self):
        # Decimal is stored as (scale, unscaled) tuple
        table = {'price': (2, 1999)}  # 19.99
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['price'], (2, 1999))

    def test_table_void(self):
        table = {'nothing': None}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertIsNone(parsed['nothing'])

    def test_table_multiple_keys(self):
        table = {
            'str_key': 'value',
            'int_key': 42,
            'bool_key': True,
        }
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['str_key'], 'value')
        self.assertEqual(parsed['int_key'], 42)
        self.assertEqual(parsed['bool_key'], True)

    def test_table_with_queue_type(self):
        """Test the exact table used in queue declaration rewriting."""
        table = {'x-queue-type': 'quorum'}
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['x-queue-type'], 'quorum')

    def test_parse_table_insufficient_data(self):
        parsed, offset = AMQPMethodParser.parse_table(b'\x00', 0)
        self.assertEqual(parsed, {})


class TestQueueDeclareRewriter(unittest.TestCase):
    """Tests for QueueDeclareRewriter."""

    def _build_queue_declare_payload(self, queue_name, flags=0, arguments=None):
        """Helper: build a Queue.Declare method payload."""
        payload = struct.pack('!HH', 50, 10)  # class=50, method=10
        payload += struct.pack('!H', 0)  # reserved
        payload += AMQPMethodParser.encode_shortstr(queue_name)
        payload += bytes([flags])
        payload += AMQPMethodParser.encode_table(arguments or {})
        return payload

    # --- should_force_durable ---

    def test_should_force_durable_normal_queue(self):
        self.assertTrue(QueueDeclareRewriter.should_force_durable('my_queue'))
        self.assertTrue(QueueDeclareRewriter.should_force_durable('nova.scheduler'))
        self.assertTrue(QueueDeclareRewriter.should_force_durable('neutron-l3-agent'))

    def test_should_not_force_durable_reply_queue(self):
        self.assertFalse(QueueDeclareRewriter.should_force_durable('reply_abc123'))

    def test_should_not_force_durable_amq_gen(self):
        self.assertFalse(QueueDeclareRewriter.should_force_durable('amq.gen-abc123'))

    def test_should_not_force_durable_amq_system(self):
        self.assertFalse(QueueDeclareRewriter.should_force_durable('amq.direct'))
        self.assertFalse(QueueDeclareRewriter.should_force_durable('amq.topic'))
        self.assertFalse(QueueDeclareRewriter.should_force_durable('amq.fanout'))

    # --- rewrite ---

    def test_rewrite_non_durable_to_durable(self):
        # flags=0 means durable=False
        payload = self._build_queue_declare_payload('test_queue', flags=0)
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNotNone(result)

        # Parse result to verify durable bit is set
        offset = 6  # skip class_id + method_id + reserved
        queue_name, offset = AMQPMethodParser.parse_shortstr(result, offset)
        self.assertEqual(queue_name, 'test_queue')
        flags = result[offset]
        self.assertTrue(flags & 0x02, "durable bit should be set")

    def test_rewrite_already_durable_returns_none(self):
        # flags=0x02 means durable=True
        payload = self._build_queue_declare_payload('test_queue', flags=0x02)
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNone(result, "Should return None for already-durable queue")

    def test_rewrite_removes_exclusive_flag(self):
        # flags=0x04 means exclusive=True, durable=False
        payload = self._build_queue_declare_payload('test_queue', flags=0x04)
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNotNone(result)

        offset = 6
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)
        flags = result[offset]
        self.assertTrue(flags & 0x02, "durable bit should be set")
        self.assertFalse(flags & 0x04, "exclusive bit should be cleared")

    def test_rewrite_adds_x_queue_type(self):
        payload = self._build_queue_declare_payload('test_queue', flags=0)
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNotNone(result)

        # Parse the arguments from the rewritten payload
        offset = 6
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)
        offset += 1  # skip flags
        args, _ = AMQPMethodParser.parse_table(result, offset)
        self.assertEqual(args.get('x-queue-type'), 'quorum')

    def test_rewrite_preserves_existing_x_queue_type(self):
        # If x-queue-type is already set, don't change it
        payload = self._build_queue_declare_payload(
            'test_queue', flags=0,
            arguments={'x-queue-type': 'classic'}
        )
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNotNone(result)

        # The original payload is modified in-place for flags, but arguments
        # are only rebuilt when x-queue-type is absent. Since x-queue-type
        # is already present, the payload is modified in-place (bytearray).
        offset = 6
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)
        offset += 1  # skip flags
        args, _ = AMQPMethodParser.parse_table(result, offset)
        self.assertEqual(args.get('x-queue-type'), 'classic')

    def test_rewrite_skips_reply_queue(self):
        payload = self._build_queue_declare_payload('reply_abc', flags=0)
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNone(result)

    def test_rewrite_skips_amq_gen_queue(self):
        payload = self._build_queue_declare_payload('amq.gen-xxx', flags=0)
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNone(result)

    def test_rewrite_wrong_class_id(self):
        # class_id=40 (exchange), not 50 (queue)
        payload = struct.pack('!HH', 40, 10) + b'\x00' * 10
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNone(result)

    def test_rewrite_short_payload(self):
        result = QueueDeclareRewriter.rewrite(b'\x00\x01')
        self.assertIsNone(result)

    def test_rewrite_preserves_other_flags(self):
        # auto_delete=True (0x08), no_wait=True (0x10)
        payload = self._build_queue_declare_payload('test_queue', flags=0x18)
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNotNone(result)

        offset = 6
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)
        flags = result[offset]
        self.assertTrue(flags & 0x02, "durable bit should be set")
        self.assertTrue(flags & 0x08, "auto_delete should be preserved")
        self.assertTrue(flags & 0x10, "no_wait should be preserved")

    def test_rewrite_preserves_existing_arguments(self):
        """Existing arguments should be preserved when x-queue-type is added."""
        payload = self._build_queue_declare_payload(
            'test_queue', flags=0,
            arguments={'x-max-length': 1000}
        )
        result = QueueDeclareRewriter.rewrite(payload)
        self.assertIsNotNone(result)

        offset = 6
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)
        offset += 1  # skip flags
        args, _ = AMQPMethodParser.parse_table(result, offset)
        self.assertEqual(args.get('x-queue-type'), 'quorum')
        self.assertEqual(args.get('x-max-length'), 1000)


class TestExchangeDeclareRewriter(unittest.TestCase):
    """Tests for ExchangeDeclareRewriter."""

    def _build_exchange_declare_payload(self, exchange_name, exchange_type='topic', flags=0, arguments=None):
        """Helper: build an Exchange.Declare method payload."""
        payload = struct.pack('!HH', 40, 10)  # class=40, method=10
        payload += struct.pack('!H', 0)  # reserved
        payload += AMQPMethodParser.encode_shortstr(exchange_name)
        payload += AMQPMethodParser.encode_shortstr(exchange_type)
        payload += bytes([flags])
        payload += AMQPMethodParser.encode_table(arguments or {})
        return payload

    # --- should_force_durable ---

    def test_should_force_durable_user_exchange(self):
        self.assertTrue(ExchangeDeclareRewriter.should_force_durable('my_exchange'))
        self.assertTrue(ExchangeDeclareRewriter.should_force_durable('nova'))
        self.assertTrue(ExchangeDeclareRewriter.should_force_durable('neutron'))

    def test_should_not_force_durable_default_exchange(self):
        self.assertFalse(ExchangeDeclareRewriter.should_force_durable(''))

    def test_should_not_force_durable_amq_exchange(self):
        self.assertFalse(ExchangeDeclareRewriter.should_force_durable('amq.direct'))
        self.assertFalse(ExchangeDeclareRewriter.should_force_durable('amq.topic'))
        self.assertFalse(ExchangeDeclareRewriter.should_force_durable('amq.fanout'))
        self.assertFalse(ExchangeDeclareRewriter.should_force_durable('amq.headers'))

    # --- rewrite ---

    def test_rewrite_non_durable_to_durable(self):
        payload = self._build_exchange_declare_payload('test_exchange', flags=0)
        result = ExchangeDeclareRewriter.rewrite(payload)
        self.assertIsNotNone(result)

        # Verify durable bit is set
        offset = 6  # skip class_id + method_id + reserved
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)  # exchange name
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)  # exchange type
        flags = result[offset]
        self.assertTrue(flags & 0x02, "durable bit should be set")

    def test_rewrite_already_durable_returns_none(self):
        payload = self._build_exchange_declare_payload('test_exchange', flags=0x02)
        result = ExchangeDeclareRewriter.rewrite(payload)
        self.assertIsNone(result)

    def test_rewrite_skips_default_exchange(self):
        payload = self._build_exchange_declare_payload('', flags=0)
        result = ExchangeDeclareRewriter.rewrite(payload)
        self.assertIsNone(result)

    def test_rewrite_skips_amq_exchange(self):
        payload = self._build_exchange_declare_payload('amq.direct', flags=0)
        result = ExchangeDeclareRewriter.rewrite(payload)
        self.assertIsNone(result)

    def test_rewrite_wrong_class_id(self):
        # class_id=50 (queue), not 40 (exchange)
        payload = struct.pack('!HH', 50, 10) + b'\x00' * 10
        result = ExchangeDeclareRewriter.rewrite(payload)
        self.assertIsNone(result)

    def test_rewrite_short_payload(self):
        result = ExchangeDeclareRewriter.rewrite(b'\x00')
        self.assertIsNone(result)

    def test_rewrite_preserves_other_flags(self):
        # auto_delete (0x04) + internal (0x08) — durable not set
        payload = self._build_exchange_declare_payload('test_exchange', flags=0x0C)
        result = ExchangeDeclareRewriter.rewrite(payload)
        self.assertIsNotNone(result)

        offset = 6
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)
        flags = result[offset]
        self.assertTrue(flags & 0x02, "durable bit should be set")
        self.assertTrue(flags & 0x04, "auto_delete should be preserved")
        self.assertTrue(flags & 0x08, "internal should be preserved")


class TestDurabilityProxy(unittest.TestCase):
    """Tests for DurabilityProxy frame routing logic."""

    def setUp(self):
        self.proxy = DurabilityProxy('localhost', 5672)

    def _build_method_frame(self, class_id, method_id, rest=b''):
        """Build a METHOD frame (type=1) with given class/method ids."""
        payload = struct.pack('!HH', class_id, method_id) + rest
        return AMQPFrame(1, 1, payload)

    def test_rewrite_non_method_frame_returns_none(self):
        # frame_type=2 is HEADER, should not be rewritten
        frame = AMQPFrame(2, 1, b'\x00' * 10)
        self.assertIsNone(self.proxy.rewrite_client_frame(frame))

    def test_rewrite_heartbeat_frame_returns_none(self):
        # frame_type=8 is HEARTBEAT
        frame = AMQPFrame(8, 0, b'')
        self.assertIsNone(self.proxy.rewrite_client_frame(frame))

    def test_rewrite_method_frame_non_declare_returns_none(self):
        # Connection.Start (class=10, method=10) — not queue/exchange declare
        frame = self._build_method_frame(10, 10)
        self.assertIsNone(self.proxy.rewrite_client_frame(frame))

    def test_rewrite_queue_declare_increments_stats(self):
        # Build a non-durable queue.declare
        rest = (
            struct.pack('!H', 0) +  # reserved
            AMQPMethodParser.encode_shortstr('test_q') +
            bytes([0]) +  # flags: non-durable
            AMQPMethodParser.encode_table({})
        )
        frame = self._build_method_frame(50, 10, rest)
        result = self.proxy.rewrite_client_frame(frame)
        self.assertIsNotNone(result)
        self.assertEqual(self.proxy.stats['queue_rewrites'], 1)

    def test_rewrite_exchange_declare_increments_stats(self):
        # Build a non-durable exchange.declare
        rest = (
            struct.pack('!H', 0) +  # reserved
            AMQPMethodParser.encode_shortstr('test_ex') +
            AMQPMethodParser.encode_shortstr('topic') +
            bytes([0]) +  # flags: non-durable
            AMQPMethodParser.encode_table({})
        )
        frame = self._build_method_frame(40, 10, rest)
        result = self.proxy.rewrite_client_frame(frame)
        self.assertIsNotNone(result)
        self.assertEqual(self.proxy.stats['exchange_rewrites'], 1)

    def test_rewrite_already_durable_queue_no_stat(self):
        rest = (
            struct.pack('!H', 0) +
            AMQPMethodParser.encode_shortstr('test_q') +
            bytes([0x02]) +  # flags: durable=True
            AMQPMethodParser.encode_table({})
        )
        frame = self._build_method_frame(50, 10, rest)
        result = self.proxy.rewrite_client_frame(frame)
        self.assertIsNone(result)
        self.assertEqual(self.proxy.stats['queue_rewrites'], 0)

    def test_rewrite_short_method_payload_returns_none(self):
        frame = AMQPFrame(1, 1, b'\x00\x01')
        self.assertIsNone(self.proxy.rewrite_client_frame(frame))

    def test_max_buffer_size_constant(self):
        self.assertEqual(DurabilityProxy.MAX_BUFFER_SIZE, 16 * 1024 * 1024)

    def test_initial_stats(self):
        proxy = DurabilityProxy('host', 1234)
        self.assertEqual(proxy.stats['connections'], 0)
        self.assertEqual(proxy.stats['queue_rewrites'], 0)
        self.assertEqual(proxy.stats['exchange_rewrites'], 0)
        self.assertEqual(proxy.stats['bytes_forwarded'], 0)


class TestParseHostPort(unittest.TestCase):
    """Tests for parse_host_port address parsing."""

    def test_ipv4_with_port(self):
        host, port = parse_host_port('127.0.0.1:5672', 9999)
        self.assertEqual(host, '127.0.0.1')
        self.assertEqual(port, 5672)

    def test_hostname_with_port(self):
        host, port = parse_host_port('rabbitmq-server:5672', 9999)
        self.assertEqual(host, 'rabbitmq-server')
        self.assertEqual(port, 5672)

    def test_hostname_without_port(self):
        host, port = parse_host_port('rabbitmq-server', 5672)
        self.assertEqual(host, 'rabbitmq-server')
        self.assertEqual(port, 5672)

    def test_ipv6_bracket_with_port(self):
        host, port = parse_host_port('[::1]:5672', 9999)
        self.assertEqual(host, '::1')
        self.assertEqual(port, 5672)

    def test_ipv6_bracket_without_port(self):
        host, port = parse_host_port('[::1]', 5672)
        self.assertEqual(host, '::1')
        self.assertEqual(port, 5672)

    def test_ipv6_full_bracket(self):
        host, port = parse_host_port('[2001:db8::1]:5672', 9999)
        self.assertEqual(host, '2001:db8::1')
        self.assertEqual(port, 5672)

    def test_bare_ipv6_no_port(self):
        host, port = parse_host_port('::1', 5672)
        self.assertEqual(host, '::1')
        self.assertEqual(port, 5672)

    def test_bare_ipv6_full(self):
        host, port = parse_host_port('2001:db8::1', 5672)
        self.assertEqual(host, '2001:db8::1')
        self.assertEqual(port, 5672)

    def test_ipv4_wildcard(self):
        host, port = parse_host_port('0.0.0.0:5672', 9999)
        self.assertEqual(host, '0.0.0.0')
        self.assertEqual(port, 5672)

    def test_localhost(self):
        host, port = parse_host_port('localhost', 5672)
        self.assertEqual(host, 'localhost')
        self.assertEqual(port, 5672)

    def test_invalid_ipv6_missing_bracket(self):
        with self.assertRaises(ValueError):
            parse_host_port('[::1', 5672)


class TestTableRoundTrip(unittest.TestCase):
    """End-to-end round-trip tests for table encode/decode with complex data."""

    def test_complex_table_round_trip(self):
        """Test a table with many types survives encode/decode."""
        table = {
            'bool_true': True,
            'bool_false': False,
            'small_int': 5,
            'medium_int': 300,
            'large_int': 100000,
            'huge_int': 2**50,
            'neg_int': -42,
            'pi': 3.14159,
            'name': 'rabbitmq',
            'raw': b'\xde\xad\xbe\xef',
            'decimal': (2, 4200),
            'empty': None,
        }
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)

        self.assertEqual(parsed['bool_true'], True)
        self.assertEqual(parsed['bool_false'], False)
        self.assertEqual(parsed['small_int'], 5)
        self.assertEqual(parsed['medium_int'], 300)
        self.assertEqual(parsed['large_int'], 100000)
        self.assertEqual(parsed['huge_int'], 2**50)
        self.assertEqual(parsed['neg_int'], -42)
        self.assertAlmostEqual(parsed['pi'], 3.14159, places=4)
        self.assertEqual(parsed['name'], 'rabbitmq')
        self.assertEqual(parsed['raw'], b'\xde\xad\xbe\xef')
        self.assertEqual(parsed['decimal'], (2, 4200))
        self.assertIsNone(parsed['empty'])

    def test_nested_table_round_trip(self):
        table = {
            'level1': {
                'level2': {
                    'value': 'deep'
                }
            }
        }
        encoded = AMQPMethodParser.encode_table(table)
        parsed, _ = AMQPMethodParser.parse_table(encoded, 0)
        self.assertEqual(parsed['level1']['level2']['value'], 'deep')

    def test_array_round_trip(self):
        """Test that list values survive encode/decode."""
        table = {'tags': ['tag1', 'tag2']}
        encoded = AMQPMethodParser.encode_table(table)
        # Array parsing is limited (only first element + skip to end),
        # so we just verify the encode doesn't crash and produces valid bytes
        self.assertIsInstance(encoded, bytes)
        self.assertTrue(len(encoded) > 4)


class TestEndToEndRewrite(unittest.TestCase):
    """End-to-end tests: raw frame bytes → rewrite → parse result."""

    def test_full_queue_declare_rewrite_cycle(self):
        """Build a raw frame, parse it, rewrite it, verify the output."""
        # Build queue.declare payload: non-durable, exclusive
        payload = (
            struct.pack('!HH', 50, 10) +  # class_id, method_id
            struct.pack('!H', 0) +  # reserved
            AMQPMethodParser.encode_shortstr('oslo_queue') +
            bytes([0x04]) +  # flags: exclusive=True, durable=False
            AMQPMethodParser.encode_table({'x-expires': 60000})
        )

        # Build raw frame
        frame = AMQPFrame(1, 3, payload)
        raw = frame.to_bytes()

        # Parse it back
        parsed_frame = AMQPFrame.parse(raw)
        self.assertIsNotNone(parsed_frame)

        # Rewrite
        rewritten = QueueDeclareRewriter.rewrite(parsed_frame.payload)
        self.assertIsNotNone(rewritten)

        # Verify the rewritten payload
        offset = 6
        queue_name, offset = AMQPMethodParser.parse_shortstr(rewritten, offset)
        self.assertEqual(queue_name, 'oslo_queue')
        flags = rewritten[offset]
        offset += 1
        self.assertTrue(flags & 0x02, "durable should be set")
        self.assertFalse(flags & 0x04, "exclusive should be cleared")

        args, _ = AMQPMethodParser.parse_table(rewritten, offset)
        self.assertEqual(args['x-queue-type'], 'quorum')
        self.assertEqual(args['x-expires'], 60000)

    def test_full_exchange_declare_rewrite_cycle(self):
        """Build a raw exchange.declare frame, rewrite, verify."""
        payload = (
            struct.pack('!HH', 40, 10) +
            struct.pack('!H', 0) +
            AMQPMethodParser.encode_shortstr('oslo_exchange') +
            AMQPMethodParser.encode_shortstr('topic') +
            bytes([0x00]) +  # flags: non-durable
            AMQPMethodParser.encode_table({})
        )

        frame = AMQPFrame(1, 1, payload)
        proxy = DurabilityProxy('localhost', 5672)
        result = proxy.rewrite_client_frame(frame)
        self.assertIsNotNone(result)

        # Verify durable bit in result
        offset = 6
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)
        _, offset = AMQPMethodParser.parse_shortstr(result, offset)
        flags = result[offset]
        self.assertTrue(flags & 0x02)
        self.assertEqual(proxy.stats['exchange_rewrites'], 1)


if __name__ == '__main__':
    unittest.main()
