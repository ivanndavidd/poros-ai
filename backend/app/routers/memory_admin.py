"""Memory (Feature 1.3) admin endpoints: inspect / add / delete per-(tenant,user)
memory. Lets an admin audit that memory never crosses tenants and clear it.
"""
from fastapi import APIRouter, Depends
from .. import store, memory
from ..console_auth import console_guard, current_principal, ensure_tenant_access
from ..models import MemoryIn

router = APIRouter(prefix="/admin", tags=["memory (1.3)"], dependencies=[Depends(console_guard)])


def _strip(m):
    return {k: v for k, v in m.items() if k != "embedding"}


@router.get("/tenants/{tid}/memories")
def list_memories(tid: str, user_id: str = None):
    return [_strip(m) for m in store.list_memories(tid, user_id)]


@router.post("/memories")
def add_memory(b: MemoryIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    mid = memory.remember(b.tenant_id, b.user_id, b.text, b.kind)
    return {"id": mid}


@router.delete("/tenants/{tid}/memories/{mem_id}")
def delete_memory(tid: str, mem_id: str):
    store.delete_memory(tid, mem_id); return {"ok": True}


@router.delete("/tenants/{tid}/memories")
def delete_user_memories(tid: str, user_id: str):
    """Bulk 'forget this user' — wipes every memory of one (tenant, user)."""
    n = store.delete_memories(tid, user_id)
    return {"ok": True, "deleted": n}
