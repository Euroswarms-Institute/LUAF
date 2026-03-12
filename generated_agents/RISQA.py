import os
import sys
import time
import json
import argparse
import tempfile
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, timedelta
import threading

import pandas as pd
import numpy as np
import requests
import httpx
from loguru import logger
from swarms import Agent
from apscheduler.schedulers.background import BackgroundScheduler
import schedule

# --- Config and Environment ---
API_PUBLIC_SOURCES = [
    "https://api.riskiq.com/v1/vendors",
    "https://api.securityscorecard.io/companies",
    "https://cve.circl.lu/api/last",
]
VENDOR_LIST = [
    "aws.amazon.com",
    "azure.microsoft.com",
    "cloud.google.com",
    "salesforce.com",
    "workday.com",
]
PROPRIETARY_API_KEY = os.environ.get("PROPRIETARY_RISK_API_KEY")
PROPRIETARY_API_URL = os.environ.get("PROPRIETARY_RISK_API_URL", "https://proprietary-risk.intelligence.api/v1/vendors")

REPORT_SAVE_PATH = os.environ.get("RISK_REPORT_PATH", tempfile.gettempdir())
MAX_RETRIES = int(os.environ.get("MAX_HTTP_RETRIES", 3))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", 45))
MODEL_NAME = os.environ.get("LLM_MODEL", "gpt-4o-mini")
MAX_LOOPS = int(os.environ.get("MAX_LOOPS", 5))
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", 60))
BILLING_RATE_PER_VENDOR = float(os.environ.get("BILLING_RATE_PER_VENDOR", "6.75"))  # USD per data refresh

# --- Data Structures ---
@dataclass
class VendorRiskRaw:
    vendor_name: str
    source: str
    risk_factors: Dict[str, Any]
    raw_score: Optional[float] = None
    last_update: Optional[str] = None

@dataclass
class VendorRiskClean:
    vendor_name: str
    compliance_flags: List[str]
    threat_level: str
    risk_score: float
    last_update: str
    sources: List[str] = field(default_factory=list)

@dataclass
class BillingEvent:
    vendor_name: str
    timestamp: str
    amount: float
    event_type: str = "refresh"
    context: Optional[Dict[str, Any]] = field(default_factory=dict)

# --- Helper Functions ---
def get_with_retries(url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None, max_retries: int = MAX_RETRIES, timeout: int = HTTP_TIMEOUT) -> Optional[requests.Response]:
    """HTTP GET with retry logic and logging."""
    delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"[HTTP] GET {url} attempt {attempt}")
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as ex:
            logger.warning(f"HTTP GET failed (attempt {attempt}): {ex}")
            if attempt == max_retries:
                logger.error(f"Final HTTP GET failure for {url}")
                return None
            time.sleep(delay)
            delay *= 2
    return None

def get_with_retries_httpx(url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None, max_retries: int = MAX_RETRIES, timeout: int = HTTP_TIMEOUT) -> Optional[httpx.Response]:
    delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"[HTTPX] GET {url} attempt {attempt}")
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                return resp
        except httpx.HTTPError as ex:
            logger.warning(f"HTTPX GET failed (attempt {attempt}): {ex}")
            if attempt == max_retries:
                logger.error(f"Final HTTPX GET failure for {url}")
                return None
            time.sleep(delay)
            delay *= 2
    return None

def fetch_vendor_risk_from_public(vendor: str) -> List[VendorRiskRaw]:
    """Aggregate vendor risk data from public sources."""
    results = []
    for url in API_PUBLIC_SOURCES:
        full_url = f"{url}?vendor={vendor}"
        resp = get_with_retries(full_url)
        if not resp:
            continue
        try:
            data = resp.json()
        except json.JSONDecodeError as ex:
            logger.error(f"Failed to decode JSON from {full_url}: {ex}")
            continue
        parsed = parse_public_risk(vendor, url, data)
        if parsed:
            results.append(parsed)
    return results

def fetch_vendor_risk_from_proprietary(vendor: str) -> Optional[VendorRiskRaw]:
    if not PROPRIETARY_API_KEY:
        logger.warning("No proprietary API key present; skipping proprietary risk source.")
        return None
    headers = {"Authorization": f"Bearer {PROPRIETARY_API_KEY}"}
    params = {"vendor": vendor}
    resp = get_with_retries_httpx(PROPRIETARY_API_URL, headers=headers, params=params)
    if not resp:
        return None
    try:
        data = resp.json()
        return parse_proprietary_risk(vendor, data)
    except Exception as ex:
        logger.error(f"Failed to parse proprietary risk data: {ex}")
        return None

