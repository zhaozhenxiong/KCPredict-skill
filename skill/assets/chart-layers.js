// Presentation only: every indicator and source event comes from the reviewed payload.
const L=D.layers||{}, views=L.frequencies||{}, dailyView=views['日线']||{bars:D.bars,pens:D.pens,zones:D.zones};
let G=dailyView, frequency='日线';
function periodKey(value){
  const day=short(value); if(frequency==='月线')return day.slice(0,7);
  if(frequency==='周线'){const d=new Date(day+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+(7-d.getUTCDay())%7);return d.toISOString().slice(0,10);}
  return day;
}
function barIndex(value){const key=periodKey(value);return B.findIndex(b=>periodKey(b.time)===key);}
function sourceRows(){return (L.source_events||[]).filter(r=>r.confirmed===true&&r.input_eligible===true&&r.candidate_eligible===true&&r.evidence_visible===true&&!r.excluded);}
function markerRows(){
  let rows=[];
  if(state.layers.fractals)for(const r of G.fractals||[])rows.push({family:'fractals',time:r.time,side:r.kind==='top'?'SELL':'BUY',label:(r.is_confirmed?'':'形成')+(r.kind==='top'?'顶':'底'),color:r.is_confirmed?colors.accent:colors.muted,detail:'锚点 '+short(r.time)+'；确认 '+(r.confirmed_at||'尚未确认')});
  if(state.layers.divergences)for(const r of G.divergences||[])rows.push({family:'divergences',time:r.confirmed_at||r.time,side:String(r.kind).includes('top')?'SELL':'BUY',label:r.label||'背驰参考',detail:JSON.stringify(r)});
  if(state.layers.sources)for(const r of sourceRows())rows.push({family:'sources',time:r.first_available_at||r.available_at||r.date,side:r.side,label:({structure:'结',wyckoff:'量',ict:'ICT'}[r.source]||r.source)+(r.side==='BUY'?' B':' S'),detail:(r.event_type||r.event)+'；首次可用 '+(r.first_available_at||r.available_at)+'；参考价 '+fmt(r.reference_price)});
  if(state.layers.wyckoff)for(const r of sourceRows().filter(r=>r.source==='wyckoff'))rows.push({family:'wyckoff',time:r.first_available_at||r.date,side:r.side,label:r.event||r.event_type,color:r.side==='BUY'?palette.wyckoff_buy:palette.wyckoff_sell,shape:r.side==='BUY'?'circle':'square',detail:'威科夫独立来源；确认 '+(r.confirmed_at||r.first_confirmed_at)});
  if(state.layers.ict&&frequency==='日线'){
    const candidates=[...(I.candidates||[])];
    if(I.latest_candidate)candidates.push(I.latest_candidate);
    const seen=new Set();
    for(const r of candidates){const time=r.sweep?.time;const id=JSON.stringify([time,r.direction]);if(!time||seen.has(id))continue;seen.add(id);const buy=r.direction==='long';rows.push({family:'ict',time,side:buy?'BUY':'SELL',label:buy?'SSL候选':'BSL候选',color:buy?palette.ict_buy:palette.ict_sell,shape:buy?'circle':'square',detail:'ICT '+r.mechanical_status+'；'+r.reason+'；不等于最终确认'});}
  }
  return rows;
}
function drawIndicators({x,y,line,valuesOnly=false}){
  const rows=G.indicators?.rows||[], specs=[];
  if(state.layers.ma)for(const n of [5,10,20,60])specs.push(['ma'+n,palette['ma'+n],'']);
  if(state.layers.boll)for(const k of ['boll_upper','boll_mid','boll_lower'])specs.push([k,palette.blue,'4 3']);
  if(state.layers.chips)for(const k of ['chip70_low','chip50','chip70_high'])specs.push([k,palette.chips,'3 3']);
  const values=[],svg=[];
  for(const [key,color,dash] of specs){let points=[];const flush=()=>{if(points.length)svg.push(line(points,color,1,dash));points=[];};
    for(let i=state.start;i<=Math.min(N-1,state.end);i++){
      const row=rows.find(r=>short(r.time)===short(B[i].time)),value=row?.[key];
      if(finite(value)){values.push(value);if(!valuesOnly)points.push([x(i),y(value)]);}else if(!valuesOnly)flush();
    }if(!valuesOnly)flush();
  }
  return valuesOnly?values:svg.join('');
}
function drawMarkers({x,y,step,interaction}){
  const grouped=new Map();
  for(const row of markerRows()){const i=barIndex(row.time);if(i<state.start||i>state.end||i<0)continue;const key=i+':'+row.side+':'+row.family;const group=grouped.get(key)||[];group.push(row);grouped.set(key,group);}
  let html='';const lanes=new Map();
  for(const [key,rows] of grouped){const [ii,side]=key.split(':'),i=Number(ii),slot=ii+side,lane=lanes.get(slot)||0,buy=side==='BUY',r=rows[0],xx=x(i),yy=y(buy?B[i].low:B[i].high)+(buy?1:-1)*(13+lane*19),color=r.color||(buy?colors.up:colors.down);
    lanes.set(slot,lane+1);
    const label=rows.length>1?r.label+' +'+(rows.length-1):r.label;
    if(r.shape==='circle')html+='<circle cx="'+xx+'" cy="'+yy+'" r="3" fill="'+color+'"/>';
    else if(r.shape==='square')html+='<rect x="'+(xx-3)+'" y="'+(yy-3)+'" width="6" height="6" fill="'+color+'"/>';
    else html+='<path d="M'+xx+' '+(yy+(buy?-5:5))+' l-4 '+(buy?7:-7)+' h8 Z" fill="'+color+'"/>';
    if(step>7||rows.some(r=>r.label.includes(' B')||r.label.includes(' S')))html+='<text x="'+xx+'" y="'+(yy+(buy?15:-8))+'" text-anchor="middle" style="fill:'+color+';font-size:10px">'+e(label)+'</text>';
    interaction.push({x0:xx-12,x1:xx+12,y0:yy-18,y1:yy+22,text:rows.map(r=>r.label).join(' / '),detail:rows.map(r=>r.detail||'来源研究参考').join('；')});
  }return html;
}
function showDateEvidence(index){
  const bar=index<N?B[index]:null, row=bar?G.indicators?.rows?.find(r=>short(r.time)===short(bar.time)):null;
  $('indicatorReadout').textContent=row?['MA5 '+fmt(row.ma5),'MA10 '+fmt(row.ma10),'MA20 '+fmt(row.ma20),'MA60 '+fmt(row.ma60),'BOLL '+fmt(row.boll_lower)+' / '+fmt(row.boll_mid)+' / '+fmt(row.boll_upper),'70%估算 '+fmt(row.chip70_low)+'–'+fmt(row.chip70_high),(row.is_closed===false?'形成中指标':'已闭合指标')].join('　'):'此日期没有已提供的历史指标';
  const selected=state.pinned&&bar?short(bar.time):null, family=$('eventFamily').value;
  const ictEvents=(I.candidates||[]).filter(r=>barIndex(r.sweep?.time)>=0).map(r=>({source:'ict',date:r.sweep.time,side:r.direction==='long'?'BUY':'SELL',event_type:(r.direction==='long'?'SSL':'BSL')+' 机械候选 · '+r.mechanical_status,first_available_at:r.sweep.time,reference_price:r.sweep.extreme,evidence:r}));
  const events=[...(L.source_events||[]),...ictEvents].filter(r=>(!family||r.source===family)&&(!selected||periodKey(r.first_available_at||r.available_at||r.date)===periodKey(selected))).sort((a,b)=>String(b.first_available_at||b.date).localeCompare(String(a.first_available_at||a.date)));
  $('eventSelection').textContent=selected?'固定 '+selected:'本次窗口 · '+events.length+'条';
  $('sourceEventList').innerHTML=events.length?events.map(r=>'<article class="source-event"><div><span>'+e(({structure:'CZSC 结构',wyckoff:'威科夫量价',ict:'ICT'}[r.source]||r.source))+'</span><time>'+e(short(r.first_available_at||r.date))+'</time></div><strong style="color:'+(r.side==='BUY'?colors.up:colors.down)+'">'+e(r.event_type||r.event)+' · '+e(r.side)+'</strong><p>可用：'+e(r.first_available_at||r.available_at||'未报告')+'<br>确认：'+e(r.first_confirmed_at||r.confirmed_at||'未报告')+'<br>来源参考价 '+fmt(r.reference_price)+' · 非成交价</p><details><summary>原始证据</summary><pre>'+e(JSON.stringify(r,null,2))+'</pre></details></article>').join(''):'<p class="note">没有匹配的来源事件。未返回不代表历史从未发生。</p>';
  const pv=bar?(L.volume_rows||[]).find(r=>short(r.date)===short(bar.time)):null;
  $('volumeMetrics').innerHTML=pv?table(['量价特征','原始值'],Object.entries(pv).filter(([key])=>key.startsWith('w_')).map(([key,value])=>[key,typeof value==='number'?fmt(value,4):String(value)])):'<p class="note">当前周期／日期未提供逐日量价特征。</p>';
}
function drawChips(y,top,bottom){
  const host=$('chipProfile');host.hidden=!state.layers.chips;if(host.hidden)return;
  const index=state.hover??N-1,bar=index<N?B[index]:null,profile=bar?G.indicators?.chip_profiles?.[short(bar.time)]:null;
  if(!profile||profile.status!=='available'){host.innerHTML='<p>筹码估算</p><small>'+(bar?'缺少该日有效分布':'未来情景无筹码分布')+'</small>';return;}
  const bins=profile.bins||[],max=Math.max(...bins.map(b=>b.mass_fraction),.001),height=$('chart').clientHeight;
  let svg='<svg viewBox="0 0 120 '+height+'" aria-label="历史成交量加权筹码分布"><text x="4" y="17">筹码估算 · '+e(short(profile.as_of).slice(5))+'</text>';
  for(const b of bins){const hi=Math.max(top,y(b.price_high)),lo=Math.min(bottom,y(b.price_low));if(lo<=hi)continue;const inside=b.price_high>=profile.chip70_low&&b.price_low<=profile.chip70_high;svg+='<rect x="4" y="'+hi+'" width="'+(108*b.mass_fraction/max)+'" height="'+Math.max(1,lo-hi)+'" fill="'+(inside?palette.chips:palette.muted)+'" opacity="'+(inside?.8:.35)+'"/>';}
  if(finite(profile.chip50))svg+='<line x1="0" x2="116" y1="'+y(profile.chip50)+'" y2="'+y(profile.chip50)+'" stroke="'+palette.chips+'" stroke-dasharray="3 3"/>';
  host.innerHTML=svg+'</svg><small>120日 HLC/3 量权分布<br>非真实持仓成本</small>';
}
function selectFrequency(name){
  frequency=name;G=views[name]||dailyView;B=G.bars;F=name==='日线'?(P.forecast||[]):[];N=B.length;H=F.length;T=N+H;last=B[N-1];dates=B.map(b=>short(b.time)).concat(F.map(f=>short(f.date)));dateIndex=new Map(dates.map((d,i)=>[d,i]));
  state.start=0;state.end=T-1;state.pinned=false;state.hover=null;
  document.querySelectorAll('[data-frequency]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.frequency===name)));
  $('periodNote').textContent=name==='日线'?'真实历史与未来情景共用时间轴':'预测与 ICT 扫描基于日线；切回日线查看。灰色末柱为形成中，不作闭合确认。';
  $('scenarioNote').textContent=name==='日线'?(H?'最后实际收盘连接 '+H+' 日采样情景':'未产生未来路径'):'当前展示原始'+name+'；未将日线预测改造成高周期预测';
  inspect(N-1);draw();
}
function bindLayers(){
  document.querySelectorAll('[data-frequency]').forEach(button=>{const v=views[button.dataset.frequency];button.disabled=button.dataset.frequency!=='日线'&&!v?.bars?.length;button.addEventListener('click',()=>selectFrequency(button.dataset.frequency));});
  $('eventFamily').addEventListener('change',()=>showDateEvidence(state.hover??N-1));
}
