import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import path from "path";
import http from "http";
import router from "./routes";
import { logger } from "./lib/logger";

const app: Express = express();

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

const publicDir = path.join(__dirname, "../../../public");
app.use("/api/shop", express.static(publicDir));
app.get("/api/shop", (_req, res) => {
  res.sendFile(path.join(publicDir, "index.html"));
});

// Proxy Telegram webhook POST requests to the Python bot's local webhook server.
// Retries for up to 30 seconds to handle Python startup lag after Autoscale cold-start.
app.post(["/api/telegram", "/api/telegram/"], (req, res) => {
  const body = JSON.stringify(req.body);

  function tryProxy(attemptsLeft: number) {
    const proxyReq = http.request(
      {
        hostname: "127.0.0.1",
        port: 8443,
        path: "/",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (proxyRes) => {
        res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
        proxyRes.pipe(res, { end: true });
      },
    );
    proxyReq.on("error", () => {
      if (attemptsLeft > 0) {
        // Python not ready yet — wait 2 sec and retry (up to 30 sec total)
        setTimeout(() => tryProxy(attemptsLeft - 1), 2000);
      } else {
        // Give up — let Telegram retry later
        res.status(502).json({ error: "Bot webhook server unavailable" });
      }
    });
    proxyReq.write(body);
    proxyReq.end();
  }

  tryProxy(15); // 15 retries × 2 sec = 30 sec maximum wait
});

app.use("/api", router);

// Root health check — generic deploy health endpoint
app.get("/", (_req, res) => {
  res.json({ status: "ok", service: "LEGENDA Bot API" });
});
app.get("/health", (_req, res) => {
  res.json({ status: "ok" });
});

export default app;
