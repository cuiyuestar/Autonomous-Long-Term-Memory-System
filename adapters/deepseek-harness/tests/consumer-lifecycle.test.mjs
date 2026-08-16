import assert from "node:assert/strict";
import test from "node:test";
import { Context } from "@deepseek-ai/cordis";
import * as Consumer from "../lib/consumer.js";
import {
  LongTermMemory,
  memoryTurnHandle,
} from "../lib/memory.js";

class FakeMemory extends LongTermMemory {
  prepared = [];
  committed = [];
  aborted = [];

  prepare(input) {
    const prepared = {
      handle: memoryTurnHandle(`cycle-${input.turnId}`),
      scope: input.scope,
      sessionId: input.sessionId,
      turnId: input.turnId,
      context: "remembered context",
      citationMemoryIds: [],
      activity: {
        includedCount: 1,
        tokenCountEstimate: 12,
        graphMatchCount: 0,
        layerCounts: { L2: 1 },
        memoryIds: [],
      },
    };
    this.prepared.push(prepared);
    return Promise.resolve(prepared);
  }

  commit(input) {
    this.committed.push(input);
    return Promise.resolve();
  }

  abort(input) {
    this.aborted.push(input);
    return Promise.resolve();
  }
}

function session() {
  return {
    id: "session",
    header: { cwd: "/tmp" },
    events: [],
  };
}

function directMessage() {
  return {
    id: "message",
    role: "user",
    content: [{ type: "text", text: "hello" }],
    source: { kind: "user" },
  };
}

async function harness() {
  const ctx = new Context();
  await ctx.plugin(FakeMemory);
  const provider = ctx.longTermMemory;
  const consumer = await ctx.plugin(Consumer, {
    tenantId: "tenant",
    workspaceId: "workspace",
    userId: "user",
    agentId: "agent",
  });
  return { ctx, provider, consumer };
}

async function prepare(ctx, subject, turn = 1) {
  const message = directMessage();
  return ctx.waterfall(
    "agent/pre-step",
    {
      agent: { session: subject },
      messages: [message],
      turn,
      step: 1,
      signal: new AbortController().signal,
    },
    () => Promise.resolve({ kind: "enter", messages: [message] }),
  );
}

test("consumer disposal aborts every prepared turn", async () => {
  const { ctx, provider, consumer } = await harness();
  const subject = session();
  const decision = await prepare(ctx, subject);
  assert.equal(decision.messages.at(-1).source.plugin, "altm-memory");
  assert.equal(decision.messages.at(-1).source.form, "notice");
  assert.equal(decision.messages.at(-1).source.summary, "1 · L2 1 · ~12 tok");

  await consumer.dispose();

  assert.equal(provider.committed.length, 0);
  assert.deepEqual(
    provider.aborted.map((item) => item.reason),
    ["consumer-unloaded"],
  );
  await ctx.fiber.dispose();
});

test("non-success turn endings abort instead of committing", async () => {
  const { ctx, provider, consumer } = await harness();
  const subject = session();
  await prepare(ctx, subject);

  ctx.emit("session/event", subject, {
    type: "turn/end",
    seq: 0,
    time: 0,
    data: {
      turn: 1,
      reason: {
        kind: "error",
        error: { message: "failed", code: "TEST" },
      },
    },
  });
  await consumer.dispose();

  assert.equal(provider.committed.length, 0);
  assert.deepEqual(
    provider.aborted.map((item) => item.reason),
    ["host-turn-error"],
  );
  await ctx.fiber.dispose();
});

test("completed turn commits its final Assistant message", async () => {
  const { ctx, provider, consumer } = await harness();
  const subject = session();
  await prepare(ctx, subject);
  subject.events.push(
    {
      type: "turn/start",
      seq: 0,
      time: 0,
      data: { turn: 1 },
    },
    {
      type: "assistant/message",
      seq: 1,
      time: 1,
      data: {
        turn: 1,
        step: 1,
        message: {
          id: "assistant",
          role: "assistant",
          content: [{ type: "text", text: "final" }],
          source: { kind: "model", provider: "test", model: "test" },
        },
      },
    },
  );

  ctx.emit("session/event", subject, {
    type: "turn/end",
    seq: 2,
    time: 2,
    data: { turn: 1, reason: { kind: "completed" } },
  });
  await consumer.dispose();

  assert.equal(provider.aborted.length, 0);
  assert.equal(provider.committed.length, 1);
  assert.equal(provider.committed[0].assistantContent, "final");
  await ctx.fiber.dispose();
});


test("anchor presets skip memory only on the first turn", async () => {
  const { ctx, provider } = await harness();
  const subject = session();
  subject.header.agentPreset = "whoami-standard";

  const first = await prepare(ctx, subject, 1);
  assert.equal(first.messages.length, 1);
  assert.equal(provider.prepared.length, 0);

  const second = await prepare(ctx, subject, 2);
  assert.equal(second.messages.length, 2);
  assert.equal(second.messages.at(-1).source.plugin, "altm-memory");
  assert.equal(provider.prepared.length, 1);

  await ctx.fiber.dispose();
});
