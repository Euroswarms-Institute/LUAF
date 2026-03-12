import os
import sys
import tempfile
import time
import json
import argparse
from typing import List, Dict, Optional, Any, Union, Set
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
import requests
import httpx
import numpy as np
import pandas as pd
from ddgs import DDGS
from swarms import Agent
from loguru import logger

# ==== Configuration ====

def get_env_var(key: str, default: Optional[str] = None) -> str:
    val = os.environ.get(key)
    if val is not None:
        return val
    if default is not None:
        return default
    raise EnvironmentError(f"Required environment variable {key} not set.")

MARINETRAFFIC_API_KEY = os.environ.get("MARINETRAFFIC_API_KEY")  # For ship location, congestion, and port data
VESSELFINDER_API_KEY = os.environ.get("VESSELFINDER_API_KEY")
CARGO_METADATA_API_KEY = os.environ.get("CARGO_METADATA_API_KEY")  # Placeholder for real vendor
default_model_name = os.environ.get("MODEL_NAME", "gpt-4o-mini")
default_max_loops = int(os.environ.get("MAX_LOOPS", "5"))

# ==== Data Structures ====

@dataclass
class VesselInfo:
    mmsi: str
    imo: Optional[str]
    name: Optional[str]
    type: Optional[str]
    latitude: float
    longitude: float
    speed: Optional[float]
    course: Optional[float]
    status: Optional[str]
    timestamp: datetime
    destination: Optional[str]
    eta: Optional[datetime]
    flag: Optional[str]

@dataclass
class PortCongestion:
    port_name: str
    country: str
    waiting_vessels: int
    berthed_vessels: int
    avg_wait_time: Optional[float]  # hours
    timestamp: datetime

@dataclass
class CargoManifest:
    mmsi: str
    cargo_type: str
    quantity: float
    units: str
    hazardous: Optional[bool]
    destination: Optional[str]
    last_update: datetime

# ==== Utility Functions ====

def retry_request(
    fn,
    max_attempts: int = 3,
    initial_backoff: float = 2.0,
    allowed_exceptions: tuple = (requests.RequestException, httpx.RequestError,)
):
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except allowed_exceptions as e:
            logger.warning(f"Attempt {attempt} failed: {e}")
            last_err = e
            time.sleep(initial_backoff * attempt)
    logger.error(f"All {max_attempts} attempts failed.")
    raise last_err

# ==== API Integrations ====

