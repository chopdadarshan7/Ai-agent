import json
import os
import shutil
import subprocess
import sys
import tempfile
from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_openai import ChatOpenAI
from sarvamai import SarvamAI

load_dotenv()

AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".webm", ".mp4", ".mov", ".mkv", ".avi")
FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "ffmpeg"

def transcribe_audio_batch(audio_path: str) -> str:
    """
    Transcribes audio/video files of ANY duration (e.g. 40 mins, 2 hours)
    using Sarvam AI's official Batch API with speaker diarization.
    """
    sarvam_key = os.getenv("SARVAM_API_KEY")
    if not sarvam_key or sarvam_key == "your_sarvam_api_key_here":
        raise ValueError("SARVAM_API_KEY is missing in your .env file.")

    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    # If it's a video or non-standard format, extract clean audio track with ffmpeg
    clean_audio_path = audio_path
    temp_conv_dir = None
    ext = os.path.splitext(audio_path)[1].lower()

    if ext in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
        temp_conv_dir = tempfile.mkdtemp(prefix="sarvam_batch_")
        clean_audio_path = os.path.join(temp_conv_dir, "meeting_audio.mp3")
        cmd = [
            FFMPEG_BIN, "-y", "-i", audio_path,
            "-vn", "-acodec", "libmp3lame", "-q:a", "2",
            clean_audio_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    try:
        client = SarvamAI(api_subscription_key=sarvam_key)

        print("[Batch API] Creating batch speech-to-text job on Sarvam AI...")
        job = client.speech_to_text_job.create_job(
            model="saaras:v4",
            language_code="unknown",  # Auto-detects Hindi, English, Hinglish, etc.
            mode="transcribe",
            with_diarization=True
        )

        print(f"[Batch API] Job created (ID: {job.job_id}). Uploading audio file...")
        job.upload_files(file_paths=[clean_audio_path])

        print("[Batch API] Starting processing on Sarvam server...")
        job.start()

        print("[Batch API] Waiting for transcription job to complete...")
        job.wait_until_complete(poll_interval=5, timeout=1800)

        print("[Batch API] Job finished. Downloading transcription results...")
        with tempfile.TemporaryDirectory() as out_dir:
            job.download_outputs(output_dir=out_dir)

            all_lines = []
            for root, _, files in os.walk(out_dir):
                for f in sorted(files):
                    if f.endswith(".json"):
                        with open(os.path.join(root, f), "r", encoding="utf-8") as jf:
                            try:
                                jdata = json.load(jf)
                                if isinstance(jdata, dict):
                                    # Check for diarized entries (Speaker 1, Speaker 2...)
                                    diarized = jdata.get("diarized_transcript")
                                    if diarized and isinstance(diarized, dict) and "entries" in diarized:
                                        for entry in diarized["entries"]:
                                            speaker = entry.get("speaker_id", "Speaker")
                                            txt = entry.get("transcript", "").strip()
                                            if txt:
                                                all_lines.append(f"[{speaker}]: {txt}")
                                    # Fallback to plain transcript
                                    elif "transcript" in jdata and jdata["transcript"]:
                                        all_lines.append(jdata["transcript"])
                                    elif "data" in jdata and isinstance(jdata["data"], dict) and "transcript" in jdata["data"]:
                                        all_lines.append(jdata["data"]["transcript"])
                            except Exception:
                                pass

            transcript = "\n".join(all_lines).strip()
            if not transcript:
                raise RuntimeError("Batch job completed, but transcript content was empty.")
            return transcript

    finally:
        if temp_conv_dir and os.path.exists(temp_conv_dir):
            shutil.rmtree(temp_conv_dir, ignore_errors=True)

def transcribe_audio_fast(audio_path: str) -> str:
    """
    Transcribes short audio/video files instantly in ~1-2 seconds
    using Sarvam AI's synchronous speech-to-text API.
    """
    sarvam_key = os.getenv("SARVAM_API_KEY")
    if not sarvam_key or sarvam_key == "your_sarvam_api_key_here":
        raise ValueError("SARVAM_API_KEY is missing in your .env file.")

    clean_audio_path = audio_path
    temp_conv_dir = None

    # Try fast ffmpeg conversion to 16kHz mono WAV, with fallback to direct audio_path
    try:
        temp_conv_dir = tempfile.mkdtemp(prefix="sarvam_sync_")
        wav_path = os.path.join(temp_conv_dir, "audio.wav")
        cmd = [
            FFMPEG_BIN, "-y", "-i", audio_path,
            "-vn", "-ar", "16000", "-ac", "1",
            wav_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0 and os.path.exists(wav_path) and os.path.getsize(wav_path) > 100:
            clean_audio_path = wav_path
    except Exception:
        clean_audio_path = audio_path

    try:
        client = SarvamAI(api_subscription_key=sarvam_key)
        with open(clean_audio_path, "rb") as af:
            resp = client.speech_to_text.transcribe(
                file=af,
                model="saaras:v4",
                language_code="unknown"
            )
            return (getattr(resp, "transcript", "") or "").strip()
    finally:
        if temp_conv_dir and os.path.exists(temp_conv_dir):
            shutil.rmtree(temp_conv_dir, ignore_errors=True)

def transcribe_audio(audio_path: str) -> str:
    """
    Smart transcriber: tries ultra-fast synchronous STT first (~1-2s).
    If audio exceeds 30s or is a long recording, falls back to Batch API.
    """
    try:
        # Check size / try fast sync first
        text = transcribe_audio_fast(audio_path)
        if text:
            return text
    except Exception as e:
        err_msg = str(e).lower()
        print(f"[STT] Fast sync notice ({err_msg}). Falling back to Batch API...")

    # Fallback to Batch API for longer files
    return transcribe_audio_batch(audio_path)


def detect_text_language(text: str) -> str:
    """Automatically detects the language of a text snippet based on script & content."""
    if not text or not text.strip():
        return "Unknown"
    
    gujarati_count = sum(1 for c in text if '\u0A80' <= c <= '\u0AFF')
    devanagari_count = sum(1 for c in text if '\u0900' <= c <= '\u097F')
    bengali_count = sum(1 for c in text if '\u0980' <= c <= '\u09FF')
    tamil_count = sum(1 for c in text if '\u0B80' <= c <= '\u0BFF')
    telugu_count = sum(1 for c in text if '\u0C00' <= c <= '\u0C7F')
    kannada_count = sum(1 for c in text if '\u0C80' <= c <= '\u0CFF')
    malayalam_count = sum(1 for c in text if '\u0D00' <= c <= '\u0D7F')
    punjabi_count = sum(1 for c in text if '\u0A00' <= c <= '\u0A7F')
    arabic_count = sum(1 for c in text if '\u0600' <= c <= '\u06FF')
    
    total_len = max(len(text.strip()), 1)
    
    if gujarati_count > 0:
        return "Gujarati"
    if devanagari_count > 0:
        return "Hindi"
    if bengali_count > 0:
        return "Bengali"
    if tamil_count > 0:
        return "Tamil"
    if telugu_count > 0:
        return "Telugu"
    if kannada_count > 0:
        return "Kannada"
    if malayalam_count > 0:
        return "Malayalam"
    if punjabi_count > 0:
        return "Punjabi"
    if arabic_count > 0:
        return "Arabic"
        
    # Check for common Hinglish / Latin script words
    lower = text.lower()
    hinglish_keywords = ["hai", "karo", "karenge", "nahi", "accha", "kya", "hum", "aaj", "kaam", "chahiye"]
    if any(k in lower.split() for k in hinglish_keywords):
        return "Hinglish"
        
    return "English"

def get_notes_chain():
    """Initializes the LangChain notes summarizer via OpenRouter."""
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if not openrouter_key or openrouter_key == "your_openrouter_api_key_here":
        raise ValueError("OPENROUTER_API_KEY is missing in your .env file.")

    model_name = os.getenv("OPENROUTER_MODEL", "openrouter/free")

    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are an expert multilingual meeting summarizer. The transcript or input may be in ANY language or mixture (English, Gujarati, Hindi, Urdu, Marathi, Telugu, Tamil, Bengali, etc.). Understand the discussion thoroughly regardless of language, and summarize it into clear, well-structured notes:\n1. **Summary / Key Points**\n2. **Discussion & Context**\n3. **Action Items / Next Steps**\nExplain the content clearly and accurately. Always generate notes directly based on whatever is provided, even if it is short. Never reply with greetings or say 'I am ready to help'."),
        ("human", "{text}")
    ])

    llm = ChatOpenAI(
        model=model_name,
        api_key=openrouter_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0.2
    )
    return prompt | llm | StrOutputParser()

