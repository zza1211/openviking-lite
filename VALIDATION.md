# 本地验证记录

日期：2026-10-08。平台：Windows。上游基线：`6e1030d9951c4c19048e4758ef130be5da4abfe0`。

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
