"""Immutable JSON values for facts and presentation; no infrastructure imports."""
import hashlib
import json
from dataclasses import dataclass


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class DiagnosisSnapshot:
    payload_json:str

    @classmethod
    def capture(cls,body):
        ex=body.get('explanation') or {}
        facts={k:body.get(k) for k in ('root_cause','hypotheses','evidence','slots')}
        facts.update(attribution_v2=ex.get('attribution_v2'),scope=ex.get('observed_system'),
            submissions=(ex.get('dialogue') or {}).get('submissions',[]))
        return cls(encoded(facts))

    def data(self):return json.loads(self.payload_json)

    def reference(self):
        data=self.data();sha=hashlib.sha256(self.payload_json.encode('utf-8')).hexdigest()
        return {'snapshot_id':'diag_'+sha[:24],'version':1,'sha256':sha,
            'evidence_sha256':digest(data.get('evidence')),
            'problem_count':len((data.get('attribution_v2') or {}).get('problems',[])),
            'evidence_count':len(data.get('evidence') or [])}


@dataclass(frozen=True)
class AnswerView:
    payload_json:str

    @classmethod
    def create(cls,content,diagnosis):
        view=dict(content,view_version=1,diagnosis_ref=diagnosis.reference())
        view['view_id']='view_'+digest(view)[:24]
        return cls(encoded(view))

    def data(self):return json.loads(self.payload_json)

    @classmethod
    def verify(cls,view):
        content={k:v for k,v in view.items() if k!='view_id'}
        if view.get('view_id')!='view_'+digest(content)[:24]:raise ValueError('saved answer integrity mismatch')
        return cls(encoded(view))
