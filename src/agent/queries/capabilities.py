"""Discovery from tool declarations AND policy. This view never grants permissions."""
from dataclasses import dataclass
from ..common.enums import ToolPermission
from ..tools.policy import BLIND_TO_RAW_TELEMETRY

SOURCES={'metrics':'metrics','logs':'logs','traces':'traces','change':'changes',
    'topology':'topology','database_read':'database','runtime_read':'runtime',
    'lifecycle_read':'lifecycle','sdk_access_read':'sdk_access'}


@dataclass(frozen=True)
class QueryCapability:
    definition:object
    roles:tuple[str,...]
    source:str

    @property
    def schema(self):return self.definition.spec.input_schema


def source_of(definition):
    matches=[SOURCES[c] for c in getattr(definition,'capabilities',()) if c in SOURCES]
    return matches[0] if len(set(matches))==1 else ''


def view(registry):
    found={}
    for name,definition in sorted(registry.all().items()):
        source=source_of(definition)
        if not source or definition.spec.permission!=ToolPermission.READ_ONLY:continue
        # raw_ref replay and trace drilldown remain controlled followups, not cause queries.
        props=definition.spec.input_schema.get('properties',{})
        if 'raw_ref' in props or 'trace_id' in props and not 'query' in props:continue
        if not {'query','service','start','end','change_id','instance','object_name'}&set(props):continue
        roles=tuple(sorted(r for r in definition.granted_roles if r not in BLIND_TO_RAW_TELEMETRY and registry.policy.can_call(r,definition).allowed))
        if roles:found[name]=QueryCapability(definition,roles,source)
    return found
