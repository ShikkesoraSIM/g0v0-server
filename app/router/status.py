"""Public server status API — no authentication required.

Collects metrics every 15 s via a background task, stores 24 h of history
in Redis so data survives restarts.  The /status page polls /api/status
and /api/status/history every 10 s and renders charts client-side.
"""

from __future__ import annotations

import asyncio
import json
import logging
import resource
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlmodel import func, select, text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import Beatmapset, User, UserStatistics
from app.database.score import Score
from app.dependencies.database import engine, get_redis

router = APIRouter(tags=["Status"])
logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────
_COLLECT_INTERVAL = 15          # seconds between background samples
_HISTORY_KEY = "status:history" # Redis list
_MAX_SAMPLES = 5760             # 24 h @ 15 s
_BOOT_MONO = time.monotonic()
_BOOT_UTC = datetime.now(timezone.utc)
_collector_task: asyncio.Task | None = None


# ── background collector ─────────────────────────────────────────────────

async def start_collector() -> None:
    global _collector_task
    if _collector_task is not None:
        return
    _collector_task = asyncio.create_task(_collector_loop())
    logger.info("Status collector started (every %ds)", _COLLECT_INTERVAL)


async def _collector_loop() -> None:
    while True:
        try:
            sample = await _collect_sample()
            redis = get_redis()
            await redis.rpush(_HISTORY_KEY, json.dumps(sample))
            await redis.ltrim(_HISTORY_KEY, -_MAX_SAMPLES, -1)
        except Exception:
            logger.exception("Status collector error")
        await asyncio.sleep(_COLLECT_INTERVAL)


async def _collect_sample() -> dict:
    redis = get_redis()

    # kick off non-DB work in parallel
    online_task = asyncio.create_task(_count_online(redis))
    perf_task = asyncio.create_task(_check_service(
        "Performance Server",
        ["http://performance-server:8080/health", "http://performance-server:8080/"],
    ))
    spec_task = asyncio.create_task(_check_service(
        "Spectator Server",
        ["http://osu-server-spectator:8006/"],
    ))

    # sequential DB queries (one session)
    async with AsyncSession(engine) as session:
        total_users = (await session.exec(select(func.count()).select_from(User))).one()
        total_scores = (await session.exec(select(func.count()).select_from(Score))).one()
        total_plays = int((await session.exec(
            select(func.sum(UserStatistics.play_count))
        )).one() or 0)
        total_beatmapsets = (await session.exec(
            select(func.count()).select_from(Beatmapset)
        )).one()
        total_pp = float((await session.exec(
            select(func.sum(UserStatistics.pp))
        )).one() or 0)

        # recent scores in last 60 s
        now_utc = datetime.now(timezone.utc)
        one_min_ago = now_utc.replace(tzinfo=None) - timedelta(seconds=60)
        recent_scores = (await session.exec(
            select(func.count()).select_from(Score).where(
                Score.ended_at >= one_min_ago
            )
        )).one()

        # DB processlist / threads
        try:
            row = (await session.exec(text(
                "SHOW STATUS LIKE 'Threads_connected'"
            ))).first()
            db_threads = int(row[1]) if row else 0
        except Exception:
            db_threads = 0

    online = await online_task
    perf = await perf_task
    spec = await spec_task

    # python process memory (RSS)
    try:
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        rss_mb = 0

    # DB connection pool
    pool = engine.pool
    pool_active = pool.checkedout()
    pool_idle = pool.checkedin()
    pool_overflow = pool.overflow()

    # Redis memory
    try:
        info = await redis.info("memory")
        redis_mb = round(info.get("used_memory", 0) / 1048576, 1)
    except Exception:
        redis_mb = 0

    return {
        "t": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "online": online,
        "users": max(0, total_users - 1),
        "scores": total_scores,
        "plays": total_plays,
        "beatmapsets": total_beatmapsets,
        "pp": round(float(total_pp), 1),
        "scores_1m": recent_scores,
        "services": {
            "api": {"s": "online", "ms": 0},
            "perf": {"s": perf["status"], "ms": perf["latency_ms"]},
            "spec": {"s": spec["status"], "ms": spec["latency_ms"]},
        },
        "rss_mb": round(rss_mb, 1),
        "db_threads": db_threads,
        "db_pool_active": pool_active,
        "db_pool_idle": pool_idle,
        "db_pool_overflow": pool_overflow,
        "redis_mb": redis_mb,
        "uptime": int(time.monotonic() - _BOOT_MONO),
    }


