from __future__ import annotations

import unittest
from unittest.mock import patch

from sidequestor import dashboard


class _ProbeSocket:
    def __init__(self, attempts: list[int], occupied: set[int]) -> None:
        self.attempts = attempts
        self.occupied = occupied
        self.port = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def bind(self, address) -> None:
        self.port = address[1]
        self.attempts.append(self.port)
        if self.port in self.occupied:
            raise OSError("address already in use")

    def getsockname(self):
        return ("127.0.0.1", self.port)


class DashboardPortTest(unittest.TestCase):
    def test_automatic_port_scan_starts_at_8877_and_skips_occupied_ports(self) -> None:
        attempts: list[int] = []

        def make_socket(*_args):
            return _ProbeSocket(attempts, {8877, 8878})

        with patch.object(dashboard.socket, "socket", side_effect=make_socket):
            self.assertEqual(dashboard._ephemeral_port(), 8879)

        self.assertEqual(attempts, [8877, 8878, 8879])


if __name__ == "__main__":
    unittest.main()
