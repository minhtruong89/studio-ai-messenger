import json
import os
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai_engine
import db
import fb_client

load_dotenv()
db.init_db()

app = FastAPI(title="Wedding Studio AI Messenger MVP")
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ModePayload(BaseModel):
    mode: str  # 'draft' | 'auto' | 'manual'


class SendPayload(BaseModel):
    text: str
    sender: str = "admin"  # 'admin' | 'ai'


class FeedbackPayload(BaseModel):
    ai_message: str
    user_feedback: str


class KnowledgePayload(BaseModel):
    title: str
    category: str = "general"
    content: str


class SimulatePayload(BaseModel):
    customer_name: str = "Khách Test (Cô dâu)"
    text: str


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ========================
# META MESSENGER WEBHOOK
# ========================
@app.get("/webhook")
async def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    expected_token = os.getenv("FB_VERIFY_TOKEN", "studio_cuoi_verify_123")
    if hub_mode == "subscribe" and hub_verify_token == expected_token:
        return PlainTextResponse(content=hub_challenge or "")
    raise HTTPException(status_code=403, detail="Verify token không khớp")


async def _handle_incoming_customer_message(psid: str, text: str, customer_name: str | None = None) -> dict:
    if not customer_name:
        customer_name = await fb_client.get_user_name(psid)

    db.upsert_conversation(psid, customer_name)
    db.add_message(psid, "customer", text, is_draft=False)

    conv = db.get_conversation(psid)
    mode = conv["mode"] if conv else "draft"

    if mode == "manual":
        return {"status": "saved_manual"}

    ai_reply = ai_engine.generate_reply(psid)
    if mode == "auto":
        send_res = await fb_client.send_message(psid, ai_reply)
        if send_res.get("ok"):
            db.add_message(psid, "ai", ai_reply, is_draft=False)
            return {"status": "auto_replied", "reply": ai_reply}
        # Nếu gửi FB lỗi, lưu thành nháp để chủ studio xử lý
        db.replace_draft(psid, ai_reply)
        return {"status": "auto_failed_saved_draft", "error": send_res.get("error")}

    # Mặc định: 'draft'
    db.replace_draft(psid, ai_reply)
    return {"status": "draft_created", "draft": ai_reply}


@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.json()
    if body.get("object") != "page":
        return {"status": "ignored"}

    for entry in body.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")
            message = event.get("message", {})
            # Bỏ qua tin nhắn echo do chính Page gửi ra
            if not sender_id or message.get("is_echo"):
                continue

            text = message.get("text")
            if not text:
                if message.get("attachments"):
                    text = "[Khách đã gửi ảnh / tệp đính kèm]"
                else:
                    continue

            await _handle_incoming_customer_message(sender_id, text)

    return {"status": "ok"}


# ========================
# WEB UI APIs
# ========================
@app.get("/api/conversations")
async def api_list_conversations():
    return {"conversations": db.list_conversations()}


@app.get("/api/conversations/{psid}/messages")
async def api_get_messages(psid: str):
    conv = db.get_conversation(psid)
    if not conv:
        raise HTTPException(status_code=404, detail="Không tìm thấy hội thoại")
    return {"conversation": conv, "messages": db.get_messages(psid, limit=100)}


@app.post("/api/conversations/{psid}/mode")
async def api_set_mode(psid: str, payload: ModePayload):
    if payload.mode not in ("draft", "auto", "manual"):
        raise HTTPException(status_code=400, detail="Mode không hợp lệ")
    db.set_conversation_mode(psid, payload.mode)
    return {"ok": True, "mode": payload.mode}


@app.post("/api/conversations/{psid}/send")
async def api_send_message(psid: str, payload: SendPayload):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Nội dung tin nhắn trống")

    res = await fb_client.send_message(psid, text)
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res.get("error", "Gửi tin nhắn thất bại"))

    db.add_message(psid, payload.sender, text, is_draft=False)
    return {"ok": True, "fb_result": res}


@app.post("/api/conversations/{psid}/regenerate")
async def api_regenerate_draft(psid: str):
    conv = db.get_conversation(psid)
    if not conv:
        raise HTTPException(status_code=404, detail="Không tìm thấy hội thoại")
    draft = ai_engine.generate_reply(psid)
    db.replace_draft(psid, draft)
    return {"ok": True, "draft": draft, "stats": ai_engine.LAST_RUN_STATS}


@app.get("/api/ai-stats")
async def api_ai_stats():
    return {"stats": ai_engine.LAST_RUN_STATS}


@app.post("/api/simulate")
async def api_simulate_customer(payload: SimulatePayload):
    """Tạo tin nhắn giả lập để test AI & quy tắc ngay trên web mà chưa cần nối Facebook."""
    psid = "sim_" + str(abs(hash(payload.customer_name)) % 1000000)
    result = await _handle_incoming_customer_message(
        psid=psid,
        text=payload.text.strip(),
        customer_name=payload.customer_name.strip(),
    )
    return {"psid": psid, "result": result}


