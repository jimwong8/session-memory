#!/usr/bin/env python3
"""Fix the hardware endpoint - remove broken single-line version and add proper version."""
import re

with open("/app/src/routes.py") as f:
    content = f.read()

# Remove the broken single-line version
broken = '@admin_router.get("/system/hardware")async def admin_system_hardware():    import os    return {        "llama_model": os.getenv("OPENAI_MODEL", "/models/Gemma-4-E2B-Q4_K_M.gguf"),        "llama_model_size": "4.2GB (Q4_K_M)",        "llama_n_ctx": 131072,        "llama_n_ctx_train": 131072,        "llama_slots_total": 2,        "llama_status": "ready",        "cpu": "Gemma 4 E2B (Local)",        "gpu": "None (CPU only)",        "mem_total": "2GB container",        "mem_used": "container",    }'
content = content.replace(broken, '', 1)

# Also remove duplicate if exists
dup_pattern = re.compile(r'@admin_router\.get\("/system/hardware"\)\s*\nasync def admin_system_hardware\(\):.*?^\s+"mem_used": "container",\s*\n\s*\}', re.MULTILINE | re.DOTALL)
# Count occurrences
matches = list(re.finditer(r'@admin_router\.get\("/system/hardware"\)', content))
if len(matches) > 1:
    # Remove all instances and add one clean version
    content = re.sub(r'\n*@admin_router\.get\("/system/hardware"\)\s*\nasync def admin_system_hardware\(\):.*?"mem_used": "container",\s*\n\s*\}', '', content, flags=re.DOTALL)

# Now insert clean version after @admin_router.get("/status")
target = '@admin_router.get("/status")'
pos = content.find(target)
if pos == -1:
    print("ERROR: /status endpoint not found")
    import sys; sys.exit(1)

# Find end of the line
end = content.find("\n", pos) + 1

clean_endpoint = '''

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

content = content[:end] + clean_endpoint + content[end:]

with open("/app/src/routes.py", "w") as f:
    f.write(content)

# Verify
count = content.count('@admin_router.get("/system/hardware")')
print(f"OK: hardware endpoint count = {count}")
