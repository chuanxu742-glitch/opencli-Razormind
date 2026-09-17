"""真实 socket 验证端口探测不会误占用或误接受运行中的监听器。"""

import socket
import sys
import unittest
from pathlib import Path

from backend.browser_account_runtime import _local_port_available


class RuntimePortTests(unittest.TestCase):
    def test_live_listener_is_unavailable(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.assertFalse(_local_port_available(listener.getsockname()[1]))

    @unittest.skipUnless(sys.platform == "linux", "Linux TIME_WAIT/reuse semantics")
    def test_closed_listener_time_wait_can_restart(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen()
            client.settimeout(2)
            client.connect(("127.0.0.1", port))
            accepted, _ = listener.accept()
            # 服务端主动关闭；读到EOF后客户端关闭，留下服务端TIME_WAIT。
            accepted.close()
            self.assertEqual(client.recv(1), b"")
        rows = [line.split() for line in Path("/proc/net/tcp").read_text().splitlines()[1:]]
        self.assertTrue(any(row[1].endswith(f":{port:04X}") and row[3] == "06" for row in rows))
        self.assertTrue(_local_port_available(port))


if __name__ == "__main__":
    unittest.main()
