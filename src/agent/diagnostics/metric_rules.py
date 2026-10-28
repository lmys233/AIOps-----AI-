"""Absolute diagnostic thresholds; no inferred service SLOs or pool capacity."""
import json
import os
from ..agents.anomaly import Detection,point_time
from ..common.enums import EvidenceDirection


def absolute_detection(points,signal):
    if os.getenv('AIOPS_APPLICATION_DIAGNOSTICS','off').lower() not in ('true','on','1'):return None
    thresholds={'pool_waiting':1.0}
    try:thresholds.update(json.loads(os.getenv('AIOPS_DIAGNOSTIC_ABSOLUTE_THRESHOLDS','{}')))
    except (ValueError,TypeError):return None
    threshold=thresholds.get(signal.name)
    if not isinstance(threshold,(int,float)) or threshold<=0:return None
    hits=[p for p in points if len(p)>=2 and float(p[1])>=threshold]
    if not hits:return None
    return Detection(direction=EvidenceDirection.SPIKE,baseline=None,
        magnitude=max(float(p[1]) for p in hits),onset=point_time(hits[0]),
        peak=max(float(p[1]) for p in hits))
