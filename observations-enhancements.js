(function(){
"use strict";
let charts=[];
let summaryText="";
const labels={temperature:"Temperature",dewpoint:"Dewpoint",wind_speed:"Wind speed",wind_gust:"Wind gust",wind_chill:"Wind chill",heat_index:"Heat index",precipitation:"Precipitation",snowfall:"Snowfall",snow_depth:"Snow depth",ice_accretion:"Ice accretion"};
const colors={temperature:"#c65b42",dewpoint:"#3d83a8",wind_speed:"#476b52",wind_gust:"#b08a42",wind_chill:"#4575b4",heat_index:"#d66a36",precipitation:"#3979ad",snowfall:"#6e9fc1",snow_depth:"#7e8dba",ice_accretion:"#8c6caa"};
const units={temperature:"°F",dewpoint:"°F",wind_speed:"mph",wind_gust:"mph",wind_chill:"°F",heat_index:"°F",precipitation:"in",snowfall:"in",snow_depth:"in",ice_accretion:"in"};
const $=id=>document.getElementById(id);
const safe=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function fmt(v){return Number(v).toLocaleString("en-US",{maximumFractionDigits:3});}
function time(v){if(!v)return "—";const d=new Date(v);return Number.isNaN(d.getTime())?"—":d.toLocaleString("en-US",{timeZone:"UTC",month:"short",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false})+"Z";}
function api(){return "https://btv-observation-api.jusmessinwityah1.workers.dev";}
async function get(path){const r=await fetch(api()+path,{cache:"no-store"});if(!r.ok)throw new Error("API "+r.status+" for "+path);return r.json();}
function populateStations(){
 const s=$("tsStation");if(!s)return;const prior=s.value;s.innerHTML='<option value="">Select a station…</option>';
 stations.slice().sort((a,b)=>(a.name||"").localeCompare(b.name||"")).forEach(x=>s.add(new Option((x.name||x.provider_station_id)+" · "+x.provider_station_id,x.station_id)));
 if(Array.from(s.options).some(o=>o.value===prior))s.value=prior;
}
function rangeFromControls(){
 if($("tsPeriod").value==="custom"){
  const a=new Date($("tsStart").value),b=new Date($("tsEnd").value);
  if(!Number.isFinite(a.getTime())||!Number.isFinite(b.getTime())||b<=a)throw new Error("Choose a valid custom start and end time.");
  return {start:a.toISOString(),end:b.toISOString()};
 }
 const end=Date.now(),hours=Number($("tsPeriod").value);return {start:new Date(end-hours*3600000).toISOString(),end:new Date(end).toISOString()};
}
async function fetchRows(element,range,stationId,extra){
 const out=[];let offset=0,total=null;
 do{
  const p=new URLSearchParams({element:element,start:range.start,end:range.end,limit:"1000",offset:String(offset),sort:"time_asc"});
  if(stationId)p.set("station_id",stationId);
  Object.entries(extra||{}).forEach(([k,v])=>{if(v!==undefined&&v!==null&&v!=="")p.set(k,String(v));});
  const d=await get("/observations?"+p.toString());total=d.total||0;out.push(...(d.results||[]));offset+=1000;
  if(!(d.results||[]).length)break;
 }while(offset<total);
 return out;
}
function clearCharts(){charts.forEach(c=>c.destroy());charts=[];}
function renderCharts(series,station){
 clearCharts();const root=$("tsCharts"),cards=[];
 Object.entries(series).forEach(([el,rows])=>{
  if(!rows.length)return;
  rows.sort((a,b)=>Date.parse(a.observation_time)-Date.parse(b.observation_time));
  const sets=[],chartLabels=rows.map(r=>new Date(r.observation_time).toLocaleString("en-US",{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:false}));
  const mk=(name,key,color)=>({label:name,data:rows.map(r=>r[key]===null||r[key]===undefined||!Number.isFinite(Number(r[key]))?null:Number(r[key])),borderColor:color,backgroundColor:color,borderWidth:2,pointRadius:2,pointHoverRadius:5,tension:0,spanGaps:false});
  if(el==="ice_accretion"){
   sets.push(mk("Flat ice","value",colors[el]));
   if(rows.some(r=>Number.isFinite(Number(r.radial_value))))sets.push(mk("Radial ice (40%)","radial_value","#4b91a2"));
  }else sets.push(mk(labels[el]||el,"value",colors[el]||"#476b52"));
  cards.push({id:"observationTsChart"+cards.length,title:labels[el]||el,sets,rows,unit:units[el]||rows[0].unit||""});
 });
 if(!cards.length){root.innerHTML='<div class="summary-empty" style="grid-column:1/-1">No observations were found for the selected station, variables and timeframe. Try a longer period or different variables.</div>';return;}
 root.innerHTML=cards.map(c=>'<div class="chart-card"><h3>'+safe(c.title)+'</h3><div class="chart-canvas"><canvas id="'+c.id+'"></canvas></div><div class="chart-caption">'+c.rows.length+' reports · '+safe(c.unit)+' · original report times</div></div>').join("");
 cards.forEach(c=>{
  const chart=new Chart($(c.id),{type:"line",data:{labels:c.labels,datasets:c.sets},options:{responsive:true,maintainAspectRatio:false,parsing:{xAxisKey:"x",yAxisKey:"y"},interaction:{mode:"nearest",intersect:false},plugins:{legend:{display:c.sets.length>1,labels:{boxWidth:10,font:{size:10}}},tooltip:{callbacks:{label:ctx=>{const r=c.rows[ctx.dataIndex];const period=r&&r.accumulation_hours?" · "+r.accumulation_hours+"h report":"";return ctx.dataset.label+": "+fmt(ctx.parsed.y)+" "+c.unit+period;}}}},scales:{x:{type:"category",ticks:{maxTicksLimit:7,maxRotation:0,font:{size:9}},grid:{display:false}},y:{title:{display:true,text:c.unit,font:{size:10}},ticks:{font:{size:9}},grid:{color:"#ece9e1"}}}}});
  charts.push(chart);
 });
 $("tsStatus").textContent=cards.length+" variables plotted · "+(station.name||station.provider_station_id);
}
async function loadTimeSeries(){
 const id=$("tsStation").value;if(!id){$("tsStatus").textContent="Select a station first";return;}
 const vars=Array.from($("tsVariables").querySelectorAll("input:checked")).map(x=>x.value);
 if(!vars.length){$("tsStatus").textContent="Select at least one variable";return;}
 const btn=$("tsLoadBtn");btn.disabled=true;btn.textContent="Loading…";$("tsStatus").textContent="Retrieving station history…";
 try{
  const range=rangeFromControls(),hours=Number($("tsPeriod").value),extra={};
  if($("tsPeriod").value!=="custom"&&["1","3","6","12","24","48","72","120"].includes(String(hours))){extra.period=hours;}
  const result=await Promise.all(vars.map(async el=>{
   const opts={};
   if(el==="precipitation"&&extra.period){opts.statistic=hours+"h_accumulation";opts.accumulation_hours=hours;}
   return [el,await fetchRows(el,range,id,opts)];
  }));
  const station=stations.find(x=>x.station_id===id)||{name:id};renderCharts(Object.fromEntries(result),station);
 }catch(e){$("tsStatus").textContent="Time series failed: "+e.message;$("tsCharts").innerHTML='<div class="summary-empty" style="grid-column:1/-1">'+safe(e.message)+'</div>';}
 finally{btn.disabled=false;btn.textContent="Load time series";}
}
function showView(view){
 const panels={table:"tablePanel",map:"mapPanel",timeseries:"timeSeriesPanel",summary:"summaryPanel"};
 const tabs={table:"tableTab",map:"mapTab",timeseries:"timeSeriesTab",summary:"summaryTab"};
 Object.entries(panels).forEach(([k,id])=>$(id).style.display=k===view?"":"none");
 Object.entries(tabs).forEach(([k,id])=>$(id).classList.toggle("active",k===view));
 if(view==="map"){ensureObservationMap();setTimeout(()=>{observationMap.invalidateSize();if(mapDirty){updateObservationMap();mapDirty=false;}},100);}
 if(view==="timeseries"&&!$("tsStation").value&&$("station").value)$("tsStation").value=$("station").value;
 if(view==="summary"&&!summaryText)generateSummary();
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
 const btn=$("generateSummaryBtn"),hours=Number($("summaryPeriod").value),end=Date.now(),range={start:new Date(end-hours*3600000).toISOString(),end:new Date(end).toISOString()},stat=hours+"h_accumulation";
 btn.disabled=true;btn.textContent="Analyzing…";$("summaryStatus").textContent="Reviewing observations across the BTV CWA…";
 $("summaryResults").innerHTML='<div class="summary-empty">Retrieving precipitation, snow, ice, apparent temperature and wind observations…</div>';
 try{
  const specs=[["precipitation",{statistic:stat,accumulation_hours:hours}],["snowfall",{}],["ice_accretion",{statistic:stat,accumulation_hours:hours}],["heat_index",{}],["wind_chill",{}],["wind_gust",{}],["snow_depth",{}],["temperature",{}]];
  const pairs=await Promise.all(specs.map(async ([el,extra])=>[el,await fetchRows(el,range,null,extra)]));
  const d=Object.fromEntries(pairs);
  const snow=(d.snowfall||[]).filter(r=>Number(r.accumulation_hours)===hours||(hours===24&&r.statistic==="daily_report"));
  const rows=[
   ["Highest precipitation · "+hours+"h",extreme(d.precipitation||[],"max"),"in",false],
   ["Greatest snowfall · "+hours+"h",extreme(snow,"max"),"in",false],
   ["Greatest ice accretion · "+hours+"h",extreme(d.ice_accretion||[],"max"),"in",true],
   ["Maximum heat index",extreme(d.heat_index||[],"max"),"°F",false],
   ["Lowest wind chill",extreme(d.wind_chill||[],"min"),"°F",false],
   ["Strongest wind gust",extreme(d.wind_gust||[],"max"),"mph",false],
   ["Greatest snow depth",extreme(d.snow_depth||[],"max"),"in",false],
   ["Highest temperature",extreme(d.temperature||[],"max"),"°F",false],
   ["Lowest temperature",extreme(d.temperature||[],"min"),"°F",false]
  ];
  const html=rows.map(x=>card(x[0],x[1],x[2],x[3])).filter(Boolean);
  const generated=new Date().toLocaleString("en-US",{timeZone:"UTC",dateStyle:"medium",timeStyle:"short"})+" UTC";
  const intro="Analysis window: "+time(range.start)+" – "+time(range.end)+" UTC · Generated "+generated+". Precipitation and ice use the selected accumulation period; snowfall is included only when its report duration matches. Heat index and wind chill appear only when valid derived observations exist.";
  $("summaryResults").innerHTML='<div class="summary-intro">'+safe(intro)+'</div>'+(html.length?'<div class="summary-grid">'+html.join("")+'</div>':'<div class="summary-empty">No qualifying observations were found. Missing categories are omitted rather than reported as zero.</div>');
  $("summaryStatus").textContent=html.length+" significant categories found";
  summaryText="BTV OPERATIONAL OBSERVATION SUMMARY\n"+intro+"\n\n"+rows.filter(x=>x[1]).map(x=>{const r=x[1],s=stations.find(y=>y.station_id===r.station_id);return x[0]+": "+fmt(r.value)+" "+x[2]+" at "+(r.station_name||(s&&s.name)||r.provider_station_id)+" · "+time(r.observation_time)+" UTC · "+(r.source||"")+" · "+(r.accumulation_hours||"")+"h"}).join("\n");
 }catch(e){$("summaryStatus").textContent="Summary failed: "+e.message;$("summaryResults").innerHTML='<div class="summary-empty">'+safe(e.message)+'</div>';}
 finally{btn.disabled=false;btn.textContent="Generate summary";}
}
let initialized=false;
function init(){
 populateStations();
 if(initialized)return;
 initialized=true;
 $("timeSeriesTab").addEventListener("click",()=>showView("timeseries"));
 $("summaryTab").addEventListener("click",()=>showView("summary"));
 $("tsPeriod").addEventListener("change",()=>$("tsCustom").style.display=$("tsPeriod").value==="custom"?"flex":"none");
 $("tsLoadBtn").addEventListener("click",loadTimeSeries);
 $("generateSummaryBtn").addEventListener("click",generateSummary);
 $("summaryPrintBtn").addEventListener("click",()=>window.print());
 $("summaryTextBtn").addEventListener("click",()=>{
  if(!summaryText){alert("Generate a summary first.");return;}
  const url=URL.createObjectURL(new Blob([summaryText],{type:"text/plain;charset=utf-8"})),a=document.createElement("a");
  a.href=url;a.download="BTV_observation_summary_"+new Date().toISOString().slice(0,10)+".txt";a.click();URL.revokeObjectURL(url);
 });
 document.addEventListener("click",e=>{const b=e.target.closest("[data-open-timeseries]");if(b)openStation(b.getAttribute("data-open-timeseries"));});
 window.showObservationView=showView;
}
window.initObservationEnhancements=init;
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",init);else init();
})();