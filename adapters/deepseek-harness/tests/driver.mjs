import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [configArgument, profileManifestArgument, tasksArgument] =
  process.argv.slice(2);
const dshRepo = process.env.DSH_REPO;
if (!configArgument || !profileManifestArgument || !tasksArgument || !dshRepo) {
  throw new Error(
    "usage: driver.mjs <cordis.yml> <profile package.json> <tasks-json>; "
    + "DSH_REPO is required"
  );
}
const tasks = JSON.parse(tasksArgument);
if (!Array.isArray(tasks) || tasks.some((task) => typeof task !== "string")) {
  throw new TypeError("tasks-json must be an array of strings");
}

const appBootUrl = pathToFileURL(
  resolve(dshRepo, "packages/boot/app-boot/src/index.ts")
).href;
const loaderSmokeUrl = pathToFileURL(
  resolve(dshRepo, "packages/test-support/loader-smoke/src/agent-turn.ts")
).href;
const { boot } = await import(appBootUrl);
const { runFixtureTurn } = await import(loaderSmokeUrl);

const context = await boot(
  "altm-deepseek-harness-e2e",
  resolve(configArgument),
  undefined,
  undefined,
  pathToFileURL(resolve(profileManifestArgument)).href
);
await waitForRootAgent(context);

const results = [];
try {
  for (const task of tasks) {
    results.push(await runFixtureTurn(context, { task }));
  }
} finally {
  await context.fiber.dispose();
}

console.log(JSON.stringify({ results }));

async function waitForRootAgent(ctx) {
  if ((ctx.get("agents")?.roots().length ?? 0) > 0) {
    return;
  }
  await new Promise((resolveReady, reject) => {
    const timeout = setTimeout(() => {
      dispose();
      reject(new Error("configured Harness agent did not start within 10s"));
    }, 10_000);
    const dispose = ctx.on("agent/created", () => {
      clearTimeout(timeout);
      dispose();
      resolveReady();
    });
    if ((ctx.get("agents")?.roots().length ?? 0) > 0) {
      clearTimeout(timeout);
      dispose();
      resolveReady();
    }
  });
}
