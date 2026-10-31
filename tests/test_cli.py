import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



class LocalTransportTests(unittest.TestCase):
    def test_proxy_environment_cannot_receive_daemon_token_or_prompt(self):
        proxy_requests = []
        direct_requests = []

        def handler(received, destination):
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_POST(self):
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    received.append((self.headers.get("Authorization"), json.loads(body)))
                    response = json.dumps({"destination": destination}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
            return Handler

        direct = ThreadingHTTPServer(("127.0.0.1", 0), handler(direct_requests, "daemon"))
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), handler(proxy_requests, "proxy"))
        for server in (direct, proxy):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            # Synthetic test data only. No real provider or daemon credential is read.
            token = "synthetic-local-daemon-token"
            (state / "token").write_text(token)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            for key in list(env):
                if key.lower().endswith("_proxy"):
                    del env[key]
            env.update(http_proxy=f"http://127.0.0.1:{proxy.server_port}",
                       no_proxy="unrelated.invalid")
            code = (
                "import json,sys; from pathlib import Path; "
                "from bot_omp_delegate.cli import Client; "
                "print(json.dumps(Client(Path(sys.argv[1]),int(sys.argv[2])).request("
                "'POST','/sessions/example/turn',{'message':'synthetic private task'})))"
            )
            result = subprocess.run([sys.executable, "-B", "-c", code, str(state), str(direct.server_port)],
                                    cwd=Path(__file__).resolve().parents[1], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"destination": "daemon"})
            self.assertEqual(proxy_requests, [])
            self.assertEqual(direct_requests, [("Bearer " + token, {"message": "synthetic private task"})])


if __name__ == "__main__":
    unittest.main()
