// SPDX-License-Identifier: AGPL-3.0
const $ = id => document.getElementById(id)
const state = {
  key: "", view: "files", dir: "viking://user/default/memories", document: null,
  layer: "text", search: null, entries: [], tree: new Map(),
  expanded: new Set(["viking://resources", "viking://user", "viking://user/default", "viking://user/default/memories"]),
  listToken: 0, documentToken: 0, sessionToken: 0, sessionListToken: 0,
  sessions: [], sessionID: null, sessionOffset: 0, sessionTotal: 0, editor: null,
}
try { state.key = sessionStorage.getItem("ov-lite-key") || "" } catch {}
const roots = ["resources", "user", "skills"].map(name => ({ uri: "viking://" + name, is_dir: true }))
const aliases = { resources: "资料", user: "用户记忆", skills: "技能", memories: "记忆", preferences: "偏好", facts: "事实", experience: "经验" }
const kindNames = { memory: "长期记忆", resource: "资料", skill: "技能" }
const basename = uri => uri.replace(/\/$/, "").split("/").at(-1)
const parent = uri => uri.slice(0, uri.lastIndexOf("/"))
const label = uri => aliases[basename(uri)] || basename(uri)
const named = item => /^[a-f0-9]{24}$/.test(basename(item.uri)) && item.abstract
  ? item.abstract.split("\n")[0].slice(0, 32) : label(item.uri)
const date = value => new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })
const chars = value => Array.from(value).length.toLocaleString()

