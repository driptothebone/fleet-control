/* Original standalone Model Board interface, adapted to authenticated host SDK. */
(function(root){
'use strict';
function createController(request){
  const state={data:null,selected:[],query:'',draft:{targets:['delegation'],route:'explicit',provider:'fixture',model:'model-b',effort:null},preview:null,confirmed:false,result:null,error:'',busy:false};
  const listeners=new Set();
  let sentDraft=null, generation=0,disposed=false;
  function emit(){if(!disposed)listeners.forEach(fn=>fn());}
  function edit(fn){if(disposed||state.busy)return;generation++;fn();state.preview=null;state.confirmed=false;sentDraft=null;state.error='';state.result=null;emit();}
  const c={state,
    invalidate(){if(state.applying)return;generation++;state.editProfiles=null;state.preview=null;state.confirmed=false;sentDraft=null;state.busy=false;emit();},
    dispose(){disposed=true;generation++;sentDraft=null;state.preview=null;state.confirmed=false;state.data=null;listeners.clear();},
    intent(d){edit(()=>{state.editProfiles=d.profiles.slice();const {profiles,expected_revisions,...draft}=d;state.draft=draft;});},
    subscribe(fn){listeners.add(fn);return()=>listeners.delete(fn);},
    async load(){const g=++generation;state.busy=true;state.error='';state.preview=null;state.confirmed=false;emit();try{const data=await request('/state');if(!disposed&&g===generation){state.data=data;state.editProfiles=null;state.selected=state.selected.filter(p=>(data.profiles||[]).some(r=>r.profile===p));}}catch(e){if(!disposed&&g===generation){state.data=null;state.selected=[];state.error='Read unavailable';}}finally{if(!disposed&&g===generation){state.busy=false;emit();}}},
    filter(q){state.query=q;emit();},
    visible(){const terms=state.query.toLowerCase().split(/[,\s]+/).filter(Boolean);return (state.data?.profiles||[]).filter(p=>!terms.length||terms.some(t=>p.profile.toLowerCase().includes(t)));},
    toggle(p){edit(()=>{if(!(state.data?.fixture===true||state.data?.available===true)||!(state.data?.profiles||[]).some(r=>r.profile===p&&r.editable!==false))return;state.selected=state.selected.includes(p)?state.selected.filter(v=>v!==p):state.selected.concat([p]);});},
    setDraft(patch){edit(()=>{state.editProfiles=null;Object.assign(state.draft,patch);if(state.draft.route!=='explicit'){state.draft.provider='';state.draft.model='';}});},
    problem(){const d=state.draft;if(!state.selected.length)return 'Select at least one profile';const wide=d.max_concurrent_children!=null||d.service_tier!=null||d.fallback!=null;if(!d.targets.length&&!wide)return 'Choose at least one target';if(d.targets.includes('main')&&['inherit','auto'].includes(d.route))return 'Main cannot inherit or use auto';if(d.targets.includes('delegation')&&d.route==='auto')return 'Delegation has no auto mode';if(d.route==='explicit'&&(!d.provider||!d.model))return 'Choose a provider and model';if(d.route==='keep'&&d.effort===null&&!wide)return 'Choose a route or effort change';return '';},
    confirm(on){if(!state.busy){state.confirmed=on===true;emit();}},
    async preview(){const profiles=state.editProfiles||state.selected;if(disposed||state.busy||!profiles.length||!state.data)return;const g=++generation;state.busy=true;state.error='';state.result=null;state.preview=null;state.confirmed=false;emit();
      const revs={};profiles.forEach(p=>{revs[p]=state.data.profiles.find(r=>r.profile===p).revision;});
      sentDraft=JSON.parse(JSON.stringify(Object.assign({},state.draft,{profiles:profiles.slice(),expected_revisions:revs})));
      try{const preview=await request('/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sentDraft)});if(!disposed&&g===generation)state.preview=preview;}catch(e){if(!disposed&&g===generation){state.error=String(e.message||'Preview failed');sentDraft=null;}}finally{if(!disposed&&g===generation){state.busy=false;emit();}}},
    async apply(){if(disposed||state.busy||!state.preview||!state.confirmed)return;
      const g=++generation,token=state.preview.token,draft=sentDraft;state.busy=true;state.applying=true;state.error='';state.preview=null;state.confirmed=false;sentDraft=null;emit();
      try{const result=await request('/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({draft,token,confirmed:true})});
        if(disposed||g!==generation)return;state.result=result;
        try{const data=await request('/state');if(!disposed&&g===generation)state.data=data;}
        catch(e){if(!disposed&&g===generation){state.data=null;state.selected=[];state.error='Saved outcome shown below; reload unavailable. No edit authority retained.';}}
      }catch(e){if(!disposed&&g===generation)state.error=String(e.message||'Save outcome uncertain; reload, never replay');}
      finally{if(!disposed&&g===generation){state.applying=false;state.busy=false;emit();}}
    }
  };
  return c;
}
const view={
  value(v){return v.present?JSON.stringify(v.value):'(not present)';},
  state(data){
    const tasks=new Map();
    const profiles=(data.profiles||[]).map(p=>{
      (p.tasks||[]).forEach(t=>tasks.set(t.task,t));
      const r=p.routes||{},main=r.model||{},del=r.delegation||{};
      const route=(b,task,scope)=>({provider:b.provider||'',model:b.model||'',effort:b.reasoning_effort,task,
        inheritance:scope==='delegation'&&!b.provider?'main':b.provider==='main'?'main':!b.provider||b.provider==='auto'?'auto-discovery':'explicit',provenance:'saved / unverified'});
      const aux=(p.tasks||Object.keys(r.auxiliary||{}).map(task=>({task}))).map(t=>route(r.auxiliary?.[t.task]||{},t.task,'auxiliary'));
      const mapped={...p,state:p.config_status==='unreadable'?'unavailable':'ready',read_only:p.editable===false,
        limits:{max_concurrent_children:del.max_concurrent_children},fallback:p.fallback||[],fallback_lock:p.fallback_lock||'',
        routes:{main:{provider:main.provider||'',model:main.default||'',effort:r.agent?.reasoning_effort,inheritance:'explicit',provenance:'saved / unverified'},delegation:route(del,'','delegation'),auxiliary:aux}};
      mapped.drift=[{...mapped.routes.delegation,scope:'delegation'},...aux.map(a=>({...a,scope:'auxiliary'}))].filter(a=>a.inheritance==='explicit'&&a.provider!==main.provider).map(a=>({scope:a.scope,task:a.task,reason:'configured off-provider pin; not runtime health'}));
      return mapped;
    });
    return {profiles,tasks:[...tasks.values()],efforts:data.efforts||['minimal','low','medium','high','xhigh'],provenance:data.inventory_provenance||'Host catalog metadata; availability/authentication unverified'};
  },
  command(data,intent){
    const targets=(intent.targets||[]).map(t=>typeof t==='string'?t:t.scope==='auxiliary'?'auxiliary.'+t.task:t.scope);
    const d={profiles:intent.profiles.slice(),targets,route:intent.route||'keep',provider:intent.provider||'',model:intent.model||'',effort:intent.effort??null,expected_revisions:{}};
    for(const p of d.profiles){const row=data.profiles.find(r=>r.profile===p);if(!row||row.editable===false||!row.revision)throw Error(p+': not editable');d.expected_revisions[p]=row.revision;}
    for(const key of ['service_tier','max_concurrent_children','fallback'])if(intent[key]!==undefined&&intent[key]!==null)d[key]=intent[key];
    return d;
  }
};
/* ---- Interface ---------------------------------------------------------
 * Same board, native theme: every colour/font/radius comes from board.css
 * tokens. Behaviour contract (preview → explicit confirm → single Apply →
 * readback) lives in the controller above and is unchanged here.
 */
const FIELD_LABEL={
  'model.default':'Main model','model.provider':'Main provider','model.context_length':'Model window',
  'agent.reasoning_effort':'Main effort','agent.service_tier':'Fast mode',
  'delegation.model':'Subagent model','delegation.provider':'Subagent provider','delegation.reasoning_effort':'Subagent effort',
  'delegation.max_concurrent_children':'Max subagents at once','fallback_providers':'Fallback chain'
};
const AUX_PART={provider:'provider',model:'model',reasoning_effort:'effort'};
const plural=(n,w)=>n+' '+w+(n===1?'':'s');
function luminance(s){
  s=String(s||'').trim();let m,r,g,b;
  if((m=/^#([0-9a-f]{6})$/i.exec(s))){const n=parseInt(m[1],16);r=n>>16&255;g=n>>8&255;b=n&255;}
  else if((m=/^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(s))){r=parseInt(m[1]+m[1],16);g=parseInt(m[2]+m[2],16);b=parseInt(m[3]+m[3],16);}
  else if((m=/rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(s))){r=+m[1];g=+m[2];b=+m[3];}
  else if((m=/color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/i.exec(s))){r=m[1]*255;g=m[2]*255;b=m[3]*255;}
  else return null;
  return (0.2126*r+0.7152*g+0.0722*b)/255;
}
function mount(container,c,options={}){
  const mode=options.standalone?'standalone':'host';
  container.innerHTML=`<div class="fleet-control" data-mode="${mode}" data-tone="dark"><main>
<div class="mb-bar"><h1 class="mb-title">Fleet Control</h1><span class="notice" id="notice" role="status" hidden></span><span class="sp"></span><button id="colourToggle" class="colour-switch" type="button" role="switch" aria-checked="false" title="Colour-code models, providers, values and status"><span class="track" aria-hidden="true"></span>Colour</button><button id="reload" type="button">Reload</button></div>
<p class="notice-detail" id="noticeDetail" hidden></p>
<div class="machines" id="machines" hidden></div>
<details class="sec" data-sec="overview" open><summary>Overview</summary>
  <div class="secbody"><div class="stats" id="stats"></div><div class="bars" id="bars"></div></div>
</details>
<details class="sec" data-sec="bulk" open><summary>Change several <span class="count" id="bulkCount"></span></summary>
  <div class="secbody">
    <div class="seg" id="scopeSeg" role="group" aria-label="Which profiles to change"><button type="button" data-s="all" class="on" aria-pressed="true">All</button><button type="button" data-s="sel" aria-pressed="false">Ticked (<span id="selN">0</span>)</button></div>
    <div id="scopeStrip" class="scope" aria-live="polite"></div>
    <div id="bulkRows"></div>
  </div>
</details>
<details class="sec" data-sec="profiles" open><summary>Profiles <span class="count" id="pCount"></span></summary>
  <div class="secbody"><div class="psearch"><label class="lbl" for="pq">Filter</label><input id="pq" type="search" placeholder="Names, comma-separated — e.g. alpha, beta" autocomplete="off" spellcheck="false"></div><div id="profiles"><div class="placeholder">Loading profiles…</div></div></div>
</details>
<div id="candidate-error" class="err" role="alert"></div><div id="result-panel" aria-live="polite"></div>
<p class="foot">Changes save profile defaults for future sessions; they don't switch or prove a running session.</p>
<details class="about" data-sec="about"><summary>About this board</summary><div class="about-body">
  <p id="candidate-status"></p>
  <p>Model lists come from this Hermes host's catalogue. A listed model doesn't prove login, entitlement or that it will answer.</p>
  <p id="aboutScope">Single host · saved defaults only. Jobs, federation, refresh and login controls are not included.</p>
  <p class="mono dim" id="provenance"></p>
</div></details></main>
<div class="overlay" id="ov"><div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheetTitle"><header><h2 id="sheetTitle" tabindex="-1"></h2><span class="sp"></span><button id="closeSheet" type="button">Close</button></header><p class="sheet-msg" id="sheetMsg" role="status"></p><div class="body" id="sheetBody"></div><div class="sheet-result" id="sheetResult" tabindex="-1" aria-live="polite"></div></div></div>
</div>`;
  const $=s=>container.querySelector(s),S={matrix:null,raw:null,sel:new Set(),scope:'all',busy:false,q:'',host:'',sheetVersion:0,previewVersion:0};
  const doc=container.ownerDocument,win=doc.defaultView;
  let disposed=false,previewToken=null,opener=null,shownResult=null,lastTrigger=null,lastCtx=null,lastData=null;const disabledBeforeApply=new WeakMap();
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const sheetOpen=()=>$('#ov').classList.contains('on');
  function toast(t){if(disposed)return;if(sheetOpen())$('#sheetMsg').textContent=t;else $('#candidate-error').textContent=t;}
  function ready(){return (S.matrix?.profiles||[]).filter(p=>p.state==='ready');}
  // Machines appear only when the owner configured another machine; otherwise every helper is a no-op.
  const multi=()=>Array.isArray(S.raw?.machines)&&S.raw.machines.length>0;
  const rowOf=n=>(S.matrix?.profiles||[]).find(p=>p.profile===n);
  const hostShown=p=>!multi()||!S.host||p.host===S.host;
  const oneMachine=list=>!multi()||new Set(list.map(n=>rowOf(n)?.host)).size<=1;
  const labelFor=n=>{if(!multi())return n;const p=rowOf(n);return p?(p.profile_name||n)+' · '+(p.host_label||p.host||''):n;};
  function terms(){return S.q.split(',').map(t=>t.trim().toLowerCase()).filter(Boolean);}
  function shown(p){const t=terms(),name=(p.profile_name||p.profile).toLowerCase();return hostShown(p)&&(!t.length||t.some(x=>name.includes(x)));}
  function allTargets(){return ready().filter(p=>shown(p)&&!p.read_only).map(p=>p.profile);}
  function targetProfiles(){return S.scope==='all'?allTargets():[...S.sel];}
  // Follow the host theme: light canvases get darker status text.
  function tone(){
    const root=$('.fleet-control');if(!root||!win?.getComputedStyle)return;
    const base=win.getComputedStyle(doc.documentElement).getPropertyValue('--background-base');
    const lum=luminance(base)??luminance(doc.body?win.getComputedStyle(doc.body).backgroundColor:'');
    if(lum!=null)root.dataset.tone=lum>0.5?'light':'dark';
  }
  // Colour roles: off by default (clean look); the owner's choice is kept per browser.
  const COLOUR_KEY='fleet-control:colour';
  function readColour(){try{return win?.localStorage?.getItem(COLOUR_KEY)==='on';}catch(e){return false;}}
  function setColour(on,save){
    const root=$('.fleet-control'),sw=$('#colourToggle');if(!root||!sw)return;
    if(on)root.dataset.colour='on';else delete root.dataset.colour;
    sw.setAttribute('aria-checked',String(!!on));
    if(save){try{win?.localStorage?.setItem(COLOUR_KEY,on?'on':'off');}catch(e){}}
  }
  setColour(readColour(),false);
  $('#colourToggle').onclick=()=>setColour($('#colourToggle').getAttribute('aria-checked')!=='true',true);
  const themeWatch=win?.MutationObserver?new win.MutationObserver(tone):null;
  if(themeWatch)themeWatch.observe(doc.documentElement,{attributes:true,attributeFilter:['style','class','data-theme','data-fixture-theme']});
const SETTINGS=[
  {key:'main-sub',label:'Main + subagents',targets:[{scope:'main',task:''},{scope:'delegation',task:''}],routes:['explicit']},
  {key:'main',label:'Main model',targets:[{scope:'main',task:''}],routes:['explicit']},
  {key:'sub',label:'Subagents',targets:[{scope:'delegation',task:''}],routes:['explicit','inherit']},
  {key:'bg',label:'Aux tasks (all)',targets:[{scope:'auxiliary',task:'*'}],routes:['explicit','inherit','auto']},
  {key:'effort',label:'Main effort',targets:[{scope:'main',task:''}],effortOnly:true},
  {key:'fast',label:'Fast mode',fast:true},
  {key:'lim',label:'Max subagents',limit:true},
  {key:'fb',label:'Fallback',fallbackChain:true},
];
const CHILDREN=[1,16];
const ROUTE_LABEL={explicit:'Specific model',inherit:'Same as main',auto:'Automatic'};
function prov(id){return PROV_SHORT[id]||id||'auto'}
const PROV_SHORT={'openai-codex':'codex'};
function routeText(r){
  if(!r)return '—';
  if(r.inheritance==='main')return 'main';
  if(r.inheritance==='auto-discovery')return 'auto';
  return (r.model||'?')+(r.effort?' · '+r.effort:'');
}
function routeHtml(r){
  const e=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  if(!r)return '—';
  if(r.inheritance==='main')return '<span class="v-inh">main</span>';
  if(r.inheritance==='auto-discovery')return '<span class="v-inh">auto</span>';
  return `<span class="v-model">${e(r.model||'?')}</span>`+(r.effort?`<span class="sep"> · </span><span class="v-eff">${e(r.effort)}</span>`:'');
}
// Same values as currentText, as escaped colour-role spans for the profile sheet.
function currentHtml(key,p,task){
  const e=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const inh=t=>`<span class="v-inh">${e(t)}</span>`,sep='<span class="sep"> · </span>';
  switch(key){
    case 'main':return `<span class="v-model">${e(p.routes.main.model)}</span>${sep}<span class="v-prov">${e(prov(p.routes.main.provider))}</span>`;
    case 'sub':return routeHtml(p.routes.delegation);
    case 'effort':return p.routes.main.effort?`<span class="v-eff">${e(p.routes.main.effort)}</span>`:inh('default');
    case 'fast':{const t=fastText(p);return t==='Fast'?`<span class="v-fast">Fast</span>`:t==='Normal'?`<span class="v-val">Normal</span>`:inh(t);}
    case 'lim':{const v=p.limits?.max_concurrent_children;return v==null?inh('default'):`<span class="v-val">${e(v)}</span>`;}
    case 'fb':{const f=p.fallback||[];return f.length?f.map(x=>`<span class="v-model">${e(x.model||'?')}</span> <span class="v-prov">(${e(x.provider)})</span>`).join('<span class="sep"> → </span>'):inh('none');}
    default:return routeHtml(p.routes.auxiliary.find(a=>a.task===task));
  }
}
function distinct(list,fn){return [...new Set(list.map(fn))];}
function summarize(list,fn){const u=distinct(list,fn);return u.length===1?u[0]:'mixed · '+u.length+' values'}
function auxText(p){
  const a=p.routes.auxiliary;if(!a.length)return '—';
  const u=[...new Set(a.map(x=>x.inheritance==='explicit'?x.model:routeText(x)))];
  if(u.length===1)return u[0];
  const pinned=a.filter(x=>x.inheritance==='explicit').length;
  return pinned?`${pinned} of ${a.length} custom`:'main / auto';
}
function fbText(p){const f=p.fallback||[];if(!f.length)return '';return (f[0].model||prov(f[0].provider))+(f.length>1?' +'+(f.length-1):'')}
function fastText(p){const c=p.fast_mode?.current;return c==='priority'?'Fast':c==='normal'?'Normal':c?`${c} (stored)`:'unavailable'}
function fbTitle(p){return (p.fallback||[]).map(f=>`${f.model||'?'} (${f.provider})`).join(' → ')}
function catText(p){const s=p.catalog?.status||'unavailable';return s==='ready'?(p.models||[]).length+' listed':s==='empty'?'none listed':s;}
function taskLabel(task){const t=(S.matrix?.tasks||[]).find(x=>x.task===task);return t?.label||String(task).replace(/_/g,' ').replace(/^./,ch=>ch.toUpperCase());}
function fieldLabel(f){
  if(FIELD_LABEL[f])return FIELD_LABEL[f];
  const m=/^auxiliary\.([A-Za-z0-9_-]+)\.(provider|model|reasoning_effort)$/.exec(f);
  return m?taskLabel(m[1])+' '+AUX_PART[m[2]]:f;
}
function status(p){
  if(p.state!=='ready')return {c:'bad',t:'Unreadable',title:p.reason||'Profile configuration unreadable; no repair attempted'};
  if(p.read_only)return {c:'ro',t:'Read-only',title:p.reason||'Read-only'};
  return {c:'ok',t:'Readable',title:'Saved config read cleanly. Not a runtime health, login or inference check.'};
}
function auxTitle(p){return p.routes.auxiliary.filter(x=>x.inheritance==='explicit').map(x=>x.task+': '+x.model).join('\n')}
function currentText(key,p,task){
  switch(key){
    case 'main-sub':return p.routes.main.model+' · subagents '+routeText(p.routes.delegation);
    case 'main':return p.routes.main.model+' · '+prov(p.routes.main.provider);
    case 'sub':return routeText(p.routes.delegation);
    case 'bg':return auxText(p);
    case 'effort':return p.routes.main.effort||'default';
    case 'fast':return fastText(p);
    case 'lim':return String(p.limits?.max_concurrent_children??'default');
    case 'fb':return fbTitle(p)||'none';
    default:return routeText(p.routes.auxiliary.find(a=>a.task===task));
  }
}
function ctxHtml(profiles,key,task,needsCatalog){
  lastCtx=[profiles,key,task,needsCatalog];
  const items=profiles.map(n=>{const p=S.matrix.profiles.find(x=>x.profile===n);if(!p)return '';
    const cat=needsCatalog&&p.catalog?.status!=='ready'?' · catalog '+(p.catalog?.status||'unavailable'):'';
    return `<li><span class="nm">${esc(labelFor(n))}</span><span class="cur mono">${esc(currentText(key,p,task)+cat)}</span></li>`;});
  const head=items.slice(0,6).join(''),rest=items.slice(6).join('');
  return `<div class="ctx" id="sheetScope"><div class="ctx-k">Applies to ${plural(profiles.length,'profile')} <span class="dim">· currently saved</span></div><ul>${head}</ul>${rest?`<details><summary>Show ${items.length-6} more</summary><ul>${rest}</ul></details>`:''}</div>`;
}
function renderOverview(){
  const all=(S.matrix?.profiles||[]).filter(hostShown),rows=ready().filter(hostShown);
  const off=rows.filter(p=>p.drift.length).length,bad=all.length-rows.length;
  const ro=rows.filter(p=>p.read_only).length,okN=rows.length-ro;
  $('#stats').innerHTML=[
    [all.length,all.length===1?'profile':'profiles','',''],
    [okN,'readable','t-ok','Saved config read cleanly. Not a runtime health, login or inference check.'],
    ...(ro?[[ro,'read-only','t-warn','']]:[]),
    ...(bad?[[bad,'unreadable','t-bad','']]:[]),
    [off,'with mixed providers','t-info','Some subagent or aux-task routes use a different provider than the main model. A configured choice, not an error.'],
    [rows.filter(p=>p.catalog?.status==='ready').length,'with model lists','',"Host catalogue metadata; not a login or entitlement check."],
  ].map(([n,l,c,t])=>`<div class="stat"${t?` title="${esc(t)}"`:''}><b class="${c}">${n}</b><span>${l}</span></div>`).join('');
  const cnt=new Map();for(const p of rows){const k=p.routes.main.model;cnt.set(k,(cnt.get(k)||0)+1)}
  const max=Math.max(1,...cnt.values());
  $('#bars').innerHTML=!cnt.size?'':`<div class="cap">Main model${S.effMode?` · effort ${esc(S.effMode)} unless shown`:''}</div>`+
    [...cnt.entries()].sort((a,b)=>b[1]-a[1]).map(([m,n])=>`<div class="barrow"><span class="l mono v-model" title="${esc(m)}">${esc(m)}</span><span class="t"><i style="width:${(n/max*100).toFixed(1)}%"></i></span><span class="n">${n}</span></div>`).join('');
}
function renderProfiles(){
  const every=(S.matrix?.profiles||[]).filter(hostShown),all=every.filter(shown);
  $('#pCount').textContent=every.length?(all.length===every.length?String(every.length):`${all.length} of ${every.length}`):'';
  if(!S.raw){$('#profiles').innerHTML=`<div class="placeholder">${c.state.error?'Profiles unavailable. Reload to try again.':'Loading profiles…'}</div>`;return;}
  const head=`<div class="prow head" aria-hidden="true"><span></span><span>Profile</span><span>Main model</span><span>Provider</span><span>Fallback</span><span>Subagents</span><span>Aux tasks</span><span>Model list</span><span>Config file</span></div>`;
  $('#profiles').innerHTML=head+(every.length&&!all.length?'<div class="placeholder">No profile matches this filter.</div>':'')+all.map(p=>{
    const st=status(p),name=p.profile_name||p.profile;
    const stHtml=`<span class="tag ${st.c&&st.c!=='ro'?'t-'+st.c:''}" title="${esc(st.title)}"><span class="dot ${st.c}"></span>${esc(st.t)}</span>`;
    if(p.state!=='ready')return `<div class="prow"><span class="c-chk"></span><span class="c-name name">${esc(name)}<span class="machine-label">${esc(p.host_label||p.host||'')}</span></span><span class="c-main dim">${esc(p.reason||'Unreadable')}</span><span class="c-prov"></span><span></span><span></span><span></span><span></span><span class="c-st">${stHtml}</span></div>`;
    const r=p.routes,m=r.main,fb=fbText(p);
    const eff=m.effort&&m.effort!==S.effMode?`<span class="sep"> · </span><span class="v-eff">${esc(m.effort)}</span>`:(!m.effort?'<span class="sep"> · </span><span class="v-inh">default</span>':'');
    const mix=p.drift.length?`<span class="mixnote" title="${esc(p.drift.map(d=>(d.task||d.scope)+': '+d.reason).join('\n'))}">${p.drift.length} subagent/aux ${p.drift.length===1?'route':'routes'} on other providers</span>`:'';
    return `<div class="prow ${S.sel.has(p.profile)?'selected':''}" data-p="${esc(p.profile)}">
      <label class="c-chk" title="Tick ${esc(labelFor(p.profile))}"><input type="checkbox" ${p.read_only?'disabled':''} ${S.sel.has(p.profile)?'checked':''} data-c="${esc(p.profile)}" aria-label="Tick ${esc(labelFor(p.profile))}"></label>
      <span class="c-name name"><button type="button" title="Open ${esc(name)}">${esc(name)}</button><span class="machine-label">${esc(p.host_label||p.host||'')}</span>${mix?'<br>'+mix:''}</span>
      <span class="c-main mono" title="${esc(m.model)} (${esc(m.provider)}) · ${esc(fastText(p))}"><span class="c-l">Main</span><span class="v-model">${esc(m.model)}</span>${eff}${p.fast_mode?.current!=='normal'?`<span class="sep"> · </span><span class="${p.fast_mode?.current==='priority'?'v-fast':'v-inh'}">${esc(fastText(p))}</span>`:''}<span class="m-extra"><span class="sep"> · </span><span class="v-prov">${esc(prov(m.provider))}</span></span></span>
      <span class="c-prov v-prov" title="${esc(m.provider)}">${esc(prov(m.provider))}</span>
      <span class="mono ${fb?'':'empty'}" title="${esc(fbTitle(p))}"><span class="c-l">Fallback</span><span class="${fb?'v-model':''}">${esc(fb||'—')}</span></span>
      <span class="mono c-sub" title="${esc(routeText(r.delegation))}"><span class="c-l">Subagents</span>${routeHtml(r.delegation)}</span>
      <span class="mono c-aux" title="${esc(auxTitle(p))}"><span class="c-l">Aux tasks</span>${(t=>/^(main|auto|main \/ auto|—)$/.test(t)?`<span class="v-inh">${esc(t)}</span>`:/ custom$/.test(t)?esc(t):`<span class="v-model">${esc(t)}</span>`)(auxText(p))}</span>
      <span class="c-cat dim" title="Host catalogue metadata; not a login, entitlement or inference check"><span class="c-l">Models</span>${esc(catText(p))}</span>
      <span class="c-st">${stHtml}</span></div>`}).join('');
}
function renderScope(tp){
  const el=$('#scopeStrip');if(!el)return;
  if(!S.raw){el.className='scope';el.innerHTML='';return;}
  const hidden=S.scope==='sel'?[...S.sel].filter(n=>{const p=S.matrix.profiles.find(x=>x.profile===n);return p&&!shown(p);}).length:0;
  const where=multi()?[...new Set(tp.map(n=>rowOf(n)?.host_label||rowOf(n)?.host).filter(Boolean))]:[];
  const skipped=S.scope==='all'?ready().filter(p=>shown(p)&&p.read_only).length:0;
  if(!tp.length){el.className='scope empty';el.innerHTML=`<span class="scope-n">No profiles to edit</span><span class="note">${S.scope==='sel'?'Tick profiles in the list below.':'No editable profile matches the filter.'}</span>`;return;}
  const first=tp.slice(0,4),rest=tp.slice(4);
  el.className='scope';
  const chips=list=>list.map(n=>`<span class="chip">${esc(labelFor(n))}</span>`).join('');
  el.innerHTML=`<span class="scope-n">Editing ${plural(tp.length,'profile')}${where.length===1?' on '+esc(where[0]):where.length>1?' on '+where.length+' machines':''}</span><span class="names" id="scopeNames">${chips(first)}</span>`+
    (rest.length?`<details><summary>+${rest.length} more</summary><span class="names">${chips(rest)}</span></details>`:'')+
    (S.scope==='all'&&terms().length?'<span class="note">· profiles matching the filter</span>':'')+
    (hidden?`<span class="note">· ${hidden} hidden by the filter, still included</span>`:'')+
    (skipped?`<span class="note">· ${skipped} read-only not included</span>`:'');
}
function renderMachines(){
  const el=$('#machines');if(!el)return;
  if(!multi()){el.hidden=true;el.innerHTML='';if(S.host)S.host='';return;}
  const ms=S.raw.machines;if(S.host&&!ms.some(m=>m.id===S.host))S.host='';
  const word={ok:'',offline:'offline',auth_failed:'sign-in refused',refused:'address refused',unavailable:'unavailable'};
  const btn=(id,text,bad)=>`<button type="button" data-h="${esc(id)}" class="${S.host===id?'on':''}" aria-pressed="${S.host===id}">${esc(text)}${bad?` <span class="t-bad">· ${esc(bad)}</span>`:''}</button>`;
  const down=ms.filter(m=>m.status!=='ok');
  el.hidden=false;
  el.innerHTML=`<div class="seg" id="hostSeg" role="group" aria-label="Which machine to show">${btn('','All machines','')}${ms.map(m=>btn(m.id,m.label,m.status==='ok'?'':(word[m.status]||m.status))).join('')}</div>`+
    (down.length?`<ul class="machine-notes" role="status">${down.map(m=>`<li><b>${esc(m.label)}</b>: ${esc(word[m.status]||m.status)} · ${esc(m.reason||'no answer')}. Its profiles are not shown and cannot be edited until it answers.</li>`).join('')}</ul>`:'');
}
function renderBulk(){
  const rows=ready(),tp=targetProfiles(),all=allTargets(),filtered=terms().length>0;
  $('#selN').textContent=S.sel.size;
  $('#bulkCount').textContent=S.scope==='all'?(filtered?`${tp.length} shown`:`all ${tp.length}`):`${tp.length} ticked`;
  $('#scopeSeg button[data-s="all"]').textContent=filtered?`All shown (${all.length})`:`All (${all.length})`;
  container.querySelectorAll('#scopeSeg button').forEach(b=>{const on=b.dataset.s===S.scope;b.classList.toggle('on',on);b.setAttribute('aria-pressed',String(on));});
  renderScope(tp);
  const tps=rows.filter(p=>tp.includes(p.profile));
  const fns={'main-sub':p=>p.routes.main.model+' / '+routeText(p.routes.delegation),main:p=>p.routes.main.model,sub:p=>routeText(p.routes.delegation),
              bg:auxText,effort:p=>p.routes.main.effort||'default',fast:fastText,lim:p=>String(p.limits?.max_concurrent_children??'default'),fb:p=>fbTitle(p)||'none'};
  const live=!S.busy&&!!(c.state.data?.available||c.state.data?.fixture);
  const mixed=!oneMachine(tp);
  $('#bulkRows').innerHTML=(mixed?'<div class="bulk-note">Choose a machine above, or tick profiles from just one machine. Each Preview → Apply stays on one machine.</div>':'')+SETTINGS.map(s=>{
    const u=tps.length?distinct(tps,fns[s.key]):[];
    const v=mixed?'choose one machine':!u.length?'—':u.length===1?`<span class="v-val">${esc(u[0])}</span>`:`<span class="mixed">${esc(summarize(tps,fns[s.key]))}</span>`;
    return `<button type="button" class="row" ${mixed?'aria-disabled="true"':'data-k="'+s.key+'"'} ${live?'':'disabled'} title="${esc(u.join('\n'))}"><span class="k">${s.label}</span><span class="v mono">${v}</span><span class="chev" aria-hidden="true"></span></button>`;}).join('');
}

  function pickData(names){const d=new Map();for(const n of names)for(const m of S.raw?.profiles.find(p=>p.profile===n)?.models||[]){const pm=d.get(m.provider)||new Map();d.set(m.provider,pm);const o=pm.get(m.model)||{c:0};o.c++;o.source='host metadata · unverified';pm.set(m.model,o);}return d;}
  function modelOptions(names){const out=[];for(const [p,pm] of pickData(names))for(const m of pm.keys())out.push({k:p+'|'+m});return out;}
function pickerHtml(id,profiles,cur){
  const d=pickData(profiles),n=profiles.length;
  const provs=[...d.entries()].map(([p,pm])=>[p,Math.max(0,...[...pm.values()].map(o=>o.c)),pm.size]).sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0]));
  const cg=cur&&!d.has(cur)?(profiles.map(n=>(S.inv?.[n]?.providers||[]).find(x=>x.provider===cur)?.group).find(Boolean)||cur):cur;
  const first=cg&&d.has(cg)?cg:provs[0]?.[0]||'';
  return `<div class="pick"><label><span>Provider</span><select class="pv" id="${id}_p">${provs.map(([p,c,k])=>`<option value="${esc(p)}"${p===first?' selected':''}>${esc(prov(p))} · ${plural(k,'model')}${c<n?` · on ${c}/${n}`:''}</option>`).join('')}</select></label><label><span>Model</span><select id="${id}_m"></select></label></div><p id="${id}_note" class="hint mono" style="overflow-wrap:anywhere"></p>`;
}
function pickerWire(id,profiles,curModel){
  const d=pickData(profiles),n=profiles.length,ps=$('#'+id+'_p'),ms=$('#'+id+'_m');if(!ps||!ms)return;
  const note=()=>{const e=$('#'+id+'_note');if(e)e.textContent=ps.value&&ms.value?ps.value+' / '+ms.value+' · listed by host metadata; availability and login unverified':'No catalog model listed for these profiles. Saved values stay visible in profile routes.';};
  const fill=()=>{const pm=d.get(ps.value)||new Map();
    ms.innerHTML=[...pm.entries()].sort((a,b)=>b[1].c-a[1].c||a[0].localeCompare(b[0])).map(([m,o])=>`<option value="${esc(m)}"${m===curModel?' selected':''}>${esc(m)}${o.c<n?` · on ${o.c}/${n}`:''}</option>`).join('');note()};
  ms.onchange=note;ps.onchange=()=>{fill();const o=$('#ed_out');if(o)o.innerHTML=''};fill();
}
function pickValue(id){const p=$('#'+id+'_p')?.value,m=$('#'+id+'_m')?.value;return p&&m?p+'|'+m:''}

  function invalidate(){S.previewVersion++;previewToken=null;c.invalidate();const out=$('#ed_out');if(out)out.innerHTML='';}
  function openerKey(el){if(!el||!el.closest)return null;const k=el.closest('[data-k]'),p=el.closest('[data-p]');return {el,k:k?.dataset.k,p:p?.dataset.p};}
  function restoreFocus(){
    const o=opener;opener=null;if(!o)return;
    let target=o.el&&o.el.isConnected?o.el:null;
    if(!target&&o.k)target=[...container.querySelectorAll('#bulkRows [data-k]')].find(e=>e.dataset.k===o.k);
    if(!target&&o.p)target=[...container.querySelectorAll('#profiles [data-p]')].find(e=>e.dataset.p===o.p)?.querySelector('.c-name button');
    try{target?.focus({preventScroll:true});}catch(e){}
  }
  function openSheet(title,html){
    // Safari never focuses clicked buttons, so fall back to the clicked trigger.
    if(!sheetOpen()){const a=doc.activeElement;opener=openerKey(lastTrigger||(a&&a!==doc.body&&container.contains(a)?a:null));}
    lastTrigger=null;
    invalidate();S.sheetVersion++;$('#sheetTitle').textContent=title;$('#sheetBody').onclick=null;$('#sheetBody').innerHTML=html;
    $('#sheetMsg').textContent='';$('#sheetResult').innerHTML='';shownResult=c.state.result;
    $('#ov').classList.add('on');$('main').inert=true;const sh=$('.sheet');if(sh)sh.scrollTop=0;
    try{$('#sheetTitle').focus({preventScroll:true});}catch(e){}
  }
  function closeSheet(){
    if(c.state.applying)return;const was=sheetOpen();invalidate();S.sheetVersion++;$('#ov').classList.remove('on');$('main').inert=false;
    $('#sheetBody').innerHTML='';$('#sheetMsg').textContent='';$('#sheetResult').innerHTML='';shownResult=c.state.result;
    if(was)restoreFocus();
  }
function editor(setting,profiles,targets,label){
  if(!profiles.length){toast('Tick at least one profile',1);return}
  if(!oneMachine(profiles)){toast('Choose profiles from one machine per Apply',1);return}
  if(setting.limit)return limitEditor(profiles,label);
  if(setting.fast)return fastEditor(profiles,label);
  if(setting.fallbackChain)return fallbackEditor(profiles,label);
  const efforts=S.matrix.efforts;
  const one=profiles.length===1?S.matrix.profiles.find(p=>p.profile===profiles[0]):null;
  const curRoute=one?(setting.key==='main'?one.routes.main:setting.key==='sub'?one.routes.delegation:targets[0]?.task&&targets[0].task!=='*'?one.routes.auxiliary.find(a=>a.task===targets[0].task):null):null;
  const curProv=curRoute?.inheritance==='explicit'?curRoute.provider:'',curModel=curRoute?.inheritance==='explicit'?curRoute.model:'';
  const routeSel=setting.effortOnly?'':`<div class="field"><label for="ed_route">Use</label><select id="ed_route">${setting.routes.map(r=>`<option value="${r}">${ROUTE_LABEL[r]}</option>`).join('')}</select></div>
    <div class="field" id="ed_mf"><span class="lbl">Provider · model</span>${pickerHtml('ed_model',profiles,curProv)}</div>`;
  const effSel=`<div class="field"><label for="ed_eff">Reasoning effort</label><select id="ed_eff"><option value="__keep">Keep current</option><option value="">Model default</option>${efforts.map(e=>`<option>${esc(e)}</option>`).join('')}</select></div>`;
  openSheet(label,`${ctxHtml(profiles,setting.key,targets[0]?.task,!setting.effortOnly)}${routeSel}${effSel}
    <button id="ed_prev" type="button" class="wide">Preview changes</button><div id="ed_out"></div>`);
  if(!setting.effortOnly)pickerWire('ed_model',profiles,curModel);
  const rs=$('#ed_route');if(rs){const t=()=>$('#ed_mf').style.display=rs.value==='explicit'?'':'none';rs.onchange=t;t()}
  if(setting.effortOnly)$('#ed_eff').value='high';
  $('#ed_prev').onclick=()=>preview(()=>bulkBody(setting,profiles,targets));
}
function revsFor(profiles){const revs={};for(const n of profiles)revs[n]=S.matrix.profiles.find(p=>p.profile===n).revision;return revs}
function baseBody(profiles){if(!oneMachine(profiles))throw new Error('Choose profiles from one machine per Apply');return {profiles,targets:[],route:'keep',provider:'',model:'',effort:null,expected_revisions:revsFor(profiles),confirmed:false,selection_token:''}}
// Explicit owner selection only: opening this editor never normalizes auto/cold/ultrafast.
function fastEditor(profiles,label){
  const rows=profiles.map(n=>S.matrix.profiles.find(p=>p.profile===n));
  const blocked=rows.filter(p=>!p.fast_mode?.supported||p.read_only);
  const readonly=rows.some(p=>p.read_only);
  openSheet(label,`${ctxHtml(profiles,'fast')}
    <p>Current: ${esc(summarize(rows,fastText))}. This is a configured preference; existing fast windows are preserved and may also apply.</p>
    ${blocked.map(p=>`<p class="t-warn" style="overflow-wrap:anywhere">${esc(labelFor(p.profile))}: ${esc(p.read_only?'Read-only: external secrets':p.fast_mode?.reason||'Fast capability unavailable')}. Normal can clear a stored preference on unsupported routes.</p>`).join('')}
    <div class="field"><label for="fast_v">Fast mode (profile main default)</label><select id="fast_v"><option value="__keep">Choose Normal or Fast…</option><option value="normal" ${readonly?'disabled':''}>Normal</option><option value="priority" ${blocked.length?'disabled':''}>Fast</option></select></div>
    <p class="warnline t-warn">Fast may consume more subscription usage or cost more. Defaults for future sessions only; running sessions and gateways are unchanged.</p>
    <button id="ed_prev" type="button" class="wide" disabled>Preview changes</button><div id="ed_out"></div>`);
  const sel=$('#fast_v');sel.onchange=()=>{$('#ed_out').innerHTML='';$('#ed_prev').disabled=sel.value==='__keep'};
  $('#ed_prev').onclick=()=>preview(()=>{if(!['normal','priority'].includes(sel.value))throw new Error('Choose Normal or Fast');return {...baseBody(profiles),service_tier:sel.value}});
}
// Max subagents: delegation.max_concurrent_children, 1..16, through the same /bulk preview/apply
function limitEditor(profiles,label){
  const cur=profiles.map(n=>S.matrix.profiles.find(p=>p.profile===n)?.limits?.max_concurrent_children).filter(v=>v!=null);
  const start=Math.min(CHILDREN[1],Math.max(CHILDREN[0],cur.length?cur[0]:3));
  openSheet(label,`${ctxHtml(profiles,'lim')}
    <div class="field"><label for="lim_v">Max subagents at once (${CHILDREN[0]}–${CHILDREN[1]})</label><div class="step"><button id="lim_dn" type="button" aria-label="fewer">−</button><input id="lim_v" type="number" inputmode="numeric" min="${CHILDREN[0]}" max="${CHILDREN[1]}" step="1" value="${start}"><button id="lim_up" type="button" aria-label="more">+</button></div></div>
    <button id="ed_prev" type="button" class="wide">Preview changes</button><div id="ed_out"></div>`);
  const inp=$('#lim_v'),bump=d=>{const v=parseInt(inp.value,10);inp.value=Math.min(CHILDREN[1],Math.max(CHILDREN[0],(Number.isFinite(v)?v:start)+d));invalidate()};
  inp.oninput=()=>$('#ed_out').innerHTML='';
  $('#lim_dn').onclick=()=>bump(-1);$('#lim_up').onclick=()=>bump(1);
  $('#ed_prev').onclick=()=>preview(()=>{const v=Number(inp.value);
    if(!Number.isInteger(v)||v<CHILDREN[0]||v>CHILDREN[1])throw new Error(`Enter a whole number from ${CHILDREN[0]} to ${CHILDREN[1]}`);
    return {...baseBody(profiles),max_concurrent_children:v}});
}
// Fallback chain: ordered {provider, model} pairs picked from the same inventory as the main model
// One profile (from its sheet) or several (Change several): same chain written to each, via /bulk.
function fallbackEditor(names,label){
  names=Array.isArray(names)?names:[names];
  const rows=names.map(n=>S.matrix.profiles.find(x=>x.profile===n));
  const locked=rows.filter(p=>p.read_only||p.fallback_lock);
  if(locked.length){toast(`${locked.map(p=>labelFor(p.profile)).join(', ')}: fallback not editable here (${locked[0].read_only?'read-only':'edit in config'}); untick to continue`,1);return}
  // Start from the shared chain when every profile has the same one, else start empty.
  const key=p=>JSON.stringify((p.fallback||[]).map(f=>[f.provider,f.model]));
  const same=rows.every(p=>key(p)===key(rows[0]));
  const chain=same?(rows[0].fallback||[]).map(f=>({provider:f.provider,model:f.model})):[];
  const opts=modelOptions(names);
  openSheet(label||`${labelFor(names[0])} · Fallback`,`${ctxHtml(names,'fb',null,true)}${!same?'<p class="warnline t-warn">These profiles have different fallbacks now; the chain below replaces all of them.</p>':''}
    <div class="field"><span class="lbl">Fallback chain · tried top to bottom when the main model fails</span><div class="chain" id="fb_list"></div>
      <div class="fb-actions"><button id="fb_clear" type="button">Clear chain</button></div></div>
    <div class="field"><span class="lbl">Add a fallback · repeat to add several</span><div class="addfb">${pickerHtml('fb_add',names)}<button id="fb_addb" type="button" ${opts.length?'':'disabled'}>Add to chain</button></div></div>
    <button id="ed_prev" type="button" class="wide">Preview changes</button><div id="ed_out"></div>`);
  const draw=()=>{invalidate();$('#ed_out').innerHTML='';$('#fb_clear').disabled=!chain.length;$('#fb_list').innerHTML=chain.length?chain.map((f,i)=>`<div class="fbrow"><span class="i">${i+1}</span><span class="v mono" title="${esc(f.model)} (${esc(f.provider)})">${esc(f.model)} <span class="dim">· ${esc(prov(f.provider))}</span></span>
      <button type="button" data-mv="${i}" data-d="-1" ${i?'':'disabled'} aria-label="Move ${esc(f.model)} up">↑</button><button type="button" data-mv="${i}" data-d="1" ${i<chain.length-1?'':'disabled'} aria-label="Move ${esc(f.model)} down">↓</button><button type="button" data-rm="${i}" aria-label="Remove ${esc(f.model)}">✕</button></div>`).join(''):'<div class="fbrow none">Empty chain — applying it removes every fallback on the listed profiles</div>'};
  $('#fb_list').onclick=e=>{const m=e.target.closest('[data-mv]'),r=e.target.closest('[data-rm]');
    if(m){const i=+m.dataset.mv,d=+m.dataset.d,j=i+d;if(!Number.isInteger(i)||![-1,1].includes(d)||i<0||i>=chain.length||j<0||j>=chain.length)return;[chain[i],chain[j]]=[chain[j],chain[i]];draw()}
    if(r){chain.splice(+r.dataset.rm,1);draw()}};
  pickerWire('fb_add',names);
  $('#fb_clear').onclick=()=>{chain.splice(0);draw()};
  $('#fb_addb').onclick=()=>{const v=pickValue('fb_add');if(!v)return;const [provider,model]=v.split('|');
    if(chain.some(f=>f.provider===provider&&f.model===model)){toast('Already in the chain',1);return}
    if(chain.length>=10){toast('At most 10 fallbacks',1);return}chain.push({provider,model});draw();toast(`Added #${chain.length}: ${model}`)};
  draw();
  $('#ed_prev').onclick=()=>preview(()=>({...baseBody(names),fallback:chain.map(f=>({...f}))}));
}
function bulkBody(setting,profiles,targets){
  const route=setting.effortOnly?'keep':$('#ed_route').value;let provider='',model='';
  if(route==='explicit'){const k=pickValue('ed_model');if(!k)throw new Error('No model available');[provider,model]=k.split('|')}
  const ev=$('#ed_eff').value;const effort=ev==='__keep'?null:ev;
  return {...baseBody(profiles),targets,route,provider,model,effort};
}

  function sec(title,count,inner,open){return `<details class="sec"${open?' open':''}><summary>${esc(title)}${count!=null?` <span class="count">${esc(count)}</span>`:''}</summary><div class="secbody">${inner}</div></details>`;}
  function openProfile(name){
    const p=S.matrix.profiles.find(p=>p.profile===name);if(!p)return;
    const models=SETTINGS.filter(s=>s.key!=='bg'&&s.key!=='main-sub').map(s=>`<button type="button" class="row" data-ed="${s.key}"><span class="k">${esc(s.label)}</span><span class="v mono">${currentHtml(s.key,p)}</span><span class="chev" aria-hidden="true"></span></button>`).join('');
    const aux=(p.tasks||[]).map(t=>t.editable?`<button type="button" class="row" data-aux="${esc(t.task)}"><span class="k">${esc(t.label||t.task)}</span><span class="v mono">${currentHtml('aux',p,t.task)}</span><span class="chev" aria-hidden="true"></span></button>`
      :`<div class="row static"><span class="k">${esc(t.label||t.task)}</span><span class="v mono">${currentHtml('aux',p,t.task)} <span class="dim">· read-only / undeclared task</span></span></div>`).join('');
    const st=status(p);
    openSheet(labelFor(name),`<p class="hint">Saved routes · config ${esc(st.t.toLowerCase())} · models: ${esc(catText(p))}${p.drift.length?` · ${plural(p.drift.length,'route')} on another provider than main (a configured choice, not an error)`:''}</p>${p.read_only?`<p class="warnline t-warn">${esc(p.reason||'Read-only')}</p>`:''}`+sec('Models',null,models,true)+sec('Aux tasks',(p.tasks||[]).length,aux,true));
    $('#sheetBody').onclick=e=>{if(p.read_only){toast(p.reason||'Read-only');return;}const ed=e.target.closest('[data-ed]');if(ed){const s=SETTINGS.find(s=>s.key===ed.dataset.ed);editor(s,[name],s.targets,labelFor(name)+' · '+s.label);return;}const ax=e.target.closest('[data-aux]');if(ax)editor({routes:['explicit','inherit','auto']},[name],[{scope:'auxiliary',task:ax.dataset.aux}],labelFor(name)+' · '+taskLabel(ax.dataset.aux));};
  }
  async function preview(make){
    if(S.busy)return;S.lastAction='preview';S.lastMake=make;let draft;try{draft=view.command(S.raw,make());}catch(e){toast(e.message);return;}
    c.intent(draft);await c.preview();
  }
  function human(field,v){
    if(field==='model.context_length'){
      if(!v?.present||v.value===0||v.value==null)return 'Auto-detect';
      if(Number.isSafeInteger(v.value)&&v.value>0)return v.value.toLocaleString()+' tokens (override)';
    }
    if(!v||!v.present)return 'not set';
    const x=v.value;
    if(x===null)return 'null';
    if(x==='')return field==='agent.service_tier'?'Normal <span class="dim">("")</span>':'"" <span class="dim">(empty)</span>';
    if(field==='agent.service_tier'&&x==='priority')return 'Fast <span class="dim">(priority)</span>';
    if(Array.isArray(x)){
      if(!x.length)return 'none <span class="dim">([])</span>';
      if(field==='fallback_providers'&&x.every(f=>f&&typeof f==='object'))return '<ol>'+x.map(f=>`<li>${esc(f.model??'?')} <span class="dim">· ${esc(f.provider??'?')}</span></li>`).join('')+'</ol>';
      return esc(JSON.stringify(x));
    }
    return esc(typeof x==='object'?JSON.stringify(x):String(x));
  }
  function changeHtml(d){
    return `<div class="chg"><div class="chg-k">${esc(fieldLabel(d.field))}</div><div class="chg-v"><span class="old${d.before?.present?'':' absent'}">${human(d.field,d.before)}</span> <span class="arr" aria-hidden="true">→</span><span class="sr-only"> changes to </span> <span class="new${d.after?.present?'':' absent'}">${human(d.field,d.after)}</span></div></div>`;
  }
  function techHtml(changes){
    return `<details class="tech"><summary>Exact config fields</summary><dl>${changes.map(d=>`<dt>${esc(d.field)}</dt><dd><span class="old">${esc(view.value(d.before))}</span> → <span class="new">${esc(view.value(d.after))}</span></dd>`).join('')}</dl></details>`;
  }
  function ceilingHtml(p){
    if(!p.changes.some(c=>c.field==='model.default'||c.field==='model.provider'))return '';
    const cap=p.compaction_ceiling;
    let label='Not reported by this host';
    if(cap?.status==='configured'&&Number.isSafeInteger(cap.tokens)&&cap.tokens>0)label=cap.tokens.toLocaleString()+' tokens · unchanged';
    if(cap?.status==='unconfigured')label='No fixed ceiling configured · unchanged';
    if(cap?.status==='disabled')label='Compaction disabled in saved config · unchanged';
    if(cap?.status==='unavailable')label='Could not read saved setting';
    const contextRow=(title, text)=>`<div class="chg"><div class="chg-k">${esc(title)}</div><div class="chg-v">${esc(text)}</div></div>`;
    return (p.model_window_auto===true?contextRow('Model window','Auto-detect · unchanged'):'')+contextRow('Compaction ceiling',label);
  }
  function renderPreview(){
    const out=$('#ed_out');if(!out)return;
    const s=c.state;
    if(!s.preview){if(previewToken){out.innerHTML='';previewToken=null;}return;}
    if(previewToken===s.preview.token){const confirm=$('#confirmation');if(confirm){confirm.checked=s.confirmed;$('#ed_apply').disabled=!s.confirmed||s.busy;const h=$('#applyHint');if(h)h.hidden=s.confirmed;}return;}
    previewToken=s.preview.token;
    const profs=s.preview.profiles,changed=profs.filter(p=>p.changes.length).length;
    const warning=String(s.preview.warning||'Sequential saves, not a transaction');
    const fast=/Fast may consume/i.test(warning),detail=warning.replace(/^Fast may consume more usage or cost more\.\s*/i,'');
    out.innerHTML=`<section class="review" id="review" tabindex="-1" aria-labelledby="reviewTitle">
      <h3 id="reviewTitle">Review <span class="dim">· exact preview, nothing saved yet</span></h3>
      <p class="sum">${plural(profs.length,'profile')}${s.preview.machine&&multi()?' on '+esc(s.preview.machine.label):''} · ${plural(profs.reduce((n,p)=>n+p.changes.length,0),'field change')}${profs.length-changed?` · ${profs.length-changed} unchanged`:''}</p>
      ${fast?'<p class="warnline">Fast may consume more usage or cost more.</p>':''}
      ${profs.map(p=>`<article class="change" data-preview-profile="${esc(p.profile)}"><header><b>${esc(labelFor(p.profile))}</b><span class="n">${p.changes.length?plural(p.changes.length,'change'):'unchanged'}</span></header>${p.changes.length?p.changes.map(changeHtml).join('')+ceilingHtml(p)+techHtml(p.changes):'<p class="nochange">No field changes · nothing will be written</p>'}</article>`).join('')}
      <div class="save-note">Saves one profile at a time — not all-or-nothing. <details><summary>What if one fails?</summary><span>Each profile is written in turn and reports its own outcome; profiles already saved are not rolled back. After a failure, reload and preview again — never replay. Host: ${esc(detail)}</span></details></div>
      <div class="applybar">
        <label class="confirmation"><input id="confirmation" type="checkbox"><span>I reviewed these exact changes for ${profs.length===1?esc(labelFor(profs[0].profile)):profs.length===2?'both profiles':'all '+profs.length+' profiles'}</span></label>
        <button id="ed_apply" type="button" class="primary" disabled>Apply to ${plural(profs.length,'profile')}</button>
        <p class="hint" id="applyHint">Tick the box to enable Apply.</p>
      </div></section>`;
    $('#confirmation').onchange=e=>c.confirm(e.target.checked);$('#ed_apply').onclick=()=>{S.lastAction='apply';c.apply();};
    S.lastReview=s.preview;
    const review=$('#review');
    try{if(review.scrollIntoView)review.scrollIntoView({block:'start'});review.focus({preventScroll:true});}catch(e){}
  }
  function resultHtml(r){
    const cls=r.partial?'partial':r.ok?'ok':'failed';
    const head=r.partial?'Partial outcome · inspect every profile':r.ok?'Saved · exact field readback':'Save uncertain / failed · reload, never replay';
    return `<section class="outcome ${cls}"><header>${esc(head)}</header>${r.profiles.map(p=>{const good=['applied','unchanged'].includes(p.state);
      // "applied" means every previewed field read back exactly, so the previewed values are the saved values.
      const done=p.state==='applied'?(S.lastReview?.profiles||[]).find(x=>x.profile===p.profile)?.changes||[]:[];
      return `<article class="change" data-result-profile="${esc(p.profile)}"><b>${esc(labelFor(p.profile))}</b>: <span class="state ${good?'good':'bad'}">${esc(p.state)}</span> <span class="dim">${esc(p.detail||(p.state==='unchanged'?'No change needed · readback verified':'Saved-field readback verified'))}</span>${done.length?`<div class="saved-vals">${done.map(d=>`<span>${esc(fieldLabel(d.field))}: ${human(d.field,d.after)}</span>`).join('')}</div>`:''}</article>`;}).join('')}</section>`;
  }
  function errorHtml(err){
    const code=(/^(\d{3}):/.exec(err)||[])[1],apply=S.lastAction==='apply';
    let title,body,action='';
    if(c.state.result&&!code){title='Reload unavailable';body='The saved outcome is shown below. Edit authority is removed until a reload succeeds.';action='reload';}
    else if(S.lastAction==='load'){title='Profiles unavailable';body='Could not read the saved state, so nothing can be edited. Try reloading.';action='reload';}
    else if(apply&&(code==='409'||code==='422')){title='Not saved';body='Nothing was written'+(code==='409'?' — a selected profile changed after this preview, or the preview expired.':'.')+' Reload to review the current saved state.';action=code==='409'?'review':'';}
    else if(apply){title='Save outcome uncertain';body='The save may or may not have happened. Reload to see what is saved now — never replay it.';action='reload';}
    else if(code==='409'){title='Preview refused';body='A selected profile changed. Reload to preview against the current saved state.';action='review';}
    else{title='Preview refused';body='Nothing was changed.';}
    return `<div class="errpanel" role="alert"><b>${title}</b><p>${esc(body)}</p><p class="mono dim">Host: ${esc(err)}</p>${action?`<button type="button" id="ed_recover" data-recover="${action}">${action==='reload'?'Reload saved state':'Reload and preview again'}</button>`:''}</div>`;
  }
  // Recovery reloads first; "preview again" is a fresh read-only preview, never an Apply retry.
  async function recover(e){
    const mode=e.currentTarget.dataset.recover;S.lastAction='load';await c.load();
    if(disposed||!c.state.data||!sheetOpen())return;
    if(mode==='review'&&S.lastMake)await preview(S.lastMake);
  }
  function render(){
    if(disposed)return;const s=c.state;S.busy=s.busy;S.raw=s.data;S.matrix=view.state(s.data||{});S.sel=new Set(s.selected);
    const notice=$('#notice');notice.classList.toggle('is-fixture',!!s.data?.fixture);notice.classList.toggle('is-unavailable',!s.data?.fixture&&!s.data?.available&&!(s.busy&&!s.error));
    const badge=s.data?.fixture?'Synthetic fixture — not live':s.data?.available?'':!s.data&&s.busy?'Reading profiles…':'Read-only · unavailable';
    notice.textContent=badge;notice.hidden=!badge;
    const nd=$('#noticeDetail'),why=!s.data?.fixture&&s.data&&!s.data.available?(s.data.reason||'No edit authority'):'';nd.textContent=why;nd.hidden=!why;
    $('#candidate-status').textContent=s.data?.fixture?'SYNTHETIC FIXTURE — not live. Synthetic host catalogs and real file-backend persistence only.':s.data?.available?(Array.isArray(s.data.machines)&&s.data.machines.length?'Native mode · authenticated local host plus the other machines you configured.':'Native mode · authenticated local host.'):!s.data&&s.busy?'Native mode · reading profiles…':'Native mode · read-only / unavailable. '+(s.data?.reason||'No edit authority');
    $('#provenance').textContent=s.data?.available&&!s.data?.fixture?S.matrix.provenance+' · host metadata is not proof of login, entitlement or inference':S.matrix.provenance;$('#candidate-error').textContent=s.error;
    renderMachines();renderOverview();renderProfiles();renderBulk();$('#reload').disabled=s.busy;
    if(multi()){const ok=S.raw.machines.filter(m=>m.status==='ok').length;$('#aboutScope').textContent=`${plural(S.raw.machines.length,'machine')} configured, ${ok} answering · saved defaults only. Each Apply stays on one machine and goes through that machine's own Hermes dashboard. Jobs, federation, refresh and login controls are not included.`;}
    container.querySelectorAll('[data-c]').forEach(el=>{el.disabled=el.disabled||s.busy;});
    container.querySelectorAll('#sheetBody input,#sheetBody select,#sheetBody button,#closeSheet').forEach(el=>{
      if(s.applying){if(!disabledBeforeApply.has(el))disabledBeforeApply.set(el,el.disabled);el.disabled=true;}
      else if(disabledBeforeApply.has(el)){el.disabled=disabledBeforeApply.get(el);disabledBeforeApply.delete(el);}
    });
    renderPreview();if($('#ed_prev'))$('#ed_prev').disabled=s.busy||($('#fast_v')?.value==='__keep');
    $('#result-panel').innerHTML=s.result?resultHtml(s.result):'';
    if(s.result!==shownResult){shownResult=s.result;const sr=$('#sheetResult');sr.innerHTML=s.result&&sheetOpen()?resultHtml(s.result):'';if(s.result&&sheetOpen()){try{sr.focus({preventScroll:false});}catch(e){}}}
    if(s.data!==lastData){lastData=s.data;const sc=$('#sheetScope');if(sc&&lastCtx&&s.data&&sheetOpen())sc.outerHTML=ctxHtml(...lastCtx);}
    if(s.error&&$('#ed_out')){$('#ed_out').innerHTML=errorHtml(s.error);const rb=$('#ed_recover');if(rb)rb.onclick=recover;}
    else if(!s.preview&&$('#ed_out .errpanel'))$('#ed_out').innerHTML='';
  }
  $('#pq').oninput=e=>{S.q=e.target.value;renderProfiles();renderBulk();};
  $('#machines').onclick=e=>{const b=e.target.closest('[data-h]');if(b){closeSheet();S.host=b.dataset.h;renderMachines();renderOverview();renderProfiles();renderBulk();}};
  $('#scopeSeg').onclick=e=>{const b=e.target.closest('button');if(b){closeSheet();S.scope=b.dataset.s;renderBulk();}};
  $('#profiles').onclick=e=>{
    // A tap on the 44px label area is followed by the checkbox's own click; never open the profile for it.
    if(e.target.closest('.c-chk')&&!e.target.closest('[data-c]'))return;
    const check=e.target.closest('[data-c]');if(check){closeSheet();c.toggle(check.dataset.c);S.scope='sel';renderBulk();return;}const row=e.target.closest('[data-p]');if(row){lastTrigger=row.querySelector('.c-name button')||row;openProfile(row.dataset.p);}};
  $('#bulkRows').onclick=e=>{if(S.busy||!(c.state.data?.available||c.state.data?.fixture))return;const r=e.target.closest('[data-k]');if(r){lastTrigger=r;const setting=SETTINGS.find(s=>s.key===r.dataset.k);editor(setting,targetProfiles(),setting.targets,setting.label);}};
  $('#reload').onclick=()=>{closeSheet();S.lastAction='load';c.load();};$('#closeSheet').onclick=closeSheet;$('#ov').onclick=e=>{if(e.target===$('#ov'))closeSheet();};
  const onField=e=>{if(e.target.id==='confirmation')return;invalidate();};$('#sheetBody').addEventListener('input',onField);$('#sheetBody').addEventListener('change',onField);
  // Escape closes; Tab stays inside the open sheet.
  const key=e=>{
    if(e.key==='Escape'){closeSheet();return;}
    if(e.key!=='Tab'||!sheetOpen())return;
    const sheet=$('.sheet'),items=[...sheet.querySelectorAll('button,select,input,summary,[tabindex]:not([tabindex="-1"])')].filter(el=>!el.disabled&&el.getClientRects().length);
    if(!items.length)return;const first=items[0],last=items[items.length-1],i=items.indexOf(doc.activeElement);
    // i<0: focus is on the title (tabindex=-1), the sheet itself or outside it.
    if(e.shiftKey&&i<=0){e.preventDefault();last.focus();}
    else if(!e.shiftKey&&(i<0||i===items.length-1)){e.preventDefault();first.focus();}
  };
  doc.addEventListener('keydown',key);
  if(options.simulateChange){const box=doc.createElement('div');box.className='fixture-tools';box.innerHTML='<button id="simulate-stale" type="button">Simulate another edit to beta</button> <span id="fixture-note" class="dim"></span>';container.querySelector('main').appendChild(box);$('#simulate-stale').onclick=async()=>{await options.simulateChange();if(!disposed)$('#fixture-note').textContent='beta changed outside this preview';};}
  tone();
  const unsub=c.subscribe(render);render();c.load();
  return ()=>{disposed=true;unsub();c.dispose();themeWatch?.disconnect();doc.removeEventListener('keydown',key);container.innerHTML='';};
}
// Hermes SDK fetchJSON throws ApiError{message,status,body}; keep the HTTP status in the
// message ("409: detail") so stale/no-write copy matches the host. status 0 = transport (uncertain).
function sdkRequest(sdk){
  return async(path,init)=>{
    try{return await sdk.fetchJSON('/api/plugins/fleet-control'+path,init);}
    catch(e){
      const status=Number(e?.status);if(!(status>0))throw e;
      let detail='';try{const b=JSON.parse(e.body||'');if(typeof b?.detail==='string')detail=b.detail;}catch(_){}
      throw new Error(status+': '+(detail||e.message||'Request refused'));
    }
  };
}
root.FleetControl={createController,mount,view,sdkRequest};
const sdk=root.__HERMES_PLUGIN_SDK__,plugins=root.__HERMES_PLUGINS__;
if(sdk?.React&&typeof sdk.fetchJSON==='function'&&typeof plugins?.register==='function'){
  const React=sdk.React;
  plugins.register('fleet-control',function FleetControlPage(){
    const ref=React.useRef(null);
    React.useEffect(()=>{const controller=createController(sdkRequest(sdk));return mount(ref.current,controller);},[]);
    return React.createElement('div',{ref});
  });
}
})(window);
