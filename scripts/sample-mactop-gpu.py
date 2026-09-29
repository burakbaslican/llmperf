#!/usr/bin/env python3
"""mactop headless JSON → LLMPerf GPU örneklemesi (Apple Silicon).

mactop tek örnek ~birkaç sn sürer; install script arka planda çalıştırır.
Çıktı: JSON array [{index,name,vendor,util_pct,mem_*,source,...}]
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys


def sample() -> list[dict]:
    mactop = shutil.which("mactop")
    if not mactop:
        return []
    try:
        out = subprocess.check_output(
            [mactop, "--headless", "--format", "json", "--count", "1"],
            text=True,
            timeout=20,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    out = (out or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return []

    soc = row.get("soc_metrics") if isinstance(row.get("soc_metrics"), dict) else {}
    gpu_m = row.get("gpu_metrics") if isinstance(row.get("gpu_metrics"), dict) else {}
    info = row.get("system_info") if isinstance(row.get("system_info"), dict) else {}
    mem = row.get("memory") if isinstance(row.get("memory"), dict) else {}

    util = row.get("gpu_usage")
    if util is None:
        util = gpu_m.get("active_percent")
    if util is None:
        util = soc.get("gpu_active")
    try:
        util_pct = float(util or 0.0)
    except (TypeError, ValueError):
        util_pct = 0.0
    util_pct = max(0.0, min(100.0, util_pct))

    name = str(info.get("name") or "Apple Silicon")
    cores = info.get("gpu_core_count")
    if cores:
        name = f"{name} · {cores} GPU"

    mem_used = mem.get("used")
    mem_total = mem.get("total")
    try:
        mem_used_i = int(mem_used) if mem_used is not None else None
        mem_total_i = int(mem_total) if mem_total is not None else None
    except (TypeError, ValueError):
        mem_used_i = mem_total_i = None
    mem_util = None
    if mem_used_i is not None and mem_total_i:
        mem_util = round(mem_used_i / mem_total_i * 100.0, 1)

    freq = gpu_m.get("freq_mhz") or soc.get("gpu_freq_mhz")
    power = soc.get("gpu_power")
    extra = {}
    if freq is not None:
        try:
            extra["freq_mhz"] = float(freq)
        except (TypeError, ValueError):
            pass
    if power is not None:
        try:
            extra["power_w"] = round(float(power), 2)
        except (TypeError, ValueError):
            pass

    return [
        {
            "index": 0,
            "name": name,
            "vendor": "Apple",
            "util_pct": round(util_pct, 1),
            "mem_util_pct": mem_util,
            "mem_used": mem_used_i,
            "mem_total": mem_total_i,
            "source": "mactop",
            **extra,
        }
    ]


def main() -> int:
    json.dump(sample(), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
