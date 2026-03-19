#!/usr/bin/env python3
"""
Sherlock Telemetry Agent
========================
Daemon that collects system/application metrics and pushes them to a remote
telemetry server, while exposing a local command API for remote management.

Usage:
    python agent.py                        # uses agent.conf in same directory
    AGENT_TOKEN=secret python agent.py     # env vars override conf file
"""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psutil
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Header, Query, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONF_PATH = Path(__file__).parent / "agent.conf"
SHERLOCK_BASE = Path(__file__).resolve().parent.parent  # ~/Sherlock/ (derived from telemetry/agent.py)
LOG_DIR = SHERLOCK_BASE / "logs"

SERVICE_COMMANDS: dict[str, dict[str, list[str]]] = {
    "web": {
        "start":   ["launchctl", "start", "com.sherlock.web"],
        "stop":    ["launchctl", "stop", "com.sherlock.web"],
        "restart": ["launchctl", "kickstart", "-k", "gui/{uid}/com.sherlock.web"],
    },
    "ollama": {
        "start":   ["launchctl", "start", "com.ollama.ollama"],
        "stop":    ["launchctl", "stop", "com.ollama.ollama"],
        "restart": ["launchctl", "kickstart", "-k", "gui/{uid}/com.ollama.ollama"],
    },
    "chromadb": {
        "start":   ["launchctl", "start", "com.sherlock.chromadb"],
        "stop":    ["launchctl", "stop", "com.sherlock.chromadb"],
        "restart": ["launchctl", "kickstart", "-k", "gui/{uid}/com.sherlock.chromadb"],
    },
    "searxng": {
        "start":   ["launchctl", "start", "com.sherlock.searxng"],
        "stop":    ["launchctl", "stop", "com.sherlock.searxng"],
        "restart": ["launchctl", "kickstart", "-k", "gui/{uid}/com.sherlock.searxng"],
    },
    "nginx": {
        "start":   ["sudo", "nginx"],
        "stop":    ["sudo", "nginx", "-s", "stop"],
        "restart": ["sudo", "nginx", "-s", "reload"],
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("sherlock.telemetry")


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
    ))
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    def __init__(self) -> None:
        self.telemetry_server_url: str = "http://100.100.100.1:9200"
        self.agent_token: str = ""
        self.node_name: str = platform.node()
        self.heartbeat_interval: int = 30
        self.agent_port: int = 9100
        self._load()

    def _load(self) -> None:
        # Load from conf file first
        if CONF_PATH.exists():
            cp = configparser.ConfigParser()
            cp.read(CONF_PATH)
            sec = cp["agent"] if "agent" in cp else {}
            self.telemetry_server_url = sec.get("TELEMETRY_SERVER_URL", self.telemetry_server_url)
            self.agent_token = sec.get("AGENT_TOKEN", self.agent_token)
            self.node_name = sec.get("NODE_NAME", self.node_name)
            self.heartbeat_interval = int(sec.get("HEARTBEAT_INTERVAL", str(self.heartbeat_interval)))
            self.agent_port = int(sec.get("AGENT_PORT", str(self.agent_port)))

        # Env vars override conf file
        self.telemetry_server_url = os.getenv("TELEMETRY_SERVER_URL", self.telemetry_server_url)
        self.agent_token = os.getenv("AGENT_TOKEN", self.agent_token)
        self.node_name = os.getenv("NODE_NAME", self.node_name)
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", str(self.heartbeat_interval)))
        self.agent_port = int(os.getenv("AGENT_PORT", str(self.agent_port)))


cfg = Config()

# ---------------------------------------------------------------------------
# Metrics collection
# ---------------------------------------------------------------------------

def _collect_system_metrics() -> dict[str, Any]:
    """Gather CPU, RAM, disk, temperature, and uptime via psutil."""
    try:
        cpu_pct = psutil.cpu_percent(interval=0.5)
        cpu_freq = psutil.cpu_freq()
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        boot = psutil.boot_time()
        uptime_s = time.time() - boot

        temps: dict[str, Any] = {}
        try:
            raw = psutil.sensors_temperatures()
            for chip, entries in raw.items():
                temps[chip] = [{"label": e.label, "current": e.current, "high": e.high, "critical": e.critical} for e in entries]
        except (AttributeError, RuntimeError):
            pass  # Not all platforms support temps

        return {
            "cpu_percent": cpu_pct,
            "cpu_count": psutil.cpu_count(),
            "cpu_freq_mhz": cpu_freq.current if cpu_freq else None,
            "ram_total_gb": round(mem.total / (1024 ** 3), 2),
            "ram_used_gb": round(mem.used / (1024 ** 3), 2),
            "ram_percent": mem.percent,
            "disk_total_gb": round(disk.total / (1024 ** 3), 2),
            "disk_used_gb": round(disk.used / (1024 ** 3), 2),
            "disk_percent": disk.percent,
            "temperatures": temps,
            "uptime_seconds": int(uptime_s),
            "load_avg": list(os.getloadavg()),
        }
    except Exception as exc:
        log.error("Failed to collect system metrics: %s", exc)
        return {"error": str(exc)}


