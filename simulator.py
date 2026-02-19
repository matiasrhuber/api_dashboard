import os
import time
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict


# ---------------- Simulator (same model as before, compact) ----------------

@dataclass
class SimConfig:
    n_cavities: int = 96
    seed: int = 42

    # Given constraints
    cycle_time_min: float = 3.8
    cycle_time_max: float = 4.5
    cavity_temp_min: float = 219.0
    cavity_temp_max: float = 221.0
    hotrunner_temp_min: float = 219.0
    hotrunner_temp_max: float = 221.0

    # Approximations
    injection_time_min: float = 0.55
    injection_time_max: float = 1.20
    overhead_min: float = 0.25
    overhead_max: float = 0.55

    # Pressure (bar) — adjust if your plant uses MPa/psi
    max_inj_pressure_min: float = 900.0
    max_inj_pressure_max: float = 1700.0
    cavity_pressure_fraction_mean: float = 0.62
    cavity_pressure_fraction_sd: float = 0.06

    # OEE (per line) — values in [0, 1], Gaussian then clipped
    oee_lines = ("TOK", "TOF", "TOE")
    oee_availability_mean: float = 0.60
    oee_productivity_mean: float = 0.80
    oee_quality_mean: float = 0.98

    # Minimal SDs (small, but not so small that quality becomes "always clipped")
    oee_availability_sd: float = 0.02
    oee_productivity_sd: float = 0.015
    oee_quality_sd: float = 0.006



