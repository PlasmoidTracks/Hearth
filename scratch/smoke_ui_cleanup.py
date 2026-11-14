"""Ad-hoc smoke: new sidebar-era endpoints + personal system prompt wiring."""
import os, sys, tempfile
_tmp = tempfile.mkdtemp(prefix="hearth-smoke-")
os.environ["HEARTH_DATA_DIR"] = _tmp
os.environ["HEARTH_BOOTSTRAP_ADMIN_PASSWORD"] = "testpass123"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from hearth.main import create_app

with TestClient(create_app()) as c:
    r = c.post("/auth/login", data={"username": "admin", "password": "testpass123"})
    assert r.status_code == 200, r.text
    csrf = c.cookies.get("hearth_csrf", "")
    H = {"X-CSRF-Token": csrf}

    # /auth/me exposes system_prompt
    me = c.get("/auth/me").json()
    assert me["system_prompt"] == "", me

    # GET prompt (includes base persona)
    d = c.get("/api/chat/system-prompt").json()
    assert d["content"] == "" and "Hearth" in d["base"], d

    # POST prompt (CSRF required)
    bad = c.post("/api/chat/system-prompt", json={"content": "x"})
    assert bad.status_code == 403, bad.status_code
    r = c.post("/api/chat/system-prompt", json={"content": "  Answer in German.  "}, headers=H)
    assert r.status_code == 200 and r.json()["content"] == "Answer in German.", r.text
    assert c.get("/auth/me").json()["system_prompt"] == "Answer in German."
    assert c.get("/api/chat/system-prompt").json()["content"] == "Answer in German."

    # too long rejected
    big = c.post("/api/chat/system-prompt", json={"content": "a" * 9000}, headers=H)
    assert big.status_code == 400, big.status_code

    # build_messages with a nonexistent user id: no personal prompt available,
    # base persona only (the real-user path is covered below in "SMOKE 2").
    cid = c.post("/api/chat/conversations", headers=H).json()["id"]
    from hearth.orchestrator import build_messages
    msgs = build_messages(cid, "does-not-exist")
    assert msgs[0]["role"] == "system" and "Answer in German" not in msgs[0]["content"], msgs[0]

    # prefs whitelist: agent_mode_default now dropped, others accepted
    r = c.post("/api/prefs", json={"agent_mode_default": False}, headers=H)
    assert r.status_code == 200 and "agent_mode_default" not in r.json()["prefs"], r.text
    r = c.post("/api/prefs", json={"enter_sends": False}, headers=H)
    assert r.json()["prefs"]["enter_sends"] is False

    # device-access API still works (grant round-trip)
    r = c.post("/api/chat/device-access", json={"conversation_id": cid, "granted": True}, headers=H)
    assert r.json()["granted"] is True, r.text
    g = c.get(f"/api/chat/device-access?conversation_id={cid}").json()
    assert g["granted"] is True and g["helper_configured"] is False, g

    # context-usage shape unchanged
    cu = c.get(f"/api/chat/context-usage?conversation_id={cid}").json()
    assert cu["source"] == "none" and cu["n_ctx"], cu

    # chat page renders with the new rail + panels, without removed controls
    html = c.get("/chat")
    for needle in ('id="rail"', 'data-panel="prompt"', 'data-panel="skills"',
                   'id="spText"', 'id="langSel"', 'id="skillList"'):
        assert needle in html.text, needle
    for gone in ("sysPromptSel", "protocolBtn", "deviceBtn", "agentModeBtn",
                 "forceBtn", "moreSheet", "settingsSheet", "deviceNotice"):
        assert gone not in html.text, gone

print("SMOKE OK")

# --- personal prompt resolution with a REAL user row -------------------------
from hearth.db import session_scope
from hearth.models import User, Conversation
uid = None
with session_scope() as s:
    uid = s.query(User).filter(User.username == "admin").one().id

with TestClient(create_app()) as c2:
    r = c2.post("/auth/login", data={"username": "admin", "password": "testpass123"})
    assert r.status_code == 200, r.text
    csrf = c2.cookies.get("hearth_csrf", "")
    H = {"X-CSRF-Token": csrf}
    cid2 = c2.post("/api/chat/conversations", headers=H).json()["id"]
    assert c2.post("/api/chat/system-prompt", json={"content": "Antworte auf Deutsch."}, headers=H).status_code == 200

from hearth.orchestrator import build_messages
msgs = build_messages(cid2, uid)
assert "Antworte auf Deutsch" in msgs[0]["content"], msgs[0]["content"][:200]

# A conversation with an explicit admin doc still wins over the personal prompt.
from hearth.models import Doc
with session_scope() as s:
    doc = s.query(Doc).filter(Doc.kind == "system_prompt").first()
    doc_id = doc.id
    conv = s.get(Conversation, cid2)
    conv.system_prompt_doc_id = doc_id
msgs = build_messages(cid2, uid)
assert "Antworte auf Deutsch" not in msgs[0]["content"], "explicit doc should win"
assert "helpful, concise assistant" in msgs[0]["content"]

print("SMOKE 2 OK")