def _check_http(url: str, timeout: float = 5) -> dict[str, Any]:
    """Check an HTTP endpoint. Returns status dict."""
    try:
        start = time.monotonic()
        resp = requests.get(url, timeout=timeout, verify=False)
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        return {"up": resp.status_code < 500, "status_code": resp.status_code, "latency_ms": elapsed_ms}
    except requests.RequestException as exc:
        return {"up": False, "error": str(exc)}


def _check_tcp(host: str, port: int, timeout: float = 3) -> dict[str, Any]:
    """Check a raw TCP port."""
    try:
        start = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            return {"up": True, "latency_ms": elapsed_ms}
    except OSError as exc:
        return {"up": False, "error": str(exc)}


def _collect_service_health() -> dict[str, Any]:
    """Health check all Sherlock services."""
    return {
        "sherlock_web": _check_http("http://localhost:3000"),
        "ollama": _check_http("http://localhost:11434/api/tags"),
        "chromadb": _check_http("http://localhost:8000/api/v1/heartbeat"),
        "searxng": _check_http("http://localhost:8888/healthz"),
        "nginx": _check_tcp("localhost", 443),
    }


def _parse_log_metrics() -> dict[str, Any]:
    """Parse Sherlock JSON logs for query counts, error counts, latency stats."""
    stats: dict[str, Any] = {
        "total_requests": 0,
        "errors": 0,
        "warnings": 0,
        "queries": 0,
        "latencies_ms": [],
        "avg_latency_ms": None,
        "p95_latency_ms": None,
        "max_latency_ms": None,
    }

    log_files = ["app.log", "rag.log", "sherlock-web.log"]
    now = time.time()
    window_s = 300  # last 5 minutes

    for fname in log_files:
        fpath = LOG_DIR / fname
        if not fpath.exists():
            continue
        try:
            # Read last 500 lines (tail) to avoid reading huge files
            lines: list[str] = deque(open(fpath, "r", errors="replace"), maxlen=500)  # type: ignore[arg-type]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Check if within time window
                ts_str = entry.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if now - ts > window_s:
                    continue

                level = entry.get("level", "").upper()
                if level == "ERROR":
                    stats["errors"] += 1
                elif level == "WARNING":
                    stats["warnings"] += 1

                msg = entry.get("msg", "")
                if msg == "request":
                    stats["total_requests"] += 1
                    dur = entry.get("duration_ms")
                    if dur is not None:
                        stats["latencies_ms"].append(dur)

                if entry.get("event") == "rag_query_start" or msg == "rag_query_start":
                    stats["queries"] += 1
                    lat = entry.get("latency_retrieve_ms")
                    if lat is not None:
                        stats["latencies_ms"].append(lat)

        except Exception as exc:
            log.warning("Error parsing %s: %s", fname, exc)

    latencies = sorted(stats["latencies_ms"])
    if latencies:
        stats["avg_latency_ms"] = round(sum(latencies) / len(latencies), 1)
        stats["p95_latency_ms"] = latencies[int(len(latencies) * 0.95)]
        stats["max_latency_ms"] = latencies[-1]
    stats["latency_count"] = len(latencies)
    del stats["latencies_ms"]  # don't ship raw list

    return stats