def create_notes(text: str) -> str:
    """Takes text input and generates structured notes using OpenRouter."""
    chain = get_notes_chain()
    return chain.invoke({"text": text})

def create_live_summary(transcript: str, target_language: str = "English") -> str:
    """
    Generates a fast, incremental LIVE summary during an ongoing meeting.
    Extracts current discussion topics, newly identified decisions, and action items.
    """
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if not openrouter_key or openrouter_key == "your_openrouter_api_key_here":
        raise ValueError("OPENROUTER_API_KEY is missing in your .env file.")

    model_name = os.getenv("OPENROUTER_MODEL", "openrouter/free")
    
    sys_prompt = f"""You are an AI Meeting Assistant monitoring a live Google Meet call.
The transcript contains ongoing speech in multiple languages (English, Hindi, Gujarati, Hinglish, etc.).
Analyze the conversation and generate an incremental Live Meeting Status in {target_language}.
Include ONLY the following 3 sections:
1. 📌 **Current Topics Discussed**: 2-4 bullet points on what is being discussed right now.
2. 🎯 **Decisions Identified So Far**: Clear decisions made (or 'None yet' if none).
3. ⚡ **Immediate Action Items**: Tasks mentioned with responsible person if known.

Keep it concise, updated, and direct. Output exclusively in {target_language}."""

    prompt = ChatPromptTemplate.from_messages([
        ("system", sys_prompt),
        ("human", "Ongoing Meeting Transcript:\n{transcript}")
    ])

    llm = ChatOpenAI(
        model=model_name,
        api_key=openrouter_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0.2
    )
    chain = prompt | llm | StrOutputParser()
    return chain.invoke({"transcript": transcript})

