"""Reserve a bounded whole stage in PG before any physical model dispatch."""
import time
from contextlib import asynccontextmanager
from ..orchestration.budget import Budget
from ..common.model_budget import model_budget_scope
from ..llm.accounting import collect_records,summary


@asynccontextmanager
async def reservation(store,tenant,subject,config,tokens):
    if min(config.daily_calls,config.daily_tokens,config.daily_wall_sec)<=0:
        raise RuntimeError('background model budget not configured; feedback remains recorded')
    pool=await store.connect();calls=3;wall=60
    async with pool.acquire() as c,c.transaction():
        await c.execute('INSERT INTO agent_preference_budget(tenant,subject,day) VALUES($1,$2,current_date) ON CONFLICT DO NOTHING',tenant,subject)
        day=await c.fetchval('''UPDATE agent_preference_budget SET calls=calls+$3,tokens=tokens+$4,wall_ms=wall_ms+$5
            WHERE tenant=$1 AND subject=$2 AND day=current_date AND calls+$3<=$6 AND tokens+$4<=$7 AND wall_ms+$5<=$8 RETURNING day''',
            tenant,subject,calls,tokens,wall*1000,config.daily_calls,config.daily_tokens,config.daily_wall_sec*1000)
        if day is None:raise RuntimeError('background daily budget exhausted')
    started=time.monotonic();budget=Budget(max_llm_calls=calls,max_tokens=tokens,max_wall_time_sec=wall)
    with collect_records() as records,model_budget_scope(budget,pre_reserved=False):
        try:yield records
        finally:
            totals=summary(records)
            # Unknown dispatches retain their reservation. A crash likewise leaves the PG debit.
            known=totals['usage_known'] and bool(records)
            used_tokens=totals['reported_tokens'] if known else max(tokens,totals['reported_tokens']+totals['reserved_tokens'])
            used_calls=totals['physical_attempts'] if known else max(calls,totals['physical_attempts'])
            await store.fetch('''UPDATE agent_preference_budget SET calls=calls-$4,tokens=tokens-$5,wall_ms=wall_ms-$6
                WHERE tenant=$1 AND subject=$2 AND day=$3 RETURNING calls''',tenant,subject,day,
                calls-used_calls,tokens-used_tokens,wall*1000-int((time.monotonic()-started)*1000))
