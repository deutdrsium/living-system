// Run by the Python suite; exercises the exact inline, framework-free editor core.
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const html=fs.readFileSync(path.join(__dirname,'editor.html'),'utf8');
const core=html.match(/<script id="graph-core">([\s\S]*?)<\/script>/)[1];
for(const script of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(script[1]);
const context=vm.createContext({crypto:webcrypto,TextEncoder,console});
vm.runInContext(core,context);
const G=vm.runInContext('GraphEditor',context);
// Values from the vm context carry foreign prototypes; compare plain JSON copies.
const plain=x=>JSON.parse(JSON.stringify(x)),eq=(a,b,m)=>assert.deepEqual(plain(a),plain(b),m);
(async()=>{
  const original=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
  G.check(original);
  assert.equal(await G.digest(original),original.digest,'Python/browser digest parity');
  // Structured node/edge access edits each have one complete history transaction.
  const schedule={status:'known',timezone:'Asia/Shanghai',windows:[{weekdays:[0,1,2,3,4],start:'08:00',end:'18:00'}]};
  for(const [collection,key] of [['nodes','place_id'],['edges','edge_id']]){
    const id=original[collection][0][key];
    const edited=G.transaction(original,2,'edit_access',(p)=>{const r=p[collection][0];G.setAccess(r,'student','campus',schedule,2);r.source='manual';r.version=2;});
    assert.equal(edited.edit_history.length,original.edit_history.length+1);
    assert.equal(edited.edit_history.at(-1).changes.length,1);
    assert.equal(edited.edit_history.at(-1).changes[0].id,id);
    assert.equal(edited[collection][0].access,original[collection][0].access,'legacy text remains readable');
    eq(edited[collection][0].opening_schedule,schedule);
    assert.equal(G.accessState(edited[collection][0]),'student');
    const undo=G.transaction(edited,2,'undo',p=>p[collection]=G.clone(original[collection]));
    eq(undo[collection],original[collection]);
  }
  eq(G.parseWindows('0,1,2,3,4 08:00 18:00\n5,6 22:00 06:00'),[{weekdays:[0,1,2,3,4],start:'08:00',end:'18:00'},{weekdays:[5,6],start:'22:00',end:'06:00'}]);
  assert.throws(()=>G.parseWindows('0,1 08:00'),/格式/);
  assert.equal(G.accessState({access:'destination'}),'unparsed');
  assert.equal(G.accessState({access:'仅校内有资格人员可用'}),'campus_member');
  assert.equal(G.openingState({opening_rules:null},true),'unknown');
  assert.equal(G.openingState({opening_rules:'Mo-Fr 08:00-18:00'},true),'unparsed');
  // Python ZoneInfo requires exact named keys. A valid digest does not make an invalid key importable.
  for(const timezone of ['+08:00','-05:30','+0800','-0530','+08','-05','+00:00','-00:00','Bad/Zone','SystemV/PST8PDT']){
    const invalidSchedule={...schedule,timezone};
    for(const collection of ['nodes','edges']){
      const imported=G.clone(original);imported[collection][0].opening_schedule=invalidSchedule;
      assert.throws(()=>G.check(imported),/IANA/,`import rejects ${timezone} on ${collection}`);
      const before=G.canonical(original);
      assert.throws(()=>G.transaction(original,2,'edit_access',q=>G.setAccess(q[collection][0],'student','campus',invalidSchedule,2)),/IANA/,`property save rejects ${timezone} on ${collection}`);
      assert.equal(G.canonical(original),before,'rejected save preserves the package and history');
    }
  }
  for(const timezone of ['Asia/Shanghai','America/Argentina/Buenos_Aires','Europe/Kyiv','US/Pacific','Etc/GMT+8','Etc/UTC','UTC','GMT','CET']){
    const lowercase=timezone.toLowerCase();
    for(const collection of ['nodes','edges']){
      const imported=G.clone(original);
      imported[collection][0].opening_schedule={...schedule,timezone};
      imported.digest=await G.digest(imported);
      G.check(imported);
      assert.equal(imported[collection][0].opening_schedule.timezone,timezone,'import preserves the exact zone key');
      assert.equal(await G.digest(imported),imported.digest,'import preserves the signed content');
      imported[collection][0].opening_schedule.timezone=lowercase;
      imported.digest=await G.digest(imported);
      assert.throws(()=>G.check(imported),/IANA/,`import rejects signed ${lowercase} on ${collection}`);
      assert.equal(await G.digest(imported),imported.digest,'rejected import does not rewrite signed content');
      for(const input of [timezone,lowercase]){
        const saved=G.transaction(original,2,'edit_access',q=>G.setAccess(q[collection][0],'student','campus',{...schedule,timezone:input},2));
        assert.equal(saved[collection][0].opening_schedule.timezone,timezone,`property save keeps the IANA key for ${input} on ${collection}`);
      }
    }
  }
  for(const bad of [{access_requirement:'vip'},{access_requirement:'resident_of:'},{access_area:''},
                    {opening_schedule:{status:'unknown',windows:[]}},
                    {opening_schedule:{...schedule,timezone:'Bad/Zone'}},
                    {opening_schedule:{...schedule,windows:[{weekdays:[7],start:'08:00',end:'18:00'}]}}])
    assert.throws(()=>G.checkAccess(bad));
  for(const access_conditionals of [null,[],{}, {'foot':'no'}, {'foot:conditional':['no']}]){
    assert.throws(()=>G.checkAccess({access_conditionals}),/条件门禁/);
  }
  const unchanged=G.clone(original);
  const target=unchanged.nodes.find(n=>n.place_id==='osm:node/2');
  G.move(unchanged,target.place_id,target.lat,target.lon,2);
  assert.equal(G.canonical(unchanged),G.canonical(original),'no-op movement preserves all provenance');
  const merged=G.transaction(original,2,'merge_nodes',p=>G.merge(p,'osm:node/3','osm:node/2',2));
  assert.equal(merged.nodes.find(n=>n.place_id===target.place_id).source,'osm','stationary target retains OSM provenance');
  for(const old of original.edges){
    const after=merged.edges.find(e=>e.edge_id===old.edge_id);
    if(old.from!=='osm:node/3'&&old.to!=='osm:node/3'){
      assert.equal(G.canonical(after),G.canonical(old),'unaffected target edge is not frozen as manual');
    }else if(after){
      assert.equal(after.source,'manual');
      const a=merged.nodes.find(n=>n.place_id===after.from),b=merged.nodes.find(n=>n.place_id===after.to);
      assert.equal(after.length_m,Math.round(G.distance(a,b)*1000)/1000,'only re-pointed edge distances change');
      assert.equal(after.estimated_duration_s,old.estimated_duration_s);
    }
  }
  let p=G.transaction(original,2,'add_node',p=>p.nodes.push(G.node(31.0015,121.0015,'manual:room',2)));
  const conditionals={'foot:conditional':'no @ (Mo-Fr 08:00-18:00)'};
  const conditionalEdge=q=>q.edges.find(e=>e.edge_id==='manual:connection');
  p=G.transaction(p,2,'add_edge',q=>{
    const edge=G.edge(q.nodes[0],q.nodes.at(-1),'manual:connection',2);
    q.edges.push({...edge,access:'yes',access_conditionals:conditionals});
  });
  assert.equal(G.accessState(conditionalEdge(p)),'unparsed','a raw public baseline still needs conditional review');
  const seconds=p.edges.at(-1).estimated_duration_s;
  p=G.transaction(p,2,'move_node',p=>G.move(p,'manual:room',31.0016,121.0015,2));
  assert.equal(p.edges.at(-1).estimated_duration_s,seconds,'moving retains editable time');
  assert.equal(p.edges.at(-1).estimate.model,'manual-retained/v1');
  const first=p.edges[0].edge_id;
  p=G.transaction(p,2,'split_edge',p=>G.split(p,first,'manual:split','manual:left','manual:right',2));
  assert.ok(!p.edges.some(e=>e.edge_id===first));
  assert.ok(p.nodes.some(n=>n.place_id==='manual:split'));
  p=G.transaction(p,2,'merge_nodes',p=>G.merge(p,'manual:split','manual:room',2));
  assert.ok(!p.nodes.some(n=>n.place_id==='manual:split'));
  assert.ok(!p.edges.some(e=>e.from==='manual:split'||e.to==='manual:split'));
  const removed=p.edges.find(e=>e.edge_id==='manual:right');
  p=G.transaction(p,2,'remove_edges',p=>G.remove(p,'edges',removed.edge_id));
  assert.ok(p.edit_history.at(-1).changes.some(c=>c.id===removed.edge_id&&c.after===null));
  assert.throws(()=>G.transaction(p,2,'bad_edit',p=>p.edges[0].to='nonexistent'),/引用/);
  assert.throws(()=>G.transaction(p,2,'bad_time',p=>p.edges[0].estimated_duration_s=0),/时间/);
  p=G.transaction(p,2,'edit_access',q=>{
    G.setAccess(q.nodes[0],'student','campus',{...schedule,timezone:'asia/shanghai'},2);
    G.setAccess(q.edges[0],'student','campus',{...schedule,timezone:'europe/kyiv'},2);
    G.setAccess(conditionalEdge(q),'public',null,null,2);
  });
  assert.equal(p.nodes[0].opening_schedule.timezone,'Asia/Shanghai');
  assert.equal(p.edges[0].opening_schedule.timezone,'Europe/Kyiv');
  assert.equal(G.accessState(conditionalEdge(p)),'unparsed','an explicit public baseline still needs conditional review');
  p.build_report=G.report(p);
  p.digest=await G.digest(p);
  const exported=JSON.stringify(p,null,2),roundTrip=JSON.parse(exported);
  assert.equal(conditionalEdge(roundTrip).access,'yes');
  assert.equal(conditionalEdge(roundTrip).access_requirement,'public');
  eq(conditionalEdge(roundTrip).access_conditionals,conditionals);
  assert.ok(roundTrip.build_report.unparsed_access.includes(conditionalEdge(roundTrip).edge_id));
  for(const accessEvidence of [
    {access_directional:{'foot:forward':'no'}},
    {osm_access_conflicts:{previous:{access:'yes'},osm:{access:'private'}}},
  ]){
    const edited=G.transaction(original,2,'edit_edges',q=>Object.assign(q.edges[0],accessEvidence));
    assert.equal(G.accessState(edited.edges[0]),'unparsed');
    assert.ok(edited.build_report.unparsed_access.includes(edited.edges[0].edge_id));
  }
  fs.writeFileSync(process.argv[3],exported);

  // ---- Box-select hit testing (identity projection: x=lon, y=lat).
  const project=(lon,lat)=>[lon,lat];
  let box=G.boxSelect(original,project,{x0:121.0005,y0:30.9995,x1:121.0015,y1:31.0015},true);
  eq(box.nodes.sort(),['osm:node/2','osm:node/3']);
  eq(box.edges,original.edges.filter(e=>[e.from,e.to].sort().join()==='osm:node/2,osm:node/3').map(e=>e.edge_id),'edge selected only when both ends are inside');
  eq(G.boxSelect(original,project,{x0:121.0015,y0:31.0015,x1:121.0005,y1:30.9995},false),{nodes:box.nodes,edges:[]},'reversed drag, edges optional');
  eq(G.boxSelect(original,project,{x0:0,y0:0,x1:1,y1:1},true),{nodes:[],edges:[]});

  // ---- Batch delete: one transaction, one history entry, connected edges and gateways included.
  const deleteIds=['osm:node/1','osm:node/4'],keptEdge=original.edges.find(e=>e.from!=='osm:node/1'&&e.to!=='osm:node/1'&&e.from!=='osm:node/4'&&e.to!=='osm:node/4');
  const impact=G.impact(original,deleteIds,[keptEdge.edge_id]);
  const deleted=G.transaction(original,2,'batch_delete',q=>G.removeMany(q,deleteIds,[keptEdge.edge_id]));
  assert.equal(deleted.edit_history.length,original.edit_history.length+1,'exactly one history entry');
  const entry=deleted.edit_history.at(-1);
  assert.equal(entry.changes.filter(c=>c.after===null&&c.collection==='nodes').length,impact.nodes);
  assert.equal(entry.changes.filter(c=>c.after===null&&c.collection==='edges').length,impact.edges);
  assert.equal(entry.changes.filter(c=>c.after===null&&c.collection==='gateways').length,impact.gateways);
  assert.ok(impact.gateways>=1&&impact.edges>=3,'gateway at node/1 and its edges counted');
  assert.ok(!deleted.edges.some(e=>deleteIds.includes(e.from)||deleteIds.includes(e.to)));
  assert.ok(!deleted.gateways.some(g=>deleteIds.includes(g.place_id)));
  // UI undo restores the pre-edit collections in a single further transaction.
  const undone=G.transaction(deleted,2,'undo',q=>{for(const key of Object.keys(G.keys))q[key]=G.clone(original[key]);});
  for(const key of Object.keys(G.keys))assert.equal(G.canonical(undone[key]),G.canonical(original[key]),'one undo reverts the whole batch');

  // ---- Batch merge into a new place: rewiring, internal edges, parallel collapse, tombstones.
  const base=G.transaction(original,2,'add_edges',q=>{const n=new Map(q.nodes.map(x=>[x.place_id,x]));q.edges.push(G.edge(n.get('osm:node/3'),n.get('osm:node/1'),'manual:p13',2),G.edge(n.get('osm:node/1'),n.get('osm:node/2'),'manual:p12',2));q.edges.find(e=>e.edge_id==='manual:p12').estimated_duration_s=5;});
  const group=['osm:node/2','osm:node/3','osm:node/4'];
  const internalIds=base.edges.filter(e=>group.includes(e.from)&&group.includes(e.to)).map(e=>e.edge_id).sort();
  const untouched=base.edges.filter(e=>!group.includes(e.from)&&!group.includes(e.to));
  const historyBefore=base.edit_history.length;
  const batch=G.transaction(base,2,'batch_merge',(q,extra)=>G.mergeMany(q,group,{place_id:'manual:node/east',name:'示例宿舍',type:'dormitory'},2,extra));
  assert.equal(batch.edit_history.length,historyBefore+1,'one history entry for the whole merge');
  const record=batch.edit_history.at(-1),note=record.note;
  const east=batch.nodes.find(n=>n.place_id==='manual:node/east');
  assert.equal(east.name,'示例宿舍');assert.equal(east.type,'dormitory');assert.equal(east.source,'manual');eq(east.osm_ids,[],'no OSM identity reassigned');
  const members=original.nodes.filter(n=>group.includes(n.place_id));
  assert.equal(east.lat,members.reduce((s,n)=>s+n.lat,0)/3,'centroid latitude');
  eq(east.merged_from,group);
  for(const id of group){
    assert.ok(!batch.nodes.some(n=>n.place_id===id));
    assert.ok(record.changes.some(c=>c.collection==='nodes'&&c.id===id&&c.after===null),'merged place is a deletion tombstone');
  }
  eq(note.removed_internal_edges,internalIds);
  for(const id of internalIds)assert.ok(!batch.edges.some(e=>e.edge_id===id),'internal edges removed');
  assert.ok(!batch.edges.some(e=>e.from===e.to),'no self-loops');
  assert.equal(note.collapsed_parallel_edges.length,1);
  const collapsed=note.collapsed_parallel_edges[0];
  assert.equal(collapsed.kept,'manual:p12','kept the smaller estimated seconds');
  eq(collapsed.removed,['manual:p13']);
  const survivor=batch.edges.find(e=>e.edge_id==='manual:p12');
  assert.equal(survivor.estimated_duration_s,5);assert.equal(survivor.estimate.needs_review,true);eq(survivor.estimate.collapsed_edge_ids,['manual:p13']);
  assert.ok(batch.build_report.edges_needing_review.includes('manual:p12'));
  assert.ok(!batch.edges.some(e=>e.edge_id==='manual:p13'));
  const directed=batch.edges.filter(e=>[e.from,e.to].includes('manual:node/east')&&[e.from,e.to].includes('osm:node/1'));
  assert.equal(directed.length,2,'forward and two-way edges are not parallel to each other');
  for(const e of untouched)assert.equal(G.canonical(batch.edges.find(x=>x.edge_id===e.edge_id)),G.canonical(e),'untouched edges are not frozen as manual');
  for(const id of note.rewired_edges){const e=batch.edges.find(x=>x.edge_id===id);assert.equal(e.source,'manual');assert.ok(e.from==='manual:node/east'||e.to==='manual:node/east');}
  assert.ok(note.rewired_edges.length>=2);

  // ---- Merge into an existing selected place keeps that place's OSM provenance.
  const intoExisting=G.transaction(original,2,'batch_merge',(q,extra)=>G.mergeMany(q,['osm:node/2','osm:node/3'],'osm:node/2',2,extra));
  assert.equal(G.canonical(intoExisting.nodes.find(n=>n.place_id==='osm:node/2')),G.canonical(original.nodes.find(n=>n.place_id==='osm:node/2')));
  assert.throws(()=>G.mergeMany(G.clone(original),['osm:node/2'],'osm:node/2',2),/两个/);
  assert.throws(()=>G.mergeMany(G.clone(original),['osm:node/2','osm:node/3'],'osm:node/4',2),/已选/);
  assert.throws(()=>G.mergeMany(G.clone(original),['osm:node/2','osm:node/3'],{place_id:'manual:x',name:' ',type:'room'},2),/名称/);
  const batchUndo=G.transaction(batch,2,'undo',q=>{for(const key of Object.keys(G.keys))q[key]=G.clone(base[key]);});
  for(const key of Object.keys(G.keys))assert.equal(G.canonical(batchUndo[key]),G.canonical(base[key]),'one undo reverts the merge');

  // ---- Edges with different walking constraints stay parallel after rewiring.
  for(const [label,left,right] of [
    ['structured public/staff',{access_requirement:'public'},{access_requirement:'staff'}],
    ['raw access with the same structured requirement',
      {access_requirement:'public',access:'public'},{access_requirement:'public',access:'仅教职工可用'}],
    ['access area',{access:'private',access_area:'east'},{access:'private',access_area:'west'}],
    ['region fallback',{access:'private',region:'east'},{access:'private',region:'west'}],
    ['access zone',{access_zone:'east'},{access_zone:'west'}],
    ['structured opening',{opening_schedule:schedule},{opening_schedule:{...schedule,windows:[]}}],
    ['raw opening with the same structured schedule',
      {opening_schedule:schedule,availability:'24/7'},{opening_schedule:schedule,availability:'Mo-Fr 08:00-18:00'}],
    ['foot override',{tags:{access:'private',foot:'yes'}},{tags:{access:'private',foot:'no'}}],
    ['conditional access',{access:'yes'},{access:'yes',access_conditionals:conditionals}],
    ['enabled',{enabled:true},{enabled:false}],
  ]){
    const parallel=G.clone(original),n=new Map(parallel.nodes.map(x=>[x.place_id,x]));
    const target=n.get('osm:node/2'),from=n.get('osm:node/3'),other=n.get('osm:node/1');
    parallel.edges=[
      {...G.edge(other,target,'manual:target-edge',2),estimated_duration_s:20,availability:'24/7',...left},
      {...G.edge(other,from,'manual:rewired-edge',2),estimated_duration_s:5,availability:'24/7',...right},
    ];
    const after=G.transaction(parallel,2,'merge_nodes',(q,extra)=>G.merge(q,from.place_id,target.place_id,2,extra));
    const history=after.edit_history.at(-1);
    eq(history.note.collapsed_parallel_edges,[],label);
    eq(history.note.rewired_edges,['manual:rewired-edge'],label);
    eq(after.edges[0],parallel.edges[0],label);
    eq(after.edges.map(e=>[e.edge_id,e.to,e.estimated_duration_s]),[
      ['manual:target-edge',target.place_id,20],['manual:rewired-edge',target.place_id,5],
    ],label);
    for(const [field,value] of Object.entries(right))eq(after.edges[1][field],value,label);
  }

  // Export after batch operations: Python verifies the digest and regeneration tombstones.
  batch.build_report=G.report(batch);
  batch.digest=await G.digest(batch);
  if(process.argv[4])fs.writeFileSync(process.argv[4],JSON.stringify(batch,null,2));

  // ---- Gateways (出入口): create, tick modes, cancel; each is one transaction and one undo.
  const gate=(q,id)=>q.gateways.find(g=>g.place_id===id),hist=original.edit_history.length;
  assert.equal(gate(original,'osm:node/1').source,'osm','fixture has one OSM walk gateway');
  let gw=G.transaction(original,2,'add_gateway',q=>G.addGateway(q,'osm:node/2','manual:gateway/school',2));
  assert.equal(gw.edit_history.length,hist+1,'one history entry');
  const place2=gw.nodes.find(n=>n.place_id==='osm:node/2');
  eq(gate(gw,'osm:node/2'),{gateway_id:'manual:gateway/school',place_id:'osm:node/2',lat:place2.lat,lon:place2.lon,coordinate_system:'WGS84',modes:['walk'],source:'manual',osm_ids:[],version:2});
  eq(gw.edit_history.at(-1).changes.map(c=>[c.collection,c.id,c.before]),[['gateways','manual:gateway/school',null]],'the place itself keeps its OSM provenance');
  assert.throws(()=>G.addGateway(G.clone(gw),'osm:node/2','manual:gateway/again',2),/已是出入口/);
  assert.throws(()=>G.addGateway(G.clone(gw),'osm:node/404','manual:gateway/x',2),/不存在/);
  assert.throws(()=>G.addGateway(G.clone(gw),'osm:node/3','manual:gateway/school',2),/重复/);
  gw=G.transaction(gw,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','metro',true,2));
  gw=G.transaction(gw,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','bus',true,2));
  eq(gate(gw,'osm:node/2').modes,['walk','bus','metro'],'modes kept in walk/bus/metro order');
  gw=G.transaction(gw,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','walk',false,2));
  eq(gate(gw,'osm:node/2').modes,['bus','metro']);
  assert.equal(gw.edit_history.length,hist+4,'one entry per tick');
  assert.throws(()=>G.transaction(gw,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','bus',true,2)),/没有变化/);
  const metroOnly=G.transaction(gw,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','bus',false,2));
  assert.throws(()=>G.setGatewayMode(G.clone(metroOnly),'osm:node/2','metro',false,2),/「地铁」是这个出入口剩下的唯一交通方式/,'the last mode cannot be unticked, with a specific message');
  assert.throws(()=>G.setGatewayMode(G.clone(gw),'osm:node/2','ferry',true,2),/未知/);
  assert.throws(()=>G.setGatewayMode(G.clone(gw),'osm:node/3','bus',true,2),/不是出入口/);
  const foreign=G.clone(gw);gate(foreign,'osm:node/2').modes=['ferry','metro'];
  G.setGatewayMode(foreign,'osm:node/2','walk',true,2);eq(gate(foreign,'osm:node/2').modes,['walk','metro','ferry'],'unknown modes are preserved');
  assert.throws(()=>{G.setGatewayMode(foreign,'osm:node/2','walk',false,2);G.setGatewayMode(foreign,'osm:node/2','metro',false,2);},/唯一/,'an unknown mode alone does not count');
  // Ticking a mode on an OSM gateway freezes only that gateway as manual, keeping its ID and OSM identity.
  gw=G.transaction(gw,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/1','metro',true,2));
  const osmGate=gate(gw,'osm:node/1'),osmBefore=gate(original,'osm:node/1');
  eq(osmGate,{...osmBefore,modes:['walk','metro'],source:'manual',version:2});
  eq(gw.edit_history.at(-1).changes.map(c=>c.id),[osmBefore.gateway_id]);
  // Moving a place carries its gateway.
  const moved=G.transaction(gw,2,'move_node',q=>G.move(q,'osm:node/2',place2.lat+.0001,place2.lon,2));
  assert.equal(gate(moved,'osm:node/2').lat,place2.lat+.0001);
  // Cancelling removes the gateway (a tombstone in history); one undo brings it back.
  const cancelled=G.transaction(gw,2,'remove_gateway',q=>G.removeGateways(q,'osm:node/2'));
  assert.ok(!gate(cancelled,'osm:node/2'));
  eq(cancelled.edit_history.at(-1).changes.map(c=>[c.id,c.after]),[['manual:gateway/school',null]]);
  assert.throws(()=>G.removeGateways(G.clone(cancelled),'osm:node/2'),/不是出入口/);
  const regained=G.transaction(cancelled,2,'undo',q=>{for(const key of Object.keys(G.keys))q[key]=G.clone(gw[key]);});
  for(const key of Object.keys(G.keys))assert.equal(G.canonical(regained[key]),G.canonical(gw[key]),'one undo restores the gateway');
  eq(G.modeText(['walk','bus','metro','ferry']),'步行、公交、地铁、ferry');eq(G.report(gw).counts.gateways,2);
  // Exports for Python: gw keeps an edited OSM gateway and a manual bus/metro one; dropOsm cancels the OSM one.
  gw.build_report=G.report(gw);gw.digest=await G.digest(gw);
  if(process.argv[5])fs.writeFileSync(process.argv[5],JSON.stringify(gw,null,2));
  const dropOsm=G.transaction(original,2,'remove_gateway',q=>G.removeGateways(q,'osm:node/1'));
  dropOsm.build_report=G.report(dropOsm);dropOsm.digest=await G.digest(dropOsm);
  if(process.argv[6])fs.writeFileSync(process.argv[6],JSON.stringify(dropOsm,null,2));
  // Merging an OSM walk gate into a metro-only 出入口 leaves two records at one place. The editor treats
  // them as one 出入口 (union of modes); the first mode edit consolidates them into the OSM gate's record.
  let mg=G.transaction(original,2,'add_gateway',q=>G.addGateway(q,'osm:node/2','manual:gateway/metro',2));
  mg=G.transaction(mg,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','metro',true,2));
  mg=G.transaction(mg,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','walk',false,2));
  mg=G.transaction(mg,2,'merge_nodes',(q,extra)=>G.merge(q,'osm:node/1','osm:node/2',2,extra));
  eq(G.gatewaysAt(mg,'osm:node/2').map(g=>g.modes).sort(),[['metro'],['walk']],'two records after merge');
  const osmGateId=osmBefore.gateway_id,mergedHist=mg.edit_history.length;
  for(const [mode,left] of [['metro',['walk']],['walk',['metro']]]){
    const after=G.transaction(mg,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2',mode,false,2));
    const list=G.gatewaysAt(after,'osm:node/2');
    assert.equal(list.length,1,'consolidated into one record');
    eq(list[0],{...osmBefore,place_id:'osm:node/2',lat:place2.lat,lon:place2.lon,modes:left,source:'manual',version:2},'keeps the OSM gate ID and osm_ids');
    assert.equal(after.edit_history.length,mergedHist+1,'one history entry');
    eq(after.edit_history.at(-1).changes.filter(c=>c.after===null).map(c=>c.id),['manual:gateway/metro'],'the other record is a tombstone');
    assert.throws(()=>G.setGatewayMode(G.clone(after),'osm:node/2',left[0],false,2),/唯一/);
    const back=G.transaction(after,2,'undo',q=>{for(const key of Object.keys(G.keys))q[key]=G.clone(mg[key]);});
    for(const key of Object.keys(G.keys))assert.equal(G.canonical(back[key]),G.canonical(mg[key]),'one undo restores both records');
  }
  const ticked=G.transaction(mg,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','bus',true,2));
  eq(G.gatewaysAt(ticked,'osm:node/2').map(g=>[g.gateway_id,g.modes]),[[osmGateId,['walk','bus','metro']]],'ticking also consolidates');
  assert.throws(()=>G.transaction(mg,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','walk',true,2)),/没有变化/,'a mode already in the union is a no-op');
  const consolidated=G.transaction(mg,2,'edit_gateway_modes',q=>G.setGatewayMode(q,'osm:node/2','walk',false,2));
  consolidated.build_report=G.report(consolidated);consolidated.digest=await G.digest(consolidated);
  if(process.argv[7])fs.writeFileSync(process.argv[7],JSON.stringify(consolidated,null,2));
  // A malformed mode list from a crafted package is rejected by the loader.
  const badModes=G.clone(original);badModes.gateways[0].modes=[''];assert.throws(()=>G.check(badModes),/出入口/);

  // ---- Web Mercator / tile grid and label collision helpers.
  eq(G.mercator(0,0),[.5,.5]);
  const [mx,my]=G.mercator(121.0015,31.0015),z=17,n=2**z;
  assert.equal(Math.floor(mx*n),Math.floor((121.0015+180)/360*n));
  const latRad=31.0015*Math.PI/180;assert.equal(Math.floor(my*n),Math.floor((1-Math.asinh(Math.tan(latRad))/Math.PI)/2*n),'slippy-map tile index');
  const back=G.unmercator(mx,my);assert.ok(Math.abs(back.lat-31.0015)<1e-9&&Math.abs(back.lon-121.0015)<1e-9);
  assert.equal(G.tileZoom(256*2**25),19,'max zoom 19');
  assert.equal(G.tileZoom(256*2**17*1.9),17,'zoom rounds down so tiles show at 1-2x');
  // With floor() zoom, the tiles a viewport needs never exceed the viewport-derived cap.
  for(const [w,h] of [[1920,1080],[2560,1440],[420,600]])for(const scale of [1,1.3,1.7,1.99]){
    const px=256*2**16*scale,zz=G.tileZoom(px),span={x0:mx-w/2/px,y0:my-h/2/px,x1:mx+w/2/px,y1:my+h/2/px};
    assert.ok(G.tilesFor(span,zz,.1).length<=G.maxTiles(w,h,.1),`tile cap ${w}x${h}@${scale}`);
  }
  // Ring winding is normalised, so nested same-colour areas fill (nonzero) instead of leaving holes.
  const signed=r=>r.reduce((s,[x1,y1],i)=>{const [x2,y2]=r[(i+1)%r.length];return s+x1*y2-x2*y1;},0);
  const outer=[[0,0],[10,0],[10,10],[0,10]],inner=[[3,3],[3,6],[6,6],[6,3]];
  assert.ok(signed(outer)*signed(inner)<0,'fixture rings start with opposite windings');
  assert.ok(signed(G.orientRing(outer))>0&&signed(G.orientRing(inner))>0,'both rings share one winding');
  // Untrusted tags: only strings, only own style-table keys.
  const table={road:1};
  assert.equal(G.own(table,'constructor'),undefined);assert.equal(G.own(table,'__proto__'),undefined);assert.equal(G.own(table,{}),undefined);assert.equal(G.own(table,'road'),1);
  assert.equal(G.tagText({name:{}},'name'),'');assert.equal(G.tagText({name:5},'name'),'');assert.equal(G.tagText(null,'name'),'');assert.equal(G.tagText({name:'湖'},'name'),'湖');
  const tiles=G.tilesFor({x0:mx,y0:my,x1:mx+.5/n,y1:my+.5/n},z,0);
  assert.ok(tiles.length>=1&&tiles.length<=4&&tiles.every(t=>t.url.startsWith('https://tile.openstreetmap.org/17/')&&t.url.endsWith('.png')));
  const labels=G.placeLabels([{text:'A',x:50,y:50,w:40,h:14,priority:2},{text:'B',x:60,y:52,w:40,h:14,priority:0},{text:'C',x:200,y:50,w:40,h:14,priority:3},{text:'B',x:100,y:100,w:40,h:14,priority:0}],{width:400,height:300});
  eq(labels.map(l=>l.text),['B','C'],'collisions and nearby duplicates dropped by priority');
  console.log('Editor core: digest parity, IANA timezone import/save validation, add/move/connect/split/merge/remove, box select, batch delete/merge, gateways, undo, tiles, labels passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
