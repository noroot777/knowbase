#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""亮色主题交互式知识图谱(单文件 HTML,数据内嵌)。

窗口默认最近 14 天(--since 覆盖),节点命名与 wiki 实体页一致,
文章/实体页链接通过 obsidian:// 跳回 new-start vault。
输出:<vault>/graph.html(被 wiki_daily.py 每日重生成)。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wikilib as W  # noqa: E402

ENTITY_MIN_DOCS = 2
TOPIC_MIN_DOCS = 3
KIND_ZH = {"tools": "工具与产品", "models": "模型", "orgs": "公司与组织",
           "concepts": "概念", "topics": "主题"}


def build_data(since: str):
    con = W.connect_ro()
    u = W.Universe(con)
    win_docs = {p: d for p, d in u.docs.items() if d["published"][:10] >= since}
    until = max((d["published"][:10] for d in win_docs.values()), default=since)

    ent_win = {k: {p for p in u.ent_docs[k] if p in win_docs} for k in u.page_keys}
    keys = sorted((k for k, ds in ent_win.items() if len(ds) >= ENTITY_MIN_DOCS),
                  key=lambda k: -len(ent_win[k]))
    top_win = {t: {p for p in ds if p in win_docs} for t, ds in u.topic_docs.items()}
    topics = {t: ds for t, ds in top_win.items() if len(ds) >= TOPIC_MIN_DOCS}

    doc_keys = defaultdict(list)
    for k in keys:
        for p in ent_win[k]:
            doc_keys[p].append(k)

    def articles_of(paths, cap=12):
        out = []
        for p in sorted(paths, key=lambda p: win_docs[p]["published"], reverse=True)[:cap]:
            d = win_docs[p]
            out.append({"t": d["title"], "a": d["account"], "d": d["published"][:10],
                        "f": p[:-3],
                        "s": (d["summary"] or "").strip().replace("\n", " ")[:90]})
        return out

    nodes, node_id = [], {}
    for k in keys:
        sub, name = u.page_file[k]
        node_id[("e", k)] = len(nodes)
        nodes.append({
            "label": name, "cat": sub if sub != "concepts" else "tools",
            "n": len(ent_win[k]),
            "desc": W.DESCS.get(name, f"近期 {len(ent_win[k])} 篇文章提到的{KIND_ZH[sub]}。"),
            "wiki": f"wiki/{sub}/{name}",
            "articles": articles_of(ent_win[k])})
    for t, ds in sorted(topics.items(), key=lambda x: -len(x[1])):
        cnt = defaultdict(int)
        for p in ds:
            for k in doc_keys[p]:
                cnt[k] += 1
        top3 = [u.page_file[k][1] for k, _ in sorted(cnt.items(), key=lambda x: -x[1])[:3]]
        node_id[("t", t)] = len(nodes)
        nodes.append({
            "label": t, "cat": "topics", "n": len(ds),
            "desc": f"『{t}』方向,近期 {len(ds)} 篇文章。"
                    + (f"高频出现:{'、'.join(top3)}。" if top3 else ""),
            "wiki": f"wiki/topics/{u.topic_file.get(t, '')}",
            "articles": articles_of(ds)})

    edges = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            w = len(ent_win[a] & ent_win[b])
            if w >= 1:
                edges.append([node_id[("e", a)], node_id[("e", b)], w])
    for t, ds in topics.items():
        for k in keys:
            w = len(ds & ent_win[k])
            if w >= 2:
                edges.append([node_id[("t", t)], node_id[("e", k)], w])

    return {"nodes": nodes, "edges": edges,
            "meta": {"docs": len(win_docs), "since": since, "until": until,
                     "vault": W.VAULT.name,
                     "entities": len(keys), "topics": len(topics)}}


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>公众号 AI 图谱 · Connected</title>
<style>
:root{
  --bg:#f3f5f4; --card:#ffffff; --line:#dce3e0; --ink:#14202b; --muted:#66727c;
  --c-topics:#7D4E9E; --c-tools:#3D5A80; --c-models:#17604F; --c-orgs:#8C6A3F;
  --panelW:min(400px,92vw);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--ink);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;}
#edges{position:absolute;top:0;left:0;pointer-events:none;z-index:3}
header{display:flex;flex-wrap:wrap;gap:14px;align-items:baseline;
  padding:26px 36px 6px;position:relative;z-index:4;transition:margin-right .22s ease}
header h1{font-size:24px;margin:0;font-weight:800;letter-spacing:.2px}
header h1 em{font-style:normal;color:var(--c-tools)}
header .sub{color:var(--muted);font-size:13.5px;max-width:520px}
.controls{margin-left:auto;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
#q{width:220px;padding:8px 14px;border:1px solid var(--line);border-radius:20px;
  background:var(--card);font-size:14px;outline:none}