# --- MarineTraffic Vessel Positions API (uses API key) ---
def fetch_marinetraffic_vessels(
    apikey: str,
    bbox: Optional[str] = None,
    fleet_ids: Optional[List[str]] = None,
    timeout: int = 60
) -> List[Dict[str, Any]]:
    """
    Fetch vessel positions from MarineTraffic API.
    bbox: bounding box as 'minLat,minLon,maxLat,maxLon' (optional)
    fleet_ids: list of fleet IDs (optional)
    Returns list of vessel dicts.
    """
    def _call():
        base_url = "https://services.marinetraffic.com/api/exportvessels/v:8/"
        params = {
            "protocol": "json",
            "msgtype": "extended",
            "timespan": 60,  # last 60 min
            "apikey": apikey,
        }
        if bbox:
            params["bbox"] = bbox
        if fleet_ids:
            params["fleet_id"] = ",".join(fleet_ids)
        resp = requests.get(base_url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    return retry_request(_call, max_attempts=3)

# --- VesselFinder Vessel Positions API ---
def fetch_vesselfinder_vessels(
    apikey: str,
    area: Optional[str] = None,
    timeout: int = 60
) -> List[Dict[str, Any]]:
    """
    Fetch vessel positions from VesselFinder API.
    area: name or code for area (optional)
    Return list of vessel dicts.
    """
    def _call():
        base_url = "https://api.vesselfinder.com/vessels"
        params = {
            "apikey": apikey,
            "format": "json",
        }
        if area:
            params["area"] = area
        resp = requests.get(base_url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    return retry_request(_call, max_attempts=3)

# --- Cargo Metadata API (Placeholder for API pattern) ---
def fetch_cargo_manifest(
    apikey: str,
    mmsi: str,
    timeout: int = 60
) -> List[Dict[str, Any]]:
    """
    Fetch cargo manifest metadata for a given vessel (by MMSI).
    """
    def _call():
        base_url = "https://api.globalcargodata.com/manifest"
        params = {
            "apikey": apikey,
            "mmsi": mmsi
        }
        resp = requests.get(base_url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("data", [])
    return retry_request(_call, max_attempts=3)

# --- Port Congestion ---
def fetch_port_congestion_marinetraffic(
    apikey: str,
    port_code: Optional[str] = None,
    timeout: int = 60
) -> List[Dict[str, Any]]:
    """
    Fetch port congestion stats from MarineTraffic.
    """
    def _call():
        base_url = "https://services.marinetraffic.com/api/portcongestion/v:2/"
        params = {
            "apikey": apikey,
            "protocol": "json"
        }
        if port_code:
            params["port_code"] = port_code
        resp = requests.get(base_url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    return retry_request(_call, max_attempts=3)

# ==== Normalization and Analytics Functions ====

def normalize_vessel(raw: Dict[str, Any]) -> VesselInfo:
    def parse_time(val: Any) -> Optional[datetime]:
        if val is None:
            return None
        try:
            # Support ISO8601 or unix timestamp
            if isinstance(val, (int, float)):
                return datetime.utcfromtimestamp(val)
            return datetime.fromisoformat(val)
        except Exception:
            return None
    return VesselInfo(
        mmsi=str(raw.get("MMSI") or raw.get("mmsi")),
        imo=str(raw.get("IMO") or raw.get("imo")) if raw.get("IMO") or raw.get("imo") else None,
        name=raw.get("SHIPNAME") or raw.get("name"),
        type=raw.get("SHIPTYPE") or raw.get("type"),
        latitude=float(raw.get("LAT", raw.get("latitude", 0.0))),
        longitude=float(raw.get("LON", raw.get("longitude", 0.0))),
        speed=float(raw.get("SPEED", raw.get("speed", 0.0))) if raw.get("SPEED") or raw.get("speed") else None,
        course=float(raw.get("COURSE", raw.get("course", 0.0))) if raw.get("COURSE") or raw.get("course") else None,
        status=raw.get("STATUS") or raw.get("status"),
        timestamp=parse_time(raw.get("TIMESTAMP") or raw.get("timestamp")),
        destination=raw.get("DESTINATION") or raw.get("destination"),
        eta=parse_time(raw.get("ETA") or raw.get("eta")),
        flag=raw.get("FLAG") or raw.get("flag")
    )

def normalize_port_congestion(raw: Dict[str, Any]) -> PortCongestion:
    def parse_time(val: Any) -> datetime:
        if val is None:
            return datetime.utcnow()
        try:
            if isinstance(val, (int, float)):
                return datetime.utcfromtimestamp(val)
            return datetime.fromisoformat(val)
        except Exception:
            return datetime.utcnow()
    return PortCongestion(
        port_name=raw.get("PORTNAME") or raw.get("port_name", "UNKNOWN"),
        country=raw.get("COUNTRY") or raw.get("country", "UNKNOWN"),
        waiting_vessels=int(raw.get("WAITING") or raw.get("waiting_vessels", 0)),
        berthed_vessels=int(raw.get("BERTHED") or raw.get("berthed_vessels", 0)),
        avg_wait_time=float(raw.get("AVG_WAIT_H") or raw.get("avg_wait_time", 0.0)),
        timestamp=parse_time(raw.get("TIMESTAMP") or raw.get("timestamp"))
    )

def normalize_cargo_manifest(raw: Dict[str, Any]) -> CargoManifest:
    def parse_time(val: Any) -> datetime:
        if val is None:
            return datetime.utcnow()
        try:
            if isinstance(val, (int, float)):
                return datetime.utcfromtimestamp(val)
            return datetime.fromisoformat(val)
        except Exception:
            return datetime.utcnow()
    return CargoManifest(
        mmsi=str(raw.get("MMSI") or raw.get("mmsi")),
        cargo_type=raw.get("cargo_type", "UNKNOWN"),
        quantity=float(raw.get("quantity", 0)),
        units=raw.get("units", "UNKNOWN"),
        hazardous=bool(raw.get("hazardous", False)),
        destination=raw.get("destination"),
        last_update=parse_time(raw.get("last_update"))
    )

# ==== Aggregation Logic ====

def aggregate_vessel_data(
    vessel_sources: List[List[Dict[str, Any]]]
) -> List[VesselInfo]:
    """
    Deduplicate and aggregate vessel info from multiple sources by MMSI.
    """
    seen_mmsi: Set[str] = set()
    vessels: List[VesselInfo] = []
    for source in vessel_sources:
        for raw in source:
            try:
                v = normalize_vessel(raw)
                if v.mmsi not in seen_mmsi:
                    vessels.append(v)
                    seen_mmsi.add(v.mmsi)
            except Exception as e:
                logger.warning(f"Failed to normalize vessel: {e}")
    return vessels

def aggregate_port_congestion(sources: List[List[Dict[str, Any]]]) -> List[PortCongestion]:
    out: List[PortCongestion] = []
    seen_names: Set[str] = set()
    for source in sources:
        for raw in source:
            try:
                pc = normalize_port_congestion(raw)
                k = f"{pc.port_name}_{pc.country}"
                if k not in seen_names:
                    out.append(pc)
                    seen_names.add(k)
            except Exception as e:
                logger.warning(f"Failed to normalize port congestion: {e}")
    return out

def aggregate_cargo_manifests(lists: List[List[Dict[str, Any]]]) -> List[CargoManifest]:
    out: List[CargoManifest] = []
    seen_mmsi: Set[str] = set()
    for items in lists:
        for item in items:
            try:
                cm = normalize_cargo_manifest(item)
                if cm.mmsi not in seen_mmsi:
                    out.append(cm)
                    seen_mmsi.add(cm.mmsi)
            except Exception as e:
                logger.warning(f"Failed to normalize cargo manifest: {e}")
    return out

# ==== Analytics and Data Export ====

def vessels_to_dataframe(vessels: List[VesselInfo]) -> pd.DataFrame:
    data = [asdict(v) for v in vessels]
    return pd.DataFrame(data)

def portcongestion_to_dataframe(congestion: List[PortCongestion]) -> pd.DataFrame:
    return pd.DataFrame([asdict(x) for x in congestion])

def cargomanifest_to_dataframe(manifests: List[CargoManifest]) -> pd.DataFrame:
    return pd.DataFrame([asdict(x) for x in manifests])

def detect_anomalies(df: pd.DataFrame, key_fields: List[str], numerical_fields: List[str]) -> List[str]:
    anomalies = []
    if df.empty:
        return anomalies
    for nf in numerical_fields:
        if nf not in df:
            continue
        vals = df[nf].dropna().values
        if len(vals) < 3:
            continue
        mean = np.mean(vals)
        std = np.std(vals)
        for idx, v in enumerate(vals):
            if std == 0:
                continue
            if abs(v - mean) > 3 * std:
                key = ','.join(str(df.iloc[idx][k]) for k in key_fields if k in df)
                anomalies.append(f"Anomalous {nf} for {key}: {v}")
    return anomalies

# ==== Web Search Helper (as external context) ====

def search_supply_chain_news(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    out = []
    try:
        ddgs = DDGS()
        for r in ddgs.text(query, max_results=max_results):
            out.append(r)
    except Exception as e:
        logger.error(f"DDGS search error: {e}")
    return out

# ==== LLM Prompt Generation ====

def compose_system_prompt() -> str:
    return (
        "You are a defense-grade logistics intelligence agent. "
        "Aggregate, normalize, and deliver real-time global shipping, vessel, port, and cargo data. "
        "Input is a JSON request with region, ports, and cargo types of interest. "
        "Fetch and deduplicate data from MarineTraffic, VesselFinder, cargo manifest APIs. "
        "Output a well-structured JSON containing: "
        "vessel_locations (list of dict), port_congestion (list), cargo_metadata (list). "
        "Detect and flag potential anomalies or threats (e.g., suspicious cargo, erratic routing, unusual wait times). "
        "Strictly machine-readable JSON. No explanations or commentary. "
        "Request should include the source API data as provenance. "
        "If a sub-request fails, include error detail in a 'errors' field, but do not abort processing. "
        "Use the following format: {'vessel_locations': [...], 'port_congestion': [...], 'cargo_metadata': [...], 'anomalies': [...], 'errors': {...}}"
    )

# ==== Input Validation ====

def validate_task_input(task: Any) -> Dict[str, Any]:
    if isinstance(task, str):
        try:
            task = json.loads(task)
        except Exception:
            logger.error("Input task must be JSON-parseable string or dict.")
            raise ValueError("Invalid JSON input.")
    if not isinstance(task, dict):
        raise ValueError("Task must be a dict.")
    # region: bounding box string, ports: list of port codes/names, cargo_types: list
    for k in ("region", "ports", "cargo_types"):
        if k not in task:
            raise ValueError(f"Task JSON must contain '{k}' field.")
    return task

# ==== Main Agent Run Logic ====

def run_logistics_aggregation(task: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    logger.info("Starting logistics aggregation task.")
    # 1. Validate and parse input
    try:
        params = validate_task_input(task)
    except Exception as e:
        logger.error(f"Task input validation failure: {e}")
        return {"errors": {"input": str(e)}}
    region = params['region']
    ports = params['ports']  # List[str]
    cargo_types = params['cargo_types']  # List[str]
    
    vessel_data = []
    vesselfinder_data = []
    cargo_data = []
    port_congestion_data = []
    errors = {}
    
    # 2. Fetch vessel positions from MarineTraffic
    if MARINETRAFFIC_API_KEY:
        try:
            vessel_data = fetch_marinetraffic_vessels(MARINETRAFFIC_API_KEY, bbox=region)
        except Exception as e:
            logger.error(f"MarineTraffic fetch failed: {e}")
            errors['marinetraffic'] = str(e)
    else:
        errors['marinetraffic'] = 'No API key provided.'
    # 3. Fetch vessel positions from VesselFinder
    if VESSELFINDER_API_KEY:
        try:
            vesselfinder_data = fetch_vesselfinder_vessels(VESSELFINDER_API_KEY, area=region)
        except Exception as e:
            logger.error(f"VesselFinder fetch failed: {e}")
            errors['vesselfinder'] = str(e)
    else:
        errors['vesselfinder'] = 'No API key provided.'
    # 4. Fetch cargo manifests for all visible vessels (up to N=50)
    if CARGO_METADATA_API_KEY:
        mmsis = set([
            v.get("MMSI") or v.get("mmsi")
            for v in vessel_data[:25] + vesselfinder_data[:25]
            if v.get("MMSI") or v.get("mmsi")
        ])
        for mmsi in mmsis:
            try:
                cargo = fetch_cargo_manifest(CARGO_METADATA_API_KEY, mmsi)
                cargo_data.extend(cargo)
            except Exception as e:
                logger.warning(f"Cargo manifest fetch for {mmsi} failed: {e}")
    else:
        errors['cargo_manifest'] = 'No API key provided.'
    # 5. Fetch port congestion info for all listed ports
    for port in ports:
        try:
            portc = fetch_port_congestion_marinetraffic(MARINETRAFFIC_API_KEY, port_code=port)
            port_congestion_data.extend(portc)
        except Exception as e:
            logger.warning(f"Port congestion fetch failed for {port}: {e}")
    # 6. Aggregate and normalize all data
    vessels = aggregate_vessel_data([vessel_data, vesselfinder_data])
    port_cong = aggregate_port_congestion([port_congestion_data])
    cargo_manifests = aggregate_cargo_manifests([cargo_data])
    # 7. Analytical processing (anomaly detection)
    vessel_df = vessels_to_dataframe(vessels)
    portc_df = portcongestion_to_dataframe(port_cong)
    cargo_df = cargomanifest_to_dataframe(cargo_manifests)
    anomalies = []
    anomalies.extend(detect_anomalies(vessel_df, ["mmsi", "name"], ["speed"]))
    anomalies.extend(detect_anomalies(portc_df, ["port_name", "country"], ["avg_wait_time"]))
    # Check for hazardous or suspicious cargo
    if not cargo_df.empty and "hazardous" in cargo_df:
        for idx, row in cargo_df.iterrows():
            if row.get("hazardous", False):
                anomalies.append(f"Hazardous cargo detected on vessel {row['mmsi']} ({row['cargo_type']}, {row['quantity']} {row['units']})")
    # Packaging output
    result = {
        "vessel_locations": [asdict(v) for v in vessels],
        "port_congestion": [asdict(c) for c in port_cong],
        "cargo_metadata": [asdict(m) for m in cargo_manifests],
        "anomalies": anomalies,
        "errors": errors
    }
    logger.info(f"Task completed. Total vessels: {len(vessels)}, ports: {len(port_cong)}, cargo: {len(cargo_manifests)}, anomalies: {len(anomalies)}")
    return result

# ==== Swarms Agent Definition ====
logistics_agent = Agent(
    agent_name="LogisticsIntel Aggregator",
    agent_description="Aggregates, normalizes, and delivers real-time global shipping and supply chain data from multiple APIs for military logistics and threat detection.",
    system_prompt=compose_system_prompt(),
    model_name=default_model_name,
    max_loops=default_max_loops
)

# ==== Entrypoint ====
def main():
    parser = argparse.ArgumentParser(
        description="Aggregates and delivers real-time shipping and supply chain data for defense logistics planning."
    )
    parser.add_argument("--region", type=str, default="-180,-90,180,90", help="Bounding box region: minLat,minLon,maxLat,maxLon (default: global)")
    parser.add_argument("--ports", type=str, default="", help="Comma-separated list of port codes/names of interest.")
    parser.add_argument("--cargo_types", type=str, default="", help="Comma-separated cargo types (e.g., hazardous, military, perishable)")
    parser.add_argument("--task_file", type=str, default="", help="Optional path to JSON file containing full task spec.")
    args = parser.parse_args()
    if args.task_file:
        with open(args.task_file, encoding="utf-8", errors="replace") as f:
            task_json = json.load(f)
            result = logistics_agent.run(task_json)
    else:
        region = args.region
        ports = [p.strip() for p in args.ports.split(",") if p.strip()] if args.ports else ["SGSIN", "USLAX", "CNSHA"]
        cargo_types = [c.strip() for c in args.cargo_types.split(",") if c.strip()] if args.cargo_types else ["hazardous", "military"]
        task = {
            "region": region,
            "ports": ports,
            "cargo_types": cargo_types
        }
        result = logistics_agent.run(task)
    logger.info("LogisticsIntel Aggregator result:")
    print(json.dumps(result, default=str, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