class IMMProxySimulator:
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)

        self.cav_temp_offset = [self.rng.uniform(-0.15, 0.15) for _ in range(cfg.n_cavities)]
        self.hr_temp_offset  = [self.rng.uniform(-0.12, 0.12) for _ in range(cfg.n_cavities)]
        self.cav_press_scale = [self.rng.uniform(0.92, 1.08) for _ in range(cfg.n_cavities)]

        self.global_temp_drift = 0.0
        self.global_press_drift = 0.0

    def _clamp(self, x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    def _randn(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        u1 = max(1e-12, self.rng.random())
        u2 = self.rng.random()
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        return mu + sigma * z
    
    def _trunc_norm_01(self, mean: float, sd: float) -> float:
        # Gaussian sample then clamp to [0, 1]
        return self._clamp(self._randn(mean, sd), 0.0, 1.0)


    def next_cycle(self, anomaly_prob: float = 0.01) -> Dict[str, float]:
        cfg = self.cfg

        # Slow drift
        self.global_temp_drift += self._randn(0.0, 0.003)
        self.global_press_drift += self._randn(0.0, 0.5)

        # Cycle time (given)
        cycle_time = self.rng.uniform(cfg.cycle_time_min, cfg.cycle_time_max)

        overhead = self.rng.uniform(cfg.overhead_min, cfg.overhead_max)
        injection_time = self.rng.uniform(cfg.injection_time_min, cfg.injection_time_max)
        cooling_time = cycle_time - overhead - injection_time + self._randn(0.0, 0.05)

        if cooling_time < 1.5:
            delta = (1.5 - cooling_time)
            injection_time = self._clamp(injection_time - 0.6 * delta, cfg.injection_time_min, cfg.injection_time_max)
            cooling_time = cycle_time - overhead - injection_time + self._randn(0.0, 0.03)
        cooling_time = self._clamp(cooling_time, 1.5, 3.2)

        # Pressure correlated with injection_time (shorter => higher)
        it_norm = (injection_time - cfg.injection_time_min) / (cfg.injection_time_max - cfg.injection_time_min)
        base_pressure = cfg.max_inj_pressure_max - it_norm * (cfg.max_inj_pressure_max - cfg.max_inj_pressure_min)
        max_injection_pressure = self._clamp(
            base_pressure + self.global_press_drift + self._randn(0.0, 35.0),
            cfg.max_inj_pressure_min,
            cfg.max_inj_pressure_max,
        )

        # Rare anomalies
        anomaly = (self.rng.random() < anomaly_prob)
        anomaly_factor_temp = self.rng.choice([0.7, 1.3]) if anomaly else 1.0
        anomaly_factor_press = self.rng.choice([0.85, 1.15]) if anomaly else 1.0

        row: Dict[str, float] = {
            "cycle_time": round(cycle_time, 4),
            "injection_time": round(injection_time, 4),
            "max_injection_pressure": round(max_injection_pressure, 2),
            "cooling_time": round(cooling_time, 4),
        }
        
        # ----- OEE stats per line (TOK, TOF, TOE) -----
        for line in cfg.oee_lines:
            availability = self._trunc_norm_01(cfg.oee_availability_mean, cfg.oee_availability_sd)
            productivity = self._trunc_norm_01(cfg.oee_productivity_mean, cfg.oee_productivity_sd)
            quality      = self._trunc_norm_01(cfg.oee_quality_mean, cfg.oee_quality_sd)

            # Rounded but still "smooth"
            row[f"oee_availability_{line}"] = round(availability, 4)
            row[f"oee_productivity_{line}"] = round(productivity, 4)
            row[f"oee_quality_{line}"]      = round(quality, 4)

        cav_center = 220.0 + self._clamp(self.global_temp_drift, -0.25, 0.25)

        for i in range(cfg.n_cavities):
            cav_temp = cav_center + self.cav_temp_offset[i] + self._randn(0.0, 0.05) * anomaly_factor_temp
            cav_temp = self._clamp(cav_temp, cfg.cavity_temp_min, cfg.cavity_temp_max)

            hr_temp = cav_temp + 0.05 + self.hr_temp_offset[i] + self._randn(0.0, 0.04) * anomaly_factor_temp
            hr_temp = self._clamp(hr_temp, cfg.hotrunner_temp_min, cfg.hotrunner_temp_max)

            frac = self._clamp(self._randn(cfg.cavity_pressure_fraction_mean, cfg.cavity_pressure_fraction_sd), 0.45, 0.85)
            cav_press = (max_injection_pressure * frac * self.cav_press_scale[i] + self._randn(0.0, 18.0)) * anomaly_factor_press
            cav_press = max(0.0, cav_press)

            row[f"cavity_temperature_{i+1:02d}"] = round(cav_temp, 3)
            row[f"cavity_pressure_{i+1:02d}"] = round(cav_press, 2)
            row[f"hotrunner_cav_temperature_{i+1:02d}"] = round(hr_temp, 3)

        return row


# ---------------- Writer: one file per variable ----------------

def safe_filename(var_name: str) -> str:
    # keep alnum, dash, underscore, dot; replace others with underscore
    out = []
    for ch in var_name:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)

def append_row(path: str, timestamp: str, value) -> None:
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new_file:
            f.write("timestamp,value\n")
        f.write(f"{timestamp},{value}\n")


def run_for_one_minute(
    data_dir: str = "data",
    seed: int = 7,
    anomaly_prob: float = 0.01,
    use_utc: bool = True,
) -> None:
    os.makedirs(data_dir, exist_ok=True)

    sim = IMMProxySimulator(SimConfig(seed=seed))

    end_time = time.time() + 180.0

    while time.time() < end_time:
        # Generate one cycle worth of data
        row = sim.next_cycle(anomaly_prob=anomaly_prob)

        # Timestamp at generation time (ISO8601 with milliseconds)
        now = datetime.now(timezone.utc) if use_utc else datetime.now().astimezone()
        ts = now.isoformat(timespec="milliseconds")

        # Append each variable into its own file
        for var, val in row.items():
            filename = safe_filename(var) + ".csv"
            path = os.path.join(data_dir, filename)
            append_row(path, ts, val)

        # Sleep approximately the cycle time (so the cadence matches your simulated cycle)
        # If you want fixed-rate sampling instead, replace with time.sleep(0.1) or similar.
        time.sleep(float(row["cycle_time"]))


if __name__ == "__main__":
    run_for_one_minute(
        data_dir="data",
        seed=7,
        anomaly_prob=0.02,
        use_utc=True,
    )
    print("Done. Files written to ./data/")
