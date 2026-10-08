import assert from "node:assert/strict"
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"
import plugin from "../index.mjs"
import { loadConfig, validateConfig } from "../config.mjs"
import { createRuntime, realText, MEMORY_TAG } from "../runtime.mjs"

function memoryServer() {
  const calls = [], sessions = new Map(), rows = new Map()
  let id = 0, commits = 0
  let fail = false
  const fetchImpl = async (url, init) => {
    const path = new URL(url).pathname, body = init.body ? JSON.parse(init.body) : undefined
    calls.push({ path, body, init })
    if (fail) return Response.json({ status: "error" }, { status: 503 })
    if (path === "/api/v1/sessions") {
      const key = body.user_id + ":" + body.external_id
      if (!sessions.has(key)) sessions.set(key, "lite-" + ++id)
      return Response.json({ status: "ok", result: { session_id: sessions.get(key) } })
    }
    if (path.endsWith("/messages")) {
      const key = path + body.message_id
      if (!rows.has(key)) rows.set(key, body)
      return Response.json({ status: "ok", result: { seq: rows.size } })
    }
    if (path.endsWith("/commit")) {
      commits++
      return Response.json({ status: "ok", result: { memories: ["preference"] } })
    }
    if (path === "/api/v1/search/find") {
      return Response.json({ status: "ok", result: { matches: [
        { uri: "viking://user/alice/memories/preferences/lang", content: "用户希望用中文回答。", score: 0.7 },
        { uri: "viking://user/other/memories/facts/private", content: "wrong scope" },
      ] } })
    }
    throw new Error("Unexpected test route")
  }
  return { fetchImpl, calls, rows, sessions, get commits() { return commits }, set fail(value) { fail = value } }
}

const settings = { user_id: "alice", api_key: "secret-test-key", commit_debounce_ms: 0 }

test("only original plain text is captured", () => {
  assert.equal(realText([
    { type: "text", text: "original" }, { type: "text", text: "injected", synthetic: true },
    { type: "text", text: "ignored", ignored: true }, { type: "reasoning", text: "reasoning" },
    { type: "text", text: `<${MEMORY_TAG}>remember</${MEMORY_TAG}>` },
    { type: "text", text: "another", metadata: { openviking_lite: true } },
    { type: "tool", text: "tool output" },
  ]), "original")
})

test("stable session mapping and message IDs survive plugin restart", async () => {
  const server = memoryServer()
  const first = createRuntime({ directory: "/project", config: settings, fetchImpl: server.fetchImpl })
  const messages = [{ id: "u1", role: "user", text: "中文" }, { id: "a1", role: "assistant", text: "好的" }]
  await first.flush("oc-1", async () => messages)
  await first.flush("oc-1", async () => messages)
  const restarted = createRuntime({ directory: "/project", config: settings, fetchImpl: server.fetchImpl })
  await restarted.flush("oc-1", async () => messages)
  assert.equal(server.sessions.size, 1)
  assert.equal(server.rows.size, 2)
  assert.ok(server.calls.every(c => c.init.headers["X-API-Key"] === "secret-test-key"))
  await restarted.flush("oc-2", async () => [{ id: "u2", role: "user", text: "new" }])
  assert.equal(server.sessions.size, 2)
})

test("recall injects a bounded synthetic context without recording it", async () => {
  const server = memoryServer()
  const runtime = createRuntime({ directory: "/p", config: settings, fetchImpl: server.fetchImpl })
  const output = { message: { id: "u1", sessionID: "oc-1" }, parts: [{ type: "text", text: "怎么写代码？" }] }
  await runtime.inject({ sessionID: "oc-1" }, output)
  assert.equal(output.parts.length, 2)
  assert.equal(output.parts[0].synthetic, true)
  assert.ok(output.parts[0].text.includes("中文"))
  assert.ok(!output.parts[0].text.includes("wrong scope"))
  assert.ok(output.parts[0].text.length <= 4000)
  assert.equal(realText(output.parts), "怎么写代码？")
  await runtime.inject({ sessionID: "oc-1" }, output)
  assert.equal(output.parts.length, 2)
  assert.equal(server.calls.filter(c => c.path === "/api/v1/search/find").length, 2)
})

test("a failed write is recovered from snapshot after host compaction", async () => {
  const server = memoryServer()
  const runtime = createRuntime({ directory: "/p", config: settings, fetchImpl: server.fetchImpl })
  server.fail = true
  await assert.rejects(runtime.flush("oc-1", async () => [{ id: "u1", role: "user", text: "中文" }]), /HTTP 503/)
  server.fail = false
  await runtime.flush("oc-1", async () => [])
  assert.equal(server.rows.size, 1)
  assert.equal(server.commits, 1)
})

