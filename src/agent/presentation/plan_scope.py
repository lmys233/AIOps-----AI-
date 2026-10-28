"""A focused plan is a view of a task, not a replacement for all its facts."""


def focus(rendered,source,problem):
    ex=rendered['explanation']
    ex['plan_scope']={'problem_id':problem['problem_id'],'service':problem['service'],
        'based_on_incident':source['incident_id'],'scope_kind':'focused_plan',
        'note':'本轮方案只围绕所选问题；任务完整诊断保留，本轮未重新采集。'}
    answer=ex.setdefault('answer',{})
    answer['summary']='本轮只给 '+problem['service']+' 的所选问题提供方案；任务中其他问题保留，未重新采集。 '+str(answer.get('summary') or '')
    answer.setdefault('details',[]).insert(0,{'label':'方案范围',
        'value':problem['service']+' · '+problem['problem_id']+'；'+ex['plan_scope']['note']})
    return rendered


def preserve(rendered,source,problem):
    # Compatibility for workers that loaded the previous plan adapter before reload.
    from ..dialogue.plan_projection import preserve as adapter
    return adapter(rendered,source,problem)
