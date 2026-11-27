"""Minimal stdlib HTTP health server simulating FastAPI for quick smoke testing."""

import http.server
import json


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))


if __name__ == "__main__":
    http.server.HTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
