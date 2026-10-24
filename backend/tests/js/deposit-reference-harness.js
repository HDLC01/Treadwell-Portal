"use strict";
/* Run the real app.js setupDeposit() and report the reference a customer is told to write on the
 * deposit (finding #21: one scope of a multi-scope project must name its scope). Lifted by brace
 * counting, like scope-group-harness.js, so it executes the shipped function, not a copy.
 *
 * Usage: node deposit-reference-harness.js <frontend-dir>   ->  one line of JSON
 */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(process.argv[2], "app.js"), "utf8");

function fn(name) {
  const m = new RegExp("\\nfunction " + name + "\\s*\\(").exec(src);
  if (!m) throw new Error(name + "() is gone from app.js — rewrite this harness, don't delete it");
  const i = src.indexOf("{", m.index + m[0].length - 1);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) return src.slice(m.index, j + 1);
  }
  throw new Error("unbalanced braces reading " + name);
}

function run(STATE) {
  const all = {};
  const $ = (id) => (all[id] = all[id] || { id, textContent: "", setAttribute() {} });
  const go = new Function("STATE", "$", "money", "show", "hide",
    `${fn("setupDeposit")}\nsetupDeposit();\n` +
    `return { line: $("deposit-due-line").textContent, check: $("check-ref").textContent };`);
  return go(STATE, $, (n) => "$" + Number(n).toFixed(2), () => {}, () => {});
}

const out = {
  scoped: run({ project_name: "Acme HQ", payment_reference: "Acme HQ - Lab 1",
                deposit: { due: 2500 } }),
  plain: run({ project_name: "Warehouse", payment_reference: "Warehouse", deposit: { due: 100 } }),
  older: run({ project_name: "Old portal payload", deposit: { due: 100 } }),
};
process.stdout.write(JSON.stringify(out) + "\n");
