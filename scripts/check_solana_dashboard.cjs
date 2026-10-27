"use strict";
// Deployment gate for Solana dashboard: a valid HTML response is not enough.
// In the past malformed inline JS returned HTTP 200 but never requested /api/status.
const fs = require("node:fs");
const vm = require("node:vm");
const file = "solana_paper/dashboard.html";
const html = fs.readFileSync(file, "utf8");
const count = (needle) => html.split(needle).length - 1;
if (!html.trim().endsWith("</html>") || count("</html>") !== 1) {
  throw new Error("Solana dashboard HTML truncated or duplicated");
}
if (count("</body>") !== 1 || count("</script>") !== 1) {
  throw new Error("Unexpected duplicate body or script tag");
}
const snippets = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
if (snippets.length !== 1 || snippets[0][1].trim().length === 0) {
  throw new Error("Expected exactly one nonempty inline JS application");
}
new vm.Script(snippets[0][1], { filename: file + ":inline-script" });
if (!snippets[0][1].includes('fetch("/api/status"')) {
  throw new Error("UI lacks Solana paper state API polling");
}
if (count("var poses=d.positions.map") !== 1) {
  throw new Error("Duplicate or absent positions renderer");
}
console.log("Solana dashboard validated: HTML structure, JS syntax and state polling");
