(function(){
"use strict";
let charts=[];
let summaryText="";
let qualityState={active:[],history:[]};
const labels={temperature:"Temperature",dewpoint:"Dewpoint",wind_speed:"Wind speed",wind_gust:"Wind gust",wind_direction:"Wind direction",wind_chill:"Wind chill",heat_index:"Heat index",precipitation:"Precipitation",snowfall:"Snowfall",snow_depth:"Snow depth",ice_accretion:"Ice accretion",relative_humidity:"Relative humidity",snow_water_equivalent:"Snow water equivalent",ice_accretion_total:"Total ice accretion",present_weather:"Present weather",soil_temperature_5cm:"Soil temperature · 5 cm",soil_temperature_25cm:"Soil temperature · 25 cm",soil_temperature_50cm:"Soil temperature · 50 cm",soil_moisture_5cm:"Soil moisture · 5 cm",soil_moisture_25cm:"Soil moisture · 25 cm",soil_moisture_50cm:"Soil moisture · 50 cm",frozen_soil_5cm:"Frozen soil · 5 cm",frozen_soil_25cm:"Frozen soil · 25 cm",frozen_soil_50cm:"Frozen soil · 50 cm"};
const colors={temperature:"#c65b42",dewpoint:"#3d83a8",wind_speed:"#476b52",wind_gust:"#b08a42",wind_direction:"#637f9c",wind_chill:"#4575b4",heat_index:"#d66a36",precipitation:"#3979ad",snowfall:"#6e9fc1",snow_depth:"#7e8dba",ice_accretion:"#8c6caa",relative_humidity:"#59a5a0",snow_water_equivalent:"#7aa6cc",ice_accretion_total:"#8c6caa",present_weather:"#7d8c9b",soil_temperature_5cm:"#d16b45",soil_temperature_25cm:"#c78c4a",soil_temperature_50cm:"#ad9a58",soil_moisture_5cm:"#498f77",soil_moisture_25cm:"#508f83",soil_moisture_50cm:"#5b8992",frozen_soil_5cm:"#678b64",frozen_soil_25cm:"#6b9270",frozen_soil_50cm:"#71977b"};
const units={temperature:"°F",dewpoint:"°F",wind_speed:"mph",wind_gust:"mph",wind_direction:"°",wind_chill:"°F",heat_index:"°F",precipitation:"in",snowfall:"in",snow_depth:"in",ice_accretion:"in",relative_humidity:"%",snow_water_equivalent:"in",ice_accretion_total:"in",present_weather:"",soil_temperature_5cm:"°F",soil_temperature_25cm:"°F",soil_temperature_50cm:"°F",soil_moisture_5cm:"m³/m³",soil_moisture_25cm:"m³/m³",soil_moisture_50cm:"m³/m³",frozen_soil_5cm:"flag",frozen_soil_25cm:"flag",frozen_soil_50cm:"flag"};
const $=id=>document.getElementById(id);
const safe=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function fmt(v){return Number(v).toLocaleString("en-US",{maximumFractionDigits:3});}
function time(v){if(!v)return "—";const d=new Date(v);return Number.isNaN(d.getTime())?"—":d.toLocaleString("en-US",{timeZone:"UTC",month:"short",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false})+"Z";}
function api(){return "https://btv-observation-api.jusmessinwityah1.workers.dev";}
async function get(path){const r=await fetch(api()+path,{cache:"no-store"});if(!r.ok)throw new Error("API "+r.status+" for "+path);return r.json();}
function stationGroup(station){
 const type=String(station.report_type||"").toLowerCase(),source=String(station.source||"").toLowerCase(),network=String(station.network||"").toLowerCase();
 if(["asos/awos","asos","awos"].includes(type)||source==="iem_asos"||source==="nws"||network.includes("asos")||network.includes("awos"))return "ASOS/AWOS";
 if(type==="cocorahs"||source==="cocorahs")return "CoCoRaHS";
 if(type==="coop"||source==="iem_coop"||source==="iem_rr3_coop"||network.includes("coop"))return "COOP";
 if(type==="rwis"||source==="iem_rwis"||network.includes("rwis"))return "RWIS";
 if(type.includes("nysm")||type.includes("mesonet")||source.includes("nysm")||source.includes("mesonet")||network.includes("nysm")||network.includes("mesonet"))return "NYSM";
 return "Public Sites";
}
function stationLabel(station){
 const name=station.name||station.station_name||station.provider_station_id||station.station_id;
 return name+" · "+station.provider_station_id;
}
function populateStations(){
 const s=$("tsStation");if(!s)return;const prior=s.value;s.innerHTML='<option value="">Select a station…</option>';
 ["ASOS/AWOS","CoCoRaHS","COOP","RWIS","NYSM","Public Sites"].forEach(group=>{
  const members=stations.filter(x=>stationGroup(x)===group).sort((a,b)=>stationLabel(a).localeCompare(stationLabel(b)));
  if(!members.length)return;
  const optgroup=document.createElement("optgroup");optgroup.label=group;
  members.forEach(x=>optgroup.appendChild(new Option(stationLabel(x),x.station_id)));
  s.appendChild(optgroup);
 });
 if(Array.from(s.options).some(o=>o.value===prior))s.value=prior;
 const qualitySelect=$("qualityStation");
 if(qualitySelect){
  const qualityPrior=qualitySelect.value;
  qualitySelect.innerHTML='<option value="">Choose a station…</option>';
  stations.slice().sort((a,b)=>stationLabel(a).localeCompare(stationLabel(b))).forEach(x=>qualitySelect.add(new Option(stationLabel(x),x.station_id)));
  if(Array.from(qualitySelect.options).some(o=>o.value===qualityPrior))qualitySelect.value=qualityPrior;
  updateSiteQualityControl();
 }
}
function normalizedStationCode(value){
 let code=String(value||"").split(":").pop().toUpperCase();
 if(/^K[A-Z0-9]{3}$/.test(code))code=code.slice(1);
 return code;
}
function resolveHistoryStation(selected){
 if(!selected)return {stationId:null,source:null};
 if(selected.source==="IEM_ASOS")return {stationId:selected.station_id,source:"IEM_ASOS"};
 const network=String(selected.network||"").toLowerCase();
 if(selected.source==="NWS"||network.includes("nws/asos")){
  const code=normalizedStationCode(selected.provider_station_id);
  const asos=stations.find(x=>x.source==="IEM_ASOS"&&normalizedStationCode(x.provider_station_id)===code);
  if(asos)return {stationId:asos.station_id,source:"IEM_ASOS"};
 }
 return {stationId:selected.station_id,source:selected.source||null};
}
function rangeFromControls(){
 if($("tsPeriod").value==="custom"){
  const a=new Date($("tsStart").value),b=new Date($("tsEnd").value);
  if(!Number.isFinite(a.getTime())||!Number.isFinite(b.getTime())||b<=a)throw new Error("Choose a valid custom start and end time.");
  return {start:a.toISOString(),end:b.toISOString()};
 }
 const end=Date.now(),hours=Number($("tsPeriod").value);return {start:new Date(end-hours*3600000).toISOString(),end:new Date(end).toISOString()};
}
const queryCache=new Map();let queryActive=0;const queryQueue=[];
function runLimited(fn){return new Promise((resolve,reject)=>{const run=()=>{queryActive++;Promise.resolve().then(fn).then(resolve,reject).finally(()=>{queryActive--;const next=queryQueue.shift();if(next)next();});};if(queryActive<3)run();else queryQueue.push(run);});}
function archivePath(params){
 const p=new URLSearchParams(params);
 const start=p.get("start"),end=p.get("end");
 const a=Date.parse(start),b=Date.parse(end);
 if(!Number.isFinite(a)||!Number.isFinite(b)||b<a)throw new Error("Invalid archive time range");
 const startDay=new Date(a).toISOString().slice(0,10),endDay=new Date(b).toISOString().slice(0,10);
 if(Math.floor((Date.parse(endDay+"T00:00:00Z")-Date.parse(startDay+"T00:00:00Z"))/86400000)+1>120)throw new Error("Historical archive searches are limited to 120 days.");
 p.delete("start");p.delete("end");p.set("start",startDay);p.set("end",endDay);
 p.set("start_time",new Date(a).toISOString());p.set("end_time",new Date(b).toISOString());
 return "/archive?"+p.toString();
}
async function fetchRows(element,range,stationId,extra){
 const params=new URLSearchParams({element:element,start:range.start,end:range.end,limit:"1000",sort:"time_asc"});
 if(stationId)params.set("station_id",stationId);
 Object.entries(extra||{}).forEach(([k,v])=>{if(v!==undefined&&v!==null&&v!=="")params.set(k,String(v));});
 const cacheKey=archivePath(params),now=Date.now(),cached=queryCache.get(cacheKey);
 if(cached&&now-cached.at<20000)return cached.rows;
 return runLimited(async()=>{
  const out=[];let offset=0,total=null;
  do{
   params.set("offset",String(offset));
   const d=await get(archivePath(params));total=d.total||0;out.push(...(d.results||[]));offset+=1000;
   if(!(d.results||[]).length)break;
  }while(offset<total);
  queryCache.set(cacheKey,{at:Date.now(),rows:out});
  for(const [key,val] of queryCache)if(Date.now()-val.at>20000)queryCache.delete(key);
  return out;
 });
}
function clearCharts(){charts.forEach(c=>c.destroy());charts=[];}
function renderCharts(series,station){
 tsLastSeries=series;tsLastStation=station;
 clearCharts();const root=$("tsCharts"),cards=[],tz=$("tsTimezone")?.value||"UTC";
 const dateLabel=ms=>new Date(ms).toLocaleString("en-US",{timeZone:tz==="UTC"?"UTC":undefined,month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:false})+(tz==="UTC"?"Z":"");
 Object.entries(series).forEach(([el,rows])=>{
  if(!rows.length)return;
  rows.sort((a,b)=>Date.parse(a.observation_time)-Date.parse(b.observation_time));
  const points=rows.map(r=>({x:Date.parse(r.observation_time),y:r.value===null||r.value===undefined||!Number.isFinite(Number(r.value))?null:Number(r.value)}));
  const sets=[];
  if(el==="precipitation"){
   const cumulative=[];let sum=0;points.forEach(p=>{if(Number.isFinite(p.y))sum+=p.y;cumulative.push({x:p.x,y:sum});});
   sets.push({type:"bar",label:"Hourly precipitation",data:points,backgroundColor:"#3979ad",borderColor:"#3979ad",borderWidth:1});
   sets.push({type:"line",label:"Running total",data:cumulative,borderColor:"#2f654b",backgroundColor:"#2f654b",borderWidth:2,pointRadius:1,yAxisID:"y1",tension:0});
  }else if(el==="ice_accretion"){
   sets.push({label:"Flat ice",data:points,borderColor:colors[el],backgroundColor:colors[el],borderWidth:2,pointRadius:2,tension:0});
   if(rows.some(r=>Number.isFinite(Number(r.radial_value))))sets.push({label:"Radial ice (40%)",data:rows.map(r=>({x:Date.parse(r.observation_time),y:Number.isFinite(Number(r.radial_value))?Number(r.radial_value):null})),borderColor:"#4b91a2",backgroundColor:"#4b91a2",borderWidth:2,pointRadius:2,tension:0});
  }else sets.push({label:labels[el]||el,data:points,borderColor:colors[el]||"#476b52",backgroundColor:colors[el]||"#476b52",borderWidth:2,pointRadius:2,tension:0,spanGaps:false});
  cards.push({id:"observationTsChart"+cards.length,title:labels[el]||el,sets,rows,unit:units[el]||rows[0].unit||"",precip:el==="precipitation"});
 });
 if(!cards.length){root.innerHTML='<div class="summary-empty" style="grid-column:1/-1">No observations were found for the selected station, variables and timeframe. Try a longer period or different variables.</div>';return;}
 root.innerHTML=cards.map(c=>'<div class="chart-card"><h3>'+safe(c.title)+'</h3><div class="chart-canvas"><canvas id="'+c.id+'"></canvas></div><div class="chart-caption">'+c.rows.length+' reports · '+safe(c.unit)+' · '+safe(tz)+' timestamps'+(c.precip?' · 1-hour bars + running total':'')+'</div></div>').join("");
 cards.forEach(c=>{
  const chart=new Chart($(c.id),{type:c.precip?"bar":"line",data:{datasets:c.sets},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"nearest",intersect:false},plugins:{legend:{display:c.sets.length>1,labels:{boxWidth:10,font:{size:10}}},tooltip:{callbacks:{title:items=>items.length?dateLabel(items[0].parsed.x):"",label:ctx=>ctx.dataset.label+": "+fmt(ctx.parsed.y)+" "+(ctx.dataset.yAxisID==="y1"?"in total":c.unit)}}},scales:{x:{type:"linear",ticks:{maxTicksLimit:7,maxRotation:0,font:{size:9},callback:v=>dateLabel(v)},grid:{display:false},title:{display:true,text:"Time ("+tz+")"}},y:{title:{display:true,text:c.unit,font:{size:10}},ticks:{font:{size:9}},grid:{color:"#ece9e1"}},...(c.precip?{y1:{position:"right",title:{display:true,text:"Running total (in)"},grid:{drawOnChartArea:false}}}:{})}}});
  charts.push(chart);
 });
 $("tsStatus").textContent=cards.length+" variables plotted · "+(station.name||station.provider_station_id);
}
async function loadTimeSeries(){
 const requestId=++tsRequestId;
 const id=$("tsStation").value;if(!id){$("tsStatus").textContent="Select a station first";return;}
 const vars=Array.from($("tsVariables").querySelectorAll("input:checked")).map(x=>x.value);
 if(!vars.length){$("tsStatus").textContent="Select at least one variable";return;}
 const btn=$("tsLoadBtn");btn.disabled=true;btn.textContent="Loading…";$("tsStatus").textContent="Retrieving station history…";
 try{
  const range=rangeFromControls(),hours=Number($("tsPeriod").value),extra={},selected=stations.find(x=>x.station_id===id),history=resolveHistoryStation(selected);
  if($("tsPeriod").value!=="custom"&&["1","3","6","12","24","48","72","120"].includes(String(hours))){extra.period=hours;}
  const result=await Promise.all(vars.map(async el=>{
   const opts={};
   if(el==="precipitation"){opts.statistic="1h_accumulation";opts.accumulation_hours=1;}
   return [el,await fetchRows(el,range,history.stationId,opts)];
  }));
  const station=selected||{name:id};if(requestId!==tsRequestId)return;renderCharts(Object.fromEntries(result),station);if(history.stationId!==id)$("tsStatus").textContent="Using "+history.source+" archive for "+(station.name||station.provider_station_id)+" · "+Object.values(Object.fromEntries(result)).reduce((n,r)=>n+r.length,0)+" reports";
 }catch(e){if(requestId!==tsRequestId)return;$("tsStatus").textContent="Time series failed: "+e.message;$("tsCharts").innerHTML='<div class="summary-empty" style="grid-column:1/-1">'+safe(e.message)+'</div>';}
 finally{if(requestId===tsRequestId){btn.disabled=false;btn.textContent="Load time series";}}
}
async function postQuality(path,body,token){
 const headers={"Content-Type":"application/json"};
 if(token)headers.Authorization="Bearer "+token;
 const response=await fetch(api()+path,{method:"POST",headers,body:JSON.stringify(body),cache:"no-store"});
 const data=await response.json().catch(()=>({error:"The API returned an unreadable response"}));
 if(!response.ok)throw new Error(data.error||("API "+response.status));
 return data;
}
async function refreshQuality(){
 $("qualityStatus").textContent="Loading quality records…";
 const state=await get("/quality");
 qualityState={active:Array.isArray(state.active)?state.active:[],history:Array.isArray(state.history)?state.history:[]};
 renderQuality();
}
function renderQuality(){
 const active=qualityState.active||[],history=(qualityState.history||[]).slice().reverse().slice(0,50);
 $("qualityStatus").textContent=active.length+" active quarantine"+(active.length===1?"":"s");
 updateSiteQualityControl();
 $("qualityActiveList").innerHTML=active.length?'<div class="summary-grid">'+active.map(entry=>'<div class="summary-card"><div class="summary-label">'+safe(labels[entry.element]||entry.element)+' · '+safe(entry.provider_station_id||entry.station_id)+'</div><div class="summary-value">'+safe(entry.station_name||entry.provider_station_id||entry.station_id)+'</div><div class="summary-meta">Flagged '+safe(time(entry.flagged_at))+' UTC by '+safe(entry.flagged_by)+'</div><div class="summary-meta">'+safe(entry.reason)+'</div><div style="margin-top:12px"><button class="btn" type="button" data-restore-quality="'+safe(entry.key)+'">Confirm data is good / restore</button></div></div>').join("")+'</div>':'<div class="summary-empty">No active site quarantines. All reporting sites are currently included.</div>';
 $("qualityHistoryList").innerHTML=history.length?'<div class="table-wrap"><table><thead><tr><th>Action</th><th>Site</th><th>Element</th><th>Reason</th><th>Person</th><th>Time (UTC)</th></tr></thead><tbody>'+history.map(item=>'<tr><td>'+safe(item.action)+'</td><td>'+safe(item.station_name||item.provider_station_id||item.station_id)+'</td><td>'+safe(labels[item.element]||item.element)+'</td><td>'+safe(item.reason)+'</td><td>'+safe(item.action==="restored"?item.restored_by:item.flagged_by)+'</td><td>'+safe(time(item.action==="restored"?item.restored_at:item.flagged_at))+'</td></tr>').join("")+'</tbody></table></div>':'<div class="summary-empty">No quality actions recorded yet.</div>';
}
function updateSiteQualityControl(){
 const select=$("qualityStation"),element=$("qualityElement"),button=$("qualityToggleBtn");
 if(!select||!element||!button)return;
 const station=stations.find(x=>x.station_id===select.value),el=element.value;
 if(!station){button.disabled=true;button.textContent="Choose a site and element";$("qualitySelectionStatus").textContent="Choose a station and weather element.";return;}
 const code=normalizedStationCode(station.provider_station_id);
 const active=(qualityState.active||[]).find(entry=>entry.element===el&&
  (entry.provider_station_id?normalizedStationCode(entry.provider_station_id)===code:entry.station_id===station.station_id));
 button.disabled=false;
 button.textContent=active?"Turn ON · Restore data":"Turn OFF · Quarantine data";
 button.classList.toggle("primary",!active);
 $("qualitySelectionStatus").textContent=active
  ? "Currently OFF — "+(labels[el]||el)+" from "+(station.name||station.provider_station_id)+" is excluded. Reason: "+active.reason
  : "Currently ON — "+(labels[el]||el)+" from "+(station.name||station.provider_station_id)+" is included.";
}
async function toggleSelectedSiteQuality(){
 const station=stations.find(x=>x.station_id===$("qualityStation").value);
 if(!station)return;
 const element=$("qualityElement").value,actor=$("qualityActor").value.trim(),token=$("qualityReviewerToken").value;
 if(!actor){alert("Enter your name so the quality action can be audited.");return;}
 const code=normalizedStationCode(station.provider_station_id);
 const active=(qualityState.active||[]).find(entry=>entry.element===element&&
  (entry.provider_station_id?normalizedStationCode(entry.provider_station_id)===code:entry.station_id===station.station_id));
 const button=$("qualityToggleBtn");button.disabled=true;
 try{
  if(active){
   if(!token)throw new Error("A reviewer authorization token is required to turn this data back ON.");
   await postQuality("/quality/restore",{key:active.key,actor},token);
   $("qualityReviewerToken").value="";
  }else{
   const reason=$("qualityReason").value.trim();
   if(!reason)throw new Error("Enter a reason before turning this site and element OFF.");
   await postQuality("/quality/flag",{station_id:station.station_id,provider_station_id:station.provider_station_id,station_name:station.name||station.provider_station_id,element,actor,reason});
   $("qualityReason").value="";
  }
  await refreshQuality();
  if(typeof retrieve==="function")await retrieve();
  if($("mapPanel").style.display!=="none"){mapDirty=true;updateObservationMap();}
 }catch(error){alert("Could not change site status: "+error.message);}
 finally{updateSiteQualityControl();}
}
async function flagObservation(button){
 const actor=prompt("Your name (recorded in the quality audit):");
 if(!actor||!actor.trim())return;
 const reason=prompt("Why should this station's "+(labels[button.dataset.element]||button.dataset.element)+" reports be quarantined?");
 if(!reason||!reason.trim())return;
 button.disabled=true;
 try{
  await postQuality("/quality/flag",{station_id:button.dataset.stationId,provider_station_id:button.dataset.providerStationId,station_name:button.dataset.stationName,element:button.dataset.element,actor:actor.trim(),reason:reason.trim()});
  await refreshQuality();
  if(typeof retrieve==="function")await retrieve();
  if($("mapPanel").style.display!=="none"){mapDirty=true;updateObservationMap();}
  alert("Site and element quarantined. Its reports are now excluded from the dashboard.");
 }catch(error){alert("Could not flag this site: "+error.message);}
 finally{button.disabled=false;}
}
async function restoreQuality(button){
 const actor=$("qualityActor").value.trim(),token=$("qualityReviewerToken").value;
 if(!actor){alert("Enter the reviewing person's name first.");return;}
 if(!token){alert("Enter the quality review authorization token first.");return;}
 button.disabled=true;
 try{
  await postQuality("/quality/restore",{key:button.dataset.restoreQuality,actor},token);
  $("qualityReviewerToken").value="";
  await refreshQuality();
  if(typeof retrieve==="function")await retrieve();
  if($("mapPanel").style.display!=="none"){mapDirty=true;updateObservationMap();}
  alert("Quarantine removed. This station's reports are eligible for use again.");
 }catch(error){alert("Could not restore this site: "+error.message);}
 finally{button.disabled=false;}
}
function showView(view){
 const panels={table:"tablePanel",map:"mapPanel",timeseries:"timeSeriesPanel",summary:"summaryPanel",sitequality:"siteQualityPanel"};
 const tabs={table:"tableTab",map:"mapTab",timeseries:"timeSeriesTab",summary:"summaryTab",sitequality:"siteQualityTab"};
 Object.entries(panels).forEach(([k,id])=>$(id).style.display=k===view?"":"none");
 Object.entries(tabs).forEach(([k,id])=>$(id).classList.toggle("active",k===view));
 if(view==="map"){ensureObservationMap();setTimeout(()=>{observationMap.invalidateSize();if(mapDirty){updateObservationMap();mapDirty=false;}},100);}
 if(view==="timeseries"&&!$("tsStation").value&&$("station").value)$("tsStation").value=$("station").value;
 if(view==="summary"&&!summaryText)generateSummary();
 if(view==="sitequality")refreshQuality().catch(e=>$("qualityStatus").textContent="Quality service unavailable: "+e.message);
}
function openStation(id){$("tsStation").value=id;showView("timeseries");loadTimeSeries();}
window.openTimeSeriesForStation=openStation;
function card(title,row,unit,ice){
 if(!row)return "";
 const station=stations.find(s=>s.station_id===row.station_id),name=row.station_name||(station&&station.name)||row.provider_station_id||"Unknown station";
 const period=row.accumulation_hours?row.accumulation_hours+"-hour accumulation":String(row.statistic||"observation").replaceAll("_"," ");
 const radial=ice?'<div class="summary-meta">Flat ice '+fmt(row.value)+' '+safe(unit)+' · Radial ice '+fmt(row.radial_value==null?Number(row.value)*.4:row.radial_value)+' '+safe(row.radial_unit||unit)+' (40% conversion)</div>':"";
 return '<div class="summary-card"><div class="summary-label">'+safe(title)+'</div><div class="summary-value">'+fmt(row.value)+' '+safe(unit)+'</div><div class="summary-site">'+safe(name)+'</div><div class="summary-meta">'+safe(time(row.observation_time))+' UTC · '+safe(row.source||"")+' · '+safe(period)+'</div>'+radial+'</div>';
}
function extreme(rows,mode){
 const valid=rows.filter(r=>typeof r.value==="number"&&Number.isFinite(r.value));if(!valid.length)return null;
 return valid.reduce((best,r)=>!best||(mode==="min"?r.value<best.value:r.value>best.value)?r:best,null);
}
async function generateSummary(){
 const btn=$("generateSummaryBtn"),hours=Number($("summaryPeriod").value),endMs=Date.now(),range={start:new Date(endMs-hours*3600000).toISOString(),end:new Date(endMs).toISOString()};
 btn.disabled=true;btn.textContent="Analyzing…";$("summaryStatus").textContent="Reviewing observations across the BTV CWA…";
 $("summaryResults").innerHTML='<div class="summary-empty">Retrieving aggregated summary from the observation Worker…</div>';
 try{
  const data=await get("/summary?hours="+encodeURIComponent(hours)+"&top=5");
  const d=data.results||{};
  const rows=[
   ["Highest precipitation · "+hours+"h",d.precipitation_max?.[0],"in",false],
   ["Greatest snowfall · "+hours+"h",d.snowfall?.[0],"in",false],
   ["Greatest ice accretion · "+hours+"h",d.ice_accretion_max?.[0],"in",true],
   ["Maximum heat index",d.heat_index_max?.[0],"°F",false],
   ["Lowest wind chill",d.wind_chill_min?.[0],"°F",false],
   ["Strongest wind gust",d.wind_gust_max?.[0],"mph",false],
   ["Greatest snow depth",d.snow_depth_max?.[0],"in",false],
   ["Highest temperature",d.temperature_max?.[0],"°F",false],
   ["Lowest temperature",d.temperature_min?.[0],"°F",false]
  ];
  const html=rows.map(x=>card(x[0],x[1],x[2],x[3])).filter(Boolean);
  const generated=new Date(data.generated_at||Date.now()).toLocaleString("en-US",{timeZone:"UTC",dateStyle:"medium",timeStyle:"short"})+" UTC";
  const intro="Analysis window: "+time(range.start)+" – "+time(range.end)+" UTC · Generated "+generated+". Precipitation and ice use the selected accumulation period; snowfall is included only when its report duration matches. Heat index and wind chill appear only when valid derived observations exist.";
  $("summaryResults").innerHTML='<div class="summary-intro">'+safe(intro)+'</div>'+(html.length?'<div class="summary-grid">'+html.join("")+'</div>':'<div class="summary-empty">No qualifying observations were found. Missing categories are omitted rather than reported as zero.</div>');
  $("summaryStatus").textContent=html.length+" significant categories found";
  summaryText="BTV OPERATIONAL OBSERVATION SUMMARY\n"+intro+"\n\n"+rows.filter(x=>x[1]).map(x=>{const r=x[1],s=stations.find(y=>y.station_id===r.station_id);return x[0]+": "+fmt(r.value)+" "+x[2]+" at "+(r.station_name||(s&&s.name)||r.provider_station_id)+" · "+time(r.observation_time)+" UTC · "+(r.source||"")+(r.accumulation_hours?" · "+r.accumulation_hours+"h accumulation":"")}).join("\n");
 }catch(e){$("summaryStatus").textContent="Summary failed: "+e.message;$("summaryResults").innerHTML='<div class="summary-empty">'+safe(e.message)+'</div>';}
 finally{btn.disabled=false;btn.textContent="Generate summary";}
}
let initialized=false;

