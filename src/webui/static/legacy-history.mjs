// Readonly legacy report/download entry; no session adoption or new query.
export function setupLegacy(api,exportReport,doc=document) {
  const box=doc.getElementById('legacy-history'),list=doc.getElementById('legacy-history-list');
  box.addEventListener('toggle',async()=>{
    if(!box.open)return;
    list.textContent='正在读取旧版档案…';
    try {
      const {status,data}=await api('/v1/legacy-incidents');
      if(status!==200)throw Error(`HTTP ${status}`);
      list.replaceChildren();
      for(const row of data.incidents || []) {
        const card=doc.createElement('article');card.className='card';
        const label=doc.createElement('p');label.textContent=row.report || row.incident_id;card.append(label);
        const id=doc.createElement('p');id.className='mono muted';id.textContent=row.incident_id;card.append(id);
        const button=doc.createElement('button');button.textContent='保存原版 Markdown';button.addEventListener('click',()=>exportReport(button,row));
        const note=doc.createElement('span');note.dataset.exportNote='';card.append(button,note);list.append(card);
      }
      if(!list.childNodes.length)list.textContent='没有可读取的未分系统事故记录。';
    } catch(error) {list.textContent=`旧版档案读取失败：${error.message}`;}
  });
}