# ── helpers ──────────────────────────────────────────────────────────────

async def _count_online(redis) -> int:
    try:
        if await redis.exists("metadata:online_users_set"):
            return int(await redis.scard("metadata:online_users_set"))
    except Exception:
        pass
    try:
        cursor, count, iters = 0, 0, 0
        while True:
            cursor, keys = await redis.scan(cursor, match="metadata:online:*", count=1000)
            count += len(keys)
            iters += 1
            if cursor == 0 or iters >= 500:
                break
        return count
    except Exception:
        return 0


async def _check_service(name: str, urls: list[str], timeout: float = 2.0) -> dict:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for url in urls:
                try:
                    r = await client.get(url)
                    if r.status_code < 500:
                        return {"name": name, "status": "online",
                                "latency_ms": round((time.monotonic() - t0) * 1000)}
                except Exception:
                    continue
    except Exception:
        pass
    return {"name": name, "status": "offline", "latency_ms": None}


# ── JSON endpoints ──────────────────────────────────────────────────────

@router.get("/api/status", include_in_schema=True)
async def get_status():
    """Latest status snapshot (also triggers a collection if history is empty)."""
    redis = get_redis()
    raw = await redis.lrange(_HISTORY_KEY, -1, -1)
    if raw:
        return json.loads(raw[0])
    # No history yet — collect one now
    sample = await _collect_sample()
    await redis.rpush(_HISTORY_KEY, json.dumps(sample))
    return sample


@router.get("/api/status/history", include_in_schema=True)
async def get_status_history():
    """All stored samples (up to 24 h)."""
    redis = get_redis()
    raw = await redis.lrange(_HISTORY_KEY, 0, -1)
    return [json.loads(r) for r in raw]


# ── HTML page ────────────────────────────────────────────────────────────

@router.get("/status", response_class=HTMLResponse, include_in_schema=False)
async def status_page():
    return _STATUS_HTML


_STATUS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Torii Server Status</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0d0d14;--card:#151520;--card2:#1a1a28;--border:#252535;
  --text:#d8d8e8;--muted:#7878a0;--dim:#4a4a6a;--accent:#a78bfa;
  --green:#22c55e;--red:#ef4444;--yellow:#eab308;--blue:#3b82f6;--pink:#ec4899;
}
body{font-family:'Inter','Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
.container{max-width:1200px;margin:0 auto;padding:20px 16px}

/* Header */
header{display:flex;align-items:center;justify-content:space-between;padding:20px 0 28px}
header .left h1{font-size:22px;font-weight:700;letter-spacing:-.5px}
header .left h1 span{color:var(--accent)}
header .left .sub{color:var(--muted);font-size:13px;margin-top:2px}
header .right{text-align:right;font-size:12px;color:var(--dim)}
header .right .live{display:inline-flex;align-items:center;gap:6px;color:var(--green);font-weight:600;font-size:13px}
header .right .live::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* Banner */
.banner{text-align:center;padding:14px;border-radius:10px;margin-bottom:24px;font-weight:600;font-size:14px}
.banner.ok{background:rgba(34,197,94,.08);color:var(--green);border:1px solid rgba(34,197,94,.2)}
.banner.down{background:rgba(239,68,68,.08);color:var(--red);border:1px solid rgba(239,68,68,.2)}

/* Stats */
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:24px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 12px;text-align:center}
.stat .v{font-size:26px;font-weight:700;line-height:1.1}
.stat .l{font-size:10px;color:var(--muted);margin-top:5px;text-transform:uppercase;letter-spacing:.6px}
.stat .v.purple{color:var(--accent)}.stat .v.green{color:var(--green)}
.stat .v.blue{color:var(--blue)}.stat .v.pink{color:var(--pink)}
.stat .v.yellow{color:var(--yellow)}

