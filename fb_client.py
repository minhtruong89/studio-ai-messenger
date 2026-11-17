import os
from typing import Any
import httpx

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


def _get_token() -> str:
    return os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()


def is_configured() -> bool:
    token = _get_token()
    return bool(token and token != "your_page_access_token_here")


async def send_message(psid: str, text: str) -> dict[str, Any]:
    """Gửi tin nhắn văn bản ngược lại cho khách trên Facebook Messenger."""
    if psid.startswith("sim_"):
        return {"ok": True, "simulated": True}

    if not is_configured():
        return {"ok": False, "error": "Chưa cấu hình FB_PAGE_ACCESS_TOKEN trong .env"}

    url = f"{GRAPH_API_BASE}/me/messages"
    params = {"access_token": _get_token()}
    payload = {
        "recipient": {"id": psid},
        "messaging_type": "RESPONSE",
        "message": {"text": text},
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, params=params, json=payload)
        data = resp.json()
        if resp.status_code >= 400:
            return {"ok": False, "error": data.get("error", {}).get("message", resp.text)}
        return {"ok": True, "data": data}


async def get_user_name(psid: str) -> str | None:
    if psid.startswith("sim_") or not is_configured():
        return None

    url = f"{GRAPH_API_BASE}/{psid}"
    params = {"fields": "name,first_name,last_name", "access_token": _get_token()}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("name") or (
                    f"{data.get('first_name', '')} {data.get('last_name', '')}".strip() or None
                )
    except Exception:
        pass
    return None


async def get_page_info() -> dict[str, Any]:
    if not is_configured():
        return {}
    url = f"{GRAPH_API_BASE}/me"
    params = {"fields": "id,name", "access_token": _get_token()}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return {}


async def fetch_page_conversations(limit: int = 15) -> dict[str, Any]:
    """Kéo danh sách các đoạn hội thoại gần nhất từ Fanpage để làm dữ liệu mẫu cho AI học."""
    if not is_configured():
        return {"ok": False, "error": "Chưa cấu hình FB_PAGE_ACCESS_TOKEN trong .env"}

    url = f"{GRAPH_API_BASE}/me/conversations"
    params = {
        "platform": "messenger",
        "fields": "id,updated_time,participants,messages.limit(25){message,from,created_time}",
        "limit": limit,
        "access_token": _get_token(),
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, params=params)
        data = resp.json()
        if resp.status_code >= 400:
            return {"ok": False, "error": data.get("error", {}).get("message", resp.text)}
        return {"ok": True, "conversations": data.get("data", [])}
