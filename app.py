import json
import os
import sys
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import default
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse

# Prevent [Errno 5] Input/output error on background/daemon stdout writes
class SafeWriter:
    def __init__(self, target):
        self.target = target
    def write(self, s):
        try:
            if self.target and not getattr(self.target, 'closed', False):
                self.target.write(s)
                self.target.flush()
        except Exception:
            pass
    def flush(self):
        try:
            if self.target and not getattr(self.target, 'closed', False):
                self.target.flush()
        except Exception:
            pass

if not isinstance(sys.stdout, SafeWriter):
    sys.stdout = SafeWriter(sys.stdout)
if not isinstance(sys.stderr, SafeWriter):
    sys.stderr = SafeWriter(sys.stderr)

import database
from notes_agent import (
    transcribe_audio,
    create_notes,
    create_live_summary,
    create_final_summary
)
from meet_bot import GoogleMeetBot

PORT = 8000

# Global state for background meeting bot session
current_bot_session = {
    "bot": None,
    "thread": None,
    "meeting_id": None,
    "status": "idle", # "idle", "joining", "waiting_admit", "listening", "completed", "error"
    "status_message": "Ready",
    "transcript": [],
    "live_summary": "",
    "final_summary": "",
    "summary_language": "English",
    "audio_capture_active": False,
    "error": ""
}

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Google Meet AI Notetaker & Real-Time Transcriber</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background: #f8fafc; color: #1e293b; padding: 32px 16px; display: flex; justify-content: center; }
    .container { width: 100%; max-width: 880px; background: white; padding: 32px; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.06); }
    h1 { font-size: 24px; font-weight: 700; margin-bottom: 6px; color: #0f172a; }
    p.sub { font-size: 14px; color: #64748b; margin-bottom: 22px; }
    .tab-bar { display: flex; gap: 8px; margin-bottom: 24px; background: #f1f5f9; padding: 4px; border-radius: 10px; }
    .tab { flex: 1; padding: 11px 8px; text-align: center; font-size: 13px; font-weight: 600; border-radius: 8px; cursor: pointer; border: none; background: transparent; color: #64748b; transition: 0.15s; }
    .tab.active { background: white; color: #0f172a; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
    .section { display: none; }
    .section.active { display: block; }
    input[type="text"], input[type="file"], textarea, select { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 14px; outline: none; margin-bottom: 14px; }
    textarea { height: 130px; resize: vertical; }
    .btn-group { display: flex; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
    button.primary-btn { flex: 1; background: #2563eb; color: white; border: none; padding: 13px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; transition: 0.2s; }
    button.primary-btn:hover { background: #1d4ed8; }
    button.action-btn { background: #059669; color: white; border: none; padding: 10px 18px; border-radius: 8px; font-size: 13.5px; font-weight: 600; cursor: pointer; }
    button.action-btn:hover { background: #047857; }
    button.danger-btn { background: #dc2626; color: white; border: none; padding: 12px 18px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; }
    button.danger-btn:hover { background: #b91c1c; }
    button.secondary-btn { background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; padding: 8px 12px; border-radius: 6px; font-size: 12.5px; font-weight: 600; cursor: pointer; }
    button.secondary-btn:hover { background: #e2e8f0; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .result-box { margin-top: 22px; padding: 20px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; }
    .result-box h3 { font-size: 13px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; color: #64748b; margin-bottom: 10px; }
    .result-content { font-size: 14.5px; line-height: 1.6; white-space: pre-wrap; color: #0f172a; }
    .transcript-box { margin-bottom: 20px; }
    .error-box { margin-top: 14px; padding: 12px; background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; border-radius: 8px; font-size: 14px; display: none; }
    .status-badge { display: inline-block; padding: 6px 14px; border-radius: 50px; font-size: 12.5px; font-weight: 700; }
    .status-badge.listening { background: #dcfce7; color: #15803d; }
    .status-badge.joining { background: #fef9c3; color: #a16207; }
    .status-badge.waiting_admit { background: #ffedd5; color: #c2410c; }
    .status-badge.completed { background: #e0f2fe; color: #0369a1; }
    .live-entry { padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-size: 14px; line-height: 1.5; }
    .live-entry b { color: #1e40af; font-weight: 600; }
    .lang-tag { display: inline-block; background: #e0e7ff; color: #3730a3; padding: 1px 6px; border-radius: 4px; font-size: 11px; font-weight: 700; margin-left: 4px; vertical-align: middle; }
    .admit-alert { background: #eff6ff; border-left: 4px solid #3b82f6; padding: 12px; border-radius: 6px; font-size: 13px; color: #1e40af; margin-bottom: 16px; line-height: 1.5; }
    .live-indicator { display: inline-block; width: 9px; height: 9px; border-radius: 50%; background: #22c55e; margin-right: 6px; animation: blink 1s infinite; }
    @keyframes blink { 0% { opacity: 1; } 50% { opacity: 0.3; } 100% { opacity: 1; } }
    .summary-card { background: white; padding: 16px; border-radius: 8px; border: 1px solid #e2e8f0; margin-top: 12px; }
    .history-table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 13px; }
    .history-table th, .history-table td { padding: 10px; border-bottom: 1px solid #e2e8f0; text-align: left; }
    .history-table th { background: #f8fafc; font-weight: 600; color: #475569; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Google Meet AI Meeting Assistant</h1>
    <p class="sub">Direct Speech-to-Text, Multilingual Detection & AI Structured Notes</p>

    <div class="tab-bar">
      <button class="tab active" onclick="switchTab('meet')">🎥 Google Meet Bot</button>
      <button class="tab" onclick="switchTab('upload')">📁 Upload Audio/Video</button>
      <button class="tab" onclick="switchTab('record')">🎙️ Record Voice</button>
      <button class="tab" onclick="switchTab('text')">✍️ Direct Text</button>
    </div>

    <!-- 1. GOOGLE MEET BOT TAB -->
    <div id="tab-meet" class="section active">
      <div class="admit-alert">
        💡 <b>AI Meeting Assistant:</b> Enter your Google Meet link. The bot enters the call, directly listens to all participants speaking, auto-detects multilingual speech (English, Gujarati, Hindi, Marathi, etc.), streams live transcripts, and creates structured meeting summaries.
      </div>

      <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 12px; margin-bottom: 12px;">
        <div>
          <label style="display:block; font-size: 13px; font-weight: 600; margin-bottom: 6px;">Google Meet URL</label>
          <input type="text" id="meet-url-input" placeholder="https://meet.google.com/akj-qbeg-kfg" style="margin-bottom: 0;">
        </div>
        <div>
          <label style="display:block; font-size: 13px; font-weight: 600; margin-bottom: 6px;">Summary Language</label>
          <select id="meet-summary-lang" style="margin-bottom: 0;">
            <option value="English" selected>English</option>
            <option value="Gujarati">Gujarati (ગુજરાતી)</option>
            <option value="Hindi">Hindi (हिन्दी)</option>
          </select>
        </div>
      </div>

      <button class="primary-btn" id="start-bot-btn" onclick="startMeetingBot()">
        🚀 Send Bot to Join Meeting
      </button>

      <div id="bot-active-controls" style="display:none; margin-top: 14px;">
        <button class="danger-btn" id="stop-bot-btn" onclick="stopMeetingSession()" style="width: 100%;">
          🛑 Leave Meeting
        </button>
      </div>

      <div id="meet-error" class="error-box"></div>

      <!-- Live Stream Results -->
      <div id="meet-result" class="result-box" style="display:none;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 8px;">
          <div id="meet-status-container" style="display: flex; gap: 8px; align-items: center;">
            <span id="meet-badge" class="status-badge joining">Connecting...</span>
            <span id="meet-audio-badge" class="status-badge" style="background:#f1f5f9; color:#64748b; display:none;">
              <span class="live-indicator"></span> 🟢 Audio Capture Active
            </span>
          </div>
          <div style="display: flex; gap: 6px;">
            <button class="secondary-btn" onclick="copyMeetTranscript()">📋 Copy</button>
            <button class="secondary-btn" onclick="downloadTranscript()">📥 Download Transcript</button>
            <button class="secondary-btn" onclick="clearMeetTranscript()">🗑️ Clear</button>
          </div>
        </div>

        <!-- 1. Live Conversation Stream -->
        <div id="meet-transcript-box" class="transcript-box">
          <h3>🎙️ Google Meet Live Conversation Stream</h3>
          <div id="meet-transcript-output" class="result-content" style="color: #334155; max-height: 240px; overflow-y: auto; background: white; padding: 14px; border-radius: 8px; border: 1px solid #e2e8f0;">
            <i style="color:#94a3b8;">Bot is joining Google Meet. Conversation between participants will stream here live...</i>
          </div>
        </div>

        <!-- 2. Live Meeting Summary Box -->
        <div style="margin-top: 20px;">
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <h3 style="margin-bottom:0;">📌 Live Meeting Summary (Ongoing Topics & Decisions)</h3>
            <button class="secondary-btn" onclick="refreshLiveSummary()">⚡ Refresh Live Summary</button>
          </div>
          <div id="meet-live-summary-output" class="result-content summary-card" style="min-height: 60px;">
            <i style="color:#94a3b8;">Live discussion points and decisions will update automatically during the call...</i>
          </div>
        </div>

        <!-- 3. Final Comprehensive Summary Box -->
        <div style="margin-top: 24px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
            <h3 style="margin-bottom:0;">📝 Final Meeting Summary (Decisions & Action Items)</h3>
            <div style="display: flex; gap: 8px;">
              <button class="action-btn" id="meet-final-summarize-btn" onclick="generateFinalSummary()">
                🏁 Generate Final Summary
              </button>
              <button class="secondary-btn" id="download-summary-btn" onclick="downloadSummary()">
                📥 Download Summary
              </button>
            </div>
          </div>
          <div id="meet-final-summary-output" class="result-content summary-card" style="min-height: 80px;">
            <i style="color:#94a3b8;">Final structured summary will appear here once generated.</i>
          </div>
        </div>
      </div>

      <!-- Past Meetings History -->
      <div style="margin-top: 36px; padding-top: 20px; border-top: 1px solid #e2e8f0;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <h3 style="font-size: 14px; font-weight: 700; color: #475569;">📜 Past Meetings History (Saved in SQLite)</h3>
          <button class="secondary-btn" onclick="loadMeetingsHistory()">🔄 Refresh History</button>
        </div>
        <div id="meetings-history-list" style="margin-top: 10px; overflow-x: auto;">
          <i style="color:#94a3b8; font-size: 13px;">Loading meetings...</i>
        </div>
      </div>
    </div>

    <!-- 2. UPLOAD AUDIO / VIDEO TAB -->
    <div id="tab-upload" class="section">
      <div class="admit-alert" style="background:#f8fafc; border-color:#94a3b8; color:#334155;">
        📁 <b>Audio/Video File Transcriber:</b> Upload any recording (MP3, WAV, MP4, MOV). Processed via Sarvam AI & OpenRouter.
      </div>
      <input type="file" id="upload-file-input" accept="audio/*,video/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.webm,.mp4,.mov,.mkv,.avi">
      <button class="primary-btn" id="upload-btn" onclick="submitUploadFile()">Transcribe & Generate Notes</button>

      <div id="upload-error" class="error-box"></div>

      <div id="upload-result" class="result-box" style="display:none;">
        <div id="upload-transcript-box" class="transcript-box">
          <h3>Transcript (Sarvam AI)</h3>
          <div id="upload-transcript-output" class="result-content" style="color: #475569; max-height: 250px; overflow-y: auto; background: white; padding: 12px; border-radius: 8px;"></div>
        </div>
        <div>
          <h3>Summary Notes (OpenRouter LangChain)</h3>
          <div id="upload-notes-output" class="result-content" style="background: white; padding: 14px; border-radius: 8px;"></div>
        </div>
      </div>
    </div>

    <!-- 3. RECORD VOICE TAB (INSTANT REAL-TIME SPEECH-TO-TEXT) -->
    <div id="tab-record" class="section">
      <div style="display: flex; gap: 12px; align-items: center; max-width: 480px; margin: 0 auto 16px auto;">
        <label style="font-size: 13px; font-weight: 600; color: #475569; white-space: nowrap;">Spoken Language:</label>
        <select id="record-speech-lang" style="margin-bottom: 0;" onchange="onRecordLangChange()">
          <option value="gu-IN" selected>🇮🇳 Gujarati (ગુજરાતી)</option>
          <option value="hi-IN">🇮🇳 Hindi / Hinglish (हिन्दी)</option>
          <option value="en-IN">🇮🇳 English - India</option>
          <option value="en-US">🇺🇸 English - US</option>
          <option value="mr-IN">🇮🇳 Marathi (मराठी)</option>
          <option value="ur-IN">🇮🇳 Urdu (اردو)</option>
          <option value="te-IN">🇮🇳 Telugu (తెలుగు)</option>
          <option value="ta-IN">🇮🇳 Tamil (தமிழ்)</option>
          <option value="bn-IN">🇮🇳 Bengali (বাংলা)</option>
        </select>
      </div>

      <div style="text-align: center; padding: 16px 0;">
        <button id="record-btn" class="primary-btn" style="width: auto; padding: 14px 32px; border-radius: 50px; background: #ef4444; box-shadow: 0 4px 14px rgba(239,68,68,0.3); font-size: 15px; font-weight: 700;" onclick="toggleRecord()">
          <span id="record-icon">🎤</span> <span id="record-text">Start Recording Voice</span>
        </button>
        <p id="record-status" style="margin-top: 12px; font-size: 13px; color: #64748b; min-height: 18px;"></p>
      </div>

      <div id="record-error" class="error-box"></div>

      <div id="record-result" class="result-box" style="display:none;">
        <div id="record-transcript-box">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <h3 style="color: #1e293b; font-size: 14px; font-weight: 700;">🎙️ Spoken Output (Exact Speech-to-Text)</h3>
            <button class="secondary-btn" onclick="copyRecordTranscript()">📋 Copy</button>
          </div>
          <div id="record-transcript-output" class="result-content" style="color: #0f172a; font-size: 17px; font-weight: 500; line-height: 1.6; max-height: 280px; overflow-y: auto; background: white; padding: 16px; border-radius: 8px; border: 1px solid #cbd5e1; min-height: 60px;">
            <i style="color:#94a3b8;">Listening... speak now, words will appear here immediately.</i>
          </div>
        </div>

        <div style="margin-top: 14px; text-align: right;">
          <button class="action-btn" id="record-summarize-btn" onclick="summarizeRecordedSpeech()" style="font-size: 13px; padding: 9px 18px; display: none;">
            ⚡ Generate Notes from this Speech
          </button>
        </div>

        <div id="record-notes-box" style="display:none; margin-top: 14px; padding-top: 14px; border-top: 1px solid #e2e8f0;">
          <h3 style="color: #1e293b; font-size: 14px; font-weight: 700;">📝 Meeting Notes (OpenRouter LangChain)</h3>
          <div id="record-notes-output" class="result-content" style="background: white; padding: 16px; border-radius: 8px; border: 1px solid #e2e8f0;"></div>
        </div>
      </div>
    </div>

    <!-- 4. DIRECT TEXT TAB -->
    <div id="tab-text" class="section">
      <textarea id="direct-text-input" placeholder="Type or paste any meeting discussion, transcripts or notes here..."></textarea>
      <button class="primary-btn" id="text-btn" onclick="submitDirectText()">Generate Notes from Text</button>

      <div id="text-error" class="error-box"></div>

      <div id="text-result" class="result-box" style="display:none;">
        <div>
          <h3>Summary Notes (OpenRouter LangChain)</h3>
          <div id="text-notes-output" class="result-content" style="background: white; padding: 14px; border-radius: 8px;"></div>
        </div>
      </div>
    </div>
  </div>

  <script>
    let pollInterval = null;
    let allMeetEntries = [];
    let currentMeetingId = null;

    function switchTab(tab) {
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
      event.target.classList.add('active');
      document.getElementById('tab-' + tab).classList.add('active');
      if (tab === 'meet') {
        loadMeetingsHistory();
      }
    }

    // ==========================================
    // GOOGLE MEET BOT AUTOMATION
    // ==========================================
    async function startMeetingBot() {
      const url = document.getElementById('meet-url-input').value.trim();
      const lang = document.getElementById('meet-summary-lang').value;
      const errBox = document.getElementById('meet-error');
      const resBox = document.getElementById('meet-result');
      const startBtn = document.getElementById('start-bot-btn');
      const activeControls = document.getElementById('bot-active-controls');
      const badge = document.getElementById('meet-badge');

      if (!url) {
        errBox.innerText = 'Please enter a valid Google Meet URL.';
        errBox.style.display = 'block';
        return;
      }

      errBox.style.display = 'none';
      startBtn.style.display = 'none';
      activeControls.style.display = 'block';
      resBox.style.display = 'block';

      badge.className = 'status-badge joining';
      badge.innerText = 'Bot Joining Call...';
      document.getElementById('meet-transcript-output').innerHTML = '<i>Bot is joining call. Please click "Admit" in Google Meet when prompted...</i>';
      document.getElementById('meet-live-summary-output').innerHTML = '<i>Live discussion points will stream here during the call...</i>';
      document.getElementById('meet-final-summary-output').innerText = 'Final structured summary will appear here once generated.';

      try {
        const res = await fetch('/api/bot/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, language: lang })
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        if (pollInterval) clearInterval(pollInterval);
        pollInterval = setInterval(pollBotStatus, 400);
      } catch (e) {
        errBox.innerText = e.message;
        errBox.style.display = 'block';
        startBtn.style.display = 'block';
        activeControls.style.display = 'none';
      }
    }

    async function pollBotStatus() {
      try {
        const res = await fetch('/api/bot/status');
        const data = await res.json();

        const badge = document.getElementById('meet-badge');
        const audioBadge = document.getElementById('meet-audio-badge');
        const trOut = document.getElementById('meet-transcript-output');
        const liveOut = document.getElementById('meet-live-summary-output');
        const finalOut = document.getElementById('meet-final-summary-output');

        if (data.meeting_id) {
          currentMeetingId = data.meeting_id;
        }

        if (data.audio_capture_active) {
          audioBadge.style.display = 'inline-block';
        }

        if (data.status === 'joining') {
          badge.className = 'status-badge joining';
          badge.innerText = data.status_message || 'Joining Meeting...';
        } else if (data.status === 'waiting_admit') {
          badge.className = 'status-badge waiting_admit';
          badge.innerText = '⚠️ Click "Admit" in Google Meet!';
        } else if (data.status === 'listening') {
          badge.className = 'status-badge listening';
          badge.innerHTML = '<span class="live-indicator"></span> 🔴 Connected in Call - Bot Listening';
          if (!data.transcript || data.transcript.length === 0) {
            trOut.innerHTML = '<i>🔴 Connected inside Google Meet! Listening directly to speech. Dialogue will stream here live...</i>';
          }
        } else if (data.status === 'completed') {
          badge.className = 'status-badge completed';
          badge.innerText = '✅ Session Completed';
          clearInterval(pollInterval);
          document.getElementById('start-bot-btn').style.display = 'block';
          document.getElementById('bot-active-controls').style.display = 'none';
          loadMeetingsHistory();
        } else if (data.status === 'error') {
          badge.className = 'status-badge';
          badge.style.background = '#fef2f2';
          badge.style.color = '#991b1b';
          badge.innerText = '❌ Error';
          clearInterval(pollInterval);
          document.getElementById('start-bot-btn').style.display = 'block';
          document.getElementById('bot-active-controls').style.display = 'none';
          document.getElementById('meet-error').innerText = data.error || 'Failed to connect bot.';
          document.getElementById('meet-error').style.display = 'block';
        }

        if (data.transcript && data.transcript.length > 0) {
          allMeetEntries = data.transcript;
          trOut.innerHTML = allMeetEntries.map(t => '<div class="live-entry">' + t + '</div>').join('');
          trOut.scrollTop = trOut.scrollHeight;
        }

        if (data.live_summary) {
          liveOut.innerText = data.live_summary;
        }

        if (data.final_summary) {
          finalOut.innerText = data.final_summary;
        }
      } catch (e) {}
    }

    async function refreshLiveSummary() {
      const liveOut = document.getElementById('meet-live-summary-output');
      liveOut.innerHTML = '<i>⚡ Updating Live Meeting Summary...</i>';
      try {
        const res = await fetch('/api/bot/generate_live_summary', { method: 'POST' });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        liveOut.innerText = data.live_summary;
      } catch(e) {
        liveOut.innerText = 'Notice: ' + e.message;
      }
    }

    async function generateFinalSummary() {
      const finalOut = document.getElementById('meet-final-summary-output');
      const lang = document.getElementById('meet-summary-lang').value;
      const genBtn = document.getElementById('meet-final-summarize-btn');

      if (genBtn) genBtn.disabled = true;
      finalOut.innerHTML = `<i>⚡ Generating Final Structured Meeting Summary in ${lang} with OpenRouter...</i>`;

      try {
        const res = await fetch('/api/bot/generate_final_summary', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ language: lang })
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        finalOut.innerText = data.final_summary;
      } catch (e) {
        finalOut.innerText = 'Error: ' + e.message;
      } finally {
        if (genBtn) genBtn.disabled = false;
        loadMeetingsHistory();
      }
    }

    async function stopMeetingSession() {
      const stopBtn = document.getElementById('stop-bot-btn');
      stopBtn.disabled = true;
      stopBtn.innerText = 'Leaving call...';

      try {
        await fetch('/api/bot/stop', { method: 'POST' });
      } catch(e){}

      clearInterval(pollInterval);
      stopBtn.disabled = false;
      stopBtn.innerText = '🛑 Leave Meeting';
      document.getElementById('start-bot-btn').style.display = 'block';
      document.getElementById('bot-active-controls').style.display = 'none';
      document.getElementById('meet-badge').className = 'status-badge completed';
      document.getElementById('meet-badge').innerText = '✅ Session Completed';
      loadMeetingsHistory();
    }

    function clearMeetTranscript() {
      allMeetEntries = [];
      document.getElementById('meet-transcript-output').innerHTML = '<i style="color:#94a3b8;">Transcript cleared.</i>';
    }

    function copyMeetTranscript() {
      const text = allMeetEntries.map(e => e.replace(/<[^>]+>/g, '')).join('\\n');
      navigator.clipboard.writeText(text);
      alert('Transcript copied to clipboard!');
    }

    function downloadTranscript() {
      if (!currentMeetingId && allMeetEntries.length === 0) {
        alert('No transcript to download.');
        return;
      }
      window.open('/api/meetings/download?type=transcript&id=' + (currentMeetingId || 'current'), '_blank');
    }

    function downloadSummary() {
      const finalOut = document.getElementById('meet-final-summary-output').innerText;
      if (!finalOut || finalOut.includes('will appear here')) {
        alert('Please generate the Final Summary before downloading.');
        return;
      }
      window.open('/api/meetings/download?type=summary&id=' + (currentMeetingId || 'current'), '_blank');
    }

    async function loadMeetingsHistory() {
      const el = document.getElementById('meetings-history-list');
      try {
        const res = await fetch('/api/meetings/history');
        const data = await res.json();
        if (!data || data.length === 0) {
          el.innerHTML = '<i style="color:#94a3b8; font-size: 13px;">No past meeting sessions found.</i>';
          return;
        }

        let html = '<table class="history-table"><thead><tr><th>ID</th><th>Meeting Link</th><th>Start Time</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
        data.forEach(m => {
          html += `<tr>
            <td>#${m.id}</td>
            <td><a href="${m.meet_url}" target="_blank" style="color:#2563eb; text-decoration:none;">${m.meet_url}</a></td>
            <td>${m.start_time || '-'}</td>
            <td><span style="font-size:11px; font-weight:700; color:${m.status === 'completed' ? '#059669':'#d97706'}">${m.status.toUpperCase()}</span></td>
            <td>
              <a href="/api/meetings/download?type=transcript&id=${m.id}" target="_blank" style="color:#2563eb; text-decoration:none; font-weight:600; margin-right:8px;">Transcript</a>
              <a href="/api/meetings/download?type=summary&id=${m.id}" target="_blank" style="color:#059669; text-decoration:none; font-weight:600;">Summary</a>
            </td>
          </tr>`;
        });
        html += '</tbody></table>';
        el.innerHTML = html;
      } catch(e) {
        el.innerHTML = '<i style="color:#94a3b8;">Unable to load history.</i>';
      }
    }

    // ==========================================
    // UPLOAD AUDIO / VIDEO
    // ==========================================
    async function submitUploadFile() {
      const fileInput = document.getElementById('upload-file-input');
      const resBox = document.getElementById('upload-result');
      const trBox = document.getElementById('upload-transcript-box');
      const trOut = document.getElementById('upload-transcript-output');
      const ntOut = document.getElementById('upload-notes-output');
      const errorBox = document.getElementById('upload-error');
      const btn = document.getElementById('upload-btn');

      if (!fileInput.files[0]) {
        errorBox.innerText = 'Please select an audio or video file first.';
        errorBox.style.display = 'block';
        return;
      }

      errorBox.style.display = 'none';
      resBox.style.display = 'block';
      trOut.innerText = 'Transcribing with Sarvam AI...';
      ntOut.innerHTML = '<i>Processing audio & generating notes...</i>';
      btn.disabled = true;

      const formData = new FormData();
      formData.append('audio', fileInput.files[0]);
      try {
        const res = await fetch('/api/audio', { method: 'POST', body: formData });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        trBox.style.display = 'block';
        trOut.innerText = data.transcript;
        ntOut.innerText = data.notes;
      } catch (e) {
        errorBox.innerText = e.message;
        errorBox.style.display = 'block';
      } finally {
        btn.disabled = false;
      }
    }

    // ==========================================
    // RECORD VOICE (DIRECT SPEECH-TO-TEXT)
    // ==========================================
    let liveRec = null;
    let isVoiceRecording = false;
    let liveVoiceFinal = "";

    function onRecordLangChange() {
      if (liveRec && isVoiceRecording) {
        try { liveRec.abort(); } catch(e){}
        isVoiceRecording = false;
        setTimeout(toggleRecord, 150);
      }
    }

    function copyRecordTranscript() {
      const text = document.getElementById('record-transcript-output').innerText;
      navigator.clipboard.writeText(text);
      alert('Transcript copied!');
    }

    async function toggleRecord() {
      const btn = document.getElementById('record-btn');
      const text = document.getElementById('record-text');
      const status = document.getElementById('record-status');
      const errorBox = document.getElementById('record-error');
      const resBox = document.getElementById('record-result');
      const trOut = document.getElementById('record-transcript-output');
      const sumBtn = document.getElementById('record-summarize-btn');
      const notesBox = document.getElementById('record-notes-box');
      const langEl = document.getElementById('record-speech-lang');
      const lang = (langEl ? langEl.value : '') || 'gu-IN';

      const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;

      if (!isVoiceRecording) {
        errorBox.style.display = 'none';
        resBox.style.display = 'block';
        sumBtn.style.display = 'none';
        notesBox.style.display = 'none';
        liveVoiceFinal = "";
        trOut.innerHTML = '<i style="color:#94a3b8;">Listening live... Speak now, your exact words will appear here immediately.</i>';

        if (SpeechRec) {
          try {
            liveRec = new SpeechRec();
            liveRec.continuous = true;
            liveRec.interimResults = true;
            liveRec.lang = lang;

            liveRec.onstart = () => {
              isVoiceRecording = true;
              btn.style.background = '#059669';
              text.innerText = '⏹️ Stop Recording';
              status.innerHTML = '<span class="live-indicator"></span> <b>Listening live!</b> Speaking...';
            };

            liveRec.onresult = (event) => {
              let interim = '';
              for (let i = event.resultIndex; i < event.results.length; ++i) {
                const piece = event.results[i][0].transcript;
                if (event.results[i].isFinal) {
                  liveVoiceFinal += piece + ' ';
                } else {
                  interim += piece;
                }
              }
              const displayFinal = liveVoiceFinal ? liveVoiceFinal : '';
              const displayInterim = interim ? `<span style="color:#94a3b8; font-style:italic;">${interim}</span>` : '';
              trOut.innerHTML = (displayFinal + displayInterim) || '<i style="color:#94a3b8;">Listening...</i>';
              trOut.scrollTop = trOut.scrollHeight;
            };

            liveRec.onerror = (err) => {
              console.warn('Live speech notice:', err.error);
            };

            liveRec.onend = () => {
              if (isVoiceRecording) {
                const lEl = document.getElementById('record-speech-lang');
                liveRec.lang = (lEl ? lEl.value : '') || 'gu-IN';
                try { liveRec.start(); } catch(e){}
              }
            };

            liveRec.start();
          } catch(e) {
            startMediaRecorderFallback();
          }
        } else {
          startMediaRecorderFallback();
        }
      } else {
        isVoiceRecording = false;
        if (liveRec) {
          try { liveRec.stop(); } catch(e){}
        }
        btn.style.background = '#ef4444';
        text.innerText = 'Start Recording Voice';
        status.innerHTML = '';

        const finalText = liveVoiceFinal.trim() || trOut.innerText.replace(/Listening live\\.\\.\\./g, '').trim();
        if (finalText) {
          trOut.innerText = finalText;
          sumBtn.style.display = 'inline-block';
        }
      }
    }

    async function summarizeRecordedSpeech() {
      const trOut = document.getElementById('record-transcript-output');
      const ntOut = document.getElementById('record-notes-output');
      const notesBox = document.getElementById('record-notes-box');
      const text = trOut.innerText.trim();

      if (!text) return;
      notesBox.style.display = 'block';
      ntOut.innerHTML = '<i>⚡ Generating notes with OpenRouter LangChain...</i>';

      try {
        const res = await fetch('/api/text', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: text })
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        ntOut.innerText = data.notes;
      } catch(e) {
        ntOut.innerHTML = '<span style="color:red;">Error: ' + e.message + '</span>';
      }
    }

    let fallbackRecorder = null;
    let fallbackChunks = [];

    async function startMediaRecorderFallback() {
      const btn = document.getElementById('record-btn');
      const text = document.getElementById('record-text');
      const status = document.getElementById('record-status');
      const errorBox = document.getElementById('record-error');

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        fallbackRecorder = new MediaRecorder(stream);
        fallbackChunks = [];
        fallbackRecorder.ondataavailable = e => { if (e.data.size > 0) fallbackChunks.push(e.data); };
        fallbackRecorder.onstop = async () => {
          const blob = new Blob(fallbackChunks, { type: 'audio/webm' });
          const formData = new FormData();
          formData.append('audio', blob, 'record.webm');
          status.innerHTML = 'Processing voice audio...';
          const res = await fetch('/api/audio', { method: 'POST', body: formData });
          const data = await res.json();
          document.getElementById('record-transcript-output').innerText = data.transcript || '';
          document.getElementById('record-notes-output').innerText = data.notes || '';
          document.getElementById('record-notes-box').style.display = 'block';
          status.innerHTML = '';
        };
        fallbackRecorder.start(500);
        isVoiceRecording = true;
        btn.style.background = '#059669';
        text.innerText = '⏹️ Stop Recording';
        status.innerHTML = '<span class="live-indicator"></span> Recording audio...';
      } catch(err) {
        errorBox.innerText = 'Microphone access denied: ' + err.message;
        errorBox.style.display = 'block';
      }
    }

    // ==========================================
    // DIRECT TEXT
    // ==========================================
    async function submitDirectText() {
      const text = document.getElementById('direct-text-input').value.trim();
      const resBox = document.getElementById('text-result');
      const ntOut = document.getElementById('text-notes-output');
      const errorBox = document.getElementById('text-error');
      const btn = document.getElementById('text-btn');

      if (!text) {
        errorBox.innerText = 'Please enter some text first.';
        errorBox.style.display = 'block';
        return;
      }

      errorBox.style.display = 'none';
      resBox.style.display = 'block';
      ntOut.innerHTML = '<i>Generating notes with OpenRouter LangChain...</i>';
      btn.disabled = true;

      try {
        const res = await fetch('/api/text', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text })
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        ntOut.innerText = data.notes;
      } catch (e) {
        errorBox.innerText = e.message;
        errorBox.style.display = 'block';
      } finally {
        btn.disabled = false;
      }
    }

    // Initial history load
    loadMeetingsHistory();
  </script>
</body>
</html>
"""

def bot_worker(meet_url, summary_lang="English"):
    global current_bot_session
    try:
        meeting_id = database.create_meeting(meet_url=meet_url, title="Google Meet Session")
        current_bot_session["meeting_id"] = meeting_id
        current_bot_session["status"] = "joining"
        current_bot_session["status_message"] = "Connecting bot..."
        current_bot_session["transcript"] = []
        current_bot_session["live_summary"] = ""
        current_bot_session["final_summary"] = ""
        current_bot_session["summary_language"] = summary_lang
        current_bot_session["audio_capture_active"] = False
        current_bot_session["error"] = ""

        bot = GoogleMeetBot(
            meet_url=meet_url,
            bot_name="AI Notetaker Bot",
            headless=False,
            meeting_id=meeting_id,
            summary_language=summary_lang
        )
        current_bot_session["bot"] = bot

        def on_speech(speaker, text, timestamp, lang="Unknown", is_update=False):
            current_bot_session["status"] = "listening"
            current_bot_session["status_message"] = "Listening to call..."
            current_bot_session["audio_capture_active"] = True
            entry = f"<b>[{timestamp}] {speaker} <span class='lang-tag'>{lang}</span>:</b> {text}"
            if is_update and current_bot_session["transcript"]:
                current_bot_session["transcript"][-1] = entry
            else:
                current_bot_session["transcript"].append(entry)

        def on_status(status_code, message):
            if status_code == "live_summary_update":
                current_bot_session["live_summary"] = message
            else:
                current_bot_session["status"] = status_code
                current_bot_session["status_message"] = message

        transcript, final_notes = bot.run(
            max_duration_minutes=180,
            on_speech_callback=on_speech,
            on_status_callback=on_status
        )

        current_bot_session["status"] = "completed"
        current_bot_session["final_summary"] = final_notes
        if not current_bot_session.get("live_summary"):
            current_bot_session["live_summary"] = bot.live_summary

    except Exception as e:
        current_bot_session["status"] = "error"
        current_bot_session["error"] = str(e)

class Handler(SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))

        elif path == "/api/bot/status":
            self._send_json({
                "status": current_bot_session["status"],
                "status_message": current_bot_session["status_message"],
                "transcript": current_bot_session["transcript"],
                "live_summary": current_bot_session["live_summary"],
                "final_summary": current_bot_session["final_summary"],
                "meeting_id": current_bot_session["meeting_id"],
                "audio_capture_active": current_bot_session["audio_capture_active"],
                "error": current_bot_session["error"]
            })

        elif path == "/api/meetings/history":
            try:
                meetings = database.get_all_meetings()
                self._send_json(meetings)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)

        elif path == "/api/meetings/download":
            dtype = params.get("type", ["transcript"])[0]
            mid_val = params.get("id", ["current"])[0]

            if mid_val == "current":
                mid = current_bot_session.get("meeting_id")
            else:
                try:
                    mid = int(mid_val)
                except Exception:
                    mid = None

            if dtype == "summary":
                # Get summary
                summary_content = ""
                if mid:
                    m = database.get_meeting(mid)
                    if m:
                        summary_content = m.get("final_summary") or m.get("live_summary") or ""
                if not summary_content:
                    summary_content = current_bot_session.get("final_summary") or current_bot_session.get("live_summary") or "No summary available."

                filename = f"Meeting_Summary_{mid or 'session'}.md"
                body = summary_content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename=\"{filename}\"")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                # Get transcript
                lines = []
                if mid:
                    entries = database.get_transcripts(mid)
                    for e in entries:
                        lines.append(f"[{e['timestamp']}] {e['speaker_name']} [{e['speaker_id']}] ({e['detected_language']}): {e['text']}")
                if not lines:
                    lines = [t.replace("<b>", "").replace("</b>", "").replace("<span class='lang-tag'>", "[").replace("</span>", "]") for t in current_bot_session.get("transcript", [])]

                transcript_content = "\n".join(lines) if lines else "No transcript available."
                filename = f"Meeting_Transcript_{mid or 'session'}.txt"
                body = transcript_content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename=\"{filename}\"")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/bot/start":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            url = data.get("url", "")
            lang = data.get("language", "English")

            if not url:
                self._send_json({"error": "Google Meet URL is required"}, status=400)
                return

            t = threading.Thread(target=bot_worker, args=(url, lang), daemon=True)
            current_bot_session["thread"] = t
            t.start()

            self._send_json({"message": "Bot launched successfully!"})

        elif path == "/api/bot/generate_live_summary":
            tr = current_bot_session.get("transcript", [])
            lang = current_bot_session.get("summary_language", "English")
            if not tr:
                self._send_json({"error": "No conversation speech recorded yet."}, status=400)
                return

            clean_text = "\n".join(t.replace("<b>", "").replace("</b>", "").replace("<span class='lang-tag'>", "[").replace("</span>", "]") for t in tr)
            try:
                ls = create_live_summary(clean_text, lang)
                current_bot_session["live_summary"] = ls
                mid = current_bot_session.get("meeting_id")
                if mid:
                    database.update_meeting_live_summary(mid, ls)
                self._send_json({"live_summary": ls})
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)

        elif path == "/api/bot/generate_final_summary":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
            data = json.loads(body) if body else {}
            lang = data.get("language") or current_bot_session.get("summary_language", "English")
            current_bot_session["summary_language"] = lang

            tr = current_bot_session.get("transcript", [])
            if not tr:
                self._send_json({"error": "No conversation speech recorded yet."}, status=400)
                return

            clean_text = "\n".join(t.replace("<b>", "").replace("</b>", "").replace("<span class='lang-tag'>", "[").replace("</span>", "]") for t in tr)
            try:
                fs = create_final_summary(clean_text, lang)
                current_bot_session["final_summary"] = fs
                mid = current_bot_session.get("meeting_id")
                if mid:
                    database.update_meeting_final_summary(mid, fs, lang)
                self._send_json({"final_summary": fs, "language": lang})
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)

        elif path == "/api/bot/stop":
            bot = current_bot_session.get("bot")
            if bot:
                bot.stop()
            current_bot_session["status"] = "completed"
            self._send_json({"message": "Bot leaving call."})

        elif path == "/api/text":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            text = data.get("text", "")
            try:
                notes = create_notes(text)
                self._send_json({"notes": notes})
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)

        elif path == "/api/audio":
            content_type = self.headers.get("Content-Type", "")
            content_length = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_length)

            header_block = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
            msg = BytesParser(policy=default).parsebytes(header_block + post_body)

            audio_bytes = b""
            filename = "media.webm"

            for part in msg.iter_parts():
                if part.get_param("name", header="content-disposition") == "audio":
                    audio_bytes = part.get_payload(decode=True)
                    fn = part.get_filename()
                    if fn:
                        filename = fn
                    break

            if not audio_bytes:
                self._send_json({"error": "No audio or video data received"}, status=400)
                return

            suffix = os.path.splitext(filename)[1] or ".webm"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            try:
                transcript = transcribe_audio(tmp_path)
                if not transcript.strip():
                    raise RuntimeError("No speech detected in audio recording.")
                notes = create_notes(transcript)
                self._send_json({"transcript": transcript, "notes": notes})
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        else:
            self.send_error(404)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

def run():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Server started at http://localhost:{PORT}")
    server.serve_forever()

if __name__ == "__main__":
    run()
