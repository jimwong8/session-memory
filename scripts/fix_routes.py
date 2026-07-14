#!/usr/bin/env python3
"""Fix MissingGreenlet errors in routes.py session endpoints."""
with open("/app/src/routes.py", "r") as f:
    code = f.read()

# Fix 1: get_session route
old_get = """    svc = SessionService(db)
    session, messages = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return SessionDetail.model_validate(session)"""

new_get = """    svc = SessionService(db)
    session, messages = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    resp = SessionDetail.model_validate(session)
    resp.messages = [MessageResponse.model_validate(m) for m in messages]
    resp.summaries = []
    return resp"""

if old_get in code:
    code = code.replace(old_get, new_get)
    print("OK: get_session route fixed")
else:
    print("WARN: get_session not found")

# Fix 2: export_session route
old_export = """    svc = SessionService(db)
    session, messages = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    detail = SessionDetail.model_validate(session)"""

new_export = """    svc = SessionService(db)
    session, messages = await svc.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    detail = SessionDetail.model_validate(session)
    detail.messages = [MessageResponse.model_validate(m) for m in messages]
    detail.summaries = []"""

if old_export in code:
    code = code.replace(old_export, new_export)
    print("OK: export_session route fixed")
else:
    print("WARN: export_session not found")

with open("/app/src/routes.py", "w") as f:
    f.write(code)
print("Routes patched successfully.")
