import http from "node:http";
import https from "node:https";
import net from "node:net";

export function parseTcpTarget(spec) {
  const raw = String(spec).trim();
  if (/^\d+$/.test(raw)) {
    const port = Number(raw);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error(`Invalid TCP port: ${spec}`);
    }
    return { host: "127.0.0.1", port };
  }

  let host;
  let portPart;
  if (raw.startsWith("[") && raw.includes("]:")) {
    const idx = raw.indexOf("]:");
    host = raw.slice(1, idx).trim();
    portPart = raw.slice(idx + 2);
  } else if (raw.includes(":")) {
    const idx = raw.lastIndexOf(":");
    host = raw.slice(0, idx).trim() || "127.0.0.1";
    portPart = raw.slice(idx + 1);
  } else {
    throw new Error(`Invalid TCP readiness target '${spec}'. Expected host:port or port.`);
  }

  if (!/^\d+$/.test(portPart)) {
    throw new Error(`Invalid port in TCP readiness target '${spec}'.`);
  }
  const port = Number(portPart);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`Port out of range in TCP readiness target '${spec}'.`);
  }
  return { host, port };
}

export function normalizeTcpTarget(spec) {
  if (typeof spec === "number") {
    if (!Number.isInteger(spec) || spec < 1 || spec > 65535) {
      throw new Error(`Invalid TCP port: ${spec}`);
    }
    return `127.0.0.1:${spec}`;
  }

  const raw = String(spec).trim();
  if (/^\d+$/.test(raw)) {
    const port = Number(raw);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error(`Invalid TCP port: ${spec}`);
    }
    return `127.0.0.1:${port}`;
  }

  parseTcpTarget(raw);
  return raw;
}

export function probeTcp(spec, timeoutSeconds = 0.25) {
  let host;
  let port;
  try {
    ({ host, port } = parseTcpTarget(spec));
  } catch {
    return Promise.resolve(false);
  }

  return new Promise((resolve) => {
    let settled = false;
    const socket = net.createConnection({ host, port });
    const done = (ok) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(Math.max(10, Math.round(timeoutSeconds * 1000)));
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    socket.once("error", () => done(false));
  });
}

export function validateReadyUrl(url) {
  const trimmed = String(url).trim();
  let parsed;
  try {
    parsed = new URL(trimmed);
  } catch {
    throw new Error(`Invalid ready_url '${url}'. Expected http:// or https:// URL.`);
  }
  if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.host) {
    throw new Error(`Invalid ready_url '${url}'. Expected http:// or https:// URL.`);
  }
  return trimmed;
}

export function probeHttp(url, timeoutSeconds = 0.5) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return Promise.resolve(false);
  }
  if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.host) {
    return Promise.resolve(false);
  }

  const client = parsed.protocol === "https:" ? https : http;
  const timeoutMs = Math.max(50, Math.round(timeoutSeconds * 1000));

  return new Promise((resolve) => {
    let settled = false;
    const done = (ok) => {
      if (settled) return;
      settled = true;
      resolve(ok);
    };

    const req = client.request(
      parsed,
      {
        method: "GET",
        headers: {
          "User-Agent": "devup-readiness/1.0",
          Connection: "close",
        },
      },
      (res) => {
        const status = res.statusCode ?? 0;
        // Destroy immediately so chunked/streaming endpoints never block readiness probes.
        res.destroy();
        req.destroy();
        done(status >= 200 && status < 400);
      }
    );

    req.setTimeout(timeoutMs, () => {
      req.destroy();
      done(false);
    });
    req.once("error", () => done(false));
    req.end();
  });
}
