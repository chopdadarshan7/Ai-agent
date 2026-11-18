import base64
import datetime
import os
import sys
import tempfile
import time
import threading
from typing import Optional, Dict, List
from dotenv import load_dotenv

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

from playwright.sync_api import sync_playwright
import database
from notes_agent import (
    create_notes,
    transcribe_audio_fast,
    detect_text_language,
    create_live_summary,
    create_final_summary
)

load_dotenv()

SYSTEM_NOISE_PHRASES = (
    "your microphone is",
    "your camera is",
    "videocam",
    "fake_device",
    "turn on camera",
    "turn off camera",
    "turn on microphone",
    "turn off microphone",
    "more options",
    "meeting details"
)

AUDIO_INTERCEPTION_SCRIPT = """
(function() {
    window.__hookedTrackIds = new Set();
    window.__meetingAudioContext = null;
    window.__meetingAudioDest = null;
    window.__meetingAnalyser = null;
    window.__isBotInCall = false;

    function getAudioMixer() {
        try {
            if (!window.__meetingAudioContext || window.__meetingAudioContext.state === 'closed') {
                const AC = window.AudioContext || window.webkitAudioContext;
                window.__meetingAudioContext = new AC();
                window.__meetingAudioDest = window.__meetingAudioContext.createMediaStreamDestination();
                window.__meetingAnalyser = window.__meetingAudioContext.createAnalyser();
                window.__meetingAnalyser.fftSize = 512;
            }
            if (window.__meetingAudioContext.state === 'suspended') {
                window.__meetingAudioContext.resume();
            }
        } catch(e) {
            console.warn('[MeetBot] Mixer init error:', e);
        }
        return { 
            ctx: window.__meetingAudioContext, 
            dest: window.__meetingAudioDest,
            analyser: window.__meetingAnalyser
        };
    }

    window.__hookAudioTrack = function(track, label) {
        if (!track || track.kind !== 'audio' || window.__hookedTrackIds.has(track.id)) return;
        window.__hookedTrackIds.add(track.id);

        try {
            const { ctx, dest, analyser } = getAudioMixer();
            if (ctx && dest) {
                const mediaStream = new MediaStream([track]);
                const src = ctx.createMediaStreamSource(mediaStream);
                src.connect(dest);
                if (analyser) src.connect(analyser);
                console.log('[MeetBotAudio] Hooked remote audio track (' + label + '):', track.id);
            }
        } catch(e) {
            console.warn('[MeetBotAudio] Hook track error:', e);
        }

        track.addEventListener('ended', () => {
            window.__hookedTrackIds.delete(track.id);
        });
    };

    window.__hookAudioStream = function(stream, label) {
        if (!stream) return;
        try {
            const tracks = stream.getAudioTracks ? stream.getAudioTracks() : [];
            tracks.forEach(tr => window.__hookAudioTrack(tr, label || 'Stream'));
        } catch(e) {}
    };

    // 1. Intercept WebRTC RTCPeerConnection (receives all incoming remote audio)
    try {
        const OrigPC = window.RTCPeerConnection;
        if (OrigPC) {
            window.RTCPeerConnection = new Proxy(OrigPC, {
                construct(target, args) {
                    const pc = Reflect.construct(target, args);
                    try {
                        pc.addEventListener('track', (e) => {
                            if (e.track && e.track.kind === 'audio') {
                                window.__hookAudioTrack(e.track, 'WebRTC Track');
                            }
                            if (e.streams && e.streams.length > 0) {
                                e.streams.forEach(s => window.__hookAudioStream(s, 'WebRTC Stream'));
                            }
                        });
                    } catch(err) {}
                    return pc;
                }
            });
        }
    } catch(e) {}

    // 2. Intercept HTMLMediaElement play() and srcObject setter
    try {
        const origPlay = HTMLMediaElement.prototype.play;
        HTMLMediaElement.prototype.play = function() {
            try {
                if (this.srcObject instanceof MediaStream) {
                    window.__hookAudioStream(this.srcObject, 'AudioElement.play');
                }
            } catch(e) {}
            return origPlay.apply(this, arguments);
        };

        const srcDesc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
        if (srcDesc && srcDesc.set) {
            Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
                set: function(val) {
                    try {
                        if (val instanceof MediaStream) {
                            window.__hookAudioStream(val, 'AudioElement.srcObject');
                        }
                    } catch(e) {}
                    return srcDesc.set.call(this, val);
                },
                get: function() {
                    return srcDesc.get.call(this);
                }
            });
        }
    } catch(e) {}

    function isBotName(name) {
        if (!name) return true;
        const l = name.toLowerCase().trim();
        return l.includes('notetaker') ||
               l.includes('notetake') ||
               l.includes('ai noter') ||
               l.includes('ai bot') ||
               l === 'you' ||
               l.startsWith('you ') ||
               l.endsWith(' (you)');
    }

    function isValidName(name) {
        if (!name || typeof name !== 'string') return false;
        name = name.trim();
        if (name.length < 2 || name.length > 40) return false;
        if (isBotName(name)) return false;
        const lower = name.toLowerCase();
        const blacklist = [
            'pin', 'unpin', 'mute', 'unmute', 'turn off', 'turn on',
            'camera', 'microphone', 'more options', 'leave call',
            'participant', 'participants', 'meeting details', 'chat',
            'raise hand', 'people', 'info', 'activities', 'fullscreen',
            'presentation', 'presenting', 'share', 'closed captions',
            'mic_off', 'videocam', 'videocam_off', 'volume_up', 'volume_off'
        ];
        if (blacklist.some(b => lower === b || lower === b + ' (you)')) return false;
        return true;
    }

    function extractNameFromTile(tile) {
        if (!tile) return null;

        // 1. Check Pin or Options button aria-label (Contains real full participant name in Google Meet)
        const btns = tile.querySelectorAll('button[aria-label], button[data-tooltip]');
        for (const btn of btns) {
            const aria = btn.getAttribute('aria-label') || btn.getAttribute('data-tooltip') || '';
            const m = aria.match(/Pin\\s+(.+?)\\s+to/i) || aria.match(/options for\\s+(.+)/i) || aria.match(/for\\s+(.+)/i);
            if (m && m[1]) {
                const cand = m[1].replace(/\\(you\\)/i, '').trim();
                if (isValidName(cand)) return cand;
            }
        }

        // 2. Check Google Meet tile name text elements
        const nameEls = tile.querySelectorAll(
            'span.zWGUib, div.poVWob, div.XEazBc, div.jxFHg, [data-self-name], ' +
            'span[class*="name" i], div[class*="name" i], div.VfPpkd-Bz112c-LgbsSe'
        );
        for (const el of nameEls) {
            const t = (el.getAttribute('data-self-name') || el.innerText || el.textContent || '').replace(/\\(you\\)/i, '').trim();
            if (isValidName(t)) return t;
        }

        // 3. Fallback: Parse inner text of the tile
        const lines = (tile.innerText || '').split('\\n').map(s => s.replace(/\\(you\\)/i, '').trim()).filter(s => s.length > 0);
        for (const line of lines) {
            if (isValidName(line)) return line;
        }

        return null;
    }

    function isTileMuted(tile) {
        if (!tile) return true;
        const icons = tile.querySelectorAll('i, span, [aria-label], [data-tooltip]');
        for (const icon of icons) {
            const txt = (icon.textContent || '').trim();
            const aria = (icon.getAttribute('aria-label') || icon.getAttribute('data-tooltip') || '').toLowerCase();
            if (txt === 'mic_off' || aria.includes('muted') || aria.includes('off')) {
                return true;
            }
        }
        return false;
    }

    function isTileSpeaking(tile) {
        if (!tile) return false;
        if (tile.getAttribute('data-is-speaking') === 'true') return true;

        const aria = (tile.getAttribute('aria-label') || '').toLowerCase();
        if (aria.includes('speaking') || aria.includes('talking')) return true;

        const speakingEls = tile.querySelectorAll(
            '[data-is-speaking="true"], ' +
            'div[aria-label*="speaking" i], ' +
            'div.g3P7ic, div.kssM4, ' +
            'svg[class*="speaking" i], div[class*="speaking" i], ' +
            'div[class*="audio-wave" i], div[class*="volume" i]'
        );
        if (speakingEls.length > 0) return true;

        try {
            const cs = window.getComputedStyle(tile);
            if (cs && (
                cs.borderColor.includes('26, 115, 232') ||
                cs.boxShadow.includes('26, 115, 232') ||
                cs.outlineColor.includes('26, 115, 232')
            )) return true;
        } catch(e) {}

        return false;
    }

    function getActiveMeetingParticipants() {
        const result = [];
        const tiles = document.querySelectorAll(
            'div[data-participant-id], ' +
            'div[data-requested-participant-id], ' +
            'div[data-allocation-id], ' +
            'div[role="listitem"]'
        );
        tiles.forEach(tile => {
            const name = extractNameFromTile(tile);
            if (name && isValidName(name)) {
                if (!result.some(p => p.name === name)) {
                    result.push({
                        name: name,
                        tile: tile,
                        muted: isTileMuted(tile),
                        speaking: isTileSpeaking(tile)
                    });
                }
            }
        });
        return result;
    }

    // Detect who is actively speaking in Google Meet dynamically
    function detectCurrentSpeaker() {
        try {
            const participants = getActiveMeetingParticipants();
            if (participants.length === 0) return "Participant";

            // 1. Participant with explicit active speaking animation/attribute
            const speaking = participants.filter(p => p.speaking);
            if (speaking.length > 0) {
                return speaking[0].name;
            }

            // 2. Participant whose microphone is unmuted (active voice)
            const unmuted = participants.filter(p => !p.muted);
            if (unmuted.length === 1) {
                return unmuted[0].name;
            } else if (unmuted.length > 1) {
                return unmuted[0].name;
            }

            // 3. Fallback to active participant
            return participants[0].name;
        } catch(e) {
            console.warn('[MeetBot] detectCurrentSpeaker error:', e);
        }
        return "Participant";
    }

    function getAverageVolume() {
        try {
            const { analyser } = getAudioMixer();
            if (!analyser) return 0;
            const data = new Uint8Array(analyser.frequencyBinCount);
            analyser.getByteFrequencyData(data);
            let sum = 0;
            for (let i = 0; i < data.length; i++) {
                sum += data[i];
            }
            return sum / data.length;
        } catch(e) {
            return 0;
        }
    }

    // Master Utterance Recorder: Produces standalone, complete WebM audio files
    window.__isRecordingLoopActive = false;
    window.__startAudioCapture = function() {
        if (window.__isRecordingLoopActive) return;
        window.__isRecordingLoopActive = true;
        window.__isBotInCall = true;

        const { ctx } = getAudioMixer();
        if (ctx && ctx.state === 'suspended') {
            ctx.resume();
        }

        console.log('[MeetBotAudio] Meeting live audio capture loop activated!');

        function recordCycle() {
            if (!window.__isBotInCall) {
                window.__isRecordingLoopActive = false;
                return;
            }

            const { ctx, dest } = getAudioMixer();
            if (!dest || !dest.stream || dest.stream.getAudioTracks().length === 0) {
                setTimeout(recordCycle, 1000);
                return;
            }

            try {
                const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                    ? 'audio/webm;codecs=opus'
                    : 'audio/webm';

                const recorder = new MediaRecorder(dest.stream, { mimeType: mime });
                const chunks = [];
                let detectedSpeaker = detectCurrentSpeaker();
                let maxVolumeSeen = 0;

                const volInterval = setInterval(() => {
                    const v = getAverageVolume();
                    if (v > maxVolumeSeen) maxVolumeSeen = v;
                    const curSpk = detectCurrentSpeaker();
                    if (curSpk && curSpk !== 'Participant') {
                        detectedSpeaker = curSpk;
                    }
                }, 150);

                recorder.ondataavailable = (e) => {
                    if (e.data && e.data.size > 0) {
                        chunks.push(e.data);
                    }
                };

                recorder.onstop = () => {
                    clearInterval(volInterval);
                    if (chunks.length > 0 && window.__onMeetingAudioChunk) {
                        const blob = new Blob(chunks, { type: mime });
                        // Process any audible speech chunk without dropping short utterances
                        if (blob.size > 1000 && maxVolumeSeen > 0.8) {
                            const reader = new FileReader();
                            reader.onloadend = () => {
                                const b64 = reader.result.split(',')[1];
                                if (b64 && window.__onMeetingAudioChunk) {
                                    window.__onMeetingAudioChunk(detectedSpeaker, b64);
                                }
                            };
                            reader.readAsDataURL(blob);
                        }
                    }

                    if (window.__isBotInCall) {
                        setTimeout(recordCycle, 40);
                    }
                };

                recorder.start();
                // 1.8-second rapid cycle for near-instant speech-to-text
                setTimeout(() => {
                    if (recorder.state === 'recording') {
                        recorder.stop();
                    }
                }, 1800);

            } catch(e) {
                console.warn('[MeetBotAudio] Record cycle error:', e);
                setTimeout(recordCycle, 2000);
            }
        }

        recordCycle();
    };

    // Periodic scanner to catch audio elements
    setInterval(() => {
        getAudioMixer();
        document.querySelectorAll('audio, video').forEach(el => {
            if (el.srcObject instanceof MediaStream) {
                window.__hookAudioStream(el.srcObject, 'PeriodicScanner');
            }
        });
    }, 2000);
})();
"""

