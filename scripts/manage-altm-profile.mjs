#!/usr/bin/env node

import { readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";

const [action, patchPath, dshRepo] = process.argv.slice(2);
if (
  !["enable", "disable", "remove", "status"].includes(action)
  || patchPath === undefined
  || dshRepo === undefined
) {
  throw new Error(
    "usage: manage-altm-profile.mjs "
    + "<enable|disable|remove|status> <cordis.patch.yml> <dsh-repo>",
  );
}

const requireFromDsh = createRequire(
  resolve(
    dshRepo,
    "packages/credentials/credentials-local/package.json",
  ),
);
const { isMap, isSeq, parseDocument } = requireFromDsh("yaml");
const source = await readFile(patchPath, "utf8");
const document = parseDocument(source, {
  customTags: [{
    tag: "tag:yaml.org,2002:js",
    resolve: (value) => value,
  }],
});
if (document.errors.length > 0) {
  throw new Error(
    `${patchPath} is invalid YAML: `
    + document.errors.map((error) => error.message).join("; "),
  );
}
if (!isSeq(document.contents)) {
  throw new TypeError(`${patchPath} must contain a YAML patch array`);
}

const managedIndexes = [];
let disabled = false;
for (const [index, item] of document.contents.items.entries()) {
  if (!isMap(item) || item.get("id") !== "altm-memory") continue;
  managedIndexes.push(index);
  disabled ||= item.get("disabled") === true;
}

if (action === "status") {
  process.stdout.write(disabled ? "disabled\n" : "enabled\n");
} else {
  for (const index of managedIndexes.reverse()) {
    document.contents.items.splice(index, 1);
  }
  if (action === "disable") {
    document.add({ id: "altm-memory", disabled: true });
  }
  await writeFile(
    patchPath,
    document.toString({ lineWidth: 0 }),
    "utf8",
  );
  process.stdout.write(action === "remove" ? "removed\n" : `${action}d\n`);
}