// Station-level QC drilldown: inspect a station's history for the active element.
let qcStation=null,qcElement=null,qcChart=null,tsLastSeries=null,tsLastStation=null,tsRequestId=0,qcRequestId=0;
function qcElementLabel(el){return labels[el]||String(el||"").replaceAll("_"," ");}
async function loadStationQC(){
 if(!qcStation||!qcElement)return;
 const hours=Number($("stationQcPeriod").value||24),end=Date.now(),range={start:new Date(end-hours*3600000).toISOString(),end:new Date(end).toISOString()};
 $("stationQcStatus").textContent="Loading hourly/history observations…";
 try{
  const rows=await fetchRows(qcElement,range,qcStation.station_id,{});
  rows.sort((a,b)=>Date.parse(a.observation_time)-Date.parse(b.observation_time));
  const stationName=qcStation.name||qcStation.provider_station_id;
  $("stationQcSubtitle").textContent=stationName+" · "+qcElementLabel(qcElement)+" · Past "+hours+" hours";
  $("stationQcStatus").textContent=rows.length+" reports retrieved";
  const chartLabels=rows.map(r=>new Date(r.observation_time).toLocaleString("en-US",{timeZone:"UTC",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:false})+"Z");
  const values=rows.map(r=>Number.isFinite(Number(r.value))?Number(r.value):null);
  if(qcChart)qcChart.destroy();
  qcChart=new Chart($("stationQcChart"),{type:qcElement==="precipitation"?"bar":"line",data:{labels:chartLabels,datasets:[{label:qcElementLabel(qcElement)+" ("+(rows[0]?.unit||"")+")",data:values,borderWidth:2,pointRadius:qcElement==="precipitation"?0:2,tension:0,spanGaps:false}]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"nearest",intersect:false},plugins:{legend:{display:true},tooltip:{callbacks:{label:c=>{const r=rows[c.dataIndex];return c.dataset.label+": "+c.parsed.y+" "+(r?.unit||"")+(r?.accumulation_hours?" · "+r.accumulation_hours+"h accumulation":"")}}}},scales:{x:{ticks:{maxTicksLimit:12,maxRotation:0}},y:{title:{display:true,text:rows[0]?.unit||""}}}}});
  $("stationQcRows").innerHTML=rows.slice().reverse().map(r=>'<tr><td>'+safe(time(r.observation_time))+'</td><td>'+safe(fmt(r.value))+'</td><td>'+safe(r.unit||"")+'</td><td>'+safe(r.statistic||"")+'</td><td>'+safe(r.accumulation_hours?r.accumulation_hours+" h":"—")+'</td><td>'+safe(r.qc_status||"unknown")+'</td><td>'+safe(JSON.stringify(r.metadata||{}))+'</td></tr>').join("")||'<tr><td colspan="7">No observations for this station and element in this window.</td></tr>';
 }catch(e){$("stationQcStatus").textContent="QC history failed: "+e.message;$("stationQcRows").innerHTML='<tr><td colspan="7">'+safe(e.message)+'</td></tr>'}
}
function openStationQC(row){
 const station=stations.find(s=>String(s.station_id)===String(row.station_id));
 qcStation=station||{station_id:row.station_id,name:row.station_name||row.provider_station_id,provider_station_id:row.provider_station_id};
 qcElement=$("element").value||row.element;
 $("stationQcPanel").style.display="";
 $("stationQcPeriod").value="24";
 loadStationQC();
 $("stationQcPanel").scrollIntoView({behavior:"smooth",block:"start"});
}

