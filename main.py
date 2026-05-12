from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import json
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field
from uuid import uuid4

DATA_DIR = Path(__file__).resolve().parent / "data"
ALLOWED_SUFFIX = ".csv"
REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
RULES_FILE = REPORTS_DIR / "rules.json"
# ---- Configure your dataset names/patterns here ----
def _list_lines() -> List[str]:
    _ensure_data_dir()
    excluded_dirs = {"reports", "__pycache__"}

    return sorted([
        p.name.upper()
        for p in DATA_DIR.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and p.name.lower() not in excluded_dirs
    ])

def _validate_line(line: str) -> str:
    line = (line or "").strip().upper()
    if not line:
        raise HTTPException(status_code=400, detail="line is required")

    available_lines = _list_lines()
    if line not in available_lines:
        raise HTTPException(status_code=404, detail=f"Unknown line: {line}")

    return line

# Single-file signals (exact filenames)
SINGLE_FILE_SIGNALS: Dict[str, str] = {
    "cycle_time": "cycle_time.csv",
    "injection_time": "injection_time.csv",
    "max_injection_pressure": "max_injection_pressure.csv",
    "cooling_time": "cooling_time.csv",
}

# OEE per line signals (exact filenames)
# OEE_LINES = ("TOK", "TOF", "TOE")
# OEE_METRICS = ("availability", "productivity", "quality")

# for line in OEE_LINES:
#     SINGLE_FILE_SIGNALS[f"oee_availability_{line}"] = f"oee_availability_{line}.csv"
#     SINGLE_FILE_SIGNALS[f"oee_productivity_{line}"] = f"oee_productivity_{line}.csv"
#     SINGLE_FILE_SIGNALS[f"oee_quality_{line}"]      = f"oee_quality_{line}.csv"

OEE_METRICS = ("availability", "productivity", "quality")

# Per-cavity signals (prefix pattern: <prefix>_<cavity>.csv)
CAVITY_SIGNALS: Dict[str, str] = {
    "cavity_temperature": "cavity_temperature_",          # cavity_temperature_1.csv ... cavity_temperature_96.csv
    "cavity_pressure": "cavity_pressure_",                # cavity_pressure_1.csv ... cavity_pressure_96.csv
    "hotrunner_cav_temperature": "hotrunner_cav_temperature_",  # hotrunner_cav_temperature_58.csv ... etc.
}

# Optional: declared expected cavity count (used only for docs/validation, not required)
EXPECTED_CAVITIES = 96

class RuleIn(BaseModel):
    name: str = Field(min_length=1)
    line: list[str] = Field(min_length=1)          # multiple lines
    metric: str
    condition: str                                 # "gt" or "lt"
    threshold: float
    holdForSec: int = 0
    email: EmailStr
    enabled: bool = True

class RuleOut(RuleIn):
    id: str
    created_at: str

