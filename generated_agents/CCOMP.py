#!/usr/bin/env python3
"""
CompliCloud: Enterprise Compliance Automation & Analytics API

A cloud-based compliance automation backend agent. Centralizes compliance document workflows, automates regulatory reporting, and provides real-time audit analytics via secure API endpoints. Integrates with enterprise apps and supports customizable, dashboard-friendly analytics outputs.

Dependencies: swarms, loguru, pandas, numpy, requests, httpx

Run as: python script.py [--port PORT] [--config CONFIG_PATH]
"""
import os
import sys
import argparse
import json
import threading
import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
import numpy as np
import pandas as pd
import requests
import httpx
from swarms import Agent
from http.server import BaseHTTPRequestHandler, HTTPServer
import base64
import tempfile

# ---------------------------------------------
# Configuration and Constants
# ---------------------------------------------
DEFAULT_PORT = int(os.getenv("COMPLI_CLOUD_PORT", 8088))
DEFAULT_CONFIG_PATH = os.getenv("COMPLI_CLOUD_CONFIG", "./config.json")
API_KEY_ENV = "COMPLI_CLOUD_API_KEY"
ALLOWED_IPS_ENV = "COMPLI_CLOUD_ALLOWED_IPS"

# Example configuration for document workflow management
DEFAULT_CONFIG = {
    "audit_log_path": "./audit_logs.csv",
    "allowed_extensions": ["pdf", "docx", "xlsx", "csv", "txt"],
    "storage_path": "./compli_docs/",
    "retention_days": 365,
    "regulatory_templates": {
        "SOX": ["doc_id", "title", "department", "created_at", "attestation"],
        "GDPR": ["doc_id", "title", "data_subject", "retention_period", "privacy_notice"],
        "HIPAA": ["doc_id", "title", "patient_id", "access_log", "compliance_status"]
    }
}

os.makedirs(DEFAULT_CONFIG["storage_path"], exist_ok=True)
def _load_config(config_path: str) -> Dict[str, Any]:
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load config {config_path}: {e}")
    return DEFAULT_CONFIG.copy()

# ---------------------------------------------
# Audit Logging Utility
# ---------------------------------------------
def append_audit_log(event: Dict[str, Any], audit_log_path: str):
    try:
        df = pd.DataFrame([event])
        mode = 'a' if os.path.exists(audit_log_path) else 'w'
        header = not os.path.exists(audit_log_path)
        df.to_csv(audit_log_path, mode=mode, header=header, index=False)
    except Exception as exc:
        logger.error(f"Audit log append failed: {exc}")

# ---------------------------------------------
# Secure API Auth (simple API key + IP allow)
# ---------------------------------------------
def check_auth(headers: Dict[str, str], client_ip: str) -> bool:
    api_key_env = os.getenv(API_KEY_ENV)
    allowed_ips = os.getenv(ALLOWED_IPS_ENV, "*").split(",")
    key_provided = headers.get("X-API-KEY", "") or headers.get("Authorization", "").replace("Bearer ", "")
    correct_key = (api_key_env is None or key_provided == api_key_env)
    ip_ok = ("*" in allowed_ips) or (client_ip in allowed_ips)
    return correct_key and ip_ok

# ---------------------------------------------
# Compliance Document Storage API
# ---------------------------------------------
def save_document(doc_content: bytes, filename: str, config: Dict[str, Any]) -> str:
    ext = filename.split(".")[-1].lower()
    if ext not in config["allowed_extensions"]:
        raise ValueError(f"Extension {ext} not allowed.")
    storage_dir = config["storage_path"]
    os.makedirs(storage_dir, exist_ok=True)
    save_path = os.path.join(storage_dir, filename)
    with open(save_path, "wb") as f:
        f.write(doc_content)
    logger.info(f"Document saved: {save_path}")
    return save_path

