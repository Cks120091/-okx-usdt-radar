"use strict";
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync("macd_radar/index.html", "utf8");
const scripts = Array.from(html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi));
if (scripts.length !== 1) throw Error("Expected exactly one inline JavaScript block");
new vm.Script(scripts[0][1], { filename: "macd_radar/index.html" });
for (const required of ["id=\"cards\"", "id=\"status\"", "id=\"gold\"", "id=\"maBull\"", "id=\"maBear\"", "id=\"divCount\"", "data-tf=\"4H\""]) {
  if (!html.includes(required)) throw Error("Missing frontend anchor " + required);
}
console.log("MA × MACD dashboard JS syntax and required DOM anchors: OK");
