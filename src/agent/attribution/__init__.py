"""归因层：候选假设 → 打分 → 反事实检验 → 仲裁。

--------------------------------------------------------------------------------
★ 与 `agents/`（取证层）的边界
--------------------------------------------------------------------------------

  · **取证层**（`agents/`）：能碰**原始遥测**（Prometheus/Loki/Tempo/PG），
    产出结构化 `Evidence`（写黑板）
  · **归因层**（`attribution/`，本包）：**只读黑板上的证据**，
    产出 `Hypothesis` / `ArbitResult`

★ 这不是文档约定，是**目录即权限**：本包的模块不导入任何数据源，
RCA/Critic 的工具集由 `enums.AGENT_TOOL_MATRIX` 限定为
`topology_read` / `memory_read`（RCA）与 `evidence_lookup`（Critic）。

--------------------------------------------------------------------------------
★ 四个文件
--------------------------------------------------------------------------------

  · `categories.py`：8 大类骨架（与编排层共用）。
    不变量：归因/编排两边的类别口径只有一份（无循环导入）
  · `likelihoods.py`：似然比表 + 先验权重。
    不变量：import 期自洽校验；每类既有支持又有反对证据
  · `rca.py`：假设生成 + 先验 + 对数空间 softmax。
    不变量：先验恒 < 1.0（不饱和）；只用证据、不碰遥测
  · `critic.py`：反事实检验（三条判据）。
    不变量：**只排除、不新增**；引用的证据必须读得回来
  · `arbiter.py`：四条仲裁规则。
    不变量：拒答必须给 report；top-2 接近 → 定向取证
"""

from __future__ import annotations

from .arbiter import (ABSTAIN_BELOW, CONFIRM_SCORE, DEFAULT_THRESHOLDS, TOP2_CLOSE,
                      ArbiterThresholds, arbitrate)
from .categories import (CATEGORY_AGENT_ROLE, CATEGORY_LABEL, CATEGORY_SIGNALS,
                         CATEGORY_UNIVERSE)
from .critic import CriticAgent, CriticReport, LookupFailed, LookupFn
from .likelihoods import (AGAINST_LR, LIKELIHOOD_TABLE, LR_LEVELS, NEUTRAL_LR,
                          PRIOR_WEIGHTS, STRONG_AGAINST_LR, STRONG_LR, SUPPORT_LR,
                          EvidenceRule, likelihood_for, rules_of)
from .rca import (CATEGORY_PRIOR_AFFINITY, MIN_PRIOR, PRIOR_FLOOR, RcaAgent,
                  anomaly_onset, compute_prior, prior_components)

__all__ = [
    # 类别骨架
    "CATEGORY_SIGNALS", "CATEGORY_LABEL", "CATEGORY_AGENT_ROLE", "CATEGORY_UNIVERSE",
    # 似然比与先验权重
    "LIKELIHOOD_TABLE", "PRIOR_WEIGHTS", "LR_LEVELS", "EvidenceRule",
    "likelihood_for", "rules_of",
    "STRONG_LR", "SUPPORT_LR", "NEUTRAL_LR", "AGAINST_LR", "STRONG_AGAINST_LR",
    # RCA
    "RcaAgent", "compute_prior", "prior_components", "CATEGORY_PRIOR_AFFINITY",
    "PRIOR_FLOOR", "MIN_PRIOR", "anomaly_onset",
    # Critic
    "CriticAgent", "CriticReport", "LookupFn", "LookupFailed",
    # 仲裁
    "arbitrate", "ArbiterThresholds", "DEFAULT_THRESHOLDS",
    "CONFIRM_SCORE", "TOP2_CLOSE", "ABSTAIN_BELOW",
]
