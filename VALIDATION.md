# 本地验证记录

最近验证：2026-10-09。平台：Windows。上游基线：`6e1030d9951c4c19048e4758ef130be5da4abfe0`。

## 0.3.0：轻量 Web 界面

| 检查 | 结果 |
| --- | --- |
| Python 3.11.14 `pytest` | 32 passed；新增页面/数据鉴权、编辑冲突、索引更新、会话分页筛选等检查 |
| Node.js 插件测试 | 12 passed；现有 OpenCode 插件不增加依赖 |
| 前端 JS 语法、Ruff、格式与 diff 检查 | 通过 |
| 浏览器 + 实际 Uvicorn + 真实 CPU 向量模型 | 鉴权连接、目录浏览、新建/编辑、语义搜索、会话筛选与原始对话查看通过 |
| 浏览器编辑冲突 | 模拟后台更新后保存旧版本返回冲突，编辑文本保留，后台内容未被覆盖 |
| 浏览器纯文本显示 | `<script>`、`<img onerror>` 作为文字显示，没有生成对应脚本/图片节点；控制台无脚本错误 |
| 布局检查 | 桌面三栏与窄屏布局正常，未出现页面横向溢出 |
| wheel / 源码包 | 包含内置 HTML/CSS/JS；从 wheel 解包加载应用，页面、静态资源和版本接口通过 |
| OpenCode 1.18.30 整链路回归 | 真实 OpenCode 和向量模型、模拟文本 API：原文记录、提取与回答前召回通过 |

Web 验证使用独立的示例工作区，不读取用户的真实聊天、记忆或模型密钥。
界面通过现有服务提供，未增加 npm 或 Python 运行依赖；静态页面公开，所有数据接口仍需服务 Key。
UI 编辑默认不调用文本 API，直接更新摘要和向量索引；真实用户文本 API、Docker 和 Linux/macOS 实机仍未验证。
界面截图见 `docs/images/web-ui.jpg`。0.1.0 / 0.2.0 的初次验证日期为 2026-10-08。

## 0.2.0：OpenCode 自动记忆

| 检查 | 结果 |
| --- | --- |
| Python 3.11.14 `pytest` | 26 passed；覆盖会话映射、消息幂等、提交冲突与长消息提取 |
| Node.js 24.13.0 `node --test integrations/opencode/tests/*.test.mjs` | 12 passed；覆盖召回、故障重试、子代理过滤、配置与安装更新 |
| Ruff 静态检查与格式检查、`git diff --check` | 通过 |
| `uv sync --locked --extra test` / `uv build` | 通过；源码包包含插件，运行不增加 npm 依赖 |
| 实际 OpenCode 1.18.30 + Lite Uvicorn + 真实中文 CPU 向量模型 | 原始用户/助手文本持久化，自动提取偏好，新会话回答前记忆进入模型请求 |
| 实际模型输入检查 | 注入内容标记 synthetic，不进入记忆提取的原始聊天 |

整链路脚本为 `tests/smoke_opencode.py`。它启动本机真实的 OpenCode 可执行文件，
使用独立的临时项目、配置与数据目录，并用本地 HTTP 服务模拟聊天/提取 API。
测试不读取你的 OpenCode 模型密钥，也不调用真实付费 API。
会话和原始聊天实际写入 SQLite，召回实际调用缓存中的 `BAAI/bge-small-zh-v1.5` CPU ONNX 模型。
真实文本模型的提取质量及是否遵从召回偏好仍需配置你的 API 后评估。

0.2.0 新增的表自动创建，不需要重建已有向量索引。OpenCode 2.x 未适配；
Windows 全局安装和项目安装流程已验证，Linux/macOS 实机尚未验证。

## 0.1.0：轻量核心验证

| 检查 | 结果 |
| --- | --- |
| Python 3.11.14 `pytest` | 21 passed |
| Python 3.14.6 `pytest` | 21 passed |
| Ruff 静态检查与格式检查 | 通过 |
| `uv lock --check` / `uv sync --locked` | 通过；使用公开 PyPI |
| `uv build` | 生成纯 Python wheel 和源码包，无原版本地编译 |
| 真实中文向量模型 | CPU ONNX 推理，512 维；使用本地缓存，离线加载成功 |
| 实际服务与 CLI 的整链路 | 4 份资料（包含提取记忆）、39 个块；3 次模拟文本 API 调用 |
| 中文检索 | “部署需要 GPU 显卡吗？”第一条命中部署资料，余弦分数约 0.747 |
| 多模态 / 服务端文件路径输入 | API 拒绝不支持的字段 |

整链路脚本为 `tests/smoke_real_model.py`：启动实际 Uvicorn 服务、使用真实向量模型，
由本地 HTTP 模拟服务提供 Chat Completions 响应；没有调用真实付费文本 API。
因此摘要/记忆提取的协议和流程已验证，真实文本模型的输出质量需要配置你的 API 后评估。

尚未验证：真实用户文本 API、Docker 构建/运行、Linux/macOS 实机、大规模数据性能、原版 SDK/插件兼容。
测试环境的 Starlette TestClient 有一条 HTTPX 弃用警告，不影响测试通过。
