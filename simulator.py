import os
import time
import math
import random
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List


# ---------------- Simulator ----------------

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

    # Pressure (bar)
    max_inj_pressure_min: float = 900.0
    max_inj_pressure_max: float = 1700.0
    cavity_pressure_fraction_mean: float = 0.62
    cavity_pressure_fraction_sd: float = 0.06

    # OEE (per line) — values in [0, 1], Gaussian then clipped
    # oee_availability_mean: float = 0.60
    oee_productivity_mean: float = 0.80
    oee_quality_mean: float = 0.98

    # oee_availability_sd: float = 0.02
    oee_productivity_sd: float = 0.015
    oee_quality_sd: float = 0.006


class IMMProxySimulator:
    """
    One simulator instance = one line.
    """

    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)

        self.cav_temp_offset = [self.rng.uniform(-0.15, 0.15) for _ in range(cfg.n_cavities)]
        self.hr_temp_offset  = [self.rng.uniform(-0.12, 0.12) for _ in range(cfg.n_cavities)]
        self.cav_press_scale = [self.rng.uniform(0.92, 1.08) for _ in range(cfg.n_cavities)]

        self.global_temp_drift = 0.0
        self.global_press_drift = 0.0

        self.last_downtime_end = 0  # track when the last downtime ended

    def _clamp(self, x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    def _randn(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        u1 = max(1e-12, self.rng.random())
        u2 = self.rng.random()
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        return mu + sigma * z

    def _trunc_norm_01(self, mean: float, sd: float) -> float:
        return self._clamp(self._randn(mean, sd), 0.0, 1.0)

    def next_cycle(self, anomaly_prob: float = 0.01, downtime_prob: float = 0.02) -> Dict[str, float]:
        cfg = self.cfg

        # Slow drift
        self.global_temp_drift += self._randn(0.0, 0.003)
        self.global_press_drift += self._randn(0.0, 0.5)

        # Determine if downtime occurs (probability defined by downtime_prob)
        current_time = time.time()
        availability = 1.0  # Default to available

        # Check if there's a disruption
        if self.rng.random() < downtime_prob:
            # Disruption: Set availability to 0 for a random time between 1 and 3 minutes
            downtime_duration = random.randint(60, 180)  # Downtime duration in seconds
            downtime_start_time = current_time
            if current_time - self.last_downtime_end > downtime_duration:  # Only if downtime is finished
                availability = 0.0
                self.last_downtime_end = current_time  # Update downtime end time
            else:
                # If downtime still ongoing, ensure it continues
                if current_time - downtime_start_time < downtime_duration:
                    availability = 0.0
                else:
                    availability = 1.0  # Normal operation after downtime ends

        # Other variables generation follows
        cycle_time = self._randn(4.0, 0.1)  # Cycle time, Gaussian distribution with mean 4.0 and stddev 0.1
        cycle_time = self._clamp(cycle_time, cfg.cycle_time_min, cfg.cycle_time_max)

        overhead = self.rng.uniform(cfg.overhead_min, cfg.overhead_max)
        injection_time = self.rng.uniform(cfg.injection_time_min, cfg.injection_time_max)
        cooling_time = cycle_time - overhead - injection_time + self._randn(0.0, 0.05)

        if cooling_time < 1.5:
            delta = (1.5 - cooling_time)
            injection_time = self._clamp(
                injection_time - 0.6 * delta,
                cfg.injection_time_min,
                cfg.injection_time_max,
            )
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

        # OEE for THIS line (no suffix; folder identifies line)
        # availability = self._trunc_norm_01(cfg.oee_availability_mean, cfg.oee_availability_sd)
        productivity = self._trunc_norm_01(cfg.oee_productivity_mean, cfg.oee_productivity_sd)
        quality = self._trunc_norm_01(cfg.oee_quality_mean, cfg.oee_quality_sd)

        row: Dict[str, float] = {
            "cycle_time": round(cycle_time, 4),
            "injection_time": round(injection_time, 4),
            "max_injection_pressure": round(max_injection_pressure, 2),
            "cooling_time": round(cooling_time, 4),
            "oee_availability": round(availability, 4),
            "oee_productivity": round(productivity, 4),
            "oee_quality": round(quality, 4),
        }

        # Continue generating the data for the cavities (like cavity_temp and cavity_pressure)
        cav_center = 220.0 + self._clamp(self.global_temp_drift, -0.25, 0.25)
        for i in range(cfg.n_cavities):
            cav_temp = self._randn(220.0, 0.5) + self.cav_temp_offset[i] + self._randn(0.0, 0.05)
            cav_temp = self._clamp(cav_temp, cfg.cavity_temp_min, cfg.cavity_temp_max)

            hr_temp = self._randn(220.0, 0.5) + 0.05 + self.hr_temp_offset[i] + self._randn(0.0, 0.04)
            hr_temp = self._clamp(hr_temp, cfg.hotrunner_temp_min, cfg.hotrunner_temp_max)

            frac = self._clamp(self._randn(cfg.cavity_pressure_fraction_mean, cfg.cavity_pressure_fraction_sd), 0.45, 0.85)
            cav_press = (max_injection_pressure * frac * self.cav_press_scale[i] + self._randn(0.0, 18.0))
            cav_press = max(0.0, cav_press)

            row[f"cavity_temperature_{i+1:02d}"] = round(cav_temp, 3)
            row[f"cavity_pressure_{i+1:02d}"] = round(cav_press, 2)
            row[f"hotrunner_cav_temperature_{i+1:02d}"] = round(hr_temp, 3)

        return row


# ---------------- Writer: one file per variable ----------------

def safe_filename(var_name: str) -> str:
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


def run_simulation(
    data_dir: str,
    lines: List[str],
    seed: int = 7,
    anomaly_prob: float = 0.02,
    downtime_prob: float = 0.1,  # Downtime probability argument
    duration_s: float = 180.0,
    use_utc: bool = True,
) -> None:
    sims: Dict[str, IMMProxySimulator] = {}
    for idx, line in enumerate(lines):
        line_seed = seed + 1000 * idx
        sims[line] = IMMProxySimulator(SimConfig(seed=line_seed))

        # Ensure per-line folder exists
        os.makedirs(os.path.join(data_dir, line), exist_ok=True)

    end_time = time.time() + duration_s

    while time.time() < end_time:
        for line, sim in sims.items():
            row = sim.next_cycle(anomaly_prob=anomaly_prob, downtime_prob=downtime_prob)  # Pass downtime_prob

            now = datetime.now(timezone.utc) if use_utc else datetime.now().astimezone()
            ts = now.isoformat(timespec="milliseconds")

            line_dir = os.path.join(data_dir, line)

            for var, val in row.items():
                filename = safe_filename(var) + ".csv"
                path = os.path.join(line_dir, filename)
                append_row(path, ts, val)

        time.sleep(0.2)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="data")
    p.add_argument("--lines", nargs="*", default=[], help="e.g. --lines TOK TOF TOE")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--anomaly-prob", type=float, default=0.02)
    p.add_argument("--downtime-prob", type=float, default=0.02, help="Probability of downtime (0.0 to 1.0)")
    p.add_argument("--duration-s", type=float, default=180.0)
    p.add_argument("--use-utc", action="store_true", default=True)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # If lines is empty -> generate nothing (matches your “no lines => no sections” intent)
    if not args.lines:
        print("No lines provided. Nothing to generate. Use: --lines TOK TOF ...")
    else:
        run_simulation(
            data_dir=args.data_dir,
            lines=args.lines,
            seed=args.seed,
            anomaly_prob=args.anomaly_prob,
            downtime_prob=args.downtime_prob,
            duration_s=args.duration_s,
            use_utc=args.use_utc,
        )
        print(f"Done. Files written to ./{args.data_dir}/<LINE>/")