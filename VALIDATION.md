# 本地验证记录

日期：2026-10-08。平台：Windows。上游基线：`6e1030d9951c4c19048e4758ef130be5da4abfe0`。

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
