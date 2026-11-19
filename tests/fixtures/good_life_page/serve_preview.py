"""Serve the good_life page locally with a mock AstrBot bridge (dev preview only, never shipped).

    py -3 tests/fixtures/good_life_page/serve_preview.py [--port 8791]

Then open http://127.0.0.1:8791/?page=wallet&wallet=ok (see mock-bridge.js for scenarios).
"""

import argparse
import functools
import http.server
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE.parents[2] / "plugins" / "astrbot_plugin_good_life" / "pages" / "我会去好好生活"
SDK = "/api/plugin/page/bridge-sdk.js"


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] == SDK:
            body = b"".join((HERE / name).read_bytes() + b"\n" for name in ("wallet-fixtures.js", "mock-bridge.js"))
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    # Windows lets SO_REUSEADDR sockets share a busy port silently; fail instead of mixing servers.
    http.server.ThreadingHTTPServer.allow_reuse_address = False
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), functools.partial(Handler, directory=str(PAGE)))
    print(f"good_life preview on http://127.0.0.1:{args.port}/?page=wallet&wallet=ok", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
