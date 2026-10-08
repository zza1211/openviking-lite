// SPDX-License-Identifier: AGPL-3.0
import { loadConfig } from "./config.mjs"
import { createRuntime, realText } from "./runtime.mjs"

// OpenCode 1.18.30's plugin module surface; no runtime dependency on a plugin SDK.
export default {
  id: "openviking-lite",
  async server({ client, directory }, options = {}) {
    const warn = message => {
      Promise.resolve(client.app?.log?.({ body: { service: "openviking-lite", level: "warn", message } })).catch(() => {})
    }
    let config
    try { config = await loadConfig(directory, options) }
    catch { warn("OpenViking Lite configuration is invalid; plugin disabled"); return {} }
    if (!config.enabled) return {}
    const runtime = createRuntime({ directory, config, warn })
    const ownership = new Map()

    async function isUserSession(sessionID) {
      if (ownership.has(sessionID)) return ownership.get(sessionID)
      // Restored sessions may not emit session.created in this process.
      const response = await client.session.get({ path: { id: sessionID } })
      if (response.error || !response.data) throw new Error("Cannot read OpenCode session metadata")
      const own = !response.data.parentID
      ownership.set(sessionID, own)
      return own
    }

    async function readMessages(sessionID) {
      const response = await client.session.messages({ path: { id: sessionID } })
      if (response.error || !Array.isArray(response.data)) throw new Error("Cannot read OpenCode session messages")
      return response.data.filter(m => m.info?.summary !== true && !m.info?.error).map(m => ({
        id: m.info?.id, role: m.info?.role, text: realText(m.parts),
      })).filter(m => m.id && m.text)
    }

    async function safely(operation) {
      try { await operation() } catch (error) { warn(error.message) }
    }

    return {
      "chat.message": async (input, output) => {
        const id = input.sessionID || output.message?.sessionID
        const text = realText(output.parts)
        if (!id) return
        let own = false
        await safely(async () => { own = await isUserSession(id) })
        if (!own) return
        // Copy the original user text before recall inserts a synthetic context part.
        if (id && text && config.capture) {
          runtime.capture(id, { id: input.messageID || output.message?.id, role: "user", text })
            .catch(error => warn(error.message))
        }
        await safely(() => runtime.inject(input, output))
      },
      event: async ({ event }) => {
        if (!config.capture) return
        const id = event.properties?.sessionID || event.properties?.info?.id
        if (!id) return
        if (event.type === "session.created") ownership.set(id, !event.properties?.info?.parentID)
        let own = false
        await safely(async () => { own = await isUserSession(id) })
        if (!own) return
        // The text snapshot is read at a completed turn, never from streaming deltas.
        if (event.type === "session.idle" || (event.type === "session.status" && event.properties.status?.type === "idle")) {
          runtime.schedule(id, () => readMessages(id))
        }
        if (event.type === "session.created") {
          await safely(() => runtime.serial(id, () => runtime.mapped(id)))
        }
      },
      "experimental.session.compacting": async input => {
        if (config.capture) await safely(async () => {
          if (await isUserSession(input.sessionID)) await runtime.flush(input.sessionID, () => readMessages(input.sessionID), false)
        })
      },
      dispose: () => runtime.finish(readMessages),
    }
  },
}
