# SubAgent — 子代理插件

让主代理能够将任务委派给拥有独立角色设定和工具集的子代理。

---

## 概述

SubAgent 实现了一套子代理委托机制：主代理可以将特定子任务委派给专门的子代理来执行。每个子代理拥有独立的角色设定、独立的工具集、独立的模型配置和独立的执行上下文，可以自主完成任务后返回结果。

核心价值：将复杂任务拆解为多个专业子任务，由不同的"专家"代理处理。

---

## 快速开始

### 列出所有子代理

```
list_subagents
```

### 调用子代理

```
call_subagent("code_expert", "帮我审查一下这段代码")
```

### 注册自定义子代理

```
register_subagent(
    subagent_id="translator",
    name="翻译专家",
    description="专业翻译助手，擅长中英互译",
    persona="你是一位专业翻译专家，翻译时注重信达雅。",
    tools=[],
    max_steps=1,
    timeout=30,
    model="fast"
)
```

### 修改子代理配置

```
edit_subagent(subagent_id="translator", model="openai:gpt-4o")
```

### 删除子代理

```
remove_subagent(subagent_id="translator")
```

---

## 配置文件 (schema.json)

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| max_concurrent | integer | 3 | 同时运行的子代理最大数量 |
| allowed_tools | string | read_file, write_file, search, extract_webpage | 子代理可用工具默认白名单 |
| allowed_read_paths | string | data/files, data/temp, data/plugins | 子代理可读取的路径 |
| allowed_write_paths | string | data/files, data/temp | 子代理可写入的路径 |

---

## SubAgentConfig 字段说明

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| subagent_id | str | 必填 | 子代理唯一标识 |
| name | str | 必填 | 显示名称 |
| description | str | 必填 | 功能描述，供主代理判断调用时机 |
| persona | str | "" | 系统人格设定（类似 system prompt） |
| tools | list[str] | [] | 允许使用的工具名称列表 |
| max_steps | int | 3 | 最大推理步数 |
| timeout | float | 60.0 | 超时时间（秒） |
| model | str | "" | 模型配置（详见下文） |

### model 字段说明

| 取值 | 含义 |
|---|---|
| ""（空字符串） | 使用系统默认模型 |
| "fast" | 使用系统快速模型 |
| "provider_id:model_id" | 指定具体模型，例如 "openai:gpt-4o" |

---

## 内置子代理

### Code Expert（代码专家）

擅长代码审查、Bug 定位、重构和技术方案评估。

- subagent_id: code_expert
- 工具: sub_read_file, sub_write_file, search, extract_webpage
- 最大步数: 5
- 超时: 120s

### Writing Expert（写作专家）

擅长创作和润色各类文字内容：文章、小说、报告等。

- subagent_id: writing_expert
- 工具: sub_write_file
- 最大步数: 3
- 超时: 120s

---

## 安全机制

| 措施 | 说明 |
|---|---|
| 全局黑名单 | 子代理不能调用 call_subagent、exec、send_email、mijia_control_device 等高危或递归性工具 |
| 内置代理保护 | 内置子代理（code_expert、writing_expert）不可删除或修改 |
| 路径白名单 | 文件读写受 allowed_read_paths / allowed_write_paths 限制 |
| 并发控制 | 通过 asyncio.Semaphore 限制同时运行的子代理数量 |
| 超时保护 | 每个子代理执行受 timeout 限制，超时自动终止 |
| 步数限制 | max_steps 限制推理循环次数，防止无限执行 |

---

## 执行流程

```
call_subagent(event, subagent_id, task)

  1. 查找子代理配置，不存在则返回错误
  2. 解析 model 配置，获取 LLM 模型实例
  3. 获取并发信号量
  4. 构建工具集（全局工具 + 自定义工具 - 黑名单）
  5. 创建 AgentExecutor，绑定工具集
  6. 构建 LLMRequest（persona + task）
  7. 构建伪事件（stub_event），适配框架接口
  8. 创建独立执行上下文
  9. 执行 agent_executor.run()，迭代步骤
  10. 返回结果或错误信息
```

关键特性：
- 每个子代理运行在独立的 AgentExecutor 中，与主代理完全隔离
- 每次调用生成新的虚拟会话，无状态
- 子代理返回的结果由主代理继续处理

---

## 项目结构

```
data/plugins/subagent/
├── __init__.py       # 空文件，标识为 Python 包
├── main.py           # 插件主逻辑
├── manifest.json     # 插件元信息
└── schema.json       # 插件配置项定义
```

## 版本

当前版本: 0.1.0

作者: Orion & 陈舒昕
