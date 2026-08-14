import { readFile, rm } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";
import { build } from "esbuild";
import { transform } from "lightningcss";

await rm(new URL("../lib", import.meta.url), {
  recursive: true,
  force: true
});

await build({
  entryPoints: {
    index: new URL("../src/root.ts", import.meta.url).pathname,
    consumer: new URL("../src/index.ts", import.meta.url).pathname,
    memory: new URL("../src/memory.ts", import.meta.url).pathname,
    provider: new URL("../src/provider.ts", import.meta.url).pathname,
    "ui-host": new URL("../src/ui-host.ts", import.meta.url).pathname
  },
  outdir: new URL("../lib", import.meta.url).pathname,
  bundle: true,
  platform: "node",
  target: "node22",
  format: "esm",
  sourcemap: false,
  external: [
    "@altm/deepseek-harness/*",
    "@deepseek-ai/*",
    "@modelcontextprotocol/sdk/*",
    "zod"
  ]
});

const clientId = "@altm/deepseek-harness";
const cssNamespace = "altm-css-module";
const cssPlugin = {
  name: "altm-css-modules-inline",
  setup(buildApi) {
    buildApi.onResolve({ filter: /\.module\.css$/ }, (args) => ({
      path: resolve(dirname(args.importer), args.path),
      namespace: cssNamespace
    }));
    buildApi.onLoad({ filter: /.*/, namespace: cssNamespace }, async (args) => {
      const source = await readFile(args.path);
      const result = transform({
        filename: args.path,
        code: source,
        cssModules: { pattern: "[hash]_[local]" },
        minify: true
      });
      const classes = {};
      for (const [local, value] of Object.entries(result.exports ?? {})) {
        classes[local] = value.name;
      }
      const tagId = `${clientId}/${basename(args.path)}`;
      return {
        loader: "js",
        contents: [
          `const css = ${JSON.stringify(result.code.toString())};`,
          `const tagId = ${JSON.stringify(tagId)};`,
          "if (typeof document !== 'undefined' && document.querySelector('style[data-plugin-css=' + JSON.stringify(tagId) + ']') === null) {",
          "  const tag = document.createElement('style');",
          `  tag.dataset.plugin = ${JSON.stringify(clientId)};`,
          "  tag.dataset.pluginCss = tagId;",
          "  tag.textContent = css;",
          "  document.head.appendChild(tag);",
          "}",
          `module.exports = ${JSON.stringify(classes)};`
        ].join("\n")
      };
    });
  }
};

await build({
  entryPoints: {
    client: new URL("../src/client/index.tsx", import.meta.url).pathname
  },
  outdir: new URL("../lib", import.meta.url).pathname,
  bundle: true,
  platform: "browser",
  target: "es2022",
  format: "cjs",
  jsx: "automatic",
  sourcemap: true,
  external: [
    "react",
    "react/jsx-runtime",
    "react-dom",
    "react-dom/client",
    "@deepseek-ai/cordis",
    "@deepseek-ai/dsh-client-runtime/client",
    "@deepseek-ai/dsh-client-ui-slots",
    "@deepseek-ai/dsh-client-ui-primitives",
    "@deepseek-ai/dsh-client-web-react",
    "@deepseek-ai/dsh-client-ui-attachment",
    "@deepseek-ai/dsh-client-schema-form"
  ],
  define: {
    "process.env.NODE_ENV": JSON.stringify(process.env.NODE_ENV ?? "production")
  },
  banner: {
    js: `window.__ModuleLoader__.load({ id: ${JSON.stringify(clientId)}, factory: (require) => { var module = { exports: {} }; var exports = module.exports;`
  },
  footer: {
    js: "return module.exports; } });"
  },
  plugins: [cssPlugin]
});
