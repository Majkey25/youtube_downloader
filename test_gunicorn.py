from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "linux", "Gunicorn requires Linux")
class GunicornDeliveryTest(unittest.TestCase):
    def test_slow_delivery_preserves_health_capacity_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / f"yt-download-test-{'1' * 32}.mp4"
            other = root / f"yt-download-test-{'2' * 32}.mp4"
            payload = b"media" * (8 * 1024 * 1024)
            ready.write_bytes(payload)
            other.write_bytes(b"second file")
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen()
            with listener, (root / "gunicorn.log").open("w+") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "gunicorn",
                        "--bind",
                        f"fd://{listener.fileno()}",
                        "--workers",
                        "1",
                        "--threads",
                        "2",
                        "--timeout",
                        "30",
                        "app:app",
                    ],
                    env={
                        **os.environ,
                        "DOWNLOAD_DIR": directory,
                        "SOCKET_TIMEOUT_SECONDS": "2",
                        "DOWNLOAD_TIMEOUT_SECONDS": "10",
                    },
                    pass_fds=(listener.fileno(),),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        if process.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            self.fail(log.read())
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", port, timeout=0.5
                        )
                        try:
                            connection.request("GET", "/")
                            if connection.getresponse().status == 200:
                                break
                        except (OSError, http.client.HTTPException):
                            pass
                        finally:
                            connection.close()
                        time.sleep(0.05)

                    with socket.socket() as slow:
                        slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                        slow.settimeout(5)
                        slow.connect(("127.0.0.1", port))
                        slow.sendall(
                            f"GET /downloads/{ready.name} HTTP/1.1\r\n"
                            f"Host: 127.0.0.1:{port}\r\n"
                            "Connection: close\r\n\r\n".encode()
                        )
                        self.assertIn(b"200 OK", slow.recv(1024))
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", port, timeout=1
                        )
                        try:
                            connection.request("GET", "/")
                            health = connection.getresponse()
                            self.assertEqual(health.status, 200)
                            health.read()
                            connection.request("GET", f"/downloads/{other.name}")
                            rejected = connection.getresponse()
                            self.assertEqual(rejected.status, 429)
                            rejected.read()
                        finally:
                            connection.close()
                        deadline = time.monotonic() + 8
                        while not ready.exists() and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertTrue(
                            ready.exists(), "Timed-out file was not restored"
                        )
                        self.assertEqual(other.read_bytes(), b"second file")

                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5
                    )
                    try:
                        connection.request("GET", f"/downloads/{ready.name}")
                        delivered = connection.getresponse()
                        self.assertEqual(delivered.status, 200)
                        self.assertEqual(
                            delivered.getheader("Content-Length"), str(len(payload))
                        )
                        self.assertIsNone(delivered.getheader("Transfer-Encoding"))
                        self.assertEqual(delivered.read(), payload)
                    finally:
                        connection.close()
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
