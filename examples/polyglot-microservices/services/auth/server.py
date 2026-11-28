import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        payload = json.dumps({
            "service": "auth",
            "status": "ok",
            "mode": os.environ.get("SHARED_MODE", ""),
            "greeting": os.environ.get("SERVICE_GREETING", ""),
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    port = int(os.environ.get("AUTH_PORT", "18081"))
    print(f"auth listening on {port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
