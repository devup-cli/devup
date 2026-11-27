"""Minimal stdlib HTTP health server simulating FastAPI for quick smoke testing."""

import http.server
import json
import socketserver


class FastHTTPServer(http.server.HTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class Handler(http.server.BaseHTTPRequestHandler):
    def address_string(self) -> str:
        return self.client_address[0]

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))


if __name__ == "__main__":
    print("Listening on http://127.0.0.1:8000", flush=True)
    FastHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
