import http from "node:http";

const port = Number(process.env.GATEWAY_PORT || "18082");

const server = http.createServer((_req, res) => {
  http
    .get("http://127.0.0.1:18081/health", (upstream) => {
      let body = "";
      upstream.on("data", (chunk) => {
        body += String(chunk);
      });
      upstream.on("end", () => {
        const out = JSON.stringify({
          gateway: process.env.GATEWAY_NAME || "",
          mode: process.env.SHARED_MODE || "",
          greeting: process.env.SERVICE_GREETING || "",
          auth: JSON.parse(body || "{}"),
        });
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(out);
      });
    })
    .on("error", (err) => {
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: err.message }));
    });
});

server.listen(port, "127.0.0.1", () => {
  console.log(`gateway listening on ${port}`);
});
