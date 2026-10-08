# OpenCode 自动记忆插件

已实测 **OpenCode 1.18.30**。插件采用该版本的 1.x 插件接口，零 npm 运行依赖。
OpenCode 2.x 的插件接口另有差异，当前未适配。

安装后，OpenCode 会在你正常聊天时完成三个动作：

1. 回答前使用当前问题检索本地长期记忆，向模型加入一段标记为 synthetic 的上下文。
2. 记录原始用户文本，在一轮回答结束后同步完整用户/助手文本，跳过推理、工具输出和注入的记忆。
3. 在结束一轮回答后后台调用 `commit_session`，提取偏好、事实和经验，下一段会话可以召回。

这不需要手动调用 Python 客户端，也不需要模型主动使用工具。OpenCode 继续使用自己的模型配置，
轻量版的文本 API 负责记忆提取，可以与 OpenCode 指向同一个兼容模型服务。

## 安装

先更新轻量版服务到本仓库当前版本（0.2.0），重新安装依赖并重启：

```powershell
cd D:\code\memory\openviking-lite
git pull
uv sync --locked
# 按根目录 README 配置 MODEL_API_BASE、MODEL_NAME、MODEL_API_KEY。
uv run ov-lite serve --config config.local.json
```

在另一个窗口安装插件：

```powershell
cd D:\code\memory\openviking-lite
# 全局安装，应用于你的各个 OpenCode 项目。
node integrations/opencode/install.mjs --global

# 或只安装到指定项目。选择一种即可，避免全局和项目同时安装本插件。
# node integrations/opencode/install.mjs --project 'D:\your-project'
```

Linux/macOS 同样执行上面的 `node` 命令。安装器默认使用 `~/.config/opencode/`，
同时遵循 `OPENCODE_CONFIG_DIR`、`XDG_CONFIG_HOME`。
全局安装的文件布局是：

```text
~/.config/opencode/
├── openviking-lite.json            # 连接与行为设置
└── plugins/
    ├── openviking-lite.js          # OpenCode 自动加载的入口
    └── openviking-lite/            # 插件实现，全部为本地文件
```

安装器不会改写已有的 OpenCode 主配置或模型配置；更新安装也会保留已有 `openviking-lite.json`。
安装或更新后 **完全退出并重新启动 OpenCode**。

## 配置连接

编辑全局 `~/.config/opencode/openviking-lite.json`，或项目 `.opencode/openviking-lite.json`：

```json
{
  "url": "http://127.0.0.1:1933",
  "api_key": "",
  "user_id": "default",
  "capture": true,
  "recall": true
}
```

- `api_key` 是轻量版服务的 `server_api_key`，不是远端模型 API Key。
- `user_id` 用于跨会话记忆归属；若以前用 `create_session('alice')` 保存记忆，此处也应填写 `alice`。
- 可使用环境变量 `OV_LITE_URL`、`OV_LITE_API_KEY`、`OV_LITE_USER` 覆盖对应字段。
- 合并优先级：默认值 → 全局文件 → 项目文件 → 环境变量 → OpenCode 插件显式选项。
- `"enabled": false` 完全停用；`"capture": false` 停止记录/提取，保留召回；`"recall": false` 停止注入。

完整选项见 [config.example.json](config.example.json)。默认每轮回答结束后等待 1 秒提取，
普通召回最多等 3 秒，记忆服务暂时不可用时继续正常聊天并记录警告。
提取在后台串行进行，不阻塞下一轮问题的召回；正常退出时插件会尝试同步尚未保存的内容。

## 验证三项自动行为

1. 开始一段 OpenCode 会话，说：“以后请用简体中文回答，代码示例优先用 Python。”
2. 等 OpenCode 完成回答，再等记忆提取 API 完成。
3. 在服务窗口以外执行：

   ```powershell
   uv run ov-lite ls viking://user/default/memories/preferences
   uv run ov-lite find '用户回答偏好' --scope viking://user/default/memories
   ```

4. 新建另一段 OpenCode 会话，问：“怎样遍历一个文件夹？”此时插件会在模型收到问题前自动加入偏好记忆。
   实际回答仍取决于你的模型；自动注入不保证模型一定遵从。

默认召回当前问题相关的最多 6 条记忆，同时补充最多 3 条稳定偏好；按 URI 去重，总上下文限制为 4000 字符。
稳定偏好可以在短问题（如“继续”）中被召回。关闭这项补充可设 `preference_limit` 为 0。

## 持久化、重试与范围

- 每个 OpenCode 会话按项目目录和会话 ID 映射为一个 Lite 会话；映射保存在 SQLite 中。
- OpenCode 消息 ID 作为幂等键，重启插件或重发相同消息不会重复写入、重复提取。
- 用户消息在回答前异步写入；完整助手文本在 `session.idle` 时同步，不保存每个流式片段。
- 长消息完整保存在数据库中，超过模型窗口时按实际 JSON 长度分批提取；超过 10 万字的消息由插件分片。
- 临时同步/提取失败会后台重试最多 3 次，并保留内存中的待同步文本；下轮聊天或正常退出会再次尝试。
- 正常压缩上下文前会保存原始对话，压缩摘要和 synthetic 内容不会当成新的用户事实。
- 子代理会话跳过，防止把 AI 生成的任务提示当成用户事实。
- 已提交消息被编辑时不会改写原来的记忆。记忆精确文本去重，仍不做语义合并或偏好冲突消解。
- 强制杀死进程可能丢失正在处理的尾部消息；重新打开原会话继续聊天后会从 OpenCode 历史补同步。
- 全局安装会自动记录你在各个项目中的普通文本聊天，并将其送到你配置的 Lite 服务。
  提取时，这些文本还会发送到 Lite 的 `llm.api_base`。需要限定范围时选择项目安装或关闭 `capture`。

## 开发验证

```powershell
node --test integrations/opencode/tests/*.test.mjs
uv run pytest

# 使用真实安装的 OpenCode、真实 CPU 向量模型与本地模拟文本 API。
# 测试隔离配置和数据，不读取你的模型密钥，也不会调用付费模型。
uv run python tests/smoke_opencode.py --cache-dir model-cache --opencode '完整的opencode可执行文件路径'
```

OpenCode 的插件目录、生命周期事件和模块接口参考：
[官方插件文档](https://docs.opencode.ai/docs/plugins/)、
[1.18.30 插件加载代码](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/plugin/index.ts)。
