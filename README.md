# ProjectLing

ProjectLing 是 AITermux 的 zsh/motd 协作层，用于 Termux 启动页、角色卡片、命令兜底和轻量 AI 协作。

它只面向 AITermux + Termux 环境，不是独立跨平台工具。发布仓库只保留源码和示例配置；API Key、memory、上下文 entries、角色状态和日志不随仓库发布。

## 要求

- Android + Termux
- 已安装 AITermux
- zsh、Python 3
- 基础 shell、环境变量、API Key 配置能力
- 不建议普通用户在不了解 Termux 文件结构和权限的情况下直接使用

## 启动

通常由 AITermux 的 `motd` 和 `zshrc` 自动接入：

```bash
cd ~/AItermux/projectling
cp config/example/env config/env
./run.sh doctor
./run.sh selftest
```

配置 DeepSeek：

```bash
DEEPSEEK_API_KEY=你的_key
```

## 仓库边界

- 只支持 AITermux 体系内使用。
- 不包含用户记忆、聊天上下文、运行日志和本机角色状态。
- WebSearch、工具执行、终端协作等能力需要用户自己配置并理解风险。
