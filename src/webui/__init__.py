"""用户 ↔ AIOps 的 Web 台。

| 文件 | 作用 |
|---|---|
| `server.py` | 静态托管（`/webui/`）+ **只读 BFF**（`/webui/api/capabilities`，能力表从 `enums` 现算） |
| `static/index.html` | 单页应用骨架：侧栏 + 主区 + 状态条（风格参考 dsh web：暗色 / 卡片 / 徽标 / 等宽技术区） |
| `static/app.js` | 七个面板：对话（同步 + **SSE 时间线**）/ 澄清卡 / 审批卡 / 告警 / 事故 / 能力 / 状态 |
| `static/style.css` | 手写样式（**零外部依赖**：不引 CDN，离线自持） |

★ 起法：`python -m agent --port 8013` → 打开 `http://127.0.0.1:8013/webui/`
★ 前端不新增业务语义：所有数据都来自现有契约端点 + `/health` + 上面的 BFF。
"""

from __future__ import annotations

from .server import STATIC_DIR, build_capabilities, install_webui

__all__ = ["STATIC_DIR", "build_capabilities", "install_webui"]
