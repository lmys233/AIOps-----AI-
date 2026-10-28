"""Execution intent and ceilings only; no sources, persistence or model imports."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class DiagnosisCommand:
    mode: Literal['initial', 'directed'] = 'initial'
    problem_id: str = ''
    candidate_ids: tuple[str, ...] = ()
    requirement_key: str = ''
    recheck: bool = False

    def __post_init__(self):
        if self.mode not in ('initial', 'directed'):
            raise ValueError('未知诊断执行目的')
        if self.mode == 'directed' and (not self.problem_id or not self.candidate_ids):
            raise ValueError('定向核查必须限定问题与候选')
        if self.mode == 'initial' and (self.problem_id or self.candidate_ids or self.requirement_key or self.recheck):
            raise ValueError('初诊不能隐式携带定向选择')
        if not isinstance(self.candidate_ids, tuple) or any(not x for x in self.candidate_ids):
            raise ValueError('候选范围必须为不可变非空标识列表')

    @property
    def max_iterations(self): return 3 if self.mode == 'initial' else 1

    @property
    def max_queries(self): return 16 if self.mode == 'initial' else min(8,max(3,2*len(self.candidate_ids)))

    @property
    def batch_size(self): return 8 if self.mode == 'initial' else self.max_queries

    def data(self):
        return dict(mode=self.mode, problem_id=self.problem_id, candidate_ids=list(self.candidate_ids),
                    requirement_key=self.requirement_key, recheck=self.recheck)
