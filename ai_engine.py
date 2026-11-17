import hashlib
import json
import math
import os
import re
import time
from typing import Any
from google import genai
from google.genai import types
import db

EMBED_MODEL = "gemini-embedding-001"
TOP_K_SAMPLES = 3
CACHE_TTL_SECONDS = 3600

# In-memory state for Gemini Context Caching
_CACHE_STATE: dict[str, Any] = {
    "hash": "",
    "name": "",
    "expires_at": 0.0,
    "too_small": False,
}

# Last run telemetry for UI display
LAST_RUN_STATS: dict[str, Any] = {}


def _get_client() -> genai.Client | None:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_gemini_api_key_here":
        return None
    return genai.Client(api_key=api_key)


def _get_model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"


def invalidate_cache() -> None:
    _CACHE_STATE["hash"] = ""
    _CACHE_STATE["name"] = ""
    _CACHE_STATE["expires_at"] = 0.0
    _CACHE_STATE["too_small"] = False


# ========================
# 1. RAG: EMBEDDING & SIMILARITY
# ========================
def _compute_embedding(client: genai.Client, text: str) -> list[float] | None:
    try:
        res = client.models.embed_content(model=EMBED_MODEL, contents=text[:4000])
        if res and res.embeddings:
            return list(res.embeddings[0].values)
    except Exception:
        pass
    return None


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def _keyword_score(query: str, doc: str) -> float:
    q_words = set(re.findall(r"\w+", query.lower()))
    d_words = set(re.findall(r"\w+", doc.lower()))
    if not q_words or not d_words:
        return 0.0
    return len(q_words & d_words) / math.sqrt(len(q_words) * len(d_words))


def _select_relevant_samples(
    client: genai.Client, query_text: str, sample_items: list[dict[str, Any]], top_k: int = TOP_K_SAMPLES
) -> list[dict[str, Any]]:
    if len(sample_items) <= top_k:
        return sample_items

    query_vec = _compute_embedding(client, query_text) if query_text.strip() else None
    scored: list[tuple[float, dict[str, Any]]] = []

    for item in sample_items:
        score = 0.0
        if query_vec is not None:
            vec: list[float] | None = None
            if item.get("embedding_json"):
                try:
                    vec = json.loads(item["embedding_json"])
                except Exception:
                    vec = None
            if vec is None:
                vec = _compute_embedding(client, f"{item['title']}\n{item['content']}")
                if vec is not None:
                    db.update_knowledge_embedding(item["id"], json.dumps(vec))
            if vec is not None:
                score = _cosine_sim(query_vec, vec)

        if score == 0.0:
            score = _keyword_score(query_text, f"{item['title']} {item['content']}")

        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_k]]


