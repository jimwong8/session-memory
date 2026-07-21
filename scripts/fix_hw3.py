#!/usr/bin/env python3
"""Fix hardware endpoint - clear all and add one clean version."""

with open("/app/src/routes.py") as f:
    content = f.read()

import re

# Remove ALL hardware endpoints
content = re.sub(
    r'\n*@admin_router\.get\("/system/hardware"\)\s*\nasync def admin_system_hardware\(\):(?:.*?\n)*?\s+"mem_used":\s*"container",\s*\n\s*\}',
    '',
    content
)
content = re.sub(
    r'\n*@admin_router\.get\("/system/hardware"\)async.*?\}.\n',
    '',
    content
)

remaining = content.count('@admin_router.get("/system/hardware")')
print("After cleanup:", remaining, "hardware endpoints remaining")

# Insert one clean version after @admin_router.get("/status")
target = '@admin_router.get("/status")'
pos = content.find(target)
end = content.find("\n", pos) + 1

clean = '''

@admin_router.get("/system/hardware")
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
    }
'''

content = content[:end] + clean + content[end:]

with open("/app/src/routes.py", "w") as f:
    f.write(content)

import subprocess
r = subprocess.run(["python3", "-c", "import ast; ast.parse(open('/app/src/routes.py']).read())"], capture_output=True)
final = content.count('@admin_router.get("/system/hardware")')
print("Syntax:", "OK" if r.returncode == 0 else "FAIL")
print("Final count:", final)
