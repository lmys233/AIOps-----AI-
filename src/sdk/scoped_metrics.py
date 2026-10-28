"""Scope the single bound SDK process's exposition without changing business metrics."""
from copy import copy


class Collector:
    def __init__(self,registry,identity):self.registry=registry;self.identity=identity

    def collect(self):
        scope={k:self.identity[k] for k in ('tenant','observed_id','env','service')}
        if self.identity.get('cluster'):scope['cluster']=self.identity['cluster']
        for metric in self.registry.collect():
            clone=copy(metric);clone.samples=[]
            for sample in metric.samples:
                if sample.labels.get('service') not in (None,self.identity['service']):continue
                clone.samples.append(sample._replace(labels=dict(sample.labels,**scope)))
            if clone.samples:yield clone
