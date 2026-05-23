# ProjectLing

ProjectLing 是 AITermux 的 zsh / motd 协作层，用于 Termux 启动页、角色卡片、命令兜底、设置入口和轻量 AI 协作。

## 状态

ProjectLing 仍处于实验性测试阶段，请勿用于日常生产或长期稳定使用。当前版本只具备研究、验证和测试价值。

我们会尽最大努力降低它的使用门槛，但它仍然会保持较高难度。使用者需要理解 Termux、zsh、环境变量、API Key、上下文文件、启动脚本，以及工具执行带来的系统风险。

## 适用范围

- AITermux / Termux 环境
- Android 设备上的 zsh / motd 启动协作
- 角色入口、命令兜底、状态提示和轻量 AI 协作测试

ProjectLing 不是独立跨平台工具，不承诺支持 Linux 桌面、Windows、macOS 或普通 Android shell。

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

- 只支持 AITermux / Termux 链路。
- 发布仓库只保留源码和示例配置。
- 不包含 API Key、用户记忆、聊天上下文、运行日志和本机角色状态。
- WebSearch、工具执行、终端协作等能力需要用户自己配置并理解风险。
