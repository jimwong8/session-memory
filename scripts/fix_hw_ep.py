#!/usr/bin/env python3
"""Update hardware endpoint with correct data."""
import re

with open("/app/src/routes.py") as f:
    content = f.read()

old_endpoint = '''@admin_router.get("/system/hardware")
async def admin_system_hardware():
    import os
    return {
        "llama_model": os.getenv("OPENAI_MODEL", "/models/Gemma-4-E2B-Q4_K_M.gguf"),
        "llama_model_size": "4.2GB (Q4_K_M)",
        "llama_n_ctx": 131072,
        "llama_n_ctx_train": 131072,
        "llama_slots_total": 2,
        "llama_status": "ready",
        "cpu": "Gemma 4 E2B (Local)",
        "gpu": "None (CPU only)",
        "mem_total": "2GB container",
        "mem_used": "container",
    }'''

new_endpoint = '''@admin_router.get("/system/hardware")
async def admin_system_hardware():
    import os, json, urllib.request
    info = {
        "llama_model": os.getenv("OPENAI_MODEL", "/models/Gemma-4-E2B-Q4_K_M.gguf"),
        "llama_model_size": "2.9GB (unsloth Q4_K_M)",
        "llama_n_ctx": 131072,
        "llama_n_ctx_train": 131072,
        "llama_slots_total": 4,
        "llama_status": "ready",
        "cpu": "Gemma 4 E2B (Local)",
        "gpu": "NVIDIA GeForce GTX 750 Ti",
        "mem_total": "2GB container",
        "mem_used": "container",
    }
    # Fetch live GPU stats from 8001 proxy (twai PVE host)
    try:
        req = urllib.request.urlopen("http://10.100.1.13:8001/gpu", timeout=2)
        gpu = json.loads(req.read())
        info["gpu"] = gpu.get("gpu_name", info["gpu"])
        info["gpu_mem_used"] = gpu.get("mem_used", "")
        info["gpu_mem_total"] = gpu.get("mem_total", "")
        info["gpu_temp"] = gpu.get("temperature")
        info["gpu_util"] = gpu.get("gpu_util")
        info["gpu_power_draw"] = gpu.get("power_draw")
        info["gpu_power_limit"] = gpu.get("power_limit")
        info["gpu_driver"] = gpu.get("driver")
    except Exception as e:
        info["gpu"] = f"unavailable: {e}"
    # Fetch slot count from llama.cpp
    try:
        req = urllib.request.urlopen("http://10.100.1.101:18881/slots", timeout=2)
        slots = json.loads(req.read())
        info["llama_slots_total"] = len(slots)
        info["llama_slots_active"] = sum(1 for s in slots if s.get("is_processing"))
    except Exception:
        pass
    return info'''

content = content.replace(old_endpoint, new_endpoint, 1)

import subprocess
r = subprocess.run(["python3", "-c", "import ast; ast.parse(open('/dev/stdin').read())"], input=content, capture_output=True, text=True)

with open("/app/src/routes.py", "w") as f:
    f.write(content)

print("Applied, python parse:", "OK" if r.returncode == 0 else f"FAIL: {r.stderr[:100]}")
