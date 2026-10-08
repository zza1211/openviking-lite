import assert from "node:assert/strict"
import { execFile } from "node:child_process"
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { promisify } from "node:util"
import { test } from "node:test"

const exec = promisify(execFile)
const installer = fileURLToPath(new URL("../install.mjs", import.meta.url))

test("project installer updates plugin files and preserves existing settings", async () => {
  const root = await mkdtemp(join(tmpdir(), "lite-plugin-install-"))
  try {
    await exec(process.execPath, [installer, "--project", root])
    const config = join(root, ".opencode", "openviking-lite.json")
    assert.equal(JSON.parse(await readFile(config, "utf8")).user_id, "default")
    const custom = JSON.stringify({ user_id: "alice", api_key: "private-test-key" })
    await writeFile(config, custom)
    await exec(process.execPath, [installer, "--project", root])
    assert.equal(await readFile(config, "utf8"), custom)
    const wrapper = join(root, ".opencode", "plugins", "openviking-lite.js")
    assert.ok((await readFile(wrapper, "utf8")).includes("./openviking-lite/index.mjs"))
    await writeFile(wrapper, "custom wrapper")
    await assert.rejects(exec(process.execPath, [installer, "--project", root]))
    assert.equal(await readFile(wrapper, "utf8"), "custom wrapper")
  } finally {
    assert.ok(resolve(root).startsWith(resolve(tmpdir())) && root.includes("lite-plugin-install-"))
    await rm(root, { recursive: true, force: true })
  }
})
