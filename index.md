# ProjectLing Code Index

更新日期：2026-05-24

这份文档是维护地图，不是运行配置。后续做全局审查、UI 优化、工具链修复、结构迭代时，先看这里再改代码；每次移动职责、重命名入口、拆合文件或新增关键链路，都同步更新本页。

## 快速定位

| 目标 | 入口/文件 | 备注 |
| --- | --- | --- |
| 启动 ProjectLing | `run.sh` | 统一入口，支持 `doctor`、`selftest`、设置、motd 渲染等子命令。 |
| 清理运行物 | `run.sh cleanup` | 清理旧日志、临时文件、临时压缩包和空临时目录；默认保留 Python bytecode。 |
| zsh 接入 | `projectling.zsh` | shell 输入拦截、`/mode`、`/send`、command-not-found 分发。 |
| CLI/UI 渲染 | `core.py` | 终端输出、Markdown 渲染、工具回执、设置菜单、motd 卡片。 |
| 对话引擎 | `projectling.py` | 配置加载、角色选择、路由、DeepSeek 请求、协作模式、上下文落盘。 |
| 工具实现 | `tooling.py` | `command`、`apply_patch`、`link`、`update_plan`、`contextmanage`、memory、terminal、web_search。 |
| 工具定义 | `config/toolbox.json`、`toolbox.json` | 工具可见性和说明；实现仍在 `tooling.py`。 |
| 角色表 | `config/roster.json` | 可抽取角色列表；不要把 diary keeper 这类系统角色混入普通轮换。 |
| 当前状态 | `config/role.json`、`config/update-plan.json`、`config/context-budget.json` | 当前主/辅角色、计划状态、上下文预算。 |
| 共享上下文 | `context/entries.jsonl`、`context/shared_context.txt` | entries 是主链路；shared_context 是兼容/摘要入口。 |
| 长期记忆 | `memory/datememory.json`、`memory/memory.db` | datememory 到阈值后进入 diary/sqlite 链路。 |
| 本地忽略规则 | `.gitignore` | 忽略运行上下文、记忆库、日志、临时包、缓存和备份。 |

## 性能热路径

- zsh 回车分类必须保持 shell 内完成；普通输入不得启动 Python 只为查询 pending 状态。
- `run.sh has-pending-command` 是 shell 快速路径，空 pending 通常应在几十毫秒内返回。
- `run.sh cleanup` 默认保留 `__pycache__` / `*.pyc`，避免每次启动重新编译；需要彻底清理时用 `run.sh cleanup --deep` 或 `AITERMUX_CLEAN_PYTHON_CACHE=1`。
- 终端打字机只用于短回复；长文本、代码块和表格走批量写入，避免大段输出卡顿。
- 空目录全新产物任务会走 `projectling.py::_fresh_project_bootstrap_plan()` 本地轻量 Planner，避免远端 reasoner 在旧上下文下 120s/524 后才降级。
- 2026-05-24 本机复测：空 pending 约 30ms；zsh 普通输入分类约 20-25ms；`render-motd-card` 约 360ms；`shell-dispatch --dry-run` 约 390ms。

## 核心文件边界

- `core.py`：只放终端 UI、设置菜单、工具回执渲染、CLI 命令分发；不要塞模型路由或工具实现。
- `projectling.py`：只放配置/prompt、角色与 MOTD、DeepSeek transport、协作路由和工具循环；不要塞终端排版细节。
- `tooling.py`：只放工具 schema/执行、context entries、memory、`update_plan`、`apply_patch`、command/terminal/aidebug/web_search/link；不要塞用户界面。
- `run.sh`：只放入口、单实例、日志清理、运行缓存清理和 Python 进程启动；不要塞业务逻辑。

## 核心链路地图

1. Shell 输入：`projectling.zsh`
   - 普通输入、`/mode`、`/send`、command-not-found 都会进入 `projectling_run_on_tty`。
   - 最终调用 `run.sh` / `core.py dispatch_shell_input`。

2. CLI 分发：`core.py`
   - `dispatch_shell_input()` 接收 shell 输入。
   - 设置菜单、motd、工具回执、流式 thinking、角色标题都在这里渲染。
   - 重要 UI 函数：`_format_role_heading()`、`_tool_heading()`、`_render_link_receipt()`、`_render_apply_patch_receipt()`、`_render_update_plan_receipt()`。

