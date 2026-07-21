#!/usr/bin/env python3
"""Fix /status endpoint - currently orphaned."""

with open("/app/src/routes.py") as f:
    content = f.read()

# The problem: @admin_router.get("/status") is orphaned, admin_live_status has no decorator
# Current structure:
#   @admin_router.get("/status")  <-- orphaned
#   
#   @admin_router.get("/system/hardware")
#   async def admin_system_hardware(): ...
#   async def admin_live_status():  <-- no decorator!

# Remove the orphaned @admin_router.get("/status") and add it before admin_live_status
old = '''@admin_router.get("/status")


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
async def admin_live_status(db'''

new = '''@admin_router.get("/system/hardware")
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

@admin_router.get("/status")
async def admin_live_status(db'''

content = content.replace(old, new, 1)

# Verify
import subprocess, ast
try:
    ast.parse(content)
    syntax_ok = True
except SyntaxError as e:
    syntax_ok = False
    print(f"Syntax error: {e}")

route_count = content.count('@admin_router.get("/status")')
hw_count = content.count('@admin_router.get("/system/hardware")')

with open("/app/src/routes.py", "w") as f:
    f.write(content)

print(f"Syntax: {'OK' if syntax_ok else 'FAIL'}")
print(f"Routes: /status={route_count}, /system/hardware={hw_count}")