// All stored content is inserted as text, including file names and chat messages.
function node(tag, className = "", text) {
  const item = document.createElement(tag)
  item.className = className
  if (text !== undefined) item.textContent = text
  return item
}
function on(element, event, handler) {
  element.addEventListener(event, async e => { try { await handler(e) } catch (error) { report(error) } })
}
let toastTimer
function toast(message, error = false) {
  clearTimeout(toastTimer)
  $("toast").textContent = message
  $("toast").classList.toggle("error", error)
  $("toast").hidden = false
  toastTimer = setTimeout(() => { $("toast").hidden = true }, error ? 6500 : 3000)
}
function showDialog(id) { if (!$(id).open) $(id).showModal() }
function connectionOnline(online, message) {
  $("connection-status").textContent = message
  $("status-dot").classList.toggle("online", online)
  $("status-dot").classList.toggle("error", !online)
}
function report(error) {
  toast(error.message || "操作失败，请重试。", true)
  if (error.status === 401) {
    connectionOnline(false, "需要 API Key")
    openConnection()
  }
}
async function api(path, { method = "GET", body, timeout = 120000 } = {}) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    const response = await fetch(path, {
      method, signal: controller.signal, redirect: "error", cache: "no-store",
      headers: { ...(body === undefined ? {} : { "Content-Type": "application/json" }), ...(state.key ? { "X-API-Key": state.key } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.status !== "ok") {
      const messages = {
        401: "服务需要有效的 API Key，请在连接设置中填写。",
        404: "这条内容或会话已不存在，请刷新后重试。",
        409: "内容已被更新，或路径仍有子内容。请刷新后重试。",
        422: "输入格式有误，请检查内容路径和筛选条件。",
        502: "文本模型调用失败，请检查服务的模型 API 配置。",
      }
      const error = new Error(messages[response.status] || (response.status === 400 ? "内容或路径不符合要求，请检查后重试。" : "服务处理失败，请查看服务日志。"))
      error.status = response.status
      throw error
    }
    return payload.result
  } catch (error) {
    if (error.status) throw error
    throw new Error(error.name === "AbortError" ? "请求超时，请检查服务后重试。" : "无法连接记忆服务，请确认服务已启动。")
  } finally { clearTimeout(timer) }
}
const query = (path, params) => path + "?" + new URLSearchParams(params)

async function loadStatus() {
  const info = await api("/api/v1/system/status", { timeout: 10000 })
  for (const key of ["documents", "memories", "sessions", "chunks"]) $("stat-" + key).textContent = Number(info[key] || 0).toLocaleString()
  connectionOnline(true, "服务已连接")
  $("version-label").textContent = "OpenViking Lite / " + info.version
  $("model-status").textContent = `向量：${info.embedding_model} · 文本：${info.llm_model}`
  $("sync-time").textContent = "更新于 " + date(new Date())
}
function clearDocument() {
  state.documentToken++
  state.document = null
  $("document-preview").hidden = true
  $("preview-empty").hidden = false
}
function renderBreadcrumb() {
  const bar = $("breadcrumb")
  bar.replaceChildren()
  if (state.search) { bar.textContent = "搜索结果 / " + state.search.query; return }
  const parts = state.dir.replace("viking://", "").split("/").filter(Boolean)
  const all = node("button", "", "全部")
  all.type = "button"
  on(all, "click", () => loadDirectory("viking://"))
  bar.append(all)
  parts.forEach((part, index) => {
    bar.append(node("span", "", "/"))
    const uri = "viking://" + parts.slice(0, index + 1).join("/")
    const button = node("button", index === parts.length - 1 ? "current" : "", aliases[part] || part)
    button.type = "button"
    on(button, "click", () => loadDirectory(uri))
    bar.append(button)
  })
}
function expandAncestors(uri) {
  if (uri === "viking://") return
  const parts = uri.replace("viking://", "").split("/")
  for (let i = 1; i <= parts.length; i++) state.expanded.add("viking://" + parts.slice(0, i).join("/"))
}
async function fetchTree(uri) {
  const children = await api(query("/api/v1/fs/ls", { uri }))
  state.tree.set(uri, children)
}
function renderTree() {
  const root = $("directory-tree")
  root.replaceChildren()
  function branch(item, container) {
    const row = node("div", "tree-row")
    const toggle = node("button", "tree-toggle", item.is_dir ? (state.expanded.has(item.uri) ? "⌄" : "›") : "")
    toggle.type = "button"
    toggle.disabled = !item.is_dir
    toggle.setAttribute("aria-label", (state.expanded.has(item.uri) ? "收起 " : "展开 ") + label(item.uri))
    on(toggle, "click", async () => {
      if (state.expanded.has(item.uri)) state.expanded.delete(item.uri)
      else { state.expanded.add(item.uri); await fetchTree(item.uri) }
      renderTree()
    })
    const button = node("button", "tree-label" + (state.dir === item.uri ? " selected" : ""))
    button.type = "button"
    button.title = item.uri
    button.setAttribute("role", "treeitem")
    if (item.is_dir) button.setAttribute("aria-expanded", String(state.expanded.has(item.uri)))
    button.append(node("span", "tree-icon", item.is_dir ? "▱" : "▤"), node("span", "", named(item)))
    on(button, "click", async () => {
      if (item.is_dir) {
        state.expanded.add(item.uri)
        await fetchTree(item.uri)
        await loadDirectory(item.uri)
        if (item.is_doc) await loadDocument(item.uri)
      } else await loadDocument(item.uri)
    })
    row.append(toggle, button)
    container.append(row)
    if (item.is_dir && state.expanded.has(item.uri)) {
      const group = node("div", "tree-children")
      group.setAttribute("role", "group")
      const children = state.tree.get(item.uri)
      if (!children) group.append(node("div", "tree-empty", "…"))
      else if (!children.length) group.append(node("div", "tree-empty", "暂无内容"))
      else [...children].sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.uri.localeCompare(b.uri)).forEach(child => branch(child, group))
      container.append(group)
    }
  }
  roots.forEach(item => branch(item, root))
}
async function refreshTree() {
  await Promise.all([...state.expanded].map(fetchTree))
  renderTree()
}
function renderList() {
  $("file-list").replaceChildren()
  $("list-count").textContent = String(state.entries.length)
  if (!state.entries.length) {
    const empty = node("div", "list-empty")
    empty.append(node("strong", "", state.search ? "没有找到匹配内容" : "目录里还没有内容"), node("span", "", state.search ? "换个描述或扩大搜索范围试试。" : "开始聊天，让记忆自动积累；也可以新建一条内容。"))
    $("file-list").append(empty)
  }
  const entries = state.search ? state.entries : [...state.entries].sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.uri.localeCompare(b.uri))
  for (const item of entries) {
    const row = node("div", "file-row" + (state.document?.uri === item.uri ? " selected" : ""))
    const button = node("button", "file-button")
    button.type = "button"
    button.title = item.uri
    const body = node("span", "file-body")
    body.append(node("span", "file-name", named(item)), node("span", "file-summary", item.abstract || (item.is_dir ? "打开目录" : "纯文本内容")))
    if (state.search) body.append(node("span", "file-score", "相似度 " + Number(item.score).toFixed(3)))
    button.append(node("span", "file-icon", item.is_dir ? "▱" : "▤"), body)
    on(button, "click", () => item.is_dir ? loadDirectory(item.uri) : loadDocument(item.uri))
    row.append(button)
    if (item.is_dir && item.is_doc) {
      const original = node("button", "branch-document", "查看此路径的原文")
      original.type = "button"
      on(original, "click", () => loadDocument(item.uri))
      row.append(original)
    }
    $("file-list").append(row)
  }
}
async function loadDirectory(uri, { keepDocument = false } = {}) {
  const token = ++state.listToken
  state.dir = uri
  expandAncestors(uri)
  state.search = null
  $("search-query").value = ""
  $("clear-search").hidden = true
  $("list-title").textContent = label(uri) || "全部内容"
  $("file-list").replaceChildren(node("div", "list-empty", "正在读取目录…"))
  if (!keepDocument) clearDocument()
  renderBreadcrumb()
  const entries = await api(query("/api/v1/fs/ls", { uri }))
  if (token !== state.listToken) return
  state.entries = entries
  state.tree.set(uri, entries)
  renderList()
  renderTree()
}
function renderDocument() {
  const doc = state.document
  if (!doc) return
  $("preview-empty").hidden = true
  $("document-preview").hidden = false
  $("document-kind").textContent = kindNames[doc.kind] || "内容"
  $("document-title").textContent = named(doc)
  $("document-uri").textContent = doc.uri
  $("document-meta").textContent = "更新于 " + date(doc.updated_at) + " · " + chars(doc.text) + " 字符"
  for (const name of ["text", "overview", "abstract"]) {
    $("tab-" + name).classList.toggle("active", state.layer === name)
    $("tab-" + name).setAttribute("aria-selected", String(state.layer === name))
  }
  $("document-content").textContent = doc[state.layer]
  renderList()
}
async function loadDocument(uri) {
  const token = ++state.documentToken
  const doc = await api(query("/api/v1/content/document", { uri }))
  if (token !== state.documentToken) return
  state.document = doc
  state.layer = "text"
  renderDocument()
}
async function searchContents(saved) {
  const text = saved?.query || $("search-query").value.trim()
  if (!text) { await loadDirectory(state.dir); return }
  const scope = $("search-scope").value
  const target = saved?.target_uri || (scope === "current" ? state.dir : scope)
  const token = ++state.listToken
  state.search = { query: text, target_uri: target }
  $("list-title").textContent = "搜索结果"
  $("clear-search").hidden = false
  $("file-list").replaceChildren(node("div", "list-empty", "正在检索内容…"))
  $("list-count").textContent = "…"
  renderBreadcrumb()
  const result = await api("/api/v1/search/find", { method: "POST", body: { query: text, target_uri: target, limit: 30, level: 2 } })
  if (token !== state.listToken) return
  state.entries = result.matches
  renderList()
}
async function refresh() {
  await loadStatus()
  if (state.view === "sessions") {
    await loadSessions()
    if (state.sessionID) await loadSession(state.sessionID)
  }
  else {
    if (state.search) await searchContents(state.search)
    else await loadDirectory(state.dir, { keepDocument: true })
    await refreshTree()
    if (state.document) await loadDocument(state.document.uri).catch(error => { if (error.status === 404) clearDocument(); else throw error })
  }
}
function switchView(view) {
  state.view = view
  $("files-view").hidden = view !== "files"
  $("sessions-view").hidden = view !== "sessions"
  $("nav-files").classList.toggle("active", view === "files")
  $("nav-sessions").classList.toggle("active", view === "sessions")
  $("page-title").textContent = view === "files" ? "记忆空间" : "聊天记录"
  $("page-description").textContent = view === "files" ? "查看、检索和整理你的长期记忆。" : "查看原始对话，了解记忆从哪里来。"
}
function renderSessions() {
  const list = $("session-list")
  list.replaceChildren()
  $("session-total").textContent = "共 " + state.sessionTotal + " 段会话"
  $("session-page").textContent = String(Math.floor(state.sessionOffset / 30) + 1)
  $("session-prev").disabled = state.sessionOffset === 0
  $("session-next").disabled = state.sessionOffset + 30 >= state.sessionTotal
  if (!state.sessions.length) list.append(node("div", "list-empty", "暂无会话。OpenCode 开始记录后，会在这里显示。"))
  for (const session of state.sessions) {
    const row = node("div", "file-row" + (state.sessionID === session.id ? " selected" : ""))
    const button = node("button", "file-button")
    button.type = "button"
    const body = node("span", "file-body")
    body.append(node("span", "file-name", session.preview || "空会话"), node("span", "file-summary", `${session.user_id} · ${date(session.created_at)} · ${session.message_count} 条消息`), node("span", "file-score", session.pending_messages ? `${session.pending_messages} 条待提取` : "已完成提取"))
    button.append(node("span", "file-icon", "◷"), body)
    on(button, "click", () => loadSession(session.id))
    row.append(button)
    list.append(row)
  }
}
async function loadSessions() {
  const token = ++state.sessionListToken
  const params = { limit: 30, offset: state.sessionOffset }
  const user = $("user-filter").value.trim()
  if (user) params.user_id = user
  const result = await api(query("/api/v1/sessions", params))
  if (token !== state.sessionListToken) return
  state.sessions = result.sessions
  state.sessionTotal = result.total
  if (state.sessionID && !state.sessions.some(s => s.id === state.sessionID)) {
    state.sessionToken++
    state.sessionID = null
    $("session-detail").replaceChildren(node("div", "empty-preview", "选择一段会话查看原始聊天。"))
  }
  renderSessions()
}
async function loadSession(id) {
  const token = ++state.sessionToken
  const session = await api("/api/v1/sessions/" + encodeURIComponent(id))
  if (token !== state.sessionToken) return
  state.sessionID = id
  renderSessions()
  const detail = $("session-detail")
  detail.replaceChildren()
  const header = node("div", "session-header")
  const info = state.sessions.find(s => s.id === id)
  header.append(node("span", "kind-tag", info?.external_id?.startsWith("opencode:") ? "OpenCode 会话" : "应用会话"), node("h2", "", info?.preview || "聊天记录"), node("p", "", `${session.user_id} · ${date(session.created_at)}\n${id}\n已提取 ${session.committed_seq} / ${session.messages.length} 条消息`))
  detail.append(header)
  if (!session.messages.length) detail.append(node("div", "list-empty", "这段会话尚未记录消息。"))
  for (const message of session.messages) {
    const card = node("article", "message " + (message.role === "assistant" ? "assistant" : ""))
    const heading = node("div", "message-heading")
    heading.append(node("strong", "", ({ user: "用户", assistant: "助手", system: "系统" })[message.role] || "消息"), node("span", "", `#${message.seq} · ${message.seq <= session.committed_seq ? "已提取" : "待提取"}`))
    card.append(heading, node("pre", "", message.content))
    detail.append(card)
  }
}
function openConnection() {
  $("api-key").value = state.key
  $("connection-error").textContent = ""
  showDialog("connection-dialog")
}
function openEditor(edit) {
  if (edit && !state.document) return
  state.documentToken++
  state.editor = edit ? { ...state.document } : null
  $("editor-title").textContent = edit ? "编辑内容" : "新建内容"
  $("editor-uri").value = edit ? state.editor.uri : (state.dir === "viking://" || roots.some(r => r.uri === state.dir) ? "viking://resources" : state.dir) + "/note-" + Date.now().toString(36)
  $("editor-uri").readOnly = edit
  $("editor-text").value = edit ? state.editor.text : ""
  $("editor-summary").checked = false
  $("editor-error").textContent = ""
  showDialog("editor-dialog")
  $("editor-text").focus()
}
function lockDialog(id, locked) {
  $(id).dataset.busy = String(locked)
  $(id).querySelectorAll("button,input,textarea").forEach(element => { element.disabled = locked })
}
for (const button of document.querySelectorAll("[data-close]")) on(button, "click", () => {
  const dialog = $(button.dataset.close)
  if (dialog.dataset.busy !== "true") dialog.close()
})
for (const dialog of document.querySelectorAll("dialog")) dialog.addEventListener("cancel", event => {
  if (dialog.dataset.busy === "true") event.preventDefault()
})
on($("connection-button"), "click", openConnection)
on($("connection-form"), "submit", async event => {
  event.preventDefault()
  state.key = $("api-key").value
  $("connection-error").textContent = ""
  $("connect-submit").disabled = true
  try {
    await refresh()
    try { if ($("remember-key").checked && state.key) sessionStorage.setItem("ov-lite-key", state.key); else sessionStorage.removeItem("ov-lite-key") } catch {}
    $("connection-dialog").close()
    toast("记忆服务已连接")
  } catch (error) { connectionOnline(false, "服务未连接"); $("connection-error").textContent = error.message }
  finally { $("connect-submit").disabled = false }
})
on($("refresh-button"), "click", async () => {
  $("refresh-button").disabled = true
  try { await refresh(); toast("内容已刷新") } finally { $("refresh-button").disabled = false }
})
on($("nav-files"), "click", () => switchView("files"))
on($("nav-sessions"), "click", async () => { switchView("sessions"); await loadSessions() })
on($("search-form"), "submit", async event => { event.preventDefault(); await searchContents() })
on($("clear-search"), "click", () => loadDirectory(state.dir))
on($("new-button"), "click", () => openEditor(false))
on($("edit-button"), "click", () => openEditor(true))
for (const name of ["text", "overview", "abstract"]) on($("tab-" + name), "click", () => { state.layer = name; renderDocument() })
on($("copy-button"), "click", async () => { if (state.document) { await navigator.clipboard.writeText(state.document[state.layer]); toast("已复制到剪贴板") } })
on($("editor-form"), "submit", async event => {
  event.preventDefault()
  const uri = $("editor-uri").value.trim(), text = $("editor-text").value
  if (!text.trim()) { $("editor-error").textContent = "请填写非空文本。"; return }
  const summarize = $("editor-summary").checked, original = state.editor
  $("editor-error").textContent = ""
  lockDialog("editor-dialog", true)
  let saved = false
  try {
    if (original) {
      await api(query("/api/v1/content/document", { uri }), { method: "PATCH", body: { text, summarize, expected_updated_at: original.updated_at }, timeout: 600000 })
    } else {
      let existing = false
      try { await api(query("/api/v1/content/document", { uri })); existing = true } catch (error) { if (error.status !== 404) throw error }
      if (existing) { $("editor-error").textContent = "此路径已存在内容，请从预览面板编辑。"; return }
      await api("/api/v1/resources", { method: "POST", body: { to: uri, text, summarize }, timeout: 600000 })
    }
    saved = true
    $("editor-dialog").close()
    toast("内容已保存，检索索引已更新")
  } catch (error) { $("editor-error").textContent = error.status === 409 ? "内容已被更新。请复制保留你的修改，关闭编辑器并刷新后再试。" : error.message }
  finally { lockDialog("editor-dialog", false) }
  if (saved) {
    state.tree.clear()
    expandAncestors(parent(uri))
    await loadDirectory(parent(uri))
    await Promise.all([refreshTree(), loadStatus(), loadDocument(uri)])
  }
})
on($("delete-button"), "click", () => {
  if (!state.document) return
  $("delete-uri").textContent = state.document.uri
  $("delete-error").textContent = ""
  showDialog("delete-dialog")
})
on($("delete-form"), "submit", async event => {
  event.preventDefault()
  const uri = $("delete-uri").textContent
  lockDialog("delete-dialog", true)
  let removed = false
  try {
    await api(query("/api/v1/fs", { uri, recursive: false }), { method: "DELETE" })
    removed = true
    $("delete-dialog").close()
    clearDocument()
    state.tree.clear()
    toast("内容已删除")
  } catch (error) { $("delete-error").textContent = error.status === 409 ? "此路径仍有子内容，无法直接删除。请先处理子内容。" : error.message }
  finally { lockDialog("delete-dialog", false) }
  if (removed) await refresh()
})
on($("session-filter"), "submit", async event => { event.preventDefault(); state.sessionOffset = 0; await loadSessions() })
on($("session-prev"), "click", async () => { state.sessionOffset = Math.max(0, state.sessionOffset - 30); await loadSessions() })
on($("session-next"), "click", async () => { state.sessionOffset += 30; await loadSessions() })
renderTree()
refresh().catch(error => { connectionOnline(false, error.status === 401 ? "需要 API Key" : "服务未连接"); $("model-status").textContent = "请确认 Lite 服务已启动，然后刷新页面。"; report(error) })