# ========================
# 2. STYLE GUIDE DISTILLATION
# ========================
def distill_style_guide() -> dict[str, Any]:
    client = _get_client()
    if not client:
        return {"ok": False, "error": "Chưa cấu hình GEMINI_API_KEY trong .env"}

    items = db.list_knowledge()
    sample_chats = [i for i in items if i["category"] == "sample_chat"]
    if not sample_chats:
        return {"ok": False, "error": "Chưa có đoạn chat mẫu nào để nén thành Cẩm nang."}

    chats_text = "\n\n---\n\n".join(
        f"Hội thoại với {c['title']}:\n{c['content']}" for c in sample_chats[:20]
    )

    prompt = f"""Dưới đây là các đoạn hội thoại thực tế giữa Studio và khách hàng.
Hãy phân tích và nén lại thành một bản **"CẨM NANG VĂN PHONG & KỊCH BẢN TƯ VẤN"** súc tích (khoảng 300-600 từ) để AI học cách nhắn tin giống hệt chủ Studio:
1. **Xưng hô & Giọng điệu:** (VD: xưng "mình/em/bên mình", gọi khách là "bạn/dâu rể", dùng từ đệm như "ạ, nè, nhé", độ dài mỗi câu...).
2. **Cách hỏi khai thác nhu cầu:** (Hỏi những thông tin gì trước khi báo giá: ngày chụp, số thành viên, gói...).
3. **Cách xử lý câu hỏi phổ biến & chốt khách:** (Những câu mở đầu, câu dẫn, cách báo giá và mời qua studio hay dùng).
4. **Những từ ngữ / mẫu câu đặc trưng:** Liệt kê 5-10 câu mẫu tự nhiên nhất trích từ lịch sử.

=== DỮ LIỆU HỘI THOẠI THỰC TẾ ===
{chats_text}"""

    try:
        response = client.models.generate_content(
            model=_get_model(),
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3),
        )
        guide_text = (response.text or "").strip()
        if not guide_text:
            return {"ok": False, "error": "Gemini trả về nội dung rỗng"}
        item_id = db.upsert_style_guide(guide_text)
        invalidate_cache()
        return {"ok": True, "id": item_id, "content": guide_text, "source_count": len(sample_chats)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ========================
# 3. CONTEXT CACHING + GENERATE REPLY
# ========================
def _get_or_create_cache(client: genai.Client, model: str, static_instruction: str) -> tuple[str | None, str]:
    """Trả về (cache_name, status_label). Tự động fallback nếu nội dung chưa đủ ngưỡng tối thiểu của Gemini."""
    content_hash = hashlib.sha256(f"{model}\n{static_instruction}".encode("utf-8")).hexdigest()
    now = time.time()

    if _CACHE_STATE["hash"] == content_hash:
        if _CACHE_STATE["too_small"]:
            return None, "standard (nội dung nhỏ hơn ngưỡng cache)"
        if _CACHE_STATE["name"] and now < _CACHE_STATE["expires_at"] - 60:
            return _CACHE_STATE["name"], "CACHE HIT"

    # Thử tạo explicit cache mới nếu nội dung đủ dài (~>= 1024 tokens, ước lượng ~3500 ký tự)
    if len(static_instruction) < 3000:
        _CACHE_STATE.update({"hash": content_hash, "name": "", "expires_at": 0.0, "too_small": True})
        return None, "standard (nội dung gọn <3000 ký tự)"

    try:
        cached = client.caches.create(
            model=model,
            config=types.CreateCachedContentConfig(
                display_name=f"studio_kb_{content_hash[:8]}",
                system_instruction=static_instruction,
                ttl=f"{CACHE_TTL_SECONDS}s",
            ),
        )
        _CACHE_STATE.update({
            "hash": content_hash,
            "name": cached.name,
            "expires_at": now + CACHE_TTL_SECONDS,
            "too_small": False,
        })
        return cached.name, "CACHE CREATED"
    except Exception:
        _CACHE_STATE.update({"hash": content_hash, "name": "", "expires_at": 0.0, "too_small": True})
        return None, "standard (fallback)"


def generate_reply(psid: str) -> str:
    global LAST_RUN_STATS
    client = _get_client()
    if not client:
        return "[Chưa cấu hình GEMINI_API_KEY trong file .env]"

    model = _get_model()
    knowledge_items = db.list_knowledge()
    rules = db.list_rules()
    messages = [m for m in db.get_messages(psid, limit=20) if not m["is_draft"]]

    # Tách tri thức cố định (bảng giá, FAQ, cẩm nang văn phong) và các đoạn chat mẫu (dùng RAG lọc)
    static_items = [i for i in knowledge_items if i["category"] != "sample_chat"]
    sample_items = [i for i in knowledge_items if i["category"] == "sample_chat"]

    # Query RAG dựa trên 3 tin nhắn gần nhất của khách
    recent_customer_msgs = [m["text"] for m in messages if m["sender"] == "customer"][-3:]
    query_text = "\n".join(recent_customer_msgs)
    selected_samples = _select_relevant_samples(client, query_text, sample_items, top_k=TOP_K_SAMPLES)

    static_kb_block = "\n\n".join(
        f"### [{item['category'].upper()}] {item['title']}\n{item['content']}"
        for item in static_items
    ) or "Chưa có bảng giá hoặc tài liệu cố định."

    rules_block = "\n".join(f"- {r['rule_text']}" for r in rules) or "Chưa có quy tắc bổ sung."

    static_instruction = f"""Bạn là nhân viên tư vấn của một studio chụp ảnh, đang nhắn tin trực tiếp với khách hàng qua Facebook Messenger.
Mục tiêu: Tư vấn thân thiện, tự nhiên như người thật (không sặc mùi AI), nắm bắt nhu cầu và khéo léo mời khách qua studio hoặc đặt lịch.

NGUYÊN TẮC BẮT BUỘC:
1. Chỉ dựa vào thông tin trong KHO TRI THỨC dưới đây để báo giá và chính sách. Không tự bịa giá hoặc khuyến mãi không có.
2. Trả lời ngắn gọn, chia ý rõ ràng giống cách nhắn tin Messenger thực tế (tránh viết thành bài văn dài).
3. Tuân thủ tuyệt đối toàn bộ các QUY TẮC TỪ CHỦ STUDIO (Feedback Rules).

=== BẢNG GIÁ, CHÍNH SÁCH & CẨM NANG VĂN PHONG ===
{static_kb_block}

=== QUY TẮC TỪ CHỦ STUDIO (ƯU TIÊN CAO NHẤT) ===
{rules_block}
"""

    samples_block = "\n\n".join(
        f"--- Mẫu tham khảo ({s['title']}) ---\n{s['content']}"
        for s in selected_samples
    )

    history_lines = []
    for m in messages:
        role_label = "Khách" if m["sender"] == "customer" else "Studio"
        history_lines.append(f"{role_label}: {m['text']}")
    history_block = "\n".join(history_lines)

    dynamic_prompt = ""
    if samples_block:
        dynamic_prompt += f"=== {len(selected_samples)} ĐOẠN CHAT MẪU LIÊN QUAN NHẤT (RAG) ===\n{samples_block}\n\n"
    dynamic_prompt += f"""=== LỊCH SỬ HỘI THOẠI HIỆN TẠI ===
{history_block}

Hãy viết câu trả lời tiếp theo của Studio gửi cho khách (chỉ xuất nội dung tin nhắn, không thêm tiền tố "Studio:" hay giải thích)."""

    cache_name, cache_status = _get_or_create_cache(client, model, static_instruction)

    try:
        if cache_name:
            config = types.GenerateContentConfig(cached_content=cache_name, temperature=0.6)
        else:
            config = types.GenerateContentConfig(system_instruction=static_instruction, temperature=0.6)

        response = client.models.generate_content(
            model=model,
            contents=dynamic_prompt,
            config=config,
        )
        usage = getattr(response, "usage_metadata", None)
        LAST_RUN_STATS = {
            "model": model,
            "rag_used": len(selected_samples),
            "rag_total": len(sample_items),
            "cache_status": cache_status,
            "prompt_tokens": getattr(usage, "prompt_token_count", 0) or 0,
            "cached_tokens": getattr(usage, "cached_content_token_count", 0) or 0,
            "output_tokens": getattr(usage, "candidates_token_count", 0) or 0,
        }
        return (response.text or "").strip()
    except Exception as exc:
        return f"[Lỗi gọi Gemini API: {exc}]"


def distill_feedback_rule(ai_message: str, user_feedback: str) -> str:
    """Chuyển góp ý thô của chủ studio thành một quy tắc ngắn gọn, rõ ràng để AI ghi nhớ."""
    client = _get_client()
    if not client:
        return user_feedback.strip()

    prompt = f"""Chủ studio vừa góp ý để sửa câu trả lời của AI.
- Câu AI đã viết: "{ai_message}"
- Góp ý của chủ studio: "{user_feedback}"

Hãy tóm tắt góp ý trên thành 1 quy tắc ngắn gọn, súc tích, mang tính chỉ dẫn trực tiếp (bắt đầu bằng động từ hoặc điều kiện "Khi khách...") để đưa vào System Prompt cho AI áp dụng ở các lần tư vấn sau. Chỉ trả về đúng 1 câu quy tắc."""

    try:
        response = client.models.generate_content(
            model=_get_model(),
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2),
        )
        text = (response.text or "").strip()
        invalidate_cache()
        return text if text else user_feedback.strip()
    except Exception:
        return user_feedback.strip()
