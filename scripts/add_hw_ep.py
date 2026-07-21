
import re

routes = open("/app/src/routes.py").read()

target = '@admin_router.get("/status")'
pos = routes.find(target)
if pos == -1:
    import sys
    print("NOT FOUND")
    sys.exit(1)

insert_text = '''

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
end = routes.find("\n", pos) + 1
new_routes = routes[:end] + insert_text + routes[end:]
open("/app/src/routes.py", "w").write(new_routes)

print(f"OK: inserted after pos {pos}, new length {len(new_routes)} (was {len(routes)})")
