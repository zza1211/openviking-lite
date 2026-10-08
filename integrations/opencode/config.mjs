// SPDX-License-Identifier: AGPL-3.0
import { readFile } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"

const defaults = {
  enabled: true, url: "http://127.0.0.1:1933", api_key: "", user_id: "default",
  capture: true, recall: true, recall_limit: 6, preference_limit: 3,
  score_threshold: 0.35, max_context_chars: 4000, recall_timeout_ms: 3000,
  request_timeout_ms: 10000, commit_timeout_ms: 600000, commit_debounce_ms: 1000,
}

export function validateConfig(input = {}) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("Invalid plugin configuration")
  for (const key of Object.keys(input)) {
    if (!(key in defaults)) throw new Error(`Unknown plugin setting: ${key}`)
  }
  const config = { ...defaults, ...input }
  for (const key of ["enabled", "capture", "recall"]) {
    if (typeof config[key] !== "boolean") throw new Error(`Plugin setting ${key} must be boolean`)
  }
  if (typeof config.api_key !== "string" || !/^[A-Za-z0-9_-]{1,64}$/.test(config.user_id)) {
    throw new Error("Invalid plugin user_id or api_key")
  }
  let url
  try { url = new URL(config.url) } catch { throw new Error("Invalid plugin service URL") }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("Plugin URL must be an HTTP(S) service root without credentials")
  }
  config.url = config.url.replace(/\/+$/, "")
  for (const [key, min, max] of [
    ["recall_limit", 1, 20], ["preference_limit", 0, 10], ["max_context_chars", 256, 16000],
    ["recall_timeout_ms", 100, 30000], ["request_timeout_ms", 100, 60000],
    ["commit_timeout_ms", 100, 600000], ["commit_debounce_ms", 0, 30000],
  ]) {
    if (!Number.isInteger(config[key]) || config[key] < min || config[key] > max) {
      throw new Error(`Plugin setting ${key} is outside its allowed range`)
    }
  }
  if (typeof config.score_threshold !== "number" || !Number.isFinite(config.score_threshold)
      || config.score_threshold < -1 || config.score_threshold > 1) {
    throw new Error("Invalid plugin score_threshold")
  }
  return config
}

export function configDirectory(env = process.env) {
  return env.OPENCODE_CONFIG_DIR || join(env.XDG_CONFIG_HOME || join(homedir(), ".config"), "opencode")
}

async function readConfig(path) {
  try { return JSON.parse(await readFile(path, "utf8")) }
  catch (error) { if (error.code === "ENOENT") return {}; throw new Error(`Cannot read plugin config: ${path}`) }
}

export async function loadConfig(directory, options = {}, env = process.env) {
  const global = await readConfig(join(configDirectory(env), "openviking-lite.json"))
  const project = await readConfig(join(directory, ".opencode", "openviking-lite.json"))
  const overrides = {}
  if (env.OV_LITE_URL !== undefined) overrides.url = env.OV_LITE_URL
  if (env.OV_LITE_API_KEY !== undefined) overrides.api_key = env.OV_LITE_API_KEY
  if (env.OV_LITE_USER !== undefined) overrides.user_id = env.OV_LITE_USER
  return validateConfig({ ...global, ...project, ...overrides, ...options })
}