/* Services */
.section{margin-bottom:24px}
.stitle{font-size:11px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
.services{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px}
.svc{display:flex;align-items:center;justify-content:space-between;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 14px}
.svc .nm{font-weight:500;font-size:14px}
.svc .rt{display:flex;align-items:center;gap:8px}
.svc .ms{color:var(--dim);font-size:12px}
.pill{padding:3px 10px;border-radius:12px;font-size:11px;font-weight:600;text-transform:uppercase}
.pill.on{background:rgba(34,197,94,.12);color:var(--green)}
.pill.off{background:rgba(239,68,68,.12);color:var(--red)}

/* Uptime */
.ubar{display:flex;gap:1px;height:28px;border-radius:5px;overflow:hidden;margin-bottom:4px}
.ubar .seg{flex:1;min-width:2px}.seg.up{background:var(--green)}.seg.unk{background:var(--border)}.seg.dn{background:var(--red)}
.uinfo{display:flex;justify-content:space-between;font-size:11px;color:var(--dim)}

/* Charts */
.charts{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:24px}
.ccard{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.ccard h3{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;display:flex;justify-content:space-between}
.ccard h3 .cur{color:var(--text);font-size:13px;font-weight:700}
.ccard canvas{width:100%!important;height:150px!important}

/* Server info */
.sinfo{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:24px}
.si{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 14px}
.si .sl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.si .sv{font-size:18px;font-weight:700;margin-top:4px;color:var(--text)}

footer{text-align:center;padding:24px 0;color:var(--dim);font-size:11px}

@media(max-width:900px){.charts{grid-template-columns:1fr}.stats{grid-template-columns:repeat(3,1fr)}}
@media(max-width:500px){.stats{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="container">

<header>
  <div class="left">
    <h1><span>Torii</span> Server Status</h1>
    <div class="sub">shikkesora.com &mdash; real-time monitoring</div>
  </div>
  <div class="right">
    <div class="live">LIVE</div>
    <div id="hdr-time" style="margin-top:4px">--</div>
  </div>
</header>

<div class="banner ok" id="banner">All systems operational</div>

<div class="stats">
  <div class="stat"><div class="v green" id="s-online">-</div><div class="l">Online Now</div></div>
  <div class="stat"><div class="v purple" id="s-users">-</div><div class="l">Total Users</div></div>
  <div class="stat"><div class="v blue" id="s-scores">-</div><div class="l">Total Scores</div></div>
  <div class="stat"><div class="v pink" id="s-plays">-</div><div class="l">Total Plays</div></div>
  <div class="stat"><div class="v yellow" id="s-maps">-</div><div class="l">Beatmapsets</div></div>
  <div class="stat"><div class="v purple" id="s-pp">-</div><div class="l">Total PP</div></div>
</div>

<div class="section">
  <div class="stitle">Services</div>
  <div class="services" id="services"></div>
</div>

<div class="section">
  <div class="stitle">Uptime</div>
  <div class="ubar" id="ubar"></div>
  <div class="uinfo"><span id="ubar-left">-</span><span id="ubar-right">Now</span></div>
</div>

<div class="charts">
  <div class="ccard"><h3>Online Players <span class="cur" id="cv-online">-</span></h3><canvas id="c-online"></canvas></div>
  <div class="ccard"><h3>Scores / min <span class="cur" id="cv-spm">-</span></h3><canvas id="c-spm"></canvas></div>
  <div class="ccard"><h3>Total Scores <span class="cur" id="cv-scores">-</span></h3><canvas id="c-scores"></canvas></div>
  <div class="ccard"><h3>Total Plays <span class="cur" id="cv-plays">-</span></h3><canvas id="c-plays"></canvas></div>
  <div class="ccard"><h3>API Memory (MB) <span class="cur" id="cv-rss">-</span></h3><canvas id="c-rss"></canvas></div>
  <div class="ccard"><h3>DB Pool Active <span class="cur" id="cv-dbpool">-</span></h3><canvas id="c-dbpool"></canvas></div>
  <div class="ccard"><h3>Redis Memory (MB) <span class="cur" id="cv-redis">-</span></h3><canvas id="c-redis"></canvas></div>
  <div class="ccard"><h3>DB Threads <span class="cur" id="cv-dbt">-</span></h3><canvas id="c-dbt"></canvas></div>
</div>

<div class="section">
  <div class="stitle">Server Info</div>
  <div class="sinfo">
    <div class="si"><div class="sl">Uptime</div><div class="sv" id="si-up">-</div></div>
    <div class="si"><div class="sl">API Memory</div><div class="sv" id="si-rss">-</div></div>
    <div class="si"><div class="sl">Redis Memory</div><div class="sv" id="si-redis">-</div></div>
    <div class="si"><div class="sl">DB Connections</div><div class="sv" id="si-db">-</div></div>
    <div class="si"><div class="sl">DB Pool Overflow</div><div class="sv" id="si-dbof">-</div></div>
    <div class="si"><div class="sl">Scores/min</div><div class="sv" id="si-spm">-</div></div>
  </div>
</div>

<footer>Auto-updates every 10s &mdash; Powered by Torii</footer>

</div>
<script>
// ── tiny chart lib ──────────────────────────────────────────────────────
class C{
  constructor(el,color='#a78bfa'){
    this.el=el;this.ctx=el.getContext('2d');this.c=color;
    this.f=color.replace(/[\d.]+\)$/,'0.10)').replace('#','rgba(');
    if(color.startsWith('#')){const r=parseInt(color.slice(1,3),16),g=parseInt(color.slice(3,5),16),b=parseInt(color.slice(5,7),16);this.f=`rgba(${r},${g},${b},.10)`}
    this.d=[];this.lb=[];this._rs();
    window.addEventListener('resize',()=>this._rs());
  }
  _rs(){const r=this.el.parentElement.getBoundingClientRect();const p=devicePixelRatio||1;this.el.width=r.width*p;this.el.height=150*p;this.ctx.setTransform(p,0,0,p,0,0);this.W=r.width;this.H=150;this.draw()}
  set(lb,d){this.lb=lb;this.d=d;this.draw()}
  draw(){
    const{ctx:x,W:w,H:h,d,c,f}=this;x.clearRect(0,0,w,h);if(d.length<2)return;
    const P={t:8,r:8,b:20,l:44},cw=w-P.l-P.r,ch=h-P.t-P.b;
    let mn=Math.min(...d),mx=Math.max(...d);if(mx===mn){mx+=1;mn=Math.max(0,mn-1)}const rng=mx-mn;
    x.strokeStyle='rgba(255,255,255,.04)';x.lineWidth=1;x.font='10px system-ui';x.fillStyle='#555';x.textAlign='right';
    for(let i=0;i<=3;i++){const y=P.t+ch-i/3*ch;x.beginPath();x.moveTo(P.l,y);x.lineTo(w-P.r,y);x.stroke();const v=mn+i/3*rng;x.fillText(v>=1e6?(v/1e6).toFixed(1)+'M':v>=1e3?(v/1e3).toFixed(1)+'k':v.toFixed(v<10?1:0),P.l-4,y+3)}
    x.textAlign='center';const st=Math.max(1,Math.floor(d.length/6));
    for(let i=0;i<d.length;i+=st){const px=P.l+i/(d.length-1)*cw;x.fillText(this.lb[i]||'',px,h-3)}
    x.beginPath();for(let i=0;i<d.length;i++){const px=P.l+i/(d.length-1)*cw,py=P.t+ch-(d[i]-mn)/rng*ch;i?x.lineTo(px,py):x.moveTo(px,py)}
    x.strokeStyle=c;x.lineWidth=1.5;x.stroke();
    const lx=P.l+(d.length-1)/(d.length-1)*cw;x.lineTo(lx,P.t+ch);x.lineTo(P.l,P.t+ch);x.closePath();x.fillStyle=f;x.fill();
    const ly=P.t+ch-(d[d.length-1]-mn)/rng*ch;x.beginPath();x.arc(lx,ly,3,0,Math.PI*2);x.fillStyle=c;x.fill()
  }
}

const F=n=>{if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return n.toFixed?n.toFixed(n<10&&n%1?1:0):String(n)};
const U=s=>{const d=~~(s/86400),h=~~(s%86400/3600),m=~~(s%3600/60);return d?d+'d '+h+'h':h?h+'h '+m+'m':m+'m'};
const T=iso=>{const d=new Date(iso);return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0')};

const charts={
  online:new C(document.getElementById('c-online'),'#22c55e'),
  spm:new C(document.getElementById('c-spm'),'#3b82f6'),
  scores:new C(document.getElementById('c-scores'),'#a78bfa'),
  plays:new C(document.getElementById('c-plays'),'#ec4899'),
  rss:new C(document.getElementById('c-rss'),'#eab308'),
  dbpool:new C(document.getElementById('c-dbpool'),'#f97316'),
  redis:new C(document.getElementById('c-redis'),'#14b8a6'),
  dbt:new C(document.getElementById('c-dbt'),'#8b5cf6'),
};

const SVC_NAMES={"api":"API Server","perf":"Performance Server","spec":"Spectator Server"};

async function poll(){
  try{
    const[sR,hR]=await Promise.all([fetch('/api/status'),fetch('/api/status/history')]);
    const s=await sR.json(), hist=await hR.json();

    // header time
    document.getElementById('hdr-time').textContent=new Date(s.t).toLocaleTimeString();

    // stats
    document.getElementById('s-online').textContent=F(s.online);
    document.getElementById('s-users').textContent=F(s.users);
    document.getElementById('s-scores').textContent=F(s.scores);
    document.getElementById('s-plays').textContent=F(s.plays);
    document.getElementById('s-maps').textContent=F(s.beatmapsets);
    document.getElementById('s-pp').textContent=F(s.pp);

    // services
    const svcs=s.services||{};
    const el=document.getElementById('services');
    el.innerHTML=Object.entries(svcs).map(([k,v])=>`
      <div class="svc"><div class="nm">${SVC_NAMES[k]||k}</div><div class="rt">
        ${v.ms!=null?`<span class="ms">${v.ms}ms</span>`:''}
        <span class="pill ${v.s==='online'?'on':'off'}">${v.s}</span>
      </div></div>`).join('');

    // banner
    const bn=document.getElementById('banner');
    const down=Object.entries(svcs).filter(([,v])=>v.s!=='online');
    if(!down.length){bn.className='banner ok';bn.textContent='All systems operational'}
    else{bn.className='banner down';bn.textContent='Disruption: '+down.map(([k])=>SVC_NAMES[k]||k).join(', ')}

    // server info
    document.getElementById('si-up').textContent=U(s.uptime);
    document.getElementById('si-rss').textContent=s.rss_mb+' MB';
    document.getElementById('si-redis').textContent=s.redis_mb+' MB';
    document.getElementById('si-db').textContent=s.db_pool_active+' / '+(s.db_pool_active+s.db_pool_idle);
    document.getElementById('si-dbof').textContent=s.db_pool_overflow;
    document.getElementById('si-spm').textContent=s.scores_1m;

    // charts from history
    if(hist.length>1){
      const lb=hist.map(h=>T(h.t));
      charts.online.set(lb,hist.map(h=>h.online));
      charts.scores.set(lb,hist.map(h=>h.scores));
      charts.plays.set(lb,hist.map(h=>h.plays));
      charts.rss.set(lb,hist.map(h=>h.rss_mb));
      charts.dbpool.set(lb,hist.map(h=>h.db_pool_active));
      charts.redis.set(lb,hist.map(h=>h.redis_mb));
      charts.dbt.set(lb,hist.map(h=>h.db_threads));

      // scores per minute
      const spm=[],slb=[];
      for(let i=1;i<hist.length;i++){
        const dt=(new Date(hist[i].t)-new Date(hist[i-1].t))/60000||.25;
        spm.push(Math.max(0,(hist[i].scores-hist[i-1].scores)/dt));
        slb.push(T(hist[i].t));
      }
      charts.spm.set(slb,spm);

      // current values for chart headers
      document.getElementById('cv-online').textContent=F(s.online);
      document.getElementById('cv-spm').textContent=F(s.scores_1m);
      document.getElementById('cv-scores').textContent=F(s.scores);
      document.getElementById('cv-plays').textContent=F(s.plays);
      document.getElementById('cv-rss').textContent=s.rss_mb;
      document.getElementById('cv-dbpool').textContent=s.db_pool_active;
      document.getElementById('cv-redis').textContent=s.redis_mb;
      document.getElementById('cv-dbt').textContent=s.db_threads;

      // uptime bar
      const bar=document.getElementById('ubar');
      const N=90;
      const slots=[];
      for(let i=0;i<N;i++){
        const idx=Math.floor(i/N*hist.length);
        if(idx<hist.length){
          const sv=hist[idx].services||{};
          const allOk=Object.values(sv).every(v=>v.s==='online');
          slots.push(allOk?'up':'dn');
        }else slots.push('unk');
      }
      bar.innerHTML=slots.map(s=>`<div class="seg ${s}"></div>`).join('');
      // time range label
      const first=new Date(hist[0].t);
      const diffMin=Math.round((Date.now()-first)/60000);
      document.getElementById('ubar-left').textContent=diffMin>=60?Math.round(diffMin/60)+'h ago':diffMin+'m ago';
    }
  }catch(e){
    console.error(e);
    document.getElementById('banner').className='banner down';
    document.getElementById('banner').textContent='Unable to reach server';
  }
}
poll();setInterval(poll,10000);
</script>
</body>
</html>"""
