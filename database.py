import sqlite3
import datetime
import os
from typing import List, Dict, Optional

DB_FILE = os.path.join(os.path.dirname(__file__), "meetings.db")

def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Meetings table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS meetings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meet_url TEXT NOT NULL,
        title TEXT,
        start_time TEXT NOT NULL,
        end_time TEXT,
        status TEXT DEFAULT 'active',
        live_summary TEXT,
        final_summary TEXT,
        summary_language TEXT DEFAULT 'English'
    );
    """)

    # 2. Transcripts table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id INTEGER NOT NULL,
        speaker_id TEXT NOT NULL,
        speaker_name TEXT,
        text TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        detected_language TEXT,
        FOREIGN KEY (meeting_id) REFERENCES meetings (id)
    );
    """)

    # 3. Action Items table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS action_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id INTEGER NOT NULL,
        task TEXT NOT NULL,
        assignee TEXT,
        deadline TEXT,
        FOREIGN KEY (meeting_id) REFERENCES meetings (id)
    );
    """)

    # 4. Decisions table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id INTEGER NOT NULL,
        decision TEXT NOT NULL,
        FOREIGN KEY (meeting_id) REFERENCES meetings (id)
    );
    """)

    conn.commit()
    conn.close()

def create_meeting(meet_url: str, title: str = "Google Meet Session") -> int:
    conn = get_connection()
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO meetings (meet_url, title, start_time, status) VALUES (?, ?, ?, 'active')",
        (meet_url, title, now_str)
    )
    meeting_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return meeting_id

def end_meeting(meeting_id: int, final_summary: str = "", summary_language: str = "English"):
    conn = get_connection()
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE meetings SET end_time = ?, status = 'completed', final_summary = ?, summary_language = ? WHERE id = ?",
        (now_str, final_summary, summary_language, meeting_id)
    )
    conn.commit()
    conn.close()

def add_transcript_entry(meeting_id: int, speaker_id: str, speaker_name: str, text: str, timestamp: str, detected_language: str = "Unknown"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO transcripts (meeting_id, speaker_id, speaker_name, text, timestamp, detected_language) VALUES (?, ?, ?, ?, ?, ?)",
        (meeting_id, speaker_id, speaker_name, text, timestamp, detected_language)
    )
    conn.commit()
    conn.close()

def update_meeting_live_summary(meeting_id: int, live_summary: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE meetings SET live_summary = ? WHERE id = ?",
        (live_summary, meeting_id)
    )
    conn.commit()
    conn.close()

def update_meeting_final_summary(meeting_id: int, final_summary: str, summary_language: str = "English"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE meetings SET final_summary = ?, summary_language = ? WHERE id = ?",
        (final_summary, summary_language, meeting_id)
    )
    conn.commit()
    conn.close()

def get_meeting(meeting_id: int) -> Optional[Dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_transcripts(meeting_id: int) -> List[Dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transcripts WHERE meeting_id = ? ORDER BY id ASC", (meeting_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_meetings() -> List[Dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT 50")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# Initialize tables automatically on import
init_db()
