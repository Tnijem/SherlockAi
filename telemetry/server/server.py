#!/usr/bin/env python3
"""Sherlock Telemetry Server — central monitoring dashboard for Sherlock agents."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Header, Request, UploadFile, File, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONF_PATH = Path(__file__).parent / "server.conf"

def _load_conf() -> dict[str, str]:
    """Parse simple KEY=VALUE config file (ignores comments and blanks)."""
    conf: dict[str, str] = {}
    if _CONF_PATH.exists():
        for line in _CONF_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf

_conf = _load_conf()

AGENT_TOKEN: str = os.getenv("AGENT_TOKEN", _conf.get("AGENT_TOKEN", ""))
LISTEN_PORT: int = int(os.getenv("LISTEN_PORT", _conf.get("LISTEN_PORT", "9200")))
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", _conf.get("WEBHOOK_URL", ""))
MAX_ALERTS: int = int(os.getenv("MAX_ALERTS", _conf.get("MAX_ALERTS", "1000")))
AGENT_SCHEME: str = os.getenv("AGENT_SCHEME", _conf.get("AGENT_SCHEME", "http"))
AGENT_PORT: int = int(os.getenv("AGENT_PORT", _conf.get("AGENT_PORT", "9201")))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sherlock.telemetry")

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

# node_id -> latest heartbeat payload + metadata
nodes: dict[str, dict[str, Any]] = {}

# Alert deque (most recent last)
alerts: deque[dict[str, Any]] = deque(maxlen=MAX_ALERTS)

# Per-node CPU history for sustained-high-CPU detection (last N readings)
_cpu_history: dict[str, deque[float]] = {}

# Per-node error timestamp ring for spike detection
_error_ts: dict[str, deque[float]] = {}

# Webhook URL (mutable at runtime via API)
_webhook_url: str = WEBHOOK_URL

# Background task handle
_heartbeat_checker_task: asyncio.Task | None = None

# Shared httpx client
_http: httpx.AsyncClient | None = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ts() -> float:
    return time.time()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _node_status(last_seen: float) -> str:
    age = _now_ts() - last_seen
    if age < 120:
        return "online"
    if age < 300:
        return "warning"
    return "offline"

def _agent_base(node: dict[str, Any]) -> str:
    host = node.get("host", node.get("ip", "127.0.0.1"))
    port = node.get("agent_port", AGENT_PORT)
    return f"{AGENT_SCHEME}://{host}:{port}"

async def _fire_webhook(alert: dict[str, Any]) -> None:
    if not _webhook_url or _http is None:
        return
    try:
        payload = {
            "text": f"[Sherlock Alert] {alert['severity'].upper()}: {alert['message']} (node: {alert.get('node_id', 'N/A')})",
            "alert": alert,
        }
        resp = await _http.post(_webhook_url, json=payload, timeout=10)
        if resp.status_code >= 400:
            log.warning("Webhook returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Webhook delivery failed: %s", exc)

def _create_alert(
    severity: str,
    message: str,
    node_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    alert = {
        "id": f"alert-{int(_now_ts()*1000)}",
        "ts": _now_iso(),
        "epoch": _now_ts(),
        "severity": severity,
        "message": message,
        "node_id": node_id,
        "details": details or {},
    }
    alerts.append(alert)
    log.warning("ALERT [%s] %s — %s", severity, node_id or "global", message)
    # Fire webhook in background (don't block caller)
    asyncio.ensure_future(_fire_webhook(alert))
    return alert

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _verify_token(authorization: str | None) -> None:
    if not AGENT_TOKEN:
        return  # No token configured — open access (dev mode)
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != AGENT_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

# ---------------------------------------------------------------------------
# Alert evaluation (called on each heartbeat)
# ---------------------------------------------------------------------------

def _evaluate_alerts(node_id: str, data: dict[str, Any]) -> None:
    # Agent sends system metrics under "system" key
    metrics = data.get("system", data.get("metrics", {}))

    # --- Service down ---
    services = data.get("services", {})
    for svc, info in services.items():
        if isinstance(info, dict):
            is_up = info.get("up", info.get("status") not in ("down", "stopped", False))
        else:
            is_up = info not in ("down", "stopped", False)
        if not is_up:
            _create_alert("warning", f"Service '{svc}' reported down", node_id, {"service": svc})

    # --- CPU >90% for 3+ consecutive heartbeats ---
    cpu = metrics.get("cpu_percent")
    if cpu is not None:
        hist = _cpu_history.setdefault(node_id, deque(maxlen=10))
        hist.append(float(cpu))
        if len(hist) >= 3 and all(c > 90 for c in list(hist)[-3:]):
            _create_alert("warning", f"CPU sustained >90% ({cpu:.1f}%)", node_id, {"cpu": cpu})

    # --- RAM >85% ---
    ram = metrics.get("ram_percent")
    if ram is not None and float(ram) > 85:
        _create_alert("warning", f"RAM usage at {ram:.1f}%", node_id, {"ram": ram})

    # --- Disk >90% on any mount ---
    disks = metrics.get("disks", {})
    if isinstance(disks, dict):
        for mount, usage in disks.items():
            pct = usage if isinstance(usage, (int, float)) else usage.get("percent", 0) if isinstance(usage, dict) else 0
            if float(pct) > 90:
                _create_alert("critical", f"Disk {mount} at {pct:.1f}%", node_id, {"mount": mount, "percent": pct})

    # --- Error count spike (>10 in 5min window) ---
    error_count = metrics.get("error_count", 0)
    if error_count:
        ring = _error_ts.setdefault(node_id, deque(maxlen=200))
        now = _now_ts()
        for _ in range(int(error_count)):
            ring.append(now)
        cutoff = now - 300
        recent = sum(1 for t in ring if t > cutoff)
        if recent > 10:
            _create_alert("warning", f"Error spike: {recent} errors in last 5min", node_id, {"recent_errors": recent})

# ---------------------------------------------------------------------------
# Background: dead-man switch
# ---------------------------------------------------------------------------

async def _heartbeat_checker() -> None:
    """Runs every 30s — flags nodes with no heartbeat for >2min."""
    while True:
        await asyncio.sleep(30)
        now = _now_ts()
        for nid, node in list(nodes.items()):
            last = node.get("last_seen_epoch", 0)
            prev_status = node.get("_prev_status", "online")
            cur_status = _node_status(last)
            if cur_status == "offline" and prev_status != "offline":
                _create_alert("critical", f"No heartbeat for >{int(now - last)}s", nid)
            elif cur_status == "warning" and prev_status == "online":
                _create_alert("warning", f"Heartbeat delayed ({int(now - last)}s)", nid)
            node["_prev_status"] = cur_status

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _heartbeat_checker_task, _http
    _http = httpx.AsyncClient()
    _heartbeat_checker_task = asyncio.create_task(_heartbeat_checker())
    log.info("Sherlock Telemetry Server starting on port %d", LISTEN_PORT)
    yield
    _heartbeat_checker_task.cancel()
    await _http.aclose()

app = FastAPI(title="Sherlock Telemetry Server", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# ---- Heartbeat (agents POST here) ----------------------------------------

@app.post("/api/heartbeat")
async def heartbeat(request: Request, authorization: str | None = Header(None)):
    _verify_token(authorization)
    data = await request.json()
    node_id = data.get("node_id")
    if not node_id:
        raise HTTPException(400, "node_id required")

    now = _now_ts()
    data["last_seen"] = _now_iso()
    data["last_seen_epoch"] = now
    data["status"] = "online"
    data.setdefault("_prev_status", nodes.get(node_id, {}).get("_prev_status", "online"))
    nodes[node_id] = data

    _evaluate_alerts(node_id, data)

    return {"ok": True, "ts": _now_iso()}

# ---- Nodes ---------------------------------------------------------------

@app.get("/api/nodes")
async def list_nodes():
    result = {}
    for nid, node in nodes.items():
        status = _node_status(node.get("last_seen_epoch", 0))
        result[nid] = {
            "node_id": nid,
            "name": node.get("name", nid),
            "host": node.get("host", node.get("ip", "unknown")),
            "status": status,
            "last_seen": node.get("last_seen"),
            "last_seen_epoch": node.get("last_seen_epoch"),
            "system": node.get("system", {}),
            "services": node.get("services", {}),
            "app_metrics": node.get("app_metrics", {}),
        }
    return result

# ---- Node logs (proxy) ---------------------------------------------------

@app.get("/api/nodes/{node_id}/logs")
async def node_logs(
    node_id: str,
    lines: int = Query(200, ge=1, le=5000),
    level: str = Query("all"),
    stream: str = Query("all"),
    authorization: str | None = Header(None),
):
    _verify_token(authorization)
    node = nodes.get(node_id)
    if not node:
        raise HTTPException(404, "Unknown node")
    if _node_status(node.get("last_seen_epoch", 0)) == "offline":
        raise HTTPException(502, "Node is offline")

    base = _agent_base(node)
    params = {"lines": lines, "level": level, "stream": stream}
    try:
        resp = await _http.get(f"{base}/cmd/logs", params=params, timeout=15)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as exc:
        raise HTTPException(502, f"Agent unreachable: {exc}")

# ---- Command proxy -------------------------------------------------------

@app.post("/api/nodes/{node_id}/command")
async def node_command(
    node_id: str,
    request: Request,
    authorization: str | None = Header(None),
):
    _verify_token(authorization)
    node = nodes.get(node_id)
    if not node:
        raise HTTPException(404, "Unknown node")
    if _node_status(node.get("last_seen_epoch", 0)) == "offline":
        raise HTTPException(502, "Node is offline")

    body = await request.json()
    command = body.get("command")
    args = body.get("args", {})
    if command not in ("service", "reboot", "reindex"):
        raise HTTPException(400, f"Unsupported command: {command}")

    base = _agent_base(node)
    try:
        resp = await _http.post(f"{base}/cmd/{command}", json=args, timeout=30)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as exc:
        raise HTTPException(502, f"Agent unreachable: {exc}")

# ---- File push (proxy) ---------------------------------------------------

@app.post("/api/nodes/{node_id}/file/push")
async def file_push(
    node_id: str,
    file: UploadFile = File(...),
    dest: str = Query(..., description="Remote destination path"),
    authorization: str | None = Header(None),
):
    _verify_token(authorization)
    node = nodes.get(node_id)
    if not node:
        raise HTTPException(404, "Unknown node")
    if _node_status(node.get("last_seen_epoch", 0)) == "offline":
        raise HTTPException(502, "Node is offline")

    base = _agent_base(node)
    content = await file.read()
    try:
        resp = await _http.post(
            f"{base}/cmd/file/push",
            files={"file": (file.filename, content, file.content_type or "application/octet-stream")},
            data={"dest": dest},
            timeout=60,
        )
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as exc:
        raise HTTPException(502, f"Agent unreachable: {exc}")

# ---- File pull (proxy) ---------------------------------------------------

@app.get("/api/nodes/{node_id}/file/pull")
async def file_pull(
    node_id: str,
    path: str = Query(..., description="Remote file path to pull"),
    authorization: str | None = Header(None),
):
    _verify_token(authorization)
    node = nodes.get(node_id)
    if not node:
        raise HTTPException(404, "Unknown node")
    if _node_status(node.get("last_seen_epoch", 0)) == "offline":
        raise HTTPException(502, "Node is offline")

    base = _agent_base(node)
    try:
        resp = await _http.get(f"{base}/cmd/file/pull", params={"path": path}, timeout=60)
        if resp.status_code != 200:
            return JSONResponse(content={"error": resp.text}, status_code=resp.status_code)
        cd = resp.headers.get("content-disposition", "")
        return StreamingResponse(
            iter([resp.content]),
            media_type=resp.headers.get("content-type", "application/octet-stream"),
            headers={"content-disposition": cd} if cd else {},
        )
    except Exception as exc:
        raise HTTPException(502, f"Agent unreachable: {exc}")

# ---- Alerts --------------------------------------------------------------

@app.get("/api/alerts")
async def list_alerts(
    limit: int = Query(100, ge=1, le=1000),
    node_id: str | None = Query(None),
    severity: str | None = Query(None),
):
    result = list(alerts)
    if node_id:
        result = [a for a in result if a.get("node_id") == node_id]
    if severity:
        result = [a for a in result if a.get("severity") == severity]
    # Most recent first
    result = list(reversed(result))[:limit]
    return result

@app.post("/api/alerts/webhook")
async def configure_webhook(request: Request, authorization: str | None = Header(None)):
    _verify_token(authorization)
    body = await request.json()
    global _webhook_url
    _webhook_url = body.get("url", "")
    log.info("Webhook URL updated: %s", _webhook_url[:60] if _webhook_url else "(cleared)")
    return {"ok": True, "webhook_url": _webhook_url}

# ---- Dashboard -----------------------------------------------------------

_DASHBOARD_PATH = Path(__file__).parent / "static" / "dashboard.html"

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    if not _DASHBOARD_PATH.exists():
        raise HTTPException(500, "Dashboard HTML not found")
    return HTMLResponse(content=_DASHBOARD_PATH.read_text(), status_code=200)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT, log_level="info")
