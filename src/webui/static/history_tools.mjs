// Restore completed call payloads in first-observed order, without editing archives.
export function restoredCalls(turn) {
  const records=Array.isArray(turn?.tools) ? turn.tools : [];
  const payloads=new Map(),saved=[];
  records.forEach((item,index)=>{
    const key=item.call_id ? String(item.call_id) : `legacy-record-${index}`;
    if(!payloads.has(key))saved.push(key);
    payloads.set(key,item); // Same live card receives its latest completed result.
  });
  const ordered=[],seen=new Set(),missing=new Set();
  for(const step of turn?.steps || []) {
    if(step.event!=="tool")continue;
    const key=String(step.data?.call_id || step.call_id || "");
    if(!key)continue;
    if(!payloads.has(key)) {missing.add(key);continue;}
    if(!seen.has(key)) {seen.add(key);ordered.push(key);}
  }
  const withoutPosition=saved.filter(key=>!seen.has(key));
  ordered.push(...withoutPosition);
  return {calls:ordered.map(key=>({key,payload:{...payloads.get(key),phase:"done"}})),
    missing:missing.size,withoutPosition:withoutPosition.length};
}
