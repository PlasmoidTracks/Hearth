"""Audit trail writer/reader. Security-relevant events are recorded here:
login success/failure, tool invocations (esp. exec), permission decisions,
admin changes. Never store secrets in `detail`.
"""
from __future__ import annotations
import json

from .db import session_scope
from .models import AuditLog


def log(user_id: str, action: str, detail: dict | str = "") -> None:
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False)[:2000]
    try:
        with session_scope() as s:
            s.add(AuditLog(user_id=user_id or "", action=action[:64], detail=str(detail)))
    except Exception:
        # auditing must never crash the request path; fall back to stderr
        import sys
        print(f"[audit-fail] {user_id} {action} {detail}", file=sys.stderr)


def recent(limit: int = 100, user_id: str | None = None) -> list[dict]:
    with session_scope() as s:
        q = s.query(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)
        if user_id:
            q = s.query(AuditLog).filter(AuditLog.user_id == user_id).order_by(
                AuditLog.ts.desc()).limit(limit)
        return [{"ts": a.ts, "user_id": a.user_id, "action": a.action, "detail": a.detail}
                for a in q.all()]
