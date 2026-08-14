class AltmE2EAdapter {
  providerInfo(provider) {
    return { id: provider, name: "ALTM E2E mock" };
  }

  providerRetryPolicy() {
    return undefined;
  }

  listModels() {
    return Promise.resolve([]);
  }

  resolveModel(provider, model) {
    return Promise.resolve({ provider, id: model, name: model });
  }

  async *stream(options) {
    const direct = options.messages
      .filter((message) => message.source?.kind === "user")
      .flatMap(messageText)
      .join("\n");
    const recalled = options.messages
      .filter(
        (message) =>
          message.source?.plugin === "altm-memory"
      )
      .flatMap(messageText)
      .join("\n");

    let text;
    if (direct.includes("What is release_codename?")) {
      const marker = recalled
        .split(/\n(?=### Memory )/)
        .find((section) => section.includes("cobalt"))
        ?.match(/memory:\/\/[A-Za-z0-9._:-]+(?:#[A-Za-z0-9]+)?/)?.[0];
      text = marker
        ? `The release codename is cobalt. ${marker}`
        : "Memory not found for this scope.";
    } else {
      text = "Stored the release codename.";
    }

    yield { type: "block-start", index: 0, blockType: "text" };
    yield { type: "text-delta", index: 0, text };
    yield {
      type: "block-end",
      index: 0,
      block: { type: "text", text }
    };
    yield {
      type: "usage",
      usage: { inputTokens: 1, outputTokens: 1 }
    };
    yield { type: "finish", reason: { kind: "stop" } };
  }
}

function messageText(message) {
  return message.content
    .filter((block) => block.type === "text")
    .map((block) => block.text);
}

export const name = "altm-e2e-mock-llm";
export const inject = ["llm"];

export function apply(ctx) {
  ctx.llm.registerAdapter(["altm-e2e"], new AltmE2EAdapter());
}
