import os
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "studio_bot.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                psid TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'draft', -- 'draft' | 'auto' | 'manual'
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                psid TEXT NOT NULL,
                sender TEXT NOT NULL, -- 'customer' | 'ai' | 'admin'
                text TEXT NOT NULL,
                is_draft INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (psid) REFERENCES conversations(psid)
            );

            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general', -- 'pricing' | 'sample_chat' | 'faq' | 'style_guide' | 'general'
                content TEXT NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '',
                embedding_json TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS feedback_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_text TEXT NOT NULL,
                source_ai_message TEXT,
                user_feedback TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Safe migration if knowledge table already existed without meta_json / embedding_json
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(knowledge)").fetchall()]
        if "meta_json" not in cols:
            conn.execute("ALTER TABLE knowledge ADD COLUMN meta_json TEXT NOT NULL DEFAULT ''")
        if "embedding_json" not in cols:
            conn.execute("ALTER TABLE knowledge ADD COLUMN embedding_json TEXT NOT NULL DEFAULT ''")


def upsert_conversation(psid: str, customer_name: str | None = None) -> None:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT psid, customer_name FROM conversations WHERE psid = ?", (psid,)
        ).fetchone()
        if existing:
            if customer_name and existing["customer_name"].startswith("Khách "):
                conn.execute(
                    "UPDATE conversations SET customer_name = ?, updated_at = CURRENT_TIMESTAMP WHERE psid = ?",
                    (customer_name, psid),
                )
            else:
                conn.execute(
                    "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE psid = ?",
                    (psid,),
                )
        else:
            name = customer_name or f"Khách {psid[-4:]}"
            conn.execute(
                "INSERT INTO conversations (psid, customer_name, mode) VALUES (?, ?, 'draft')",
                (psid, name),
            )


def get_conversation(psid: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE psid = ?", (psid,)).fetchone()
        return dict(row) if row else None


def list_conversations() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT id FROM messages m WHERE m.psid = c.psid AND m.is_draft = 0 ORDER BY m.id DESC LIMIT 1) AS last_msg_id,
                   (SELECT sender FROM messages m WHERE m.psid = c.psid AND m.is_draft = 0 ORDER BY m.id DESC LIMIT 1) AS last_sender,
                   (SELECT text FROM messages m WHERE m.psid = c.psid ORDER BY m.id DESC LIMIT 1) AS last_message,
                   (SELECT COUNT(*) FROM messages m WHERE m.psid = c.psid AND m.is_draft = 1) AS has_draft
            FROM conversations c
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def set_conversation_mode(psid: str, mode: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE conversations SET mode = ? WHERE psid = ?", (mode, psid))


def add_message(psid: str, sender: str, text: str, is_draft: bool = False) -> int:
    with get_conn() as conn:
        if not is_draft:
            # Clear older pending drafts when a real message is sent
            conn.execute("DELETE FROM messages WHERE psid = ? AND is_draft = 1", (psid,))
        cur = conn.execute(
            "INSERT INTO messages (psid, sender, text, is_draft) VALUES (?, ?, ?, ?)",
            (psid, sender, text, 1 if is_draft else 0),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE psid = ?",
            (psid,),
        )
        return int(cur.lastrowid)


def get_messages(psid: str, limit: int = 50) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages WHERE psid = ? ORDER BY id DESC LIMIT ?
            ) sub ORDER BY id ASC
            """,
            (psid, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def replace_draft(psid: str, text: str) -> int:
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE psid = ? AND is_draft = 1", (psid,))
        cur = conn.execute(
            "INSERT INTO messages (psid, sender, text, is_draft) VALUES (?, 'ai', ?, 1)",
            (psid, text),
        )
        return int(cur.lastrowid)


def list_knowledge() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM knowledge ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def add_knowledge(title: str, category: str, content: str, meta_json: str = "", embedding_json: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO knowledge (title, category, content, meta_json, embedding_json) VALUES (?, ?, ?, ?, ?)",
            (title, category, content, meta_json, embedding_json),
        )
        return int(cur.lastrowid)


def upsert_fb_knowledge(fb_conv_id: str, title: str, content: str, meta_json: str, embedding_json: str = "") -> int:
    with get_conn() as conn:
        if fb_conv_id:
            conn.execute(
                "DELETE FROM knowledge WHERE category = 'sample_chat' AND meta_json LIKE ?",
                (f'%"fb_conv_id": "{fb_conv_id}"%',),
            )
        cur = conn.execute(
            "INSERT INTO knowledge (title, category, content, meta_json, embedding_json) VALUES (?, 'sample_chat', ?, ?, ?)",
            (title, content, meta_json, embedding_json),
        )
        return int(cur.lastrowid)


def update_knowledge_embedding(item_id: int, embedding_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE knowledge SET embedding_json = ? WHERE id = ?",
            (embedding_json, item_id),
        )


def upsert_style_guide(content: str) -> int:
    with get_conn() as conn:
        conn.execute("DELETE FROM knowledge WHERE category = 'style_guide'")
        cur = conn.execute(
            "INSERT INTO knowledge (title, category, content) VALUES (?, 'style_guide', ?)",
            ("Cẩm nang văn phong & kịch bản Studio (Tự động nén)", content),
        )
        return int(cur.lastrowid)


def delete_knowledge(item_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM knowledge WHERE id = ?", (item_id,))


def list_rules() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM feedback_rules ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def add_rule(rule_text: str, source_ai_message: str = "", user_feedback: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO feedback_rules (rule_text, source_ai_message, user_feedback) VALUES (?, ?, ?)",
            (rule_text, source_ai_message, user_feedback),
        )
        return int(cur.lastrowid)


def delete_rule(rule_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM feedback_rules WHERE id = ?", (rule_id,))