#q:focus{border-color:var(--c-tools)}
.starter{font-size:12.5px;color:var(--muted)}
.starter button{border:1px solid var(--line);background:var(--card);border-radius:16px;
  padding:5px 12px;font-size:12.5px;cursor:pointer;color:var(--ink)}
.starter button:hover{border-color:var(--c-tools);color:var(--c-tools)}
main{padding:10px 36px 120px;position:relative;z-index:2;max-width:1180px;
  transition:margin-right .22s ease}
body.panelOpen header,body.panelOpen main{margin-right:var(--panelW)}
body.panelOpen footer{right:var(--panelW)}
@media (max-width:760px){
  body.panelOpen header,body.panelOpen main{margin-right:0}
  body.panelOpen footer{right:0}
}
.band{background:var(--card);border:1px solid var(--line);border-radius:16px;
  padding:16px 20px 18px;margin:16px 0;box-shadow:0 1px 3px rgba(20,32,43,.04)}
.band h2{font-size:13px;margin:0 0 10px;letter-spacing:1.5px;font-weight:800;display:flex;gap:10px;align-items:baseline}
.band h2 .tag{font-weight:400;letter-spacing:0;color:var(--muted);font-size:12.5px}
.band h2 .dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none;align-self:center}
.pills{display:flex;flex-wrap:wrap;gap:9px}
.pill{position:relative;z-index:4;border:1.5px solid var(--line);background:#fff;border-radius:18px;
  padding:5px 14px;font-size:13.5px;cursor:pointer;transition:opacity .18s,transform .12s,border-color .18s,background .18s;
  user-select:none;white-space:nowrap}
