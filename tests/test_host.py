"""Tests for host/tabdisplayd.py that need neither a tablet nor mutter."""
import os
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "host"))
import tabdisplayd as td  # noqa: E402


def hello(version=td.VERSION, width=2560, height=1600, dpi=244, codecs=3,
          magic=b"TABD"):
    return magic + struct.pack("<HIIII", version, width, height, dpi, codecs)


class WireTest(unittest.TestCase):
    def test_messages_round_trip_over_a_socket(self):
        a, b = socket.socketpair()
        try:
            a.sendall(td.pack_msg("A", b"\x01\x02") + td.pack_msg("T"))
            self.assertEqual(td.read_msg(b), ("A", b"\x01\x02"))
            self.assertEqual(td.read_msg(b), ("T", b""))
        finally:
            a.close()
            b.close()

    def test_peer_closing_mid_message_is_an_error(self):
        a, b = socket.socketpair()
        try:
            a.sendall(td.pack_msg("A", b"abcdef")[:7])
            a.close()
            with self.assertRaises(ConnectionError):
                td.read_msg(b)
        finally:
            b.close()

    def test_oversized_length_is_rejected_before_reading_it(self):
        a, b = socket.socketpair()
        try:
            a.sendall(b"V" + struct.pack("<I", (64 << 20) + 1))
            with self.assertRaises(ConnectionError):
                td.read_msg(b)
        finally:
            a.close()
            b.close()


class HelloTest(unittest.TestCase):
    def test_accepts_a_current_client(self):
        self.assertEqual(td.parse_hello(hello()), (2560, 1600, 244, 3))

    def test_rejects_other_protocols_and_versions(self):
        for bad in (hello(magic=b"HTTP"), hello(version=td.VERSION - 1),
                    hello()[:10]):
            with self.assertRaises(ConnectionError):
                td.parse_hello(bad)

    def test_rejects_sizes_no_encoder_would_take(self):
        for w, h in ((0, 0), (64, 64), (8000, 1600)):
            with self.assertRaises(ConnectionError):
                td.parse_hello(hello(width=w, height=h))


class LayoutKeyTest(unittest.TestCase):
    @staticmethod
    def logical(*connectors):
        return [(0, 0, 1.0, 0, False, [(c, "v", "p", "s")], {})
                for c in connectors]

    def test_key_names_the_real_monitors_in_any_order(self):
        a = td.Layout._key(self.logical("eDP-1", "Meta-0", "DP-3"))
        b = td.Layout._key(self.logical("DP-3", "eDP-1", "Meta-7"))
        self.assertEqual(a, b)
        self.assertEqual(a, "DP-3,eDP-1")


class StatsTest(unittest.TestCase):
    def test_reports_nothing_without_frames_and_resets_after_reporting(self):
        s = td.Stats()
        self.assertIsNone(s.report())
        s.frames, s.bytes = 10, 1000
        s.latency = [30000, 40000, 50000]
        s.held, s.encode, s.relay, s.rtt = [15000], [12000], [100], [3000]
        line = s.report()
        self.assertIn("p50 40.0", line)
        self.assertIn("link rtt 3.0", line)
        self.assertIsNone(s.report())


if __name__ == "__main__":
    unittest.main()