def parse_public_risk(vendor: str, source: str, data: Any) -> Optional[VendorRiskRaw]:
    try:
        if "companies" in source or "vendors" in source:
            # Standard structure: {"vendor": ..., "factors": {...}}
            risk_factors = data.get("riskFactors") or data.get("factors") or {}
            last_update = data.get("lastUpdate") or datetime.utcnow().isoformat()
            return VendorRiskRaw(
                vendor_name=vendor,
                source=source,
                risk_factors=risk_factors,
                raw_score=data.get("score") or None,
                last_update=last_update,
            )
        elif "cve" in source:
            # Assume data is a list of CVEs
            threat_count = sum(1 for cve in data if vendor.lower() in cve.get("summary", "").lower())
            return VendorRiskRaw(
                vendor_name=vendor,
                source=source,
                risk_factors={"cve_count": threat_count},
                raw_score=None,
                last_update=datetime.utcnow().isoformat(),
            )
        else:
            logger.warning(f"Unrecognized public risk data structure for source {source}")
            return None
    except Exception as ex:
        logger.error(f"Error parsing public risk data for {vendor} from {source}: {ex}")
        return None

def parse_proprietary_risk(vendor: str, data: Any) -> Optional[VendorRiskRaw]:
    try:
        risk_factors = data.get("riskMetrics") or {}
        return VendorRiskRaw(
            vendor_name=vendor,
            source="proprietary",
            risk_factors=risk_factors,
            raw_score=data.get("score"),
            last_update=data.get("timestamp", datetime.utcnow().isoformat()),
        )
    except Exception as ex:
        logger.error(f"Failed to parse proprietary data: {ex}")
        return None

def cleanse_and_score_risk(risks: List[VendorRiskRaw]) -> Optional[VendorRiskClean]:
    if not risks:
        return None
    try:
        # DataFrame to normalize/aggregate
        df = pd.DataFrame([
            {"vendor": r.vendor_name, "score": r.raw_score or np.nan, **r.risk_factors} for r in risks
        ])
        # Normalize risk factors, fill NA
        if "score" in df:
            scores = df["score"].astype(float).fillna(df["score"].mean())
        else:
            scores = np.repeat(0.5, len(risks))
        cve_count = df.get("cve_count", pd.Series([0]*len(risks))).astype(int)
        # Simple compliance inference
        compliance_flags = []
        if df.columns.intersection(["soc2", "iso27001", "hipaa", "pci", "gdpr"]).any():
            for v in ["soc2", "iso27001", "hipaa", "pci", "gdpr"]:
                if df.get(v, pd.Series([False]*len(risks))).any():
                    compliance_flags.append(v.upper())
        # Threat score: higher if more CVEs
        threat_score = min(1.0, np.log1p(cve_count.sum())/5.0)
        threat_level = "High" if threat_score > 0.6 else ("Medium" if threat_score > 0.3 else "Low")
        # Final weighted risk score
        risk_score = float(np.clip(scores.mean()*0.7 + threat_score*0.3, 0, 1))
        # Recent update
        last_update = max([r.last_update for r in risks if r.last_update], default=datetime.utcnow().isoformat())
        return VendorRiskClean(
            vendor_name=risks[0].vendor_name,
            compliance_flags=compliance_flags,
            threat_level=threat_level,
            risk_score=risk_score,
            last_update=last_update,
            sources=list(sorted(set(r.source for r in risks))),
        )
    except Exception as ex:
        logger.error(f"Error cleansing/scoring risk for vendor {risks[0].vendor_name}: {ex}")
        return None

def bill_usage_event(vendor_name: str, risk_score: float) -> BillingEvent:
    timestamp = datetime.utcnow().isoformat()
    context = {"risk_score": risk_score}
    event = BillingEvent(
        vendor_name=vendor_name,
        timestamp=timestamp,
        amount=BILLING_RATE_PER_VENDOR,
        context=context,
    )
    logger.info(f"Billing event created: {event}")
    return event

def save_report(cleaned: List[VendorRiskClean], billings: List[BillingEvent], fname_prefix: Optional[str] = None) -> str:
    if fname_prefix is None:
        fname_prefix = "risk_report"
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    fname = f"{fname_prefix}_{ts}.json"
    fpath = os.path.join(REPORT_SAVE_PATH, fname)
    report = {
        "asOf": ts,
        "vendors": [asdict(v) for v in cleaned],
        "billing": [asdict(b) for b in billings],
    }
    try:
        with open(fpath, "w", encoding="utf-8", errors="replace") as f:
            json.dump(report, f, indent=2)
        logger.info(f"Saved risk intelligence report to {fpath}")
        return fpath
    except Exception as ex:
        logger.error(f"Error saving report: {ex}")
        return ""

def aggregate_and_score_all(vendor_list: List[str]) -> Tuple[List[VendorRiskClean], List[BillingEvent]]:
    cleaned: List[VendorRiskClean] = []
    billings: List[BillingEvent] = []
    for vendor in vendor_list:
        logger.info(f"Processing vendor: {vendor}")
        public_risks = fetch_vendor_risk_from_public(vendor)
        proprietary_risk = fetch_vendor_risk_from_proprietary(vendor)
        all_risks = list(public_risks)
        if proprietary_risk:
            all_risks.append(proprietary_risk)
        clean = cleanse_and_score_risk(all_risks)
        if clean:
            cleaned.append(clean)
            billings.append(bill_usage_event(vendor, clean.risk_score))
    return cleaned, billings