3. 引擎路由：`projectling.py`
   - `ProjectLingEngine.chat()` 是主对话入口。
   - 路由决定 rapid/standard/precise、planner/executor、工具域、是否需要 `update_plan`。
   - 空 cwd + 全新文件/项目创建 + `plan_required` 时，`_run_planner_step()` 使用本地 bootstrap plan，仍显示 Planner thinking 和 X-Link，但不发远端 Planner 请求。
   - `update_plan` 触发 `_maybe_review_plan_update()` 动态复审；复审提示必须同步工具硬规则，尤其是禁止 command 写文件回退和失败步骤虚假 done。
   - 工具返回 error/blocked/timeout 后，`_tool_failure_guidance()` 会向后续模型轮次插入失败处理规则，防止用旧产物或失败验证收束。
   - 协作提示词集中在 `_tool_instruction_prompt()`、`_executor_handoff_prompt()`、`_speaker_handoff_prompt()`。

4. 工具执行：`tooling.py`
   - `ToolRegistry.execute_tool_call()` 统一执行工具并压缩返回给模型。
   - `apply_patch` 入口是 `_execute_apply_patch_tool()`。
   - `link` / `update_plan` / `contextmanage` 是协作和上下文治理的核心工具。

5. 上下文落盘：`projectling.py` + `tooling.py`
   - `append_external_context_turn()` 写入 user/tool/assistant entries。
   - `contextmanage` 按 entry id 做 status/read/replace/fold，不再按角色维护独立上下文。

## apply_patch 维护点

- schema 走结构化优先：`operation`、`target_file`、`content/find/replace/edits`。
- `target_file` 必须相对当前 cwd；`~/`、`$HOME`、绝对路径和 `..` 都会被阻断并提示重试相对路径。
- 兼容 diff/patch 文本：`PATCH_TEXT_ARG_KEYS`、`_extract_patch_from_model_text()`。
- 结构化写入成功后需要生成 diff 预览，供前端显示编辑详情。
- 禁止模型失败后退回 `cat > file`、heredoc、`touch`、`sed -i`、`python -c write` 等直接写文件路径；安全拦截在 command 相关逻辑。
- command 现在会直接阻断明显写文件和文件结构变更回退：`cat/echo/printf > file`、`tee`、`touch`、`mkdir`、`cp/mv/rm/rmdir/ln/install`、`sed -i`、`perl -i`、`python -c write`、`dd of=`、`install/cp /dev/stdin`。
- Android/APK 任务的本地 Planner 会提醒 debug keystore 放在不会被清空的位置，避免反复构建时签名变化导致 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`。
- ADB `pm list`、`am start`、`input` 只算安装/启动烟测；无法替代人工功能验收。最终汇报不能把失败的 ADB input 说成完整功能验证通过。

## 常用验证命令

```bash
./run.sh selftest
./run.sh cleanup
python -m py_compile core.py projectling.py tooling.py
bash -n run.sh
zsh -n projectling.zsh
```

## 维护规则

- 小 UI 修正优先改 `core.py`，不要动引擎链路。
- 工具行为优先改 `tooling.py`，不要在 `projectling.py` 里硬编码工具细节。
- 路由、模式、planner/executor 提示词才改 `projectling.py`。
- 新增状态文件必须放在 `config/`、`context/`、`memory/` 或 `aidebug/` 对应目录，不要散落根目录。
- 运行生成物必须被 `.gitignore` 覆盖；临时包和空临时目录要能被 `./run.sh cleanup` 清理，Python bytecode 默认保留给启动性能。
- 每次完成结构性修改后更新本页，保持“打开就知道该去哪里改”的作用。

## 当前满意度评级

- 结构聚合：9/10。核心已收敛到 `core.py`、`projectling.py`、`tooling.py`，并补充了源码维护区说明；单文件仍偏大，但边界清楚。
- 工具链：8.5/10。`apply_patch`、`update_plan`、`link` 已形成主链路；仍需持续压测模型误用工具的边界。
- UI 观感：8.5/10。角色协作和工具回执已有辨识度，执行位显示已压缩；状态动画更快，长输出减少打字机卡顿。
- 上下文治理：8/10。entries id 链路已成型，后续重点是减少污染和优化 replace/fold 时机。
- 运行物治理：9/10。`cleanup`、日志限长、临时包清理和本地 `.gitignore` 已形成闭环；默认保留 bytecode 以换取启动流畅度。
- 结构健康度：约 92% 前后。剩余主要是大文件继续分区、边缘 UI 噪声、真实任务回归和长期压测。