def _load_rules() -> list[dict]:
    if not RULES_FILE.exists():
        return []
    with RULES_FILE.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def _save_rules(rules: list[dict]) -> None:
    with RULES_FILE.open("w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2)

app = FastAPI(title="Manufacturing CSV Gateway", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------- Helpers --------------------

def _ensure_data_dir() -> None:
    if not DATA_DIR.exists() or not DATA_DIR.is_dir():
        raise RuntimeError(f"Data directory not found: {DATA_DIR}")


def _safe_resolve(line: str, filename: str) -> Path:
    # prevent path traversal and enforce .csv
    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not filename.lower().endswith(ALLOWED_SUFFIX):
        raise HTTPException(status_code=400, detail="Only .csv files are supported")

    line = _validate_line(line)

    base = (DATA_DIR / line).resolve()
    path = (base / filename).resolve()

    # ensure still within the line directory
    if path.parent != base:
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {line}/{filename}")

    return path


def _read_header(path: Path) -> List[str]:
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
    return header or []


def _read_rows(path: Path, offset: int, limit: int) -> List[Dict[str, Any]]:
    if offset < 0 or limit < 1:
        raise HTTPException(status_code=400, detail="offset must be >= 0 and limit must be >= 1")

    rows: List[Dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []

        # Skip offset
        for _ in range(offset):
            try:
                next(reader)
            except StopIteration:
                return []

        for i, row in enumerate(reader):
            if i >= limit:
                break
            rows.append(row)

    return rows


def _list_csv_filenames(line: str) -> List[str]:
    line = _validate_line(line)
    base = DATA_DIR / line
    return sorted([p.name for p in base.iterdir() if p.is_file() and p.suffix.lower() == ALLOWED_SUFFIX])


def _find_cavities_for_prefix(line: str, prefix: str) -> List[int]:
    cavities: List[int] = []
    for name in _list_csv_filenames(line):
        if not name.startswith(prefix) or not name.endswith(".csv"):
            continue
        mid = name[len(prefix):-4]
        if mid.isdigit():
            cavities.append(int(mid))
    return sorted(set(cavities))

def _filename_for_cavity(prefix: str, cavity: int) -> str:
    return f"{prefix}{cavity:02d}.csv"


# -------------------- Core Gateway Endpoints --------------------

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/catalog")
def catalog() -> Dict[str, Any]:
    lines = _list_lines()

    cavity_availability = {
        line: {
            signal: _find_cavities_for_prefix(line, prefix)
            for signal, prefix in CAVITY_SIGNALS.items()
        }
        for line in lines
    }

    return {
        "data_dir": str(DATA_DIR),
        "lines": lines,
        "single_file_signals": SINGLE_FILE_SIGNALS,
        "cavity_signals": {k: f"{v}<cavity>.csv" for k, v in CAVITY_SIGNALS.items()},
        "expected_cavities": EXPECTED_CAVITIES,
        "cavities_detected": cavity_availability,
    }

# -------------------- Single-file signals --------------------

@app.get("/lines/{line}/signals/{signal}/meta")
def signal_meta(line: str, signal: str) -> Dict[str, Any]:
    if signal not in SINGLE_FILE_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown signal: {signal}")

    filename = SINGLE_FILE_SIGNALS[signal]
    path = _safe_resolve(line, filename)
    stat = path.stat()

    return {
        "line": _validate_line(line),
        "signal": signal,
        "filename": filename,
        "size_bytes": stat.st_size,
        "modified_unix": int(stat.st_mtime),
        "columns": _read_header(path),
    }

@app.get("/lines/{line}/signals/{signal}/rows")
def signal_rows(
    line: str,
    signal: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
) -> Dict[str, Any]:
    if signal not in SINGLE_FILE_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown signal: {signal}")

    filename = SINGLE_FILE_SIGNALS[signal]
    path = _safe_resolve(line, filename)

    rows = _read_rows(path, offset=offset, limit=limit)
    return {
        "line": _validate_line(line),
        "signal": signal,
        "filename": filename,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "rows": rows,
    }

@app.get("/lines/{line}/cavities/signals")
def list_cavity_signals(line: str) -> Dict[str, Any]:
    line = _validate_line(line)
    return {
        "line": line,
        "expected_cavities": EXPECTED_CAVITIES,
        "signals": {
            signal: _find_cavities_for_prefix(line, prefix)
            for signal, prefix in CAVITY_SIGNALS.items()
        },
    }
@app.get("/lines/{line}/cavities/{signal}/rows")
def multi_cavity_rows(
    line: str,
    signal: str,
    cavities: str = Query(..., description="Comma-separated cavity numbers, e.g. 1,2,3"),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
    x_key: str = Query("timestamp", description="CSV column to use as x axis"),
) -> Dict[str, Any]:
    line = _validate_line(line)
    if signal not in CAVITY_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown cavity signal: {signal}")

    prefix = CAVITY_SIGNALS[signal]
    cav_list = []
    for part in cavities.split(","):
        part = part.strip()
        if part.isdigit():
            cav_list.append(int(part))
    cav_list = sorted(set([c for c in cav_list if c >= 1]))
    if not cav_list:
        raise HTTPException(status_code=400, detail="No valid cavities provided")

    cav_rows: Dict[int, List[Dict[str, Any]]] = {}
    for cav in cav_list:
        filename = _filename_for_cavity(prefix, cav)
        path = _safe_resolve(line, filename)
        cav_rows[cav] = _read_rows_by_index(path, offset=offset, limit=limit)

    merged: List[Dict[str, Any]] = []
    max_len = max((len(v) for v in cav_rows.values()), default=0)

    for i in range(max_len):
        row_out: Dict[str, Any] = {}

        x_val = None
        for cav in cav_list:
            if i < len(cav_rows[cav]):
                x_val = cav_rows[cav][i].get(x_key)
                if x_val is not None:
                    break
        row_out[x_key] = x_val

        for cav in cav_list:
            key = f"v{cav}"
            val = None
            if i < len(cav_rows[cav]):
                val = cav_rows[cav][i].get("value")
            row_out[key] = val

        merged.append(row_out)

    return {
        "line": line,
        "signal": signal,
        "cavities": cav_list,
        "offset": offset,
        "limit": limit,
        "returned": len(merged),
        "x_key": x_key,
        "rows": merged,
    }
@app.get("/lines/{line}/cavities/{signal}/{cavity}/meta")
def cavity_signal_meta(line: str, signal: str, cavity: int) -> Dict[str, Any]:
    line = _validate_line(line)
    if signal not in CAVITY_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown cavity signal: {signal}")
    if cavity < 1:
        raise HTTPException(status_code=400, detail="cavity must be >= 1")

    prefix = CAVITY_SIGNALS[signal]
    filename = _filename_for_cavity(prefix, cavity)
    path = _safe_resolve(line, filename)
    stat = path.stat()

    return {
        "line": line,
        "signal": signal,
        "cavity": cavity,
        "filename": filename,
        "size_bytes": stat.st_size,
        "modified_unix": int(stat.st_mtime),
        "columns": _read_header(path),
    }


@app.get("/lines/{line}/cavities/{signal}/{cavity}/rows")
def cavity_signal_rows(
    line: str,
    signal: str,
    cavity: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
) -> Dict[str, Any]:
    line = _validate_line(line)
    if signal not in CAVITY_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown cavity signal: {signal}")
    if cavity < 1:
        raise HTTPException(status_code=400, detail="cavity must be >= 1")

    prefix = CAVITY_SIGNALS[signal]
    filename = _filename_for_cavity(prefix, cavity)
    path = _safe_resolve(line, filename)

    rows = _read_rows(path, offset=offset, limit=limit)
    return {
        "line": line,
        "signal": signal,
        "cavity": cavity,
        "filename": filename,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "rows": rows,
    }

@app.get("/signals/{signal}/meta")
def signal_meta(signal: str) -> Dict[str, Any]:
    if signal not in SINGLE_FILE_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown signal: {signal}")

    filename = SINGLE_FILE_SIGNALS[signal]
    path = _safe_resolve(filename)
    stat = path.stat()

    return {
        "signal": signal,
        "filename": filename,
        "size_bytes": stat.st_size,
        "modified_unix": int(stat.st_mtime),
        "columns": _read_header(path),
    }


@app.get("/signals/{signal}/rows")
def signal_rows(
    signal: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
) -> Dict[str, Any]:
    if signal not in SINGLE_FILE_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown signal: {signal}")

    filename = SINGLE_FILE_SIGNALS[signal]
    path = _safe_resolve(filename)

    rows = _read_rows(path, offset=offset, limit=limit)
    return {
        "signal": signal,
        "filename": filename,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "rows": rows,
    }


# -------------------- Per-cavity signals --------------------

@app.get("/cavities/signals")
def list_cavity_signals() -> Dict[str, Any]:
    """
    Lists available cavities for each cavity-based signal (temperature/pressure/hotrunner temp).
    """
    return {
        "expected_cavities": EXPECTED_CAVITIES,
        "signals": {
            signal: _find_cavities_for_prefix(prefix)
            for signal, prefix in CAVITY_SIGNALS.items()
        },
    }


@app.get("/cavities/{signal}/{cavity}/meta")
def cavity_signal_meta(signal: str, cavity: int) -> Dict[str, Any]:
    if signal not in CAVITY_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown cavity signal: {signal}")
    if cavity < 1:
        raise HTTPException(status_code=400, detail="cavity must be >= 1")

    prefix = CAVITY_SIGNALS[signal]
    filename = _filename_for_cavity(prefix, cavity)
    path = _safe_resolve(filename)
    stat = path.stat()

    return {
        "signal": signal,
        "cavity": cavity,
        "filename": filename,
        "size_bytes": stat.st_size,
        "modified_unix": int(stat.st_mtime),
        "columns": _read_header(path),
    }


@app.get("/cavities/{signal}/{cavity}/rows")
def cavity_signal_rows(
    signal: str,
    cavity: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
) -> Dict[str, Any]:
    if signal not in CAVITY_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown cavity signal: {signal}")
    if cavity < 1:
        raise HTTPException(status_code=400, detail="cavity must be >= 1")

    prefix = CAVITY_SIGNALS[signal]
    filename = _filename_for_cavity(prefix, cavity)
    path = _safe_resolve(filename)

    rows = _read_rows(path, offset=offset, limit=limit)
    return {
        "signal": signal,
        "cavity": cavity,
        "filename": filename,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "rows": rows,
    }


def _read_rows_by_index(path: Path, offset: int, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []
        for _ in range(offset):
            try:
                next(reader)
            except StopIteration:
                return []
        for i, row in enumerate(reader):
            if i >= limit:
                break
            rows.append(row)
    return rows

@app.get("/cavities/{signal}/rows")
def multi_cavity_rows(
    signal: str,
    cavities: str = Query(..., description="Comma-separated cavity numbers, e.g. 1,2,3"),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
    x_key: str = Query("timestamp", description="CSV column to use as x axis"),
) -> Dict[str, Any]:
    if signal not in CAVITY_SIGNALS:
        raise HTTPException(status_code=404, detail=f"Unknown cavity signal: {signal}")

    prefix = CAVITY_SIGNALS[signal]
    cav_list = []
    for part in cavities.split(","):
        part = part.strip()
        if part.isdigit():
            cav_list.append(int(part))
    cav_list = sorted(set([c for c in cav_list if c >= 1]))
    if not cav_list:
        raise HTTPException(status_code=400, detail="No valid cavities provided")

    # Read each cavity file rows
    cav_rows: Dict[int, List[Dict[str, Any]]] = {}
    for cav in cav_list:
        filename = _filename_for_cavity(prefix, cav)  # IMPORTANT: must match your file naming
        path = _safe_resolve(filename)
        cav_rows[cav] = _read_rows_by_index(path, offset=offset, limit=limit)

    # Merge by row index (assumes rows align across cavities)
    # If timestamps differ, we still take x from the first cavity that has it.
    merged: List[Dict[str, Any]] = []
    max_len = max((len(v) for v in cav_rows.values()), default=0)

    for i in range(max_len):
        row_out: Dict[str, Any] = {}

        # x value: use first available
        x_val = None
        for cav in cav_list:
          if i < len(cav_rows[cav]):
              x_val = cav_rows[cav][i].get(x_key)
              if x_val is not None:
                  break
        row_out[x_key] = x_val

        # y values: map each cavity to vN
        for cav in cav_list:
            key = f"v{cav}"
            val = None
            if i < len(cav_rows[cav]):
                # your files are timestamp,value so "value" is the data
                val = cav_rows[cav][i].get("value")
            row_out[key] = val

        merged.append(row_out)

    return {
        "signal": signal,
        "cavities": cav_list,
        "offset": offset,
        "limit": limit,
        "returned": len(merged),
        "x_key": x_key,
        "rows": merged,
    }

@app.get("/rules", response_model=list[RuleOut])
def list_rules():
    return _load_rules()

@app.post("/rules", response_model=RuleOut)
def create_rule(rule: RuleIn):
    rules = _load_rules()

    new_rule = rule.model_dump()
    new_rule["id"] = uuid4().hex
    new_rule["created_at"] = datetime.utcnow().isoformat() + "Z"

    rules.append(new_rule)
    _save_rules(rules)

    return new_rule

@app.delete("/rules/{rule_id}")
def delete_rule(rule_id: str):
    rules = _load_rules()
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) == len(rules):
        raise HTTPException(status_code=404, detail="Rule not found")
    _save_rules(new_rules)
    return {"status": "deleted", "id": rule_id}

@app.get("/oee/catalog")
def oee_catalog() -> Dict[str, Any]:
    lines = _list_lines()
    return {
        "lines": lines,
        "metrics": list(OEE_METRICS),
        "files": {
            line: {metric: f"oee_{metric}.csv" for metric in OEE_METRICS}
            for line in lines
        },
    }


@app.get("/oee/{line}/{metric}/meta")
def oee_meta(line: str, metric: str) -> Dict[str, Any]:
    line = _validate_line(line)
    metric = metric.lower()

    if metric not in OEE_METRICS:
        raise HTTPException(status_code=404, detail=f"Unknown metric: {metric}")

    filename = f"oee_{metric}.csv"
    path = _safe_resolve(line, filename)
    stat = path.stat()

    return {
        "line": line,
        "metric": metric,
        "filename": filename,
        "size_bytes": stat.st_size,
        "modified_unix": int(stat.st_mtime),
        "columns": _read_header(path),
    }


@app.get("/oee/{line}/{metric}/rows") # http://127.0.0.1:8000/oee/TOK/availability/rows
def oee_rows(
    line: str,
    metric: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
) -> Dict[str, Any]:
    line = _validate_line(line)
    metric = metric.lower()

    if metric not in OEE_METRICS:
        raise HTTPException(status_code=404, detail=f"Unknown metric: {metric}")

    filename = f"oee_{metric}.csv"
    path = _safe_resolve(line, filename)

    rows = _read_rows(path, offset=offset, limit=limit)
    return {
        "line": line,
        "metric": metric,
        "filename": filename,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "rows": rows,
    }