# --- Task and Scheduling Logic ---
def periodic_refresh_task() -> str:
    logger.info("Starting periodic vendor risk intelligence refresh task…")
    cleaned, billings = aggregate_and_score_all(VENDOR_LIST)
    report_path = save_report(cleaned, billings, fname_prefix="periodic_risk_report")
    logger.info(f"Periodic refresh complete. {len(cleaned)} vendors. Report: {report_path}")
    return report_path

def schedule_periodic_refresh(minutes: int = REFRESH_MINUTES):
    scheduler = BackgroundScheduler()
    scheduler.add_job(periodic_refresh_task, 'interval', minutes=minutes, next_run_time=datetime.now() + timedelta(seconds=5))
    scheduler.start()
    logger.info(f"Scheduled periodic risk intelligence refresh every {minutes} minutes.")
    return scheduler

def on_demand_risk_report(vendor_list: Optional[List[str]] = None) -> str:
    if vendor_list is None:
        vendor_list = VENDOR_LIST
    cleaned, billings = aggregate_and_score_all(vendor_list)
    report_path = save_report(cleaned, billings, fname_prefix="on_demand_risk_report")
    return report_path

# --- Swarms Agent Definition ---
AGENT_NAME = "RiskIntelligence API Agent"
AGENT_DESC = (
    "Aggregates and updates vendor risk intelligence from public and proprietary sources, cleans/normalizes, computes risk scores, adds compliance/threat annotations, and saves via API or periodic refresh. Revenue via usage-based billing/event log. For B2B API integration. Output: JSON report of vendor risk profiles and billing events."
)
SYSTEM_PROMPT = (
    "You are a backend agent that aggregates, cleans, normalizes, and risk-scores vendor intelligence data from public/proprietary APIs."
    " Given input vendor(s), fetch latest data from multiple APIs (public security, proprietary sources), reconcile records, compute risk and compliance scores (SOC2/ISO27001/PCI etc), annotate threat levels, and output a JSON risk report. Billing events should be created per processed vendor."
    " Always handle HTTP/network errors, fail with clear error messages, and never emit partial or corrupt results. Ensure data freshness. If asked to refresh data, perform the periodic fetch, normalization, risk scoring, and save the report. Only output valid JSON or error details."
)

risk_agent = Agent(
    agent_name=AGENT_NAME,
    agent_description=AGENT_DESC,
    system_prompt=SYSTEM_PROMPT,
    model_name=MODEL_NAME,
    max_loops=MAX_LOOPS,
)

def agent_task(task: str = "periodic_refresh", vendors: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Entry for LLM-driven or external invocations. Task can be 'periodic_refresh' or 'on_demand'.
    """
    try:
        if task == "periodic_refresh":
            report_path = periodic_refresh_task()
            return {"status": "success", "report": report_path}
        elif task == "on_demand":
            vendor_list = vendors if vendors is not None else VENDOR_LIST
            report_path = on_demand_risk_report(vendor_list)
            return {"status": "success", "report": report_path}
        else:
            logger.error(f"Unknown agent task: {task}")
            return {"status": "error", "error": "Unknown task"}
    except Exception as ex:
        logger.error(f"Agent task failed: {ex}")
        return {"status": "error", "error": str(ex)}

# --- Entrypoint ---
def main():
    parser = argparse.ArgumentParser(description="RiskIntelligence API Agent")
    parser.add_argument("--task", type=str, default="periodic_refresh", help="Task: periodic_refresh or on_demand")
    parser.add_argument("--vendors", type=str, default="", help="Comma-separated list of vendor domains for on-demand report")
    parser.add_argument("--schedule", action="store_true", default=False, help="Enable periodic scheduling via apscheduler")
    parser.add_argument("--refresh-minutes", type=int, default=REFRESH_MINUTES, help="Periodic refresh interval (minutes)")
    args = parser.parse_args()

    if args.schedule:
        scheduler = schedule_periodic_refresh(minutes=args.refresh_minutes)
        logger.info("Periodic risk refresh enabled. Ctrl+C to exit.")
        try:
            while True:
                time.sleep(180)
        except KeyboardInterrupt:
            scheduler.shutdown()
            logger.info("Scheduler stopped.")
        sys.exit(0)
    if args.task == "on_demand":
        vendors = [v.strip() for v in args.vendors.split(",") if v.strip()]
        if not vendors:
            vendors = VENDOR_LIST
        result = agent_task("on_demand", vendors)
    else:
        result = agent_task("periodic_refresh")

    agent_response = risk_agent.run(json.dumps(result))
    print(json.dumps({"agent_response": agent_response, "result": result}, indent=2))

if __name__ == "__main__":
    main()