@app.post("/api/feedback")
async def api_submit_feedback(payload: FeedbackPayload):
    if not payload.user_feedback.strip():
        raise HTTPException(status_code=400, detail="Vui lòng nhập nội dung góp ý")
    rule_text = ai_engine.distill_feedback_rule(
        payload.ai_message.strip(),
        payload.user_feedback.strip(),
    )
    rule_id = db.add_rule(
        rule_text=rule_text,
        source_ai_message=payload.ai_message.strip(),
        user_feedback=payload.user_feedback.strip(),
    )
    return {"ok": True, "id": rule_id, "rule_text": rule_text}


@app.get("/api/rules")
async def api_list_rules():
    return {"rules": db.list_rules()}


@app.delete("/api/rules/{rule_id}")
async def api_delete_rule(rule_id: int):
    db.delete_rule(rule_id)
    ai_engine.invalidate_cache()
    return {"ok": True}


@app.get("/api/knowledge")
async def api_list_knowledge():
    return {"items": db.list_knowledge()}


@app.post("/api/knowledge")
async def api_add_knowledge(payload: KnowledgePayload):
    if not payload.title.strip() or not payload.content.strip():
        raise HTTPException(status_code=400, detail="Tiêu đề và nội dung không được để trống")
    item_id = db.add_knowledge(
        title=payload.title.strip(),
        category=payload.category,
        content=payload.content.strip(),
    )
    ai_engine.invalidate_cache()
    return {"ok": True, "id": item_id}


@app.delete("/api/knowledge/{item_id}")
async def api_delete_knowledge(item_id: int):
    db.delete_knowledge(item_id)
    ai_engine.invalidate_cache()
    return {"ok": True}


@app.post("/api/knowledge/distill-style")
async def api_distill_style():
    res = ai_engine.distill_style_guide()
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Không thể nén cẩm nang"))
    return res


@app.post("/api/import-fb-history")
async def api_import_fb_history():
    """Kéo các đoạn hội thoại gần nhất từ Fanpage vào Kho tri thức để AI học cách trả lời."""
    page_info = await fb_client.get_page_info()
    res = await fb_client.fetch_page_conversations(limit=15)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))

    conversations = res.get("conversations", [])
    page_id = page_info.get("id", "")
    page_name = page_info.get("name", "")

    # Nếu /me bị thiếu quyền pages_read_engagement, tự nhận diện Page là participant xuất hiện ở nhiều hội thoại nhất
    if not page_id and len(conversations) >= 2:
        freq: dict[str, tuple[int, str]] = {}
        for c in conversations:
            for p in c.get("participants", {}).get("data", []):
                pid = p.get("id", "")
                pname = p.get("name", "")
                if pid:
                    cnt, _ = freq.get(pid, (0, pname))
                    freq[pid] = (cnt + 1, pname)
        if freq:
            best_pid, (best_cnt, best_name) = max(freq.items(), key=lambda x: x[1][0])
            if best_cnt >= 2:
                page_id = best_pid
                page_name = best_name

    imported = 0
    for conv in conversations:
        conv_id = conv.get("id", "")
        participants = conv.get("participants", {}).get("data", [])
        customer_name = None
        for p in participants:
            if p.get("id") != page_id and p.get("name") != page_name:
                customer_name = p.get("name")
                break

        msgs = conv.get("messages", {}).get("data", [])
        if not msgs:
            continue

        # Meta trả về mới nhất trước -> đảo ngược lại theo thứ tự thời gian
        chronological_msgs = []
        lines = []
        for m in reversed(msgs):
            from_info = m.get("from", {})
            sender_id = from_info.get("id", "")
            sender_name = from_info.get("name", "Khách")
            is_page = (page_id and sender_id == page_id) or (page_name and sender_name == page_name)

            if not is_page and not customer_name:
                customer_name = sender_name

            text = (m.get("message") or "").strip() or "[Tệp đính kèm / Hình ảnh]"
            created_time = m.get("created_time", "")

            chronological_msgs.append({
                "sender": "page" if is_page else "customer",
                "name": "Bạn" if is_page else sender_name,
                "text": text,
                "time": created_time,
            })
            role_label = "Bạn" if is_page else sender_name
            lines.append(f"{role_label}: {text}")

        if not chronological_msgs:
            continue

        display_name = customer_name or f"Khách #{conv_id[-6:]}"
        last_msg = chronological_msgs[-1]
        last_preview = f"Bạn: {last_msg['text']}" if last_msg["sender"] == "page" else last_msg["text"]
        last_time = last_msg["time"] or conv.get("updated_time", "")

        meta = {
            "fb_conv_id": conv_id,
            "customer_name": display_name,
            "last_preview": last_preview,
            "last_time": last_time,
            "messages": chronological_msgs,
        }
        db.upsert_fb_knowledge(
            fb_conv_id=conv_id,
            title=display_name,
            content="\n".join(lines),
            meta_json=json.dumps(meta, ensure_ascii=False),
        )
        imported += 1

    ai_engine.invalidate_cache()
    return {"ok": True, "imported_count": imported}
