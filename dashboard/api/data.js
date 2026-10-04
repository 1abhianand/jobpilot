// Serves the snapshot only to someone who knows DASHBOARD_PASSWORD (a Vercel environment variable).
const { readFileSync } = require("fs");
const { join } = require("path");
const { timingSafeEqual, createHash } = require("crypto");

const digest = (text) => createHash("sha256").update(String(text)).digest();

module.exports = (req, res) => {
  const expected = process.env.DASHBOARD_PASSWORD;
  const given = req.headers["x-dashboard-password"] || "";
  res.setHeader("Cache-Control", "no-store");
  if (!expected) return res.status(500).json({ error: "DASHBOARD_PASSWORD is not set on Vercel" });
  if (!timingSafeEqual(digest(given), digest(expected))) return res.status(401).json({ error: "wrong password" });
  res.setHeader("Content-Type", "application/json");
  res.status(200).send(readFileSync(join(process.cwd(), "data.json"), "utf8"));
};
