"""最小 JSON Schema 校验器（工具调用链路上的**第 1 道保护**）。

--------------------------------------------------------------------------------
★ 为什么自己写，而不是 `pip install jsonschema`
--------------------------------------------------------------------------------

| 理由 | 说明 |
|---|---|
| 少一个依赖 | Agent 侧目前只有 pydantic / PyYAML / httpx（+ fastapi）。为一次参数校验引一个库，收益与成本不匹配 |
| **错误信息要说人话** | 这一层的错误会**喂回给模型自修**（`TOOL_INVALID_PARAMS` 允许模型自修后重试）。标准库的报错是给程序员看的，我会写成「`query` 缺少必填字段」这种模型能直接照着改的形式 |
| 只要子集 | 工具的入参 schema 由我们自己写，只会用到下面这张表里的关键字 |

支持的关键字（**只支持这些**，遇到不认识的关键字会被显式拒绝，见 `validate_params`）：

| 关键字 | 语义 |
|---|---|
| `type` | `object` / `array` / `string` / `integer` / `number` / `boolean` / `null` |
| `properties` / `required` / `additionalProperties` | 对象结构（`additionalProperties=false` 时多给字段一律拒 —— 幻觉字段必须被发现） |
| `items` | 数组元素 |
| `enum` / `const` | 取值闭集 |
| `minimum` / `maximum` / `exclusiveMinimum` / `exclusiveMaximum` | 数值边界 |
| `minLength` / `maxLength` / `pattern` | 字符串约束 |

★ 不认识的关键字**直接报错**而不是忽略：静默忽略一个约束 = 那道保护形同虚设，
  而且没人会发现自己写的约束没生效（本项目反复踩的就是这种"看起来在守、其实没守"）。
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping

__all__ = ["validate_params", "SchemaError", "SUPPORTED_KEYWORDS"]

_TYPE_MAP: Final[dict[str, tuple[type, ...]]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}

#: 我们支持的 JSON Schema 关键字全集
SUPPORTED_KEYWORDS: Final[frozenset[str]] = frozenset({
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "minItems", "maxItems", "pattern", "description", "title", "default", "examples",
})

# ★ 这里**没有**一张"只作说明的关键字"常量：`description` / `title` / `default` /
#   `examples` 已经在 `SUPPORTED_KEYWORDS` 里（它们允许出现、不参与校验），
#   另立一张表既没人读、又会与上面那张漂移。


class SchemaError(Exception):
    """schema 自身写错了（不是入参的问题）—— 属于**实现缺陷**，要立刻炸。"""


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return {
        dict: "object", list: "array", str: "string", type(None): "null",
    }.get(type(value), type(value).__name__)


def _check_type(expected: Any, value: Any, path: str) -> list[str]:
    names = [expected] if isinstance(expected, str) else list(expected)
    for name in names:
        if name not in _TYPE_MAP:
            raise SchemaError(f"{path}: schema 里的 type={name!r} 不受支持")
        if name == "integer" and isinstance(value, bool):
            continue                      # bool 是 int 的子类，必须排除掉
        if name == "number" and isinstance(value, bool):
            continue
        if isinstance(value, _TYPE_MAP[name]):
            return []
    return [f"{path or '参数'} 应为 {'/'.join(names)}，实际是 {_type_name(value)}"]


def _validate(node: Mapping[str, Any], value: Any, path: str, errors: list[str]) -> None:
    unknown = set(node) - SUPPORTED_KEYWORDS
    if unknown:
        raise SchemaError(
            f"{path or '<root>'}: schema 用了未支持的关键字 {sorted(unknown)} —— "
            f"要么用支持的关键字重写，要么把它加进 SUPPORTED_KEYWORDS（不许静默忽略）"
        )

    if "type" in node:
        bad = _check_type(node["type"], value, path)
        if bad:
            errors.extend(bad)
            return                        # 类型都不对，后面的约束没意义

    if "const" in node and value != node["const"]:
        errors.append(f"{path} 只能是 {node['const']!r}，实际 {value!r}")
    if "enum" in node and value not in node["enum"]:
        errors.append(f"{path} 只能是 {list(node['enum'])} 之一，实际 {value!r}")

    if isinstance(value, bool):
        return                            # bool 不参与数值/字符串约束

    if isinstance(value, (int, float)):
        if "minimum" in node and value < node["minimum"]:
            errors.append(f"{path} 不得小于 {node['minimum']}，实际 {value}")
        if "maximum" in node and value > node["maximum"]:
            errors.append(f"{path} 不得大于 {node['maximum']}，实际 {value}")
        if "exclusiveMinimum" in node and value <= node["exclusiveMinimum"]:
            errors.append(f"{path} 必须大于 {node['exclusiveMinimum']}，实际 {value}")
        if "exclusiveMaximum" in node and value >= node["exclusiveMaximum"]:
            errors.append(f"{path} 必须小于 {node['exclusiveMaximum']}，实际 {value}")

    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            errors.append(f"{path} 至少 {node['minLength']} 个字符，实际 {len(value)}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            errors.append(f"{path} 最多 {node['maxLength']} 个字符，实际 {len(value)}")
        if "pattern" in node:
            try:
                matched = re.search(node["pattern"], value)
            except re.error as exc:
                raise SchemaError(f"{path}: schema 里的 pattern 非法：{exc}") from exc
            if matched is None:
                errors.append(f"{path} 不符合格式 {node['pattern']!r}，实际 {value!r}")

    if isinstance(value, dict):
        required = node.get("required") or []
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key} 是必填字段，但没给")
        props = node.get("properties") or {}
        for key, item in value.items():
            if key in props:
                _validate(props[key], item, f"{path}.{key}", errors)
            elif node.get("additionalProperties") is False:
                errors.append(
                    f"{path}.{key} 不是允许的参数（schema 未声明）—— "
                    f"可用参数：{sorted(props)}"
                )
            elif isinstance(node.get("additionalProperties"), Mapping):
                _validate(node["additionalProperties"], item, f"{path}.{key}", errors)

    if isinstance(value, list) and "items" in node:
        if 'minItems' in node and len(value)<node['minItems']:errors.append(f"{path} 数组至少 {node['minItems']} 项")
        if 'maxItems' in node and len(value)>node['maxItems']:errors.append(f"{path} 数组最多 {node['maxItems']} 项")
        for index, item in enumerate(value):
            _validate(node["items"], item, f"{path}[{index}]", errors)


def validate_params(schema: Mapping[str, Any], params: Any) -> list[str]:
    """校验入参。返回**人类/模型可读**的错误列表（空列表 = 通过）。

    Raises:
        SchemaError: **schema 自身**有问题（实现缺陷，例如写了不支持的关键字）。
            这类错误不该被降级成"参数错误" —— 那样会把实现缺陷伪装成模型的错。
    """
    errors: list[str] = []
    if not isinstance(schema, Mapping):
        raise SchemaError("input_schema 必须是映射")
    _validate(schema, params, "", errors)
    return errors