def create_final_summary(transcript: str, target_language: str = "English") -> str:
    """
    Generates the comprehensive Final Meeting Summary with all 6 required sections:
    A. Meeting Overview
    B. Key Discussion Points
    C. Important Decisions
    D. Action Items (Tasks, Assignee, Deadline - NO inventing)
    E. Questions and Answers
    F. Meeting Highlights
    """
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if not openrouter_key or openrouter_key == "your_openrouter_api_key_here":
        raise ValueError("OPENROUTER_API_KEY is missing in your .env file.")

    model_name = os.getenv("OPENROUTER_MODEL", "openrouter/free")

    sys_prompt = f"""You are an executive AI Meeting Summarizer.
Analyze the complete Google Meet meeting transcript, which may contain speech in English, Hindi, Gujarati, Marathi, or mixed languages.
Generate a comprehensive, professional Final Meeting Summary in {target_language}.

You MUST structure the summary into these exact 6 sections:
### A. Meeting Overview
- Executive Summary of the meeting
- Primary Meeting Purpose
- Main Topics Discussed

### B. Key Discussion Points
- Detailed technical & organizational points discussed
- Problems / challenges identified
- Proposed suggestions and solutions

### C. Important Decisions
- Decisions officially agreed upon or concluded
- Approved plans & next milestones

### D. Action Items
- Specific task discussed
- Assigned person (Explicitly write 'Not specified' if no name was mentioned. NEVER invent names.)
- Deadline (Explicitly write 'Not specified' if no date/time was mentioned. NEVER invent deadlines.)

### E. Questions and Answers
- Questions raised by participants
- Answers provided during the call
- Questions that remain open / unanswered

### F. Meeting Highlights
- Important statements & critical updates
- High-priority takeaways

CRITICAL RULES:
- Output the summary clearly in {target_language}.
- Never invent names, deadlines, tasks, or facts. Rely strictly on the transcript.
- Do not output polite greetings or conversational filler."""

    prompt = ChatPromptTemplate.from_messages([
        ("system", sys_prompt),
        ("human", "Complete Meeting Transcript:\n{transcript}")
    ])

    llm = ChatOpenAI(
        model=model_name,
        api_key=openrouter_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0.2
    )
    chain = prompt | llm | StrOutputParser()
    return chain.invoke({"transcript": transcript})

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = " ".join(sys.argv[1:])
    else:
        target = input("Enter text or audio/video file path (e.g., meeting.mp3): ").strip()

    if not target:
        print("Error: No input provided.", file=sys.stderr)
        sys.exit(1)

    try:
        if target.lower().endswith(AUDIO_EXTENSIONS) or os.path.isfile(target):
            print(f"\n[1/2] Processing via Sarvam AI Batch API ({target})...")
            transcribed_text = transcribe_audio_batch(target)
            print(f"\nTranscript:\n{transcribed_text}\n")
            input_text = transcribed_text
        else:
            input_text = target

        if not input_text.strip():
            print("Error: Empty text/transcript.", file=sys.stderr)
            sys.exit(1)

        print("\n[2/2] Generating notes with OpenRouter LLM...")
        notes = create_notes(input_text)

        print("\n--- Summary Notes ---\n")
        print(notes)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
