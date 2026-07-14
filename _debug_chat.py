import sys, os
sys.path.insert(0, "/app")
import asyncio, uuid
from src.config import settings
from src.database import async_session
from src.services.context_builder import ContextBuilder
from src.services.session_service import SessionService

async def main():
    print("model:", settings.model)
    print("api_key:", bool(settings.openai_api_key))
    
    async with async_session() as db:
        from src.models import Session as SessionModel
        sid = uuid.uuid4()
        s = SessionModel(id=sid, user_id="test", title="dbg-ctx")
        db.add(s)
        await db.commit()
        print(f"Session: {sid}")
        
        try:
            builder = ContextBuilder(db)
            ctx = await builder.build(session_id=sid, user_message="Hello world")
            print(f"budget_state: {ctx.budget_state}")
            print(f"summary: {ctx.summary is not None}")
            print(f"memory_context: {ctx.memory_context is not None}")
            print(f"retrieved: {len(ctx.retrieved_messages)}")
            msgs = builder.to_messages(ctx, "Hello world")
            print(f"to_messages: {len(msgs)} msgs")
            print(f"memory_injected: {bool(ctx.memory_context)}")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        
        await db.delete(s)
        await db.commit()
    print("ALL OK")

asyncio.run(main())