def list_documents(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    docs = []
    storage_dir = config["storage_path"]
    for fname in os.listdir(storage_dir):
        fpath = os.path.join(storage_dir, fname)
        try:
            st = os.stat(fpath)
            docs.append({
                "filename": fname,
                "size": st.st_size,
                "last_modified": datetime.datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z"
            })
        except Exception as e:
            logger.error(f"List doc failed: {e}")
    return docs

def delete_document(filename: str, config: Dict[str, Any]) -> bool:
    storage_dir = config["storage_path"]
    fpath = os.path.join(storage_dir, filename)
    if os.path.exists(fpath):
        try:
            os.remove(fpath)
            logger.info(f"Deleted doc: {fpath}")
            return True
        except Exception as e:
            logger.error(f"Delete doc failed: {e}")
    return False

# ---------------------------------------------
# Regulatory Reporting Automation
# ---------------------------------------------
def collect_regulatory_evidence(document_list: List[Dict[str, Any]], template_name: str, config: Dict[str, Any]) -> pd.DataFrame:
    # For demonstration, synthesize evidence collection from file properties
    required_fields = config["regulatory_templates"].get(template_name, [])
    records = []
    for doc in document_list:
        rec = {k: doc.get(k, "N/A") for k in required_fields}
        rec.update({"filename": doc.get("filename"), "last_checked": datetime.datetime.utcnow().isoformat()})
        records.append(rec)
    df = pd.DataFrame(records)
    return df

def generate_regulatory_report(template: str, config: Dict[str, Any]) -> Tuple[str, pd.DataFrame]:
    docs = list_documents(config)
    df = collect_regulatory_evidence(docs, template, config)
    fname = f"regulatory_report_{template}_{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
    report_path = os.path.join(config["storage_path"], fname)
    df.to_csv(report_path, index=False)
    logger.info(f"Regulatory report generated: {report_path}")
    return report_path, df

# ---------------------------------------------
# Analytics Engine: Real-time Audit Analytics
# ---------------------------------------------
def audit_analytics_summary(audit_log_path: str) -> Dict[str, Any]:
    try:
        df = pd.read_csv(audit_log_path)
    except Exception:
        return {"message": "No audits found.", "total_events": 0}
    if df.empty:
        return {"message": "Empty audit log.", "total_events": 0}

    # Aggregate basic metrics
    events_per_day = df.groupby(df['timestamp'].str[:10]).size()
    recent = df.iloc[-1].to_dict()
    top_actions = df['action'].value_counts().to_dict()
    departments = df['department'].value_counts().to_dict() if 'department' in df.columns else {}
    analytics = {
        "total_events": int(df.shape[0]),
        "recent_event": recent,
        "events_per_day": events_per_day.to_dict(),
        "top_actions": top_actions,
        "departments": departments,
        "unique_users": int(df['user'].nunique()) if 'user' in df.columns else 0
    }
    return analytics

def custom_dashboard_analytics(audit_log_path: str, metrics: List[str]) -> Dict[str, Any]:
    try:
        df = pd.read_csv(audit_log_path)
        if df.empty:
            return {"message": "No data."}
    except Exception:
        return {"message": "Audit log unavailable."}
    result = {}
    for metric in metrics:
        if metric == "daily_active_users":
            if "user" in df.columns:
                result[metric] = df.groupby(df['timestamp'].str[:10])['user'].nunique().to_dict()
        elif metric == "docs_uploaded":
            if "action" in df.columns:
                uploads = df[df['action'].str.contains("upload", case=False)]
                result[metric] = uploads.groupby(uploads['timestamp'].str[:10]).size().to_dict()
        elif metric == "compliance_failures":
            if "status" in df.columns:
                fails = df[df['status'].str.lower() == "failed"]
                result[metric] = fails.groupby(fails['timestamp'].str[:10]).size().to_dict()
    return result

# ---------------------------------------------
# Workflow Orchestration: Swarms for Automation
# ---------------------------------------------
# Compliance Audit Swarm Agent
compliance_audit_swarm = Agent(
    agent_name="ComplianceAuditSwarm",
    agent_description=(
        "Automates evidence collection and reporting for compliance audits. "
        "Ingests uploaded documents, checks against regulatory templates, runs analytics, and composes reports."
    ),
    system_prompt="You are a compliance automation AI agent. For every task, validate document properties, correlate audit log events, summarize regulatory status, and deliver actionable summaries.",
    model_name="gpt-4",
    max_loops=3
)

def run_compliance_audit(task: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Orchestrate full compliance audit via swarms Agent."""
    logger.info(f"Running compliance audit for task: {task}")
    # Generate summary analytics, then use Agent to review and summarize
    analytics = audit_analytics_summary(config['audit_log_path'])
    prompt = (
        f"## COMPLIANCE AUDIT TASK\nTask: {task}\n"
        f"Recent analytics: {json.dumps(analytics)}\nGenerate a summary: identify compliance issues, recent events, and suggested actions."
    )
    try:
        result = compliance_audit_swarm.run(prompt)
        logger.info("Swarm audit complete.")
        # Append result to audit log
        audit_event = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "action": "swarm_audit",
            "details": result,
            "user": "system",
            "department": "compliance",
            "status": "complete"
        }
        append_audit_log(audit_event, config['audit_log_path'])
        return {"audit_summary": result, "analytics": analytics}
    except Exception as exc:
        logger.error(f"Swarm audit failed: {exc}")
        return {"error": str(exc)}

# ---------------------------------------------
# API Web Server (HTTPServer, Threaded)
# ---------------------------------------------
class ComplianceHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "CompliCloud/1.1"
    config = DEFAULT_CONFIG.copy()

    def _send_json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, default=str).encode("utf-8"))

    def _parse_json_body(self) -> Optional[Dict[str, Any]]:
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length:
                body = self.rfile.read(content_length)
                return json.loads(body.decode("utf-8"))
        except Exception as exc:
            logger.error(f"JSON body parse failed: {exc}")
        return None

    def do_GET(self):
        client_ip = self.client_address[0]
        if not check_auth(self.headers, client_ip):
            self._send_json({"error": "Unauthorized"}, 401)
            return
        if self.path == "/status":
            self._send_json({"status": "ok", "time": datetime.datetime.utcnow().isoformat()})
        elif self.path == "/documents":
            docs = list_documents(self.config)
            self._send_json({"documents": docs})
        elif self.path == "/audit/analytics":
            result = audit_analytics_summary(self.config['audit_log_path'])
            self._send_json(result)
        elif self.path.startswith("/dashboard/analytics"):
            # e.g., /dashboard/analytics?metrics=daily_active_users,docs_uploaded
            query = self.path.split("?")[-1] if "?" in self.path else ""
            metrics = []
            for q in query.split("&"):
                if q.startswith("metrics="):
                    metrics = q[len("metrics="):].split(",")
            result = custom_dashboard_analytics(self.config['audit_log_path'], metrics)
            self._send_json(result)
        else:
            self._send_json({"error": "Unknown GET endpoint."}, 404)

    def do_POST(self):
        client_ip = self.client_address[0]
        if not check_auth(self.headers, client_ip):
            self._send_json({"error": "Unauthorized"}, 401)
            return
        if self.path == "/documents/upload":
            body = self._parse_json_body()
            if not body or 'filename' not in body or 'content_b64' not in body:
                self._send_json({"error": "Missing filename or base64 content."}, 400)
                return
            try:
                content_bin = base64.b64decode(body['content_b64'])
                save_path = save_document(content_bin, body['filename'], self.config)
                audit_event = {
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                    "action": "upload_document",
                    "user": body.get('user', 'api'),
                    "department": body.get('department', 'unknown'),
                    "filename": body['filename'],
                    "status": "uploaded"
                }
                append_audit_log(audit_event, self.config['audit_log_path'])
                self._send_json({"status": "uploaded", "path": save_path})
            except Exception as exc:
                logger.error(f"Upload failed: {exc}")
                self._send_json({"error": str(exc)}, 500)
        elif self.path == "/documents/delete":
            body = self._parse_json_body()
            if not body or 'filename' not in body:
                self._send_json({"error": "Missing filename."}, 400)
                return
            ok = delete_document(body['filename'], self.config)
            status = "deleted" if ok else "not_found"
            audit_event = {
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "action": "delete_document",
                "user": body.get('user', 'api'),
                "department": body.get('department', 'unknown'),
                "filename": body['filename'],
                "status": status
            }
            append_audit_log(audit_event, self.config['audit_log_path'])
            self._send_json({"status": status})
        elif self.path == "/regulatory/report":
            body = self._parse_json_body() or {}
            template = body.get("template", "SOX")
            path, df = generate_regulatory_report(template, self.config)
            summary = df.describe(include='all').to_dict() if not df.empty else {}
            self._send_json({"report_path": path, "summary": summary})
        elif self.path == "/audit/run":
            body = self._parse_json_body() or {}
            task = body.get("task", "Full compliance audit")
            result = run_compliance_audit(task, self.config)
            self._send_json(result)
        else:
            self._send_json({"error": "Unknown POST endpoint."}, 404)

    def log_message(self, format, *args):
        # Suppress http.server base logging, log via loguru instead
        logger.info("%s - %s" % (self.client_address[0], format%args))

# ---------------------------------------------
# Main Entrypoint
# ---------------------------------------------
def serve_compliance_api(port: int, config_path: str):
    config = _load_config(config_path)
    ComplianceHTTPRequestHandler.config = config
    server = HTTPServer(("0.0.0.0", port), ComplianceHTTPRequestHandler)
    logger.info(f"CompliCloud API running on port {port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down server.")
        server.server_close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CompliCloud Compliance Automation API")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to serve API")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_PATH, help="Path to config.json")
    args = parser.parse_args()
    logger.add(sys.stderr, level="INFO")
    serve_compliance_api(args.port, args.config)