def collect_full_metrics() -> dict[str, Any]:
    """Assemble the complete metrics payload."""
    return {
        "node_id": cfg.node_name,
        "name": cfg.node_name,
        "host": platform.node(),
        "ip": socket.gethostbyname(socket.gethostname()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "system": _collect_system_metrics(),
        "services": _collect_service_health(),
        "app_metrics": _parse_log_metrics(),
    }


# ---------------------------------------------------------------------------
# Heartbeat loop (runs in background)
# ---------------------------------------------------------------------------
_shutdown_event = asyncio.Event()


async def _heartbeat_loop() -> None:
    """Periodically POST metrics to the telemetry server."""
    log.info("Heartbeat loop started (interval=%ds, server=%s)", cfg.heartbeat_interval, cfg.telemetry_server_url)
    while not _shutdown_event.is_set():
        try:
            payload = await asyncio.get_event_loop().run_in_executor(None, collect_full_metrics)
            url = f"{cfg.telemetry_server_url.rstrip('/')}/api/heartbeat"
            headers = {"Content-Type": "application/json"}
            if cfg.agent_token:
                headers["Authorization"] = f"Bearer {cfg.agent_token}"
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: requests.post(url, json=payload, headers=headers, timeout=10),
            )
            log.info("Heartbeat sent, server responded %d", resp.status_code)
        except requests.RequestException as exc:
            log.warning("Heartbeat failed: %s", exc)
        except Exception as exc:
            log.error("Unexpected heartbeat error: %s", exc)

        # Wait for interval or shutdown
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=cfg.heartbeat_interval)
            break  # shutdown signaled
        except asyncio.TimeoutError:
            pass  # interval elapsed, loop again


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _verify_token(authorization: Optional[str] = Header(None)) -> None:
    if not cfg.agent_token:
        return  # no token configured, skip auth
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != cfg.agent_token:
        raise HTTPException(status_code=403, detail="Invalid token")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start heartbeat on startup, cancel on shutdown."""
    task = asyncio.create_task(_heartbeat_loop())
    log.info("Telemetry agent started on port %d (node=%s)", cfg.agent_port, cfg.node_name)
    yield
    _shutdown_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log.info("Telemetry agent stopped")


app = FastAPI(title="Sherlock Telemetry Agent", lifespan=lifespan)


# --- Health (no auth required) ---

@app.get("/health")
async def health():
    """Full current metrics snapshot."""
    payload = await asyncio.get_event_loop().run_in_executor(None, collect_full_metrics)
    return payload


# --- Command endpoints (auth required) ---

class ServiceCmd(BaseModel):
    service: str
    action: str


@app.post("/cmd/service")
async def cmd_service(body: ServiceCmd, authorization: Optional[str] = Header(None)):
    _verify_token(authorization)

    if body.service not in SERVICE_COMMANDS:
        raise HTTPException(400, f"Unknown service: {body.service}. Valid: {list(SERVICE_COMMANDS)}")
    if body.action not in ("start", "stop", "restart"):
        raise HTTPException(400, f"Unknown action: {body.action}. Valid: start, stop, restart")

    cmd_template = SERVICE_COMMANDS[body.service][body.action]
    uid = str(os.getuid())
    cmd = [c.replace("{uid}", uid) for c in cmd_template]

    log.info("Executing service command: %s", " ".join(cmd))
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=30),
        )
        return {
            "ok": result.returncode == 0,
            "service": body.service,
            "action": body.action,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Command timed out")
    except Exception as exc:
        log.error("Service command failed: %s", exc)
        raise HTTPException(500, str(exc))


@app.post("/cmd/reboot")
async def cmd_reboot(authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    log.warning("Reboot command received")
    try:
        subprocess.Popen(["sudo", "shutdown", "-r", "+1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "msg": "Reboot scheduled in 1 minute"}
    except Exception as exc:
        log.error("Reboot failed: %s", exc)
        raise HTTPException(500, str(exc))


@app.post("/cmd/reindex")
async def cmd_reindex(authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    log.info("Reindex command received")
    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.post("http://localhost:3000/api/index/trigger", timeout=30),
        )
        return {"ok": resp.status_code < 400, "status_code": resp.status_code, "body": resp.text[:500]}
    except requests.RequestException as exc:
        log.error("Reindex request failed: %s", exc)
        raise HTTPException(502, str(exc))


@app.get("/cmd/logs")
async def cmd_logs(
    authorization: Optional[str] = Header(None),
    stream: str = Query(default="app", description="Log stream name (app, rag, audit, indexer, sherlock-web)"),
    lines: int = Query(default=100, ge=1, le=5000, description="Number of lines to return"),
):
    _verify_token(authorization)

    fname = f"{stream}.log"
    fpath = LOG_DIR / fname
    if not fpath.exists():
        available = [f.name for f in LOG_DIR.glob("*.log")]
        raise HTTPException(404, f"Log stream '{stream}' not found. Available: {available}")

    try:
        all_lines = deque(open(fpath, "r", errors="replace"), maxlen=lines)
        return PlainTextResponse("".join(all_lines))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/cmd/file/push")
async def cmd_file_push(
    request: Request,
    authorization: Optional[str] = Header(None),
    path: str = Query(..., description="Destination path on this machine"),
    file: UploadFile = File(...),
):
    _verify_token(authorization)

    dest = Path(path)
    # Safety: only allow writes under Sherlock base
    try:
        dest.resolve().relative_to(SHERLOCK_BASE.resolve())
    except ValueError:
        raise HTTPException(403, f"Writes restricted to {SHERLOCK_BASE}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    dest.write_bytes(content)
    log.info("File pushed: %s (%d bytes)", dest, len(content))
    return {"ok": True, "path": str(dest), "bytes": len(content)}


@app.get("/cmd/file/pull")
async def cmd_file_pull(
    authorization: Optional[str] = Header(None),
    path: str = Query(..., description="File path to retrieve"),
):
    _verify_token(authorization)

    fpath = Path(path)
    # Safety: only allow reads under Sherlock base
    try:
        fpath.resolve().relative_to(SHERLOCK_BASE.resolve())
    except ValueError:
        raise HTTPException(403, f"Reads restricted to {SHERLOCK_BASE}")

    if not fpath.exists() or not fpath.is_file():
        raise HTTPException(404, f"File not found: {path}")

    return FileResponse(str(fpath), filename=fpath.name)


# ---------------------------------------------------------------------------
# Signal handling & main
# ---------------------------------------------------------------------------

def _handle_signal(sig: int, _frame: Any) -> None:
    name = signal.Signals(sig).name
    log.info("Received %s, shutting down", name)
    _shutdown_event.set()


def main() -> None:
    _setup_logging()

    if not cfg.agent_token:
        log.warning("AGENT_TOKEN is not set -- command endpoints are UNPROTECTED")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Config: server=%s node=%s interval=%ds port=%d",
             cfg.telemetry_server_url, cfg.node_name, cfg.heartbeat_interval, cfg.agent_port)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=cfg.agent_port,
        log_level="warning",
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