test("simultaneous writes are serialized and commits follow capture", async () => {
  const server = memoryServer()
  const runtime = createRuntime({ directory: "/p", config: settings, fetchImpl: server.fetchImpl })
  await Promise.all([
    runtime.capture("oc", { id: "u1", role: "user", text: "question" }),
    runtime.flush("oc", async () => [{ id: "u1", role: "user", text: "question" }, { id: "a1", role: "assistant", text: "answer" }]),
  ])
  assert.equal(server.sessions.size, 1)
  assert.equal(server.rows.size, 2)
  assert.equal(server.calls.at(-1).path.endsWith("/commit"), true)
})

test("long Unicode chat is split without losing surrogate pairs", async () => {
  const server = memoryServer()
  const runtime = createRuntime({ directory: "/p", config: settings, fetchImpl: server.fetchImpl })
  const text = "😀".repeat(100005)
  await runtime.flush("oc", async () => [{ id: "u", role: "user", text }])
  assert.equal(server.rows.size, 2)
  assert.equal([...server.rows.values()].map(r => r.content).join(""), text)
})

test("timeout is bounded and does not expose input or keys", async () => {
  const runtime = createRuntime({ directory: "/p", config: { ...settings, recall_timeout_ms: 100 },
    fetchImpl: (url, { signal }) => new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => reject(new Error("secret-test-key PRIVATE CHAT")))
    }),
  })
  const keepAlive = setTimeout(() => {}, 500)
  try { await assert.rejects(runtime.recall("private chat"), error => !error.message.includes("secret-test-key") && error.message.includes("timed out")) }
  finally { clearTimeout(keepAlive) }
})

test("config validates and merges global/project/environment/options", async () => {
  for (const value of [{ user_id: "../x" }, { url: "https://secret@host" }, { capture: "true" }, { unknown: 1 }]) {
    assert.throws(() => validateConfig(value))
  }
  const root = await mkdtemp(join(tmpdir(), "lite-plugin-config-"))
  try {
    await mkdir(join(root, "project", ".opencode"), { recursive: true })
    await writeFile(join(root, "openviking-lite.json"), JSON.stringify({ user_id: "global", recall_limit: 2 }))
    await writeFile(join(root, "project", ".opencode", "openviking-lite.json"), JSON.stringify({ user_id: "project" }))
    const config = await loadConfig(join(root, "project"), { recall_limit: 3 }, { OPENCODE_CONFIG_DIR: root, OV_LITE_USER: "env" })
    assert.equal(config.user_id, "env")
    assert.equal(config.recall_limit, 3)
  } finally { await rm(root, { recursive: true, force: true }) }
})

test("OpenCode hook module loads and survives memory service outage", async () => {
  const warnings = []
  const hooks = await plugin.server({ directory: "/empty-project",
    client: { session: { get: async () => ({ data: { id: "oc" } }) }, app: { log: async ({ body }) => warnings.push(body.message) } },
  }, { ...settings, url: "http://127.0.0.1:1", recall_timeout_ms: 100, capture: false })
  const output = { message: { id: "u1" }, parts: [{ type: "text", text: "question" }] }
  await hooks["chat.message"]({ sessionID: "oc" }, output)
  assert.equal(output.parts.length, 1)
  assert.ok(warnings.some(m => m.includes("unavailable")))
  await hooks.dispose()
})

test("subagent sessions do not become user memories", async () => {
  const hooks = await plugin.server({ directory: "/empty-project", client: {
    session: { get: async () => ({ data: { id: "child", parentID: "parent" } }) },
  } }, { ...settings, url: "http://127.0.0.1:1" })
  const output = { message: { id: "u1" }, parts: [{ type: "text", text: "AI generated subtask" }] }
  await hooks["chat.message"]({ sessionID: "child" }, output)
  assert.equal(output.parts.length, 1)
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "child" } } })
  await hooks.dispose()
})

test("completed turn captures user summary metadata and skips synthetic/compaction output", async () => {
  const server = memoryServer(), originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  let hooks
  try {
    hooks = await plugin.server({ directory: "/empty-project", client: { session: {
      get: async () => ({ data: { id: "oc" } }),
      messages: async () => ({ data: [
        { info: { id: "u", role: "user", summary: { title: "user summary", diffs: [] } },
          parts: [{ type: "text", text: "中文" }, { type: "text", text: "injected", synthetic: true }] },
        { info: { id: "a", role: "assistant" }, parts: [{ type: "text", text: "好的" }] },
        { info: { id: "summary", role: "assistant", summary: true }, parts: [{ type: "text", text: "compression" }] },
      ] }),
    } } }, { ...settings, recall: false })
    await hooks.event({ event: { type: "session.idle", properties: { sessionID: "oc" } } })
    await new Promise(resolve => setTimeout(resolve, 20))
    await hooks.dispose()
    assert.equal(server.rows.size, 2)
    assert.equal([...server.rows.values()].find(r => r.role === "user").content, "中文")
    assert.equal([...server.rows.values()].find(r => r.role === "assistant").content, "好的")
    assert.ok(server.commits > 0)
  } finally { if (hooks) await hooks.dispose(); globalThis.fetch = originalFetch }
})
