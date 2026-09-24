"""Bounded JSONL agent-eye traces and an offline, dependency-free replay viewer."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from pydantic import model_validator
from .contracts import AgentView, EyeModel, Ref

MAX_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 2048


class Record(EyeModel):
    record_version: Literal["agent-eye-trace/1"] = "agent-eye-trace/1"
    is_mock: bool
    view: AgentView
    selected_action: Ref | None = None

    @model_validator(mode="after")
    def selected_from_candidates(self):
        if self.selected_action is not None and self.selected_action not in {a.id for a in self.view.current.actions}:
            raise ValueError("recorded action is not a candidate in this view")
        return self


def write_trace(records: list[Record], path: str | Path) -> None:
    if not records or len(records) > MAX_RECORDS:
        raise ValueError("trace must contain 1..2048 records")
    payload = "".join(r.model_dump_json() + "\n" for r in records)
    if len(payload.encode("utf-8")) > MAX_BYTES:
        raise ValueError("trace exceeds 32 MiB")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        f.write(payload)


def read_trace(path: str | Path) -> list[Record]:
    with Path(path).open("rb") as f:
        raw = f.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("trace exceeds 32 MiB")
    lines = raw.decode("utf-8").splitlines()
    if not 1 <= len(lines) <= MAX_RECORDS or any(not line.strip() for line in lines):
        raise ValueError("trace must contain 1..2048 nonempty records")
    records = [Record.model_validate_json(line) for line in lines]
    previous = None
    decisions = set()
    for record in records:
        frame = record.view.current
        if previous and (frame.episode_id != previous.episode_id or frame.turn < previous.turn):
            raise ValueError("one trace represents one forward episode branch")
        if frame.decision_id in decisions:
            raise ValueError("duplicate decision in trace")
        decisions.add(frame.decision_id)
        previous = frame
    return records


def render_html(records: list[Record], output: str | Path) -> None:
    # Prevent </script>, HTML and Unicode line-separator breakout. All UI strings
    # use textContent; game messages/descriptions are untrusted text, not markup.
    data = json.dumps([r.model_dump(mode="json") for r in records], ensure_ascii=True, allow_nan=False)
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    if len(data.encode()) > MAX_BYTES * 2:
        raise ValueError("viewer data too large")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as f:
        f.write(_HTML.replace("__TRACE_DATA__", data))


_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'">
<title>Gyms of Qud · Agent-eye replay</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#10151c;color:#e1e8f0;font:15px system-ui,sans-serif}
header{padding:24px 28px;border-bottom:1px solid #344154}h1{font-size:25px;margin:0 0 8px}h2{font-size:17px;color:#bfccdc}
main{max-width:1550px;margin:auto;padding:20px;display:grid;grid-template-columns:minmax(380px,1.4fr) minmax(340px,1fr);gap:18px}
section{background:#17202b;border:1px solid #344154;border-radius:8px;padding:16px;min-width:0}.wide{grid-column:1/-1}
.controls{display:flex;flex-wrap:wrap;align-items:center;gap:10px}button,select{padding:8px 12px;background:#25374b;color:inherit;border:1px solid #698096;border-radius:4px}
button:focus,input:focus,select:focus{outline:2px solid #80ceff}input[type=range]{flex:1;min-width:180px}canvas{width:100%;background:#0e131a;border:1px solid #465363;margin:12px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px ui-monospace,monospace;max-height:450px;overflow:auto}
.entry{white-space:pre-wrap;border-top:1px solid #344154;padding:10px 0;overflow-wrap:anywhere}.pill{border:1px solid #56687c;border-radius:4px;padding:2px 5px;margin-right:8px;font-size:11px}
.observed{color:#a7f2d7}.remembered{color:#eabf72}.inferred{color:#cdb7ff}.unknown{color:#acb6c4}.muted{color:#adbacb;font-size:13px}.selected{background:#253f50;padding:8px}
.scroll{max-height:450px;overflow:auto}details{margin:10px 0}summary{cursor:pointer}#where{min-height:24px}
@media(max-width:850px){main{grid-template-columns:1fr}.wide{grid-column:auto}}
</style></head><body>
<header><h1>Gyms of Qud · Agent-eye replay</h1><div id="origin"></div><p class="muted">Only the recorded policy view. No engine state, seed, hidden identities, or oracle overlay.</p></header>
<main><section class="wide controls"><button id="prev">Previous</button><button id="play">Play</button><button id="next">Next</button><input id="seek" aria-label="Decision index" type="range" min="0" value="0"><strong id="cursor"></strong></section>
<section><h2>Perception and remembered locations</h2><div class="controls"><select id="zones" aria-label="Zone"></select><label><input type="checkbox" id="memory" checked> Show memory</label></div>
<canvas id="map" width="900" height="500" aria-label="Agent-eye map"></canvas><div id="where" class="muted">Point to a cell to inspect its evidence.</div>
<p><span class="observed">● Current</span> &nbsp; <span class="remembered">□ Remembered</span> &nbsp; <span class="unknown">? Undisclosed</span></p><div id="messages"></div></section>
<section><h2>Current body, contacts and objects</h2><div id="entities" class="scroll"></div></section>
<section><h2>Candidate actions</h2><div id="actions" class="scroll"></div></section>
<section><h2>Memory and hypotheses</h2><p id="forgotten" class="muted"></p><div id="beliefs"></div><div id="remembered" class="scroll"></div></section>
<section class="wide"><details><summary>Complete recorded policy view (including relations and provenance)</summary><pre id="raw"></pre></details></section>
</main><script id="trace" type="application/json">__TRACE_DATA__</script>
<script>
'use strict';
(() => {
const rows=JSON.parse(document.getElementById('trace').textContent);let index=0,timer=null,layout=null;
const el=id=>document.getElementById(id), clear=id=>el(id).replaceChildren();
function entry(id,text,cls=''){const d=document.createElement('div');d.className='entry '+cls;d.textContent=text;el(id).appendChild(d);}
function ev(e){return e.status+' via '+e.channel+' · turn '+e.turn;}
function value(f){return f.value===null?'UNKNOWN':String(f.value)+(f.unit?' '+f.unit:'');}
function location(p){return p.zone+' ('+p.x+','+p.y+')';}
function draw(){const r=rows[index],v=r.view,f=v.current;el('seek').value=index;
el('origin').textContent=r.is_mock?'SYNTHETIC FIXTURE — not a Qud playthrough':'Recorded agent observation';
el('cursor').textContent='Decision '+index+' / '+(rows.length-1)+' · turn '+f.turn+' · '+f.phase;
el('prev').disabled=index===0;el('next').disabled=index===rows.length-1;
const old=el('zones').value;clear('zones');for(const z of f.zones){const o=document.createElement('option');o.value=z.id;o.textContent=z.id;el('zones').appendChild(o);}if(f.zones.some(z=>z.id===old))el('zones').value=old;
for(const id of ['entities','messages','actions','beliefs','remembered'])clear(id);
if(f.prompt)entry('messages',f.prompt.kind+': '+f.prompt.text,'selected');
for(const e of f.events)entry('messages',e.text+' ['+ev(e.evidence)+']');
for(const e of f.entities){let lines=[e.id+' · '+e.kind+(e.id===f.controlled_actor?' · CONTROLLED BODY':'')];if(e.location)lines.push(location(e.location)+' ['+ev(e.location.evidence)+']');for(const p of e.facts)lines.push(p.attribute+': '+value(p)+' ['+ev(p.evidence)+']');entry('entities',lines.join('\n'),'observed');}
for(const a of f.actions)entry('actions',(a.id===r.selected_action?'SELECTED → ':'')+a.id+' · '+a.label+'\n'+JSON.stringify(a),a.id===r.selected_action?'selected':'');
el('forgotten').textContent=v.forgotten_records+' facts and '+v.forgotten_events+' events evicted by the memory budget; absence is not world-state deletion.';
for(const h of v.hypotheses)entry('beliefs','INFERENCE, NOT FACT · '+h.text+' · model '+h.model+' · evidence '+h.based_on_decisions.join(', '),'inferred');
for(const m of v.remembered){const p=m.fact;entry('remembered',m.subject+' · '+m.attribute+': '+('value' in p?value(p):location(p))+'\nLast evidence '+m.last_turn+' ('+(f.turn-m.last_turn)+' turns ago), decision '+m.last_decision+' · '+ev(p.evidence),'remembered');}
for(const m of v.remembered_events)entry('remembered','PAST EVENT · '+m.event.text+' ['+ev(m.event.evidence)+']','remembered');
el('raw').textContent=JSON.stringify(v,null,2);drawMap();}
function drawMap(){const v=rows[index].view,f=v.current,z=f.zones.find(z=>z.id===el('zones').value),c=el('map'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);layout=null;if(!z)return;
const s=Math.min(c.width/z.width,c.height/z.height),ox=(c.width-z.width*s)/2,oy=(c.height-z.height*s)/2;layout={z,s,ox,oy};
function tile(x,y,txt,color){ctx.fillStyle=color;ctx.fillRect(ox+x*s+1,oy+y*s+1,Math.max(1,s-2),Math.max(1,s-2));if(s>=12){ctx.fillStyle='#dce5ef';ctx.font=Math.min(22,s*.55)+'px monospace';ctx.textAlign='center';ctx.fillText(txt,ox+(x+.5)*s,oy+(y+.68)*s);}}
if(el('memory').checked)for(const m of v.remembered){const prefix='cell:'+z.id+':';if(m.subject.startsWith(prefix)){const xy=m.subject.slice(prefix.length).split(':').map(Number);if(xy.length===2&&xy.every(Number.isFinite)&&xy[0]<z.width&&xy[1]<z.height)tile(xy[0],xy[1],String(m.fact.value??'?').slice(0,1),'#54432c');}}
for(const cell of z.cells){const p=cell.layers[0].fact;tile(cell.x,cell.y,String(p.value??'?').slice(0,1),'#2b4b48');}
if(el('memory').checked)for(const m of v.remembered){const p=m.fact;if(m.attribute==='location'&&p.zone===z.id){ctx.strokeStyle='#eabf72';ctx.lineWidth=3;ctx.strokeRect(ox+p.x*s+5,oy+p.y*s+5,Math.max(1,s-10),Math.max(1,s-10));}}
for(const e of f.entities){const p=e.location;if(!p||p.zone!==z.id)continue;ctx.beginPath();ctx.arc(ox+(p.x+.5)*s,oy+(p.y+.5)*s,Math.max(2,s*.28),0,Math.PI*2);ctx.fillStyle=e.id===f.controlled_actor?'#f0f5fa':'#80d7bc';ctx.fill();if(s>=16){ctx.fillStyle='#0b1720';ctx.font=Math.min(15,s*.35)+'px monospace';ctx.textAlign='center';ctx.fillText(e.id,ox+(p.x+.5)*s,oy+(p.y+.58)*s);}}}
el('map').addEventListener('mousemove',e=>{if(!layout)return;const rect=el('map').getBoundingClientRect(),{z,s,ox,oy}=layout;const x=Math.floor(((e.clientX-rect.left)*900/rect.width-ox)/s),y=Math.floor(((e.clientY-rect.top)*500/rect.height-oy)/s);const cell=z.cells.find(c=>c.x===x&&c.y===y);el('where').textContent=location({zone:z.id,x,y})+': '+(cell?cell.layers.map(l=>l.id+'='+value(l.fact)+' ['+ev(l.fact.evidence)+']').join('; '):'not currently disclosed');});
el('seek').max=Math.max(0,rows.length-1);el('seek').oninput=()=>{index=Number(el('seek').value);draw();};el('prev').onclick=()=>{index=Math.max(0,index-1);draw();};el('next').onclick=()=>{index=Math.min(rows.length-1,index+1);draw();};
el('zones').onchange=drawMap;el('memory').onchange=drawMap;el('play').onclick=()=>{if(timer){clearInterval(timer);timer=null;el('play').textContent='Play';}else{el('play').textContent='Pause';timer=setInterval(()=>{if(index>=rows.length-1){clearInterval(timer);timer=null;el('play').textContent='Play';}else{index++;draw();}},750);}};
if(rows.length)draw();
})();
</script></body></html>'''
