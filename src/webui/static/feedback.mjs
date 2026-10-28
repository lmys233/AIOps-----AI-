// One component for live and restored answers. Saved answer content is immutable.
export function nextVote(current, clicked) { return current===clicked?null:clicked; }

export function bindFeedback(card, {body, target=null, feedback=null, unavailable=false, api}) {
  const oid=body.explanation?.observed_system?.observed_id;
  const sid=body.session_id;
  target=target || {turn_id:body.turn_id,answer_ref:body.explanation?.answer?.view_id};
  if(!oid || !sid || !target?.turn_id || !target?.answer_ref)return;
  let saved=feedback || {vote:null,revision:0}; let pending=false;
  const area=document.createElement('div');area.className='answer-feedback';
  const up=document.createElement('button'),down=document.createElement('button');
  up.type=down.type='button';up.textContent='👍 点赞';down.textContent='👎 点踩';
  const details=document.createElement('details'),label=document.createElement('summary');label.textContent='补充原因（可选）';details.append(label);
  const reason=document.createElement('select');reason.setAttribute('aria-label','反馈原因');
  for(const [value,text] of [['','选择原因'],['concise','希望更简洁'],['detailed','希望解释详细'],['plain_language','希望更通俗'],['examples','希望有例子'],['structured','结构清楚'],['explain_gaps','请说明还缺哪些数据'],['evidence_first','先给依据'],['effective_process','核查流程有效'],['too_long','太长'],['unclear','不够清楚'],['missing_evidence','缺少依据'],['incorrect','可能不准确'],['unhelpful','未回答问题']]) {
    const option=document.createElement('option');option.value=value;option.textContent=text;reason.append(option);
  }
  reason.value=saved.reason_codes?.[0] || '';
  const comment=document.createElement('textarea');comment.maxLength=1000;comment.placeholder='备注（最多1000字，不要填写凭据）';comment.value=saved.comment || '';
  details.append(reason,comment);
  const note=document.createElement('span');note.className='muted';note.setAttribute('role','status');
  if(unavailable)note.textContent='反馈状态暂不可读，请刷新后重试';
  function render() {
    up.setAttribute('aria-pressed',String(saved.vote==='up'));down.setAttribute('aria-pressed',String(saved.vote==='down'));
    up.disabled=down.disabled=pending || unavailable;reason.disabled=comment.disabled=pending;
  }
  async function save(clicked) {
    if(pending || unavailable)return;
    pending=true;render();note.textContent='保存中…';
    const data={answer_ref:target.answer_ref,vote:nextVote(saved.vote,clicked),reason_codes:reason.value?[reason.value]:[],
                comment:comment.value,expected_revision:saved.revision,
                request_id:globalThis.crypto?.randomUUID?.() || `fb_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`};
    try {
      const response=await api(`/v1/observed-systems/${encodeURIComponent(oid)}/sessions/${encodeURIComponent(sid)}/turns/${encodeURIComponent(target.turn_id)}/feedback`,
        {method:'PUT',body:data,headers:{'X-AIOps-Feedback':'1'}});
      if(response.stale || !card.isConnected)return;
      if(response.status===200 && response.data?.saved) { saved=response.data.feedback;note.textContent=saved.vote?'已保存':'已撤销'; }
      else if(response.status===409 && response.data?.feedback) { saved=response.data.feedback;note.textContent='其他页面已更新，已显示最新状态，请重选'; }
      else note.textContent='未保存，请重试';
    } catch { if(card.isConnected)note.textContent='未保存，请检查连接后重试'; }
    finally { pending=false;render(); }
  }
  up.addEventListener('click',()=>save('up'));down.addEventListener('click',()=>save('down'));
  area.append(up,down,details,note);card.append(area);render();
}