function init(){
 populateStations();
 if(initialized)return;
 initialized=true;
 $("stationQcClose").addEventListener("click",()=>{$("stationQcPanel").style.display="none";});
 $("stationQcReload").addEventListener("click",loadStationQC);
 $("stationQcPeriod").addEventListener("change",loadStationQC);
 document.addEventListener("click",e=>{const row=e.target.closest("#rows tr");if(!row||e.target.closest("button"))return;const idx=Array.from($("rows").children).indexOf(row);if(idx>=0&&lastRows[idx])openStationQC(lastRows[idx]);});
 $("timeSeriesTab").addEventListener("click",()=>showView("timeseries"));
 $("summaryTab").addEventListener("click",()=>showView("summary"));
 $("siteQualityTab").addEventListener("click",()=>showView("sitequality"));
 $("qualityRefreshBtn").addEventListener("click",()=>refreshQuality().catch(e=>alert("Quality refresh failed: "+e.message)));
 $("qualityStation").addEventListener("change",updateSiteQualityControl);
 $("qualityElement").addEventListener("change",updateSiteQualityControl);
 $("qualityToggleBtn").addEventListener("click",toggleSelectedSiteQuality);
 $("tsPeriod").addEventListener("change",()=>$("tsCustom").style.display=$("tsPeriod").value==="custom"?"flex":"none");
 $("tsLoadBtn").addEventListener("click",loadTimeSeries);
 $("tsTimezone").addEventListener("change",()=>{if(tsLastSeries&&tsLastStation)renderCharts(tsLastSeries,tsLastStation);});
 $("generateSummaryBtn").addEventListener("click",generateSummary);
 $("summaryPrintBtn").addEventListener("click",()=>window.print());
 $("summaryTextBtn").addEventListener("click",()=>{
  if(!summaryText){alert("Generate a summary first.");return;}
  const url=URL.createObjectURL(new Blob([summaryText],{type:"text/plain;charset=utf-8"})),a=document.createElement("a");
  a.href=url;a.download="BTV_observation_summary_"+new Date().toISOString().slice(0,10)+".txt";a.click();URL.revokeObjectURL(url);
 });
 document.addEventListener("click",e=>{const stationButton=e.target.closest("[data-open-timeseries]");if(stationButton)openStation(stationButton.getAttribute("data-open-timeseries"));const flagButton=e.target.closest("[data-flag-observation]");if(flagButton)flagObservation(flagButton);const restoreButton=e.target.closest("[data-restore-quality]");if(restoreButton)restoreQuality(restoreButton);});
 refreshQuality().catch(e=>{$("qualityStatus").textContent="Quality service unavailable: "+e.message;});
 window.showObservationView=showView;
}
window.initObservationEnhancements=init;
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",init);else init();
})();