.pill:hover{transform:translateY(-1px)}
.pill .n{color:var(--muted);font-size:11px;margin-left:5px}
body.hasSel .pill{opacity:.18}
body.hasSel .pill.lit{opacity:1}
body.hasSel .pill.sel{opacity:1}
.pill.sel{color:#fff !important;border-color:transparent !important}
.pill.sel .n{color:rgba(255,255,255,.75)}
body.searching .pill{opacity:.15}
body.searching .pill.hit{opacity:1}
.pill[data-cat=topics].lit{border-color:var(--c-topics);background:#F1ECF5}
.pill[data-cat=tools].lit{border-color:var(--c-tools);background:#ECF0F5}
.pill[data-cat=models].lit{border-color:var(--c-models);background:#DFF0E9}
.pill[data-cat=orgs].lit{border-color:var(--c-orgs);background:#F3EDE4}
.pill[data-cat=topics].sel{background:var(--c-topics)}
.pill[data-cat=tools].sel{background:var(--c-tools)}
.pill[data-cat=models].sel{background:var(--c-models)}
.pill[data-cat=orgs].sel{background:var(--c-orgs)}
#panel{position:fixed;top:0;right:0;width:var(--panelW);height:100vh;overflow-y:auto;
  background:var(--card);border-left:1px solid var(--line);box-shadow:-8px 0 30px rgba(20,32,43,.07);
  padding:26px 26px 40px;z-index:10;transform:translateX(105%);transition:transform .22s ease}
#panel.show{transform:none}
#panel .badge{display:inline-block;color:#fff;font-size:11px;letter-spacing:1.5px;font-weight:700;
  border-radius:6px;padding:3px 9px;margin-bottom:10px}
#panel h3{margin:0 0 10px;font-size:22px;font-weight:800}
#panel .desc{font-size:14px;color:#14202b}
#panel .sec{font-size:11.5px;letter-spacing:1.5px;color:var(--muted);font-weight:800;margin:20px 0 8px}
#panel .chips{display:flex;flex-wrap:wrap;gap:7px}
#panel .chips button{border:1px solid var(--line);background:#fff;border-radius:15px;padding:4px 11px;
  font-size:12.5px;cursor:pointer;color:var(--ink)}
#panel .chips button:hover{border-color:var(--c-tools);color:var(--c-tools)}
#panel .art{padding:9px 0;border-bottom:1px dashed var(--line)}
#panel .art a{color:var(--ink);text-decoration:none;font-size:13.5px;font-weight:600}
#panel .art a:hover{color:var(--c-tools)}
#panel .art .meta{color:var(--muted);font-size:12px;margin-top:2px}
#panel .art .sum{color:#66727c;font-size:12.5px;margin-top:3px}
#panel .openwiki{display:inline-block;margin-top:16px;font-size:13px;color:var(--c-tools);text-decoration:none}
#panel .close{position:absolute;top:14px;right:16px;border:none;background:none;font-size:20px;
  color:var(--muted);cursor:pointer}
footer{position:fixed;left:0;bottom:0;right:0;padding:10px 36px;font-size:12.5px;color:var(--muted);
  background:linear-gradient(transparent,var(--bg) 40%);z-index:4;pointer-events:none;
  transition:right .22s ease}
footer b{color:#66727c}
</style>
</head>
<body>
<svg id="edges"></svg>
<header>
  <div>
    <h1>公众号 AI 图谱<em> · Connected</em></h1>
    <div class="sub">最近两周的公众号文章都在聊什么。点任意节点:看它是什么、和谁有关、出自哪些文章;顺着右侧面板可以一直点下去。</div>
  </div>
  <div class="controls">
    <input id="q" type="search" placeholder="搜索节点…">
    <span class="starter">从这里开始:</span>
    <span class="starter" id="starters"></span>
  </div>
</header>
<main id="main"></main>
<div id="panel">
  <button class="close" onclick="reset()">✕</button>
  <div id="panelBody"></div>
</div>
<footer id="foot"></footer>
<script>
const DATA = __DATA__;
const CATS = [
  ["topics","主题","内容都在聊什么方向"],
  ["tools","工具与产品","实际在用、在讨论的东西"],
  ["models","模型","谁在驱动这一切"],
  ["orgs","公司与组织","背后的玩家"],
];
const CATLABEL = Object.fromEntries(CATS.map(c=>[c[0],c[1]]));
const COLOR = {topics:"#7D4E9E",tools:"#3D5A80",models:"#17604F",orgs:"#8C6A3F"};
const adj = new Map();
DATA.edges.forEach(([a,b,w])=>{
  if(!adj.has(a)) adj.set(a,[]);
  if(!adj.has(b)) adj.set(b,[]);
  adj.get(a).push({o:b,w}); adj.get(b).push({o:a,w});
});
const main = document.getElementById("main");
const pills = [];
CATS.forEach(([cat,label,tag])=>{
  const idxs = DATA.nodes.map((n,i)=>[n,i]).filter(([n])=>n.cat===cat);
  if(!idxs.length) return;
  idxs.sort((x,y)=>y[0].n-x[0].n);
  const band = document.createElement("section");
  band.className="band";
  band.innerHTML = `<h2><span class="dot" style="background:${COLOR[cat]}"></span>${label}
    <span class="tag">${tag}</span></h2><div class="pills"></div>`;
  const box = band.querySelector(".pills");
  idxs.forEach(([n,i])=>{
    const el = document.createElement("span");
    el.className="pill"; el.dataset.cat=cat; el.dataset.id=i;
    el.innerHTML = `${n.label}<span class="n">${n.n}</span>`;
    el.onclick = e=>{e.stopPropagation(); select(i);};
    box.appendChild(el); pills[i]=el;
  });
  main.appendChild(band);
});
const starters = ["Claude Code","GLM-5.2","ai编程"];
const stBox = document.getElementById("starters");
starters.forEach(s=>{
  const i = DATA.nodes.findIndex(n=>n.label===s);
  if(i<0) return;
  const b=document.createElement("button"); b.textContent=s;
  b.onclick=e=>{e.stopPropagation(); select(i);};
  stBox.appendChild(b);
});
document.getElementById("foot").innerHTML =
  `<b>${DATA.meta.entities}</b> 个实体 · <b>${DATA.meta.topics}</b> 个主题 · <b>${DATA.edges.length}</b> 条关联 · `+
  `来自 <b>${DATA.meta.docs}</b> 篇公众号文章(${DATA.meta.since} ~ ${DATA.meta.until})`+
  ` &nbsp;·&nbsp; Esc 或点击空白处复位`;

const svg = document.getElementById("edges");
let selected = -1;
function center(el){
  const r = el.getBoundingClientRect();
  return [r.left+r.width/2+scrollX, r.top+r.height/2+scrollY];
}
function drawEdges(i){
  svg.innerHTML="";
  svg.setAttribute("width",document.documentElement.scrollWidth);
  svg.setAttribute("height",document.documentElement.scrollHeight);
  const [x1,y1]=center(pills[i]);
  (adj.get(i)||[]).forEach(({o,w})=>{
    const [x2,y2]=center(pills[o]);
    const dy=(y2-y1)*0.45, dx=(x2-x1)*0.12;
    const p=document.createElementNS("http://www.w3.org/2000/svg","path");
    p.setAttribute("d",`M${x1},${y1} C${x1+dx},${y1+dy} ${x2-dx},${y2-dy} ${x2},${y2}`);
    p.setAttribute("fill","none");
    p.setAttribute("stroke",COLOR[DATA.nodes[o].cat]);
    p.setAttribute("stroke-width",Math.min(1+w*0.55,4.5));
    p.setAttribute("stroke-opacity",.5);
    p.setAttribute("stroke-linecap","round");
    svg.appendChild(p);
  });
}
// 面板挤压布局的过渡期间,pill 位置逐帧变化,连线要跟着重画
function trackEdges(dur=320){
  const t0=performance.now();
  (function tick(t){
    if(selected>=0)drawEdges(selected);
    if(t-t0<dur)requestAnimationFrame(tick);
  })(t0);
}
function select(i){
  selected=i;
  document.body.classList.add("hasSel");
  document.body.classList.remove("searching");
  document.getElementById("q").value="";
  pills.forEach(p=>p.classList.remove("sel","lit"));
  pills[i].classList.add("sel");
  (adj.get(i)||[]).forEach(({o})=>pills[o].classList.add("lit"));
  panelFor(i);
  trackEdges();
  pills[i].scrollIntoView({block:"nearest",behavior:"smooth"});
}
function panelFor(i){
  const n = DATA.nodes[i];
  const rel = (adj.get(i)||[]).slice().sort((a,b)=>b.w-a.w);
  const groups = {};
  rel.forEach(({o,w})=>{
    const c = DATA.nodes[o].cat;
    (groups[c]=groups[c]||[]).push({o,w});
  });
  let h = `<span class="badge" style="background:${COLOR[n.cat]}">${CATLABEL[n.cat]}</span>`;
  h += `<h3>${n.label}</h3><div class="desc">${n.desc}</div>`;
  h += `<div class="sec">关联(${rel.length})</div>`;
  for(const [cat] of CATS){
    if(!groups[cat]) continue;
    h += `<div class="sec" style="margin-top:10px;color:${COLOR[cat]}">${CATLABEL[cat]}</div><div class="chips">`;
    h += groups[cat].slice(0,14).map(({o,w})=>
      `<button onclick="event.stopPropagation();select(${o})" title="共同出现 ${w} 篇">${DATA.nodes[o].label}<span style="color:#66727c"> ${w}</span></button>`).join("");
    h += `</div>`;
  }
  h += `<div class="sec">出自这些文章</div>`;
  h += n.articles.map(a=>{
    const uri = `obsidian://open?vault=${encodeURIComponent(DATA.meta.vault)}&file=${encodeURIComponent(a.f)}`;
    return `<div class="art"><a href="${uri}">${a.t}</a><div class="meta">${a.a} · ${a.d}</div>`+
      (a.s?`<div class="sum">${a.s}…</div>`:"")+`</div>`;
  }).join("");
  const wikiUri = `obsidian://open?vault=${encodeURIComponent(DATA.meta.vault)}&file=${encodeURIComponent(n.wiki)}`;
  h += `<a class="openwiki" href="${wikiUri}">在 Obsidian 中打开「${n.label}」wiki 页 →</a>`;
  document.getElementById("panelBody").innerHTML=h;
  document.getElementById("panel").classList.add("show");
  document.body.classList.add("panelOpen");
}
function reset(){
  selected=-1;
  document.body.classList.remove("hasSel","searching","panelOpen");
  pills.forEach(p=>p.classList.remove("sel","lit","hit"));
  svg.innerHTML="";
  document.getElementById("panel").classList.remove("show");
}
document.body.addEventListener("click",e=>{
  if(e.target.closest("#panel")||e.target.closest(".pill")||e.target.closest("header"))return;
  reset();
});
addEventListener("keydown",e=>{if(e.key==="Escape")reset();});
addEventListener("resize",()=>{if(selected>=0)drawEdges(selected);});
const q=document.getElementById("q");
q.addEventListener("input",()=>{
  const v=q.value.trim().toLowerCase();
  pills.forEach(p=>p.classList.remove("hit"));
  if(!v){document.body.classList.remove("searching");return;}
  reset(); document.body.classList.add("searching");
  DATA.nodes.forEach((n,i)=>{
    if(n.label.toLowerCase().includes(v))pills[i].classList.add("hit");
  });
});
q.addEventListener("keydown",e=>{
  if(e.key!=="Enter")return;
  const v=q.value.trim().toLowerCase(); if(!v)return;
  const i=DATA.nodes.findIndex(n=>n.label.toLowerCase().includes(v));
  if(i>=0)select(i);
});
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    # 纯滚动窗口:近 14 天。历史文章已全量提取,无需再钳制在 EPOCH
    default_since = (date.today() - timedelta(days=14)).isoformat()
    ap.add_argument("--since", default=default_since)
    ap.add_argument("--out", default=str(W.VAULT / "graph.html"))
    a = ap.parse_args()
    data = build_data(a.since)
    Path(a.out).write_text(
        TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)),
        encoding="utf-8")
    print(f"nodes={len(data['nodes'])} edges={len(data['edges'])} -> {a.out}")


if __name__ == "__main__":
    main()
