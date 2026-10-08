// SPDX-License-Identifier: AGPL-3.0
import { copyFile, mkdir, readFile, stat, writeFile } from "node:fs/promises"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { configDirectory } from "./config.mjs"

const args = process.argv.slice(2)
const projectIndex = args.indexOf("--project")
if (!(args.length === 1 && args[0] === "--global")
    && !(args.length === 2 && projectIndex === 0 && args[1])) {
  console.error("Usage: node integrations/opencode/install.mjs --global | --project <directory>")
  process.exit(1)
}
const configDir = projectIndex >= 0 ? join(resolve(args[1]), ".opencode") : configDirectory()
if (projectIndex >= 0 && !(await stat(resolve(args[1]))).isDirectory()) {
  throw new Error("Project target must be an existing directory")
}
const root = dirname(fileURLToPath(import.meta.url))
const pluginDir = join(configDir, "plugins")
const destination = join(pluginDir, "openviking-lite")
const wrapper = 'export { default } from "./openviking-lite/index.mjs"\n'
try {
  if ((await readFile(join(pluginDir, "openviking-lite.js"), "utf8")).trim() !== wrapper.trim()) {
    throw new Error("Existing plugin entry was customized; installer will not overwrite it")
  }
} catch (error) { if (error.code !== "ENOENT") throw error }
await mkdir(destination, { recursive: true })
for (const name of ["index.mjs", "runtime.mjs", "config.mjs", "package.json"]) {
  await copyFile(join(root, name), join(destination, name))
}
await writeFile(join(pluginDir, "openviking-lite.js"), wrapper)
const configPath = join(configDir, "openviking-lite.json")
try {
  await writeFile(configPath, await readFile(join(root, "config.example.json")), { flag: "wx", mode: 0o600 })
} catch (error) { if (error.code !== "EEXIST") throw error }
console.log(`Installed: ${join(pluginDir, "openviking-lite.js")}`)
console.log(`Settings: ${configPath}`)
console.log("Start ov-lite serve and restart OpenCode. No npm dependencies are required.")
