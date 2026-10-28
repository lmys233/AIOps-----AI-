"""Generic register/catalog/use/destroy API; handlers use the shared ToolExecutor."""
from dataclasses import dataclass
from ..tools.registry import ToolRegistry
from ..tools.executor import ToolExecutor
from ..tools.handlers import ToolContext


@dataclass(frozen=True)
class CommandDefinition:
    name:str
    usage:str
    tool:object
    validate_arguments:object=None

    def __post_init__(self):
        if not self.name.isidentifier() or not self.usage or len(self.usage)>20:raise ValueError('命令名或20字内用法不合法')
        if self.tool.spec.permission.value!='read_only':raise ValueError('当前命令集仅允许只读工具')
        if 'input_ref' not in self.tool.spec.input_schema.get('properties',{}):raise ValueError('命令工具必须声明由执行器绑定的input_ref')

    def public(self):
        return dict(name=self.name,usage=self.usage,tool=self.tool.name,version=self.tool.version,
            description=self.tool.spec.description,parameters=self.tool.spec.input_schema,permission='read_only')


class CommandRegistry:
    def __init__(self,context=None):
        self.entries={};self.tools=ToolRegistry()
        self.executor=ToolExecutor(context=context or ToolContext(),registry=self.tools)

    def register(self,definition):
        if definition.name in self.entries:raise ValueError('命令已注册；先销毁旧版本')
        if not self.tools.policy.can_call('command_agent',definition.tool):raise ValueError('命令角色没有该插件声明的能力')
        self.tools.register(definition.tool,reason='注册对话命令插件')
        self.entries[definition.name]=definition

    def catalog(self):return [entry.public() for entry in self.entries.values()]

    def destroy(self,name):
        entry=self.entries.pop(name,None)
        if entry:self.tools.unregister(entry.tool.name,reason='销毁对话命令插件')
        return bool(entry)

    async def use(self,name,arguments):
        entry=self.entries.get(name)
        if not entry:raise ValueError('命令未注册或已销毁')
        if not isinstance(arguments.get('input_ref'),str) or not 1<=len(arguments['input_ref'])<=160:raise ValueError('调用缺少已绑定的命令输入引用')
        return await self.executor.call('command_agent',entry.tool.name,arguments)
