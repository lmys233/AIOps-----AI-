"""`.env` 装载 —— 让 `.env.example` 的承诺成真。

--------------------------------------------------------------------------------
★ 为什么必须补这一个文件
--------------------------------------------------------------------------------

`.env.example` 一直写着"复制为 .env 并按需修改"，但**全仓没有任何代码读它**：
`config.py` 与 `run.ps1` 都只认 `os.environ`。后果是——

  · 照文档操作的人：明明填了 `AIOPS_LLM_API_KEY`，却**不生效**；
  · 而 Agent 会因为缺 `AIOPS_LLM_BASE_URL`（REQUIRED）**直接 fail-fast 起不来**。

"文档承诺了一个不存在的机制"比没有文档更糟：它让人以为自己配好了。

--------------------------------------------------------------------------------
★ 四条硬性口径（与项目其它部分保持一致）
--------------------------------------------------------------------------------

1. **真实环境变量优先**：已存在且**非空**的变量**不被覆盖**。
   这样"在 shell 里临时覆盖一个值做演练"依然有效 ——
   与 SDK 的 `env_bridge`（shell > 配置文件）是同一条口径。
2. **文件不存在不是错误**：生产上完全可能只用真实环境变量 / 密钥管理服务。
   缺文件就返回空列表，不抛异常、不告警。
3. ★ **绝不打印值**：只回报**键名**。`.env` 里有 API Key，
   值一旦进了日志/终端历史就等于泄露 —— 与 `RUNNER_DATABASE_URL` 同一处理方式。
4. **零依赖**：不引入 `python-dotenv`，纯标准库（与 `config.py` 的取向一致）。

--------------------------------------------------------------------------------
★ 支持的语法（够用即可，不追求 dotenv 全集）
--------------------------------------------------------------------------------

```ini
# 注释行
KEY=value
KEY="带空格或 # 的值"
KEY='单引号也行'
export KEY=value          # 有些人的习惯写法，兼容
KEY=value   # 行内注释      # 仅当 `#` 前面有空白时才当注释（与 dotenv 一致）
```

不支持的（**明确不做**，而不是静默忽略）：多行值、变量插值 `${OTHER}`、
`set -a` 之类 shell 语义。遇到不认识的行**跳过并记入跳过清单**（可打印，便于排查），
但不报错 —— `.env` 是给人手写的，容错比严格更重要。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

__all__ = ["DEFAULT_ENV_FILE", "load_env_file", "parse_env_text"]

#   <repo>/src/agent/common/envfile.py
#     parents[0]=common  [1]=agent  [2]=src  [3]=<repo>
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


def parse_env_text(text: str) -> tuple[dict[str, str], list[str]]:
    """把 `.env` 文本解析成 `(键值对, 被跳过的行)`。

    ★ 单独抽出来是为了**可测**：解析规则是这个模块唯一"有分支"的地方，
      可以直接喂字符串验证，不必落盘。
    """
    values: dict[str, str] = {}
    skipped: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):          # 兼容 `export KEY=VALUE`
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            skipped.append(raw_line)
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]                 # 成对引号：原样取，不处理行内注释
        else:
            # 未加引号时，` #` 之后算行内注释（`#` 前必须有空白，否则是值的一部分）
            for mark in (" #", "\t#"):
                idx = value.find(mark)
                if idx >= 0:
                    value = value[:idx].rstrip()
                    break
        values[key] = value

    return values, skipped


def load_env_file(
    path: Optional[Path] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    override: bool = False,
) -> list[str]:
    """把 `.env` 里的键值放进环境变量。

    `path` 是 `.env` 路径（默认仓库根的 `.env`）；`environ` 是目标环境映射
    （默认 `os.environ`，便于测试注入）；`override=False`（默认）时**不覆盖**
    已存在且非空的变量。

    返回**实际设置的键名**列表（按文件顺序，去重）。
    ★ 只返回键名 —— 值里可能有 API Key，任何调用方都不该把它打印出来。
    """
    env = os.environ if environ is None else environ  # type: ignore[assignment]
    env_file = Path(path) if path is not None else DEFAULT_ENV_FILE

    try:
        text = env_file.read_text(encoding="utf-8-sig")   # ★ utf-8-sig：带 BOM 也能读
    except FileNotFoundError:
        return []                                        # 口径 2：缺文件不是错误
    except OSError:
        return []                                        # 权限等：也按"没有"处理

    values, _skipped = parse_env_text(text)

    applied: list[str] = []
    for key, value in values.items():
        existing = env.get(key)
        if not override and existing is not None and str(existing).strip():
            continue                                     # 口径 1：真实环境变量优先
        env[key] = value  # type: ignore[index]
        applied.append(key)
    return applied
