"""Request-local entity vocabulary from registry; shared router configuration never mutates."""
from dataclasses import replace
import re
from .context import current


def scoped(config):
    scope=current()
    if not scope:return config
    return vocabulary(config,scope.names,scope.cluster or '')


def vocabulary(config,names,cluster=None):
    aliases={n:a for n,a in config.services if n in names}
    services=tuple((n,aliases.get(n,())) for n in names)
    # Chinese prose can immediately follow an ASCII identifier; Unicode \w
    # incorrectly makes “ledger-v2超时” fail to match the registered service.
    patterns={n:config.service_re.get(n) or re.compile(r'(?<![a-z0-9_.:-])'+re.escape(n.lower())+r'(?![a-z0-9_.:-])') for n in names}
    return replace(config,services=services,service_re=patterns,
        alias_pairs=tuple((a,n) for a,n in config.alias_pairs if n in names),
        default_cluster=config.default_cluster if cluster is None else cluster)