class GoogleMeetBot:
    def __init__(self, meet_url: str, bot_name: str = "AI Notetaker Bot", headless: bool = False, meeting_id: Optional[int] = None, summary_language: str = "English"):
        self.meet_url = meet_url.strip()
        if not self.meet_url.startswith("http"):
            self.meet_url = f"https://{self.meet_url}"
        self.bot_name = bot_name
        self.headless = headless
        self.meeting_id = meeting_id
        self.summary_language = summary_language
        self.transcript_entries = []
        self.live_summary = ""
        self.final_summary = ""
        self.speaker_map = {}
        self.audio_active = False
        self.is_running = False
        self.page = None
        self.browser = None
        self.in_call = False

    def get_speaker_info(self, raw_speaker: str):
        raw_speaker = (raw_speaker or "").strip()
        
        # Real detected participant name
        if raw_speaker and raw_speaker.lower() not in ("speaker", "meeting", "unknown", "participant", "bot", "notetaker"):
            if raw_speaker not in self.speaker_map:
                assigned_id = f"Speaker {len(self.speaker_map) + 1}"
                self.speaker_map[raw_speaker] = assigned_id
            return self.speaker_map[raw_speaker], raw_speaker

        # Truly unmapped speaker
        assigned_id = f"Speaker {len(self.speaker_map) + 1}"
        return assigned_id, assigned_id

    def stop(self):
        """Signals the bot to leave the meeting."""
        self.is_running = False
        try:
            if self.page:
                leave_btn = self.page.locator('button[aria-label*="Leave call" i], button[data-tooltip*="Leave call" i]').first
                if leave_btn.is_visible(timeout=1000):
                    leave_btn.click()
        except Exception:
            pass

    def run(self, max_duration_minutes: int = 180, on_speech_callback=None, on_status_callback=None):
        """
        Joins the meeting room, waits for true call admission,
        transcribes live speech in real time, and generates structured notes.
        """
        self.is_running = True
        self.in_call = False
        print(f"\n[Meeting Bot] Launching bot '{self.bot_name}' for {self.meet_url}...")
        if on_status_callback:
            on_status_callback("joining", "Launching browser...")

        with sync_playwright() as p:
            self.browser = p.chromium.launch(
                channel="chrome",
                headless=self.headless,
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--disable-blink-features=AutomationControlled",
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-web-security",
                    "--allow-running-insecure-content",
                    "--window-size=1280,800",
                    "--start-maximized",
                    "--lang=en-US"
                ]
            )

            context = self.browser.new_context(
                permissions=["microphone", "camera"],
                viewport={"width": 1280, "height": 800}
            )

            self.page = context.new_page()

            def process_utterance(speaker_raw: str, text: str, timestamp: str = None, is_update: bool = False):
                if not text or not text.strip():
                    return
                text = text.strip()
                lower_text = text.lower()
                if any(phrase in lower_text for phrase in SYSTEM_NOISE_PHRASES):
                    return

                timestamp = timestamp or datetime.datetime.now().strftime("%H:%M:%S")
                lang = detect_text_language(text)
                speaker_id, speaker_name = self.get_speaker_info(speaker_raw)

                # Prioritize real participant name (e.g. "Om Nakum", "Chopda Darshan", "rgsrgs")
                if speaker_name and speaker_name.lower() not in ("participant", "speaker", "unknown"):
                    display_speaker = speaker_name
                else:
                    display_speaker = speaker_id
                entry = f"[{timestamp}] {display_speaker} ({lang}): {text}"

                # Only avoid identical repeated sentences by the same speaker in immediate succession
                if self.transcript_entries and not is_update:
                    last_entry = self.transcript_entries[-1]
                    # Check if exact same content was just posted
                    if f"{display_speaker} ({lang}): {text}" in last_entry:
                        return

                if is_update and self.transcript_entries:
                    self.transcript_entries[-1] = entry
                else:
                    self.transcript_entries.append(entry)

                # Persist to SQLite Database
                if self.meeting_id:
                    try:
                        database.add_transcript_entry(
                            meeting_id=self.meeting_id,
                            speaker_id=speaker_id,
                            speaker_name=speaker_name,
                            text=text,
                            timestamp=timestamp,
                            detected_language=lang
                        )
                    except Exception:
                        pass

                print(f"🎙️ {entry}")
                if on_speech_callback:
                    on_speech_callback(display_speaker, text, timestamp, lang, is_update)

            def handle_live_caption(speaker, text):
                process_utterance(speaker, text, is_update=False)

            def handle_audio_chunk(*args):
                if len(args) == 2:
                    speaker, b64_data = args[0], args[1]
                elif len(args) == 1:
                    speaker, b64_data = "Participant", args[0]
                else:
                    return

                self.audio_active = True
                try:
                    raw_bytes = base64.b64decode(b64_data)
                    if len(raw_bytes) < 800:
                        return
                    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tf:
                        tf.write(raw_bytes)
                        chunk_path = tf.name
                    try:
                        print(f"[Meeting Bot Audio] 🎙️ Processing speech audio chunk ({len(raw_bytes)} bytes) from '{speaker}'...")
                        txt = transcribe_audio_fast(chunk_path)
                        txt = (txt or "").strip()
                        if txt and len(txt.strip()) >= 1:
                            lower_t = txt.lower()
                            if any(p in lower_t for p in ["thank you", "subtitles by", "amara.org", "subscribe"]):
                                return
                            print(f"[Meeting Bot Audio] ✅ Transcribed speech ({speaker}): {txt}")
                            process_utterance(speaker or "Participant", txt)
                    except Exception as err:
                        print(f"[Meeting Bot Audio STT Notice] {err}")
                    finally:
                        if os.path.exists(chunk_path):
                            os.remove(chunk_path)
                except Exception as e:
                    print(f"[Meeting Bot Audio Notice] {e}")

            try:
                self.page.expose_function("onCaptionCaptured", handle_live_caption)
            except Exception:
                pass

            try:
                self.page.expose_function("__onMeetingAudioChunk", handle_audio_chunk)
            except Exception:
                pass

            try:
                self.page.add_init_script(AUDIO_INTERCEPTION_SCRIPT)
            except Exception:
                pass

            self.page.on("console", lambda msg: print(f"[Browser Console] {msg.type}: {msg.text}"))
            self.page.on("pageerror", lambda err: print(f"[Browser PageError] {err}"))

            try:
                print(f"[Meeting Bot] Navigating to Google Meet room...")
                if on_status_callback:
                    on_status_callback("joining", "Opening Google Meet link...")

                self.page.goto(self.meet_url, timeout=60000, wait_until="domcontentloaded")
                time.sleep(3)

                # Mute mic and camera on pre-join screen
                try:
                    self.page.keyboard.press("Meta+d")
                    time.sleep(0.3)
                    self.page.keyboard.press("Meta+e")
                    time.sleep(0.3)
                except Exception:
                    pass

                # Enter bot name if name input exists
                try:
                    name_input = self.page.locator('input[aria-label*="name" i], input[type="text"]').first
                    if name_input.is_visible(timeout=3000):
                        name_input.fill(self.bot_name)
                        time.sleep(0.5)
                except Exception:
                    pass

                # Click "Ask to join" or "Join now"
                print(f"[Meeting Bot] Requesting to join call...")
                if on_status_callback:
                    on_status_callback("joining", "Clicking 'Ask to join'. Please click 'Admit' inside your Google Meet call!")

                join_btn = self.page.locator('button[jsname="Qx7uuf"], button:has-text("Ask to join"), button:has-text("Join now"), button:has-text("Join"), button:has-text("જોડાવા"), button:has-text("शामिल")').first
                if join_btn.is_visible(timeout=4000):
                    join_btn.click()
                    time.sleep(1)

                print(f"[Meeting Bot] Waiting for host to click 'Admit' in Google Meet...")
                if on_status_callback:
                    on_status_callback("waiting_admit", "Waiting for host to click 'Admit' in Google Meet...")

                # STRICT CHECK: Wait until "Leave call" button appears (proves we are INSIDE the call)
                leave_call_btn = self.page.locator(
                    'button[jsname="CQylAd"], '
                    'button[aria-label*="Leave call" i], '
                    'button[data-tooltip*="Leave call" i], '
                    'button:has(i:has-text("call_end")), '
                    'button:has(span:has-text("call_end")), '
                    'button[aria-label*="કૉલ" i], '
                    'button[aria-label*="कॉल" i]'
                ).first
                leave_call_btn.wait_for(state="visible", timeout=300000)

                self.in_call = True
                print(f"[Meeting Bot] ✅ Inside meeting room! Activating live captions...")
                if on_status_callback:
                    on_status_callback("listening", "In call! Live captions active...")

                start_time = time.time()

                def is_captions_active():
                    try:
                        return self.page.evaluate("""() => {
                            const hasRegion = !!document.querySelector('[role="region"][aria-label*="caption" i], [role="region"][aria-label*="subtitle" i], div[jsname="dsTNUb"]');
                            const hasTurnOff = Array.from(document.querySelectorAll('button, div[role="button"]')).some(b => {
                                const l = (b.getAttribute('aria-label') || b.getAttribute('data-tooltip') || '').toLowerCase();
                                const prs = (b.getAttribute('aria-pressed') || '').toLowerCase();
                                return l.includes('turn off caption') || (l.includes('caption') && prs === 'true');
                            });
                            return hasRegion || hasTurnOff;
                        }""")
                    except Exception:
                        return False

                captions_enabled = False
                def ensure_captions_on():
                    nonlocal captions_enabled
                    if captions_enabled or is_captions_active():
                        captions_enabled = True
                        return True

                    # 1. Dismiss any blocking dialogs/popups (e.g. "Add others", "Your meeting is ready", "Got it")
                    try:
                        dialog_btn = self.page.locator('button[aria-label*="Close" i], button:has-text("Got it"), button:has-text("Dismiss"), button:has-text("Close")').first
                        if dialog_btn.is_visible(timeout=300):
                            dialog_btn.click(force=True)
                            time.sleep(0.2)
                    except Exception:
                        pass

                    # 2. Wake up bottom toolbar by moving mouse to bottom
                    try:
                        vp = self.page.viewport_size or {"width": 800, "height": 600}
                        self.page.mouse.move(vp["width"] // 2, vp["height"] - 30)
                    except Exception:
                        pass

                    # 3. Direct CC button on toolbar (wide window)
                    try:
                        cc = self.page.locator(
                            'button[aria-label*="Turn on captions" i], '
                            'button[data-tooltip*="Turn on captions" i], '
                            'button[aria-label*="captions (c)" i], '
                            'button:has(i:has-text("closed_caption_off")), '
                            'button:has(span:has-text("closed_caption_off"))'
                        ).first
                        if cc.is_visible(timeout=400):
                            lbl = (cc.get_attribute("aria-label") or "").lower()
                            prs = (cc.get_attribute("aria-pressed") or "").lower()
                            if "turn off" not in lbl and prs != "true":
                                cc.click(force=True)
                                captions_enabled = True
                                print("[Meeting Bot] ✅ Clicked toolbar 'Turn on captions' button.")
                                time.sleep(0.5)
                                return True
                    except Exception:
                        pass

                    # 4. Three Dots (More options) menu (narrow window / collapsed toolbar)
                    try:
                        more = self.page.locator(
                            'button[aria-label*="More options" i], '
                            'button[data-tooltip*="More options" i], '
                            'button[jsname="E20TRe"], '
                            'button:has(i:has-text("more_vert")), '
                            'button:has(span:has-text("more_vert"))'
                        ).first
                        if more.is_visible(timeout=500):
                            more.click(force=True)
                            time.sleep(0.4)
                            cap = self.page.locator(
                                '[role="menuitem"]:has-text("caption"), '
                                '[role="menuitem"]:has-text("Caption"), '
                                '[role="menuitem"]:has-text("Captions"), '
                                '[role="menuitem"]:has-text("કૅપ્શન"), '
                                '[role="menuitem"]:has-text("कैप्शन"), '
                                '[role="menuitem"]:has-text("subtitle"), '
                                '[role="menuitem"]:has(i:has-text("closed_caption")), '
                                '[role="menuitem"]:has(span:has-text("closed_caption"))'
                            ).first
                            if cap.is_visible(timeout=600):
                                txt = (cap.inner_text() or "").lower()
                                if "turn off" not in txt and "બંધ" not in txt:
                                    cap.click(force=True)
                                    captions_enabled = True
                                    print("[Meeting Bot] ✅ Clicked 'Turn on captions' in More Options menu!")
                                    time.sleep(0.5)
                                    return True
                            self.page.keyboard.press("Escape")
                            time.sleep(0.2)
                    except Exception:
                        pass

                    # 5. Direct keyboard shortcut 'c' (only once if not active)
                    try:
                        self.page.keyboard.press("c")
                        captions_enabled = True
                        print("[Meeting Bot] ⌨️ Sent 'c' keyboard shortcut for captions.")
                        time.sleep(0.3)
                    except Exception:
                        pass

                    # 6. Check if language selection modal appeared and confirm it
                    try:
                        apply_btn = self.page.locator('button:has-text("Apply"), button:has-text("Save"), button:has-text("લાગુ કરો"), button:has-text("लागू करें")').first
                        if apply_btn.is_visible(timeout=400):
                            apply_btn.click(force=True)
                            print("[Meeting Bot] Clicked 'Apply' on captions language modal.")
                    except Exception:
                        pass

                    return is_captions_active()

                # 1. Activate Live Audio Capture immediately
                try:
                    self.page.mouse.click(200, 200)
                    self.page.evaluate("() => { if (window.__startAudioCapture) window.__startAudioCapture(); }")
                    print("[Meeting Bot] 🎙️ Direct WebRTC meeting audio capture started!")
                except Exception as e:
                    print(f"[Meeting Bot Audio Start Notice] {e}")

                # 2. Activate Captions as complementary fast-path
                ensure_captions_on()
                time.sleep(0.5)

                print(f"[Meeting Bot] 🔴 Live listening active! Capturing conversation...")

                # Inject CC extractor and observer
                self.page.evaluate("""() => {
                    function extractCaptions() {
                        // 1. Accessibility caption region (Universal standard)
                        const region = document.querySelector('[role="region"][aria-label*="caption" i], [role="region"][aria-label*="subtitle" i], div[jsname="dsTNUb"]');
                        if (region) {
                            const blocks = region.querySelectorAll('[jsname="tgaKEf"], div[class*="caption" i], div.bh44bd');
                            if (blocks.length > 0) {
                                blocks.forEach(c => {
                                    let speaker = "Speaker";
                                    const speakerEl = c.querySelector('.zs7s8d, .jxFHg, [class*="speaker" i], [class*="name" i], img[alt]');
                                    if (speakerEl) {
                                        speaker = speakerEl.getAttribute('alt') || speakerEl.innerText || speakerEl.textContent || "Speaker";
                                        speaker = speaker.trim();
                                    }

                                    const textEl = c.querySelector('.iTTPOb, .iOzk7, span.ygicle, [jsname="YSxPtf"]') || c;
                                    let text = (textEl.innerText || textEl.textContent || "").trim();

                                    if (speakerEl && text.startsWith(speaker)) {
                                        text = text.substring(speaker.length).trim();
                                    }
                                    if (text && text.length > 0 && window.onCaptionCaptured) {
                                        window.onCaptionCaptured(speaker, text);
                                    }
                                });
                                return;
                            }
                        }

                        // 2. Class-based fallback query
                        const nodes = document.querySelectorAll(
                            'div[jsname="dsTNUb"] [jsname="tgaKEf"], ' +
                            '[role="region"][aria-label*="Captions" i] [jsname="tgaKEf"], ' +
                            'div.iTTPOb, div.bh44bd, div.TBMuR, ' +
                            'div[jsname="tgaKEf"], div[class*="caption" i]'
                        );

                        nodes.forEach(c => {
                            let speaker = "Speaker";
                            const speakerEl = c.querySelector('.zs7s8d, .jxFHg, [class*="speaker" i], [class*="name" i], img[alt]');
                            if (speakerEl) {
                                speaker = speakerEl.getAttribute('alt') || speakerEl.innerText || speakerEl.textContent || "Speaker";
                                speaker = speaker.trim();
                            }

                            const textEl = c.querySelector('.iTTPOb, .iOzk7, span.ygicle, [jsname="YSxPtf"]') || c;
                            let text = (textEl.innerText || textEl.textContent || "").trim();

                            if (speakerEl && text.startsWith(speaker)) {
                                text = text.substring(speaker.length).trim();
                            }
                            if (text && text.length > 0 && window.onCaptionCaptured) {
                                window.onCaptionCaptured(speaker, text);
                            }
                        });
                    }

                    // Check CC button state without blind toggling
                    function keepCaptionsEnabled() {
                        const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
                        const cc = buttons.find(b => {
                            const l = (b.getAttribute('aria-label') || b.getAttribute('data-tooltip') || '').toLowerCase();
                            const pressed = (b.getAttribute('aria-pressed') || '').toLowerCase();
                            return (l.includes('turn on caption') || l.includes('captions (c)')) && pressed !== 'true' && !l.includes('turn off');
                        });
                        if (cc) {
                            cc.click();
                        }
                    }

                    // Observe DOM changes live
                    try {
                        const observer = new MutationObserver(() => {
                            extractCaptions();
                        });
                        observer.observe(document.body, { childList: true, subtree: true, characterData: true });
                    } catch(e){}

                    // Continuous extraction
                    setInterval(extractCaptions, 300);
                }""")

                last_live_summary_count = 0
                while self.is_running and (time.time() - start_time) < (max_duration_minutes * 60):
                    # Check if browser was closed or call ended
                    if self.page.is_closed():
                        print("[Meeting Bot] Meeting window closed.")
                        break
                    if "/landing" in self.page.url:
                        print("[Meeting Bot] Navigated away from meeting room.")
                        break

                    elapsed = int(time.time() - start_time)

                    # Discover & sync meeting participants roster from Google Meet
                    if elapsed > 0 and elapsed % 4 == 0:
                        try:
                            roster = self.page.evaluate("""() => {
                                const names = [];
                                const tiles = document.querySelectorAll(
                                    'div[data-participant-id], div[data-requested-participant-id], div[role="listitem"]'
                                );
                                tiles.forEach(t => {
                                    const btns = t.querySelectorAll('button[aria-label], button[data-tooltip]');
                                    for (const b of btns) {
                                        const aria = b.getAttribute('aria-label') || b.getAttribute('data-tooltip') || '';
                                        const m = aria.match(/Pin\\s+(.+?)\\s+to/i) || aria.match(/options for\\s+(.+)/i);
                                        if (m && m[1]) {
                                            const n = m[1].replace(/\\(you\\)/i, '').trim();
                                            const lower = n.toLowerCase();
                                            if (n && n.length > 1 && !lower.includes('bot') && !lower.includes('notetaker') && !names.includes(n)) {
                                                names.push(n);
                                            }
                                        }
                                    }
                                });
                                return names;
                            }""")
                            if roster and isinstance(roster, list):
                                self.current_roster = roster
                        except Exception:
                            pass

                    # Check captions state periodically only if not active
                    if elapsed > 0 and elapsed % 5 == 0 and not is_captions_active():
                        try:
                            ensure_captions_on()
                        except Exception:
                            pass

                    # Periodic Live Summary generation (every 50s if new transcripts arrived)
                    if elapsed > 0 and elapsed % 50 == 0:
                        curr_len = len(self.transcript_entries)
                        if curr_len > last_live_summary_count and curr_len >= 2:
                            last_live_summary_count = curr_len
                            def bg_live_summary():
                                try:
                                    raw_text = "\n".join(self.transcript_entries)
                                    live_s = create_live_summary(raw_text, self.summary_language)
                                    self.live_summary = live_s
                                    if self.meeting_id:
                                        database.update_meeting_live_summary(self.meeting_id, live_s)
                                    if on_status_callback:
                                        on_status_callback("live_summary_update", live_s)
                                except Exception as e:
                                    print(f"[Meeting Bot Live Summary Notice] {e}")
                            threading.Thread(target=bg_live_summary, daemon=True).start()

                    # Keep session alive by subtle mouse activity
                    if elapsed > 0 and elapsed % 15 == 0:
                        try:
                            self.page.mouse.move(640, 680)
                        except Exception:
                            pass

                    time.sleep(1)

            except Exception as e:
                print(f"[Meeting Bot] Session message: {e}")
            finally:
                try:
                    self.browser.close()
                except Exception:
                    pass

        print(f"[Meeting Bot] Total utterances captured: {len(self.transcript_entries)}")
        full_transcript = "\n".join(self.transcript_entries).strip()

        if not full_transcript:
            return "No conversation speech was detected. Please ensure you spoke with microphone enabled while the bot was in the call.", "No notes generated (empty transcript)."

        print(f"[Meeting Bot] Generating structured final summary ({self.summary_language}) with LangChain & OpenRouter...")
        try:
            self.final_summary = create_final_summary(full_transcript, self.summary_language)
        except Exception:
            self.final_summary = create_notes(full_transcript)

        notes = self.final_summary

        # Persist to SQLite Database
        if self.meeting_id:
            try:
                database.end_meeting(self.meeting_id, self.final_summary, self.summary_language)
            except Exception:
                pass

        # Save to file
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Meeting_Notes_{now_str}.md"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# Meeting Notes ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n")
            f.write(f"**Meeting URL:** {self.meet_url}\n\n")
            f.write("## 📝 Executive Summary & Action Items\n\n")
            f.write(notes + "\n\n")
            f.write("## 📜 Full Transcript\n\n")
            f.write(full_transcript + "\n")

        print(f"[Meeting Bot] ✅ Saved notes to: {filename}")
        return full_transcript, notes

if __name__ == "__main__":
    url = sys.argv[1].strip() if len(sys.argv) > 1 else input("Enter Google Meet URL: ").strip()
    if not url:
        print("Error: No URL provided.")
        sys.exit(1)

    bot = GoogleMeetBot(meet_url=url, bot_name="AI Notetaker Bot", headless=False)
    transcript, notes = bot.run()
    print("\n" + "="*50)
    print("MEETING NOTES:\n" + notes)
