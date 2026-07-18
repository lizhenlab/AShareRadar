import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, resolve, sep } from "node:path";

const root = process.cwd();
const port = Number(process.env.PORT || 4173);
const quoteStreams = new Set();
const mimeTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

createServer(async (request, response) => {
  const url = new URL(request.url || "/", `http://${request.headers.host || "127.0.0.1"}`);
  if (url.pathname === "/api/stream/quotes") {
    serveQuoteStream(request, response);
    return;
  }
  if (url.pathname === "/__e2e/quote-streams" && request.method === "GET") {
    serveJson(response, { clients: quoteStreams.size });
    return;
  }
  if (url.pathname === "/__e2e/quote-frame" && request.method === "POST") {
    for (const stream of quoteStreams) sendQuoteFrame(stream);
    serveJson(response, { sent: quoteStreams.size });
    return;
  }
  const pathname = url.pathname === "/" ? "/static/index.html" : url.pathname;
  const filePath = resolve(root, `.${pathname}`);
  if (filePath !== root && !filePath.startsWith(`${root}${sep}`)) {
    response.writeHead(403).end("Forbidden");
    return;
  }
  try {
    const details = await stat(filePath);
    if (!details.isFile()) throw new Error("Not a file");
    response.writeHead(200, { "Content-Type": mimeTypes[extname(filePath)] || "application/octet-stream" });
    createReadStream(filePath).pipe(response);
  } catch {
    response.writeHead(404).end("Not found");
  }
}).listen(port, "127.0.0.1");

function serveQuoteStream(request, response) {
  response.writeHead(200, {
    "Cache-Control": "no-cache",
    "Content-Type": "text/event-stream; charset=utf-8",
    "X-Accel-Buffering": "no",
  });
  response.write(": connected\n\n");
  quoteStreams.add(response);
  const keepaliveTimer = setInterval(() => response.write(": keepalive\n\n"), 10000);
  request.on("close", () => {
    quoteStreams.delete(response);
    clearInterval(keepaliveTimer);
  });
}

function sendQuoteFrame(response) {
  response.write(
    `data: ${JSON.stringify([
      {
        code: "600519",
        market: "SH",
        name: "浏览器行情帧",
        price: 1500,
        change_pct: 1.2,
        amount: 100000000,
      },
    ])}\n\n`
  );
}

function serveJson(response, payload) {
  response.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(payload));
}
