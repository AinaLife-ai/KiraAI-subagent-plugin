import asyncio
import time
import uuid
from dataclasses import dataclass, field

from core.plugin import BasePlugin, register
from core.logging_manager import get_logger
from core.agent.agent_executor import AgentExecutor, AgentExecutionContext
from core.agent.tool import ToolSet
from core.utils.tool_utils import BaseTool
from core.prompt_manager import Prompt
from core.provider import LLMRequest
from core.chat.session import Session
from core.chat.message_utils import KiraMessageBatchEvent
from core.adapter.adapter_info import AdapterInfo

sub_logger = get_logger("subagent", "magenta")


@dataclass
class SubAgentConfig:
    subagent_id: str
    name: str
    description: str
    persona: str = ""
    tools: list[str] = field(default_factory=list)
    max_steps: int = 3
    timeout: float = 60.0
    model: str = ""  # 格式 provider_id:model_id，或 "fast" 用快速模型，空字符串用默认模型


# 全局黑名单 - 子代理无论如何都不能使用的工具
_GLOBAL_BLACKLIST = {
    "call_subagent", "register_subagent", "list_subagents", "remove_subagent",
    "exec", "mijia_control_device", "send_email",
    "delete_qq_msg", "qzone_delete", "qzone_publish",
}

_STUB_ADAPTER = AdapterInfo(
    enabled=True,
    adapter_id="subagent",
    name="subagent",
    platform="subagent",
    description="SubAgent stub adapter",
)


def _code_expert() -> SubAgentConfig:
    return SubAgentConfig(
        subagent_id="code_expert",
        name="Code Expert",
        description="Skilled in code review, bug locating, refactoring and code explanation",
        persona=(
            "你是一位资深软件工程师，擅长代码审查、Bug 定位、重构和技术方案评估。"
            "优先给出可运行的代码示例，指出潜在风险，保持代码风格一致。"
        ),
        tools=["sub_read_file", "sub_write_file", "search", "extract_webpage"],
        max_steps=5,
        timeout=120.0,
        model="",
    )


def _writing_expert() -> SubAgentConfig:
    return SubAgentConfig(
        subagent_id="writing_expert",
        name="Writing Expert",
        description="Skilled in writing and polishing articles, novels, reports and various copy",
        persona=(
            "你是一位专业的写作专家，擅长创作各类文字内容。"
            "无论是小说、散文、诗歌、剧本，还是工作报告、技术文档、广告文案，"
            "你都能根据需求完成。注重文笔流畅、逻辑清晰、风格贴合目标读者。"
        ),
        tools=["sub_write_file"],
        max_steps=3,
        timeout=120.0,
        model="",
    )


