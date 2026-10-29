"use strict";
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync("macd_radar/index.html", "utf8");
const scripts = Array.from(html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi));
if (scripts.length !== 1) throw Error("Expected exactly one inline JavaScript block");
new vm.Script(scripts[0][1], { filename: "macd_radar/index.html" });
for (const required of ["id=\"cards\"", "id=\"status\"", "id=\"gold\"", "id=\"maBull\"", "id=\"maBear\"", "id=\"divCount\"", "id=\"dualMode\"", "id=\"watchMode\"", "id=\"dualCount\"", "data-tf=\"4H\""]) {
  if (!html.includes(required)) throw Error("Missing frontend anchor " + required);
}
// Long explanatory text and optional scan errors must never occupy the
// primary signal dashboard until the user explicitly opens details.
const details = html.match(/<details\b([^>]*)id="infoDetails"([^>]*)>([\s\S]*?)<\/details>/i);
if (!details) throw Error("Missing collapsed diagnostics section");
if (/\bopen(?:\s|=|>)/i.test(details[1] + " " + details[2])) throw Error("Diagnostics must default closed");
if (!details[3].includes('id="errors"') || !details[3].includes('class="info"')) {
  throw Error("Explanations and scan errors must be inside collapsible diagnostics");
}
if (!details[0].includes('id="diagIndicator"')) throw Error("Missing collapsed warning counter");
if (!html.includes('data.count+"/"+data.requested')) throw Error("Missing concise scan success status");
console.log("MA × MACD dashboard JS syntax and required DOM anchors: OK");