class SubAgentPlugin(BasePlugin):
    """SubAgent plugin: lets the main agent delegate tasks to specialized sub-agents."""

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self._configs: dict[str, SubAgentConfig] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._custom_tools_cache: list[Tool] | None = None

    async def initialize(self):
        # Read config
        max_concurrent = int(self.plugin_cfg.get("max_concurrent", 3))
        self._semaphore = asyncio.Semaphore(max_concurrent)

        # Register built-in sub-agents
        self._configs[_code_expert().subagent_id] = _code_expert()
        self._configs[_writing_expert().subagent_id] = _writing_expert()
        sub_logger.info(f"SubAgent plugin loaded, registered: {list(self._configs.keys())}")

    def register_subagent_config(self, config: SubAgentConfig):
        """Public API for other plugins to register their own sub-agents."""
        self._configs[config.subagent_id] = config

    def _is_path_allowed(self, path: str, config_key: str) -> bool:
        """Check if path is in the configured allowed paths."""
        default_paths = {
            "allowed_read_paths": "data/files, data/temp, data/plugins",
            "allowed_write_paths": "data/files, data/temp",
        }
        raw = self.plugin_cfg.get(config_key, default_paths.get(config_key, ""))
        allowed_paths = [p.strip() for p in raw.split(",") if p.strip()]
        resolved = []
        for ap in allowed_paths:
            if ap.startswith("/"):
                resolved.append(ap)
            else:
                resolved.append(ap)
                resolved.append(f"/root/KiraAI/{ap}")
        for rp in resolved:
            if path.startswith(rp):
                return True
        return False

    def _register_custom_tools(self) -> list[BaseTool]:
        """Register custom file tools for sub-agent use with configurable paths."""
        if self._custom_tools_cache is not None:
            return self._custom_tools_cache

        class SubReadTool(BaseTool):
            name = "sub_read_file"
            description = "Read a file from allowed paths. Configure allowed_read_paths in plugin settings."
            parameters = {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"}
                },
                "required": ["path"],
            }

            def __init__(self, plugin_inst):
                super().__init__()
                self._plugin = plugin_inst

            async def execute(self, event, path: str) -> str:
                if not self._plugin._is_path_allowed(path, "allowed_read_paths"):
                    raw = self._plugin.plugin_cfg.get("allowed_read_paths", "data/files, data/temp, data/plugins")
                    return f"Error: Path not in allowed read paths: {raw}"
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read()
                except Exception as e:
                    return f"Error reading file: {e}"

        class SubWriteTool(BaseTool):
            name = "sub_write_file"
            description = "Write content to a file in allowed paths. Configure allowed_write_paths in plugin settings."
            parameters = {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write to"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            }

            def __init__(self, plugin_inst):
                super().__init__()
                self._plugin = plugin_inst

            async def execute(self, event, path: str, content: str) -> str:
                if not self._plugin._is_path_allowed(path, "allowed_write_paths"):
                    raw = self._plugin.plugin_cfg.get("allowed_write_paths", "data/files, data/temp")
                    return f"Error: Path not in allowed write paths: {raw}"
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                    return f"Written to {path}"
                except Exception as e:
                    return f"Error writing file: {e}"

        read_tool = SubReadTool(plugin_inst=self)
        write_tool = SubWriteTool(plugin_inst=self)
        self._custom_tools_cache = [read_tool, write_tool]
        return self._custom_tools_cache

    def _build_allowed_tool_set(self, allowed_tool_names: set) -> ToolSet:
        """Build a ToolSet containing only the allowed tools."""
        full_set = self.ctx.llm_api.build_tool_set()
        # Add custom subagent tools
        custom_tools = self._register_custom_tools()
        for ct in custom_tools:
            full_set.add(ct)
        tool_set = ToolSet()
        for tool in full_set.tools:
            if tool.name in allowed_tool_names and tool.name not in _GLOBAL_BLACKLIST:
                tool_set.add(tool)
        return tool_set

    @register.tool(
        name="list_subagents",
        description="列出所有已注册的子代理(subagent)及其可用工具列表",
        params={
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
    async def list_subagents(self, event) -> str:
        if not self._configs:
            return "当前没有已注册的子代理。"
        lines = []
        for cid, cfg in self._configs.items():
            tools_str = ", ".join(cfg.tools) if cfg.tools else "无"
            lines.append(f"[{cid}] {cfg.name} — {cfg.description}")
            model_str = cfg.model or "default"
            lines.append(f"        可用工具: {tools_str} | 最大步数: {cfg.max_steps} | 超时: {cfg.timeout}s | 模型: {model_str}")
        return "已注册的子代理:\n" + "\n".join(lines)

    @register.tool(
        name="register_subagent",
        description="动态注册一个新的子代理(subagent)，之后可通过 call_subagent 调用。subagent_id 必须唯一，不能与已有的重复。",
        params={
            "type": "object",
            "properties": {
                "subagent_id": {
                    "type": "string",
                    "description": "子代理唯一标识，例如 'translator'",
                },
                "name": {
                    "type": "string",
                    "description": "子代理显示名称，例如 '翻译专家'",
                },
                "description": {
                    "type": "string",
                    "description": "子代理功能描述，用于主代理判断何时调用",
                },
                "persona": {
                    "type": "string",
                    "description": "子代理的系统人格设定，描述其角色和能力",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "允许子代理使用的工具名称列表，如 ['sub_read_file', 'sub_write_file', 'search']",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "最大推理步数，默认3",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（秒），默认60",
                },
            },
            "required": ["subagent_id", "name", "description", "persona"],
        },
    )
    async def register_subagent_tool(self, event, subagent_id: str, name: str, description: str, persona: str,
                                      tools: list[str] | None = None, max_steps: int = 3, timeout: int = 60,
                                      model: str = "") -> str:
        if subagent_id in self._configs:
            return f"错误: 子代理 '{subagent_id}' 已存在，请使用不同的 ID 或先删除。"
        config = SubAgentConfig(
            subagent_id=subagent_id,
            name=name,
            description=description,
            persona=persona,
            tools=tools or [],
            max_steps=max_steps,
            timeout=timeout,
            model=model,
        )
        self._configs[subagent_id] = config
        sub_logger.info(f"Registered new sub-agent: {subagent_id} ({name})")
        return f"子代理 '{subagent_id}' ({name}) 注册成功！可用工具: {config.tools}"

    @register.tool(
        name="remove_subagent",
        description="删除一个已注册的子代理。不可删除内置子代理（code_expert、writing_expert）。",
        params={
            "type": "object",
            "properties": {
                "subagent_id": {
                    "type": "string",
                    "description": "要删除的子代理 ID",
                },
            },
            "required": ["subagent_id"],
        },
    )
    async def remove_subagent_tool(self, event, subagent_id: str) -> str:
        builtin = {"code_expert", "writing_expert"}
        if subagent_id in builtin:
            return f"错误: '{subagent_id}' 是内置子代理，不可删除。"
        if subagent_id not in self._configs:
            return f"错误: 子代理 '{subagent_id}' 不存在。"
        del self._configs[subagent_id]
        sub_logger.info(f"Removed sub-agent: {subagent_id}")
        return f"子代理 '{subagent_id}' 已删除。"

    @register.tool(
        name="edit_subagent",
        description="修改已注册子代理的配置。只需提供要修改的字段，未提供的保持原样。不可修改内置子代理。",
        params={
            "type": "object",
            "properties": {
                "subagent_id": {
                    "type": "string",
                    "description": "要修改的子代理 ID",
                },
                "name": {
                    "type": "string",
                    "description": "新的显示名称（可选）",
                },
                "description": {
                    "type": "string",
                    "description": "新的功能描述（可选）",
                },
                "persona": {
                    "type": "string",
                    "description": "新的系统人格设定（可选）",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "新的可用工具列表（可选）",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "新的最大推理步数（可选）",
                },
                "timeout": {
                    "type": "integer",
                    "description": "新的超时时间，单位秒（可选）",
                },
                "model": {
                    "type": "string",
                    "description": "新的模型（可选），格式 provider_id:model_id 或 fast",
                },
            },
            "required": ["subagent_id"],
        },
    )
    async def edit_subagent_tool(self, event, subagent_id: str,
                                  name: str | None = None, description: str | None = None,
                                  persona: str | None = None, tools: list[str] | None = None,
                                  max_steps: int | None = None, timeout: int | None = None,
                                  model: str | None = None) -> str:
        builtin = {"code_expert", "writing_expert"}
        if subagent_id in builtin:
            return f"错误: 内置子代理 '{subagent_id}' 不可修改。"
        config = self._configs.get(subagent_id)
        if not config:
            return f"错误: 子代理 '{subagent_id}' 不存在。"
        if name is not None:
            config.name = name
        if description is not None:
            config.description = description
        if persona is not None:
            config.persona = persona
        if tools is not None:
            config.tools = tools
        if max_steps is not None:
            config.max_steps = max_steps
        if timeout is not None:
            config.timeout = timeout
        if model is not None:
            config.model = model
        sub_logger.info(f"Edited sub-agent: {subagent_id}")
        return f"子代理 '{subagent_id}' 已更新。当前配置: 名称={config.name}, 步数={config.max_steps}, 超时={config.timeout}s, 模型={config.model or '默认'}, 工具={config.tools}"

    @register.tool(
        name="call_subagent",
        description="调用一个已注册的子代理(subagent)完成子任务。子代理拥有独立的角色设定和工具集，会自主完成任务并返回结果。适用于代码审查、深度分析、翻译等需要专业能力的子任务。",
        params={
            "type": "object",
            "properties": {
                "subagent_id": {
                    "type": "string",
                    "description": "子代理ID，例如 'code_expert'、'writing_expert'，或通过 list_subagents 查看",
                },
                "task": {
                    "type": "string",
                    "description": "需要完成的具体任务描述",
                },
            },
            "required": ["subagent_id", "task"],
        },
    )
    async def call_subagent(self, event, subagent_id: str, task: str) -> str:
        config = self._configs.get(subagent_id)
        if not config:
            available = list(self._configs.keys())
            return f"Error: SubAgent '{subagent_id}' not found. Available: {available}"

        try:
            if config.model and ":" in config.model:
                provider_id, model_id = config.model.split(":", 1)
                llm_model = self.ctx.provider_mgr.get_model_client(provider_id, model_id)
                if not llm_model:
                    llm_model = self.ctx.provider_mgr.get_default_llm()
            elif config.model == "fast":
                llm_model = self.ctx.provider_mgr.get_default_fast_llm()
            else:
                llm_model = self.ctx.provider_mgr.get_default_llm()
        except Exception:
            return "Error: No default LLM configured"

        # Acquire semaphore
        if self._semaphore:
            await self._semaphore.acquire()
            try:
                return await self._run_subagent(config, llm_model, task, subagent_id)
            finally:
                self._semaphore.release()
        else:
            return await self._run_subagent(config, llm_model, task, subagent_id)

    async def _run_subagent(self, config: SubAgentConfig, llm_model, task: str, subagent_id: str) -> str:
        # Build filtered tool set, never allow recursive subagent calls
        allowed = set(config.tools) - {"call_subagent", "register_subagent", "list_subagents", "remove_subagent"}
        tool_set = self._build_allowed_tool_set(allowed)

        agent_executor = AgentExecutor(self.ctx.llm_api, tool_set)

        messages = []
        system_prompts = []
        if config.persona:
            system_prompts.append(Prompt(config.persona, name="persona", source="system"))
        system_prompts.append(Prompt(
            "You are a specialized sub-agent. Focus on the assigned task and respond concisely. "
            "Return your final answer directly without extra meta-commentary.",
            name="subagent_role",
            source="system",
        ))

        llm_request = LLMRequest(messages=messages, tool_set=tool_set)
        llm_request.system_prompt.extend(system_prompts)
        llm_request.user_prompt.append(Prompt(task, name="task", source="user"))
        llm_request.assemble_prompt()

        cid = f"sub_{uuid.uuid4().hex[:12]}"
        stub_event = KiraMessageBatchEvent(
            message_types=[],
            timestamp=int(time.time()),
            session=Session(adapter_name="subagent", session_type="dm", session_id=cid),
            adapter=_STUB_ADAPTER,
        )

        agent_ctx = AgentExecutionContext(
            event=stub_event,
            request=llm_request,
            new_messages=[],
            model_group=[llm_model],
        )

        async def _run():
            final_text = ""
            async for step in agent_executor.run(agent_ctx, max_steps=config.max_steps):
                resp = step.llm_response
                if not resp:
                    break
                if resp.text_response:
                    final_text = resp.text_response
                if step.state == "error":
                    return f"Error: agent error - {step.err or 'unknown'}"
                if not step.has_tool_calls or step.is_final:
                    break
            return final_text

        try:
            result = await asyncio.wait_for(_run(), timeout=config.timeout)
        except asyncio.TimeoutError:
            return f"Error: SubAgent '{config.subagent_id}' timed out after {config.timeout}s"
        except Exception as e:
            sub_logger.error(f"SubAgent '{config.subagent_id}' error: {e}")
            return f"Error: SubAgent '{config.subagent_id}' failed: {e}"

        if result.startswith("Error:"):
            return result
        return f"SubAgent '{config.subagent_id}' result:\n{result}"

    async def terminate(self):
        self._configs.clear()
        self._custom_tools_cache = None
        sub_logger.info("SubAgent plugin terminated")