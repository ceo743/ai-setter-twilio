"""
Twilio AI Setter Voice Agent
-----------------------------
Flask + flask-sock server that orchestrates:
  Twilio Media Stream  ->  Deepgram STT  ->  Claude Haiku 4.5  ->  ElevenLabs Turbo TTS  ->  Twilio

Everything runs on a single port so a single tunnel (serveo.net) works.
"""

import asyncio
import audioop
import base64
import json
import logging
import os
import queue
import threading
import time
from typing import Optional

import anthropic
import httpx
import websocket  # websocket-client for Deepgram raw WS
from dotenv import load_dotenv
from flask import Flask, Response, request
from flask_sock import Sock
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, VoiceResponse

from knowledge_base import get_knowledge_prompt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_env_path, override=False)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
MY_PHONE_NUMBER = os.getenv("MY_PHONE_NUMBER")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")

# Fix: if PUBLIC_URL is empty or points to dead serveo tunnel, clear it
# so the server uses request.headers["Host"] which works on Render automatically
if "serveo" in PUBLIC_URL:
    print("WARNING: PUBLIC_URL points to old serveo tunnel, ignoring it")
    PUBLIC_URL = ""
if not PUBLIC_URL:
    PUBLIC_URL = "https://ai-setter-twilio.onrender.com"
print("  PUBLIC_URL = %s" % PUBLIC_URL)

SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PORT", os.getenv("SERVER_PORT", "8080")))

OPENING_MESSAGE = "Ciao Alessandro, sono Stefania del team LinkedIn di Davide Caiazzo."

# Validate critical keys at startup
for _key_name in ["ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID"]:
    _val = os.getenv(_key_name)
    if not _val:
        raise RuntimeError("Missing env var: {}".format(_key_name))
    print("  {} = {}...".format(_key_name, _val[:15]))

# ElevenLabs streaming TTS endpoint
ELEVENLABS_TTS_URL = (
    "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    "?output_format=ulaw_8000"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("setter-agent")

# ---------------------------------------------------------------------------
# Flask app + flask-sock
# ---------------------------------------------------------------------------
app = Flask(__name__)
sock = Sock(app)


@app.route("/incoming-call", methods=["POST"])
def incoming_call():
    """Twilio webhook for inbound calls.  Returns TwiML that connects the
    caller to our WebSocket media stream."""
    logger.info("Incoming call from %s", request.form.get("From", "unknown"))
    response = VoiceResponse()
    connect = Connect()

    # Build the wss:// stream URL from PUBLIC_URL env var
    if PUBLIC_URL:
        # PUBLIC_URL is like https://xxxx.serveo.net
        ws_host = PUBLIC_URL.replace("https://", "").replace("http://", "").rstrip("/")
        stream_url = "wss://{}/media-stream".format(ws_host)
    else:
        host = request.headers.get("Host", "localhost:{}".format(SERVER_PORT))
        stream_url = "wss://{}/media-stream".format(host)

    logger.info("Stream URL: %s", stream_url)
    connect.stream(url=stream_url)
    response.append(connect)
    return Response(str(response), mimetype="application/xml")


# Store lead data for active calls (call_sid -> lead_data)
active_leads = {}


@app.route("/make-call", methods=["POST"])
def make_call():
    """Initiate an outbound call with lead data from Calendly form."""
    data = request.json if request.is_json else {}
    to_number = data.get("to", MY_PHONE_NUMBER)

    # Lead data from Calendly form (passed by n8n)
    lead_data = {
        "nome": data.get("nome", ""),
        "cognome": data.get("cognome", ""),
        "email": data.get("email", ""),
        "cellulare": data.get("cellulare", to_number),
        "ruolo": data.get("ruolo", ""),
        "acquisizione_clienti": data.get("acquisizione_clienti", ""),
        "obiettivi_linkedin": data.get("obiettivi_linkedin", ""),
        "usa_linkedin": data.get("usa_linkedin", ""),
        "sito_web": data.get("sito_web", ""),
        "fatturato": data.get("fatturato", ""),
        "budget": data.get("budget", ""),
        "data_consulenza": data.get("data_consulenza", ""),
        "ora_consulenza": data.get("ora_consulenza", ""),
    }

    if PUBLIC_URL:
        public_url = PUBLIC_URL.rstrip("/")
    else:
        public_url = "https://{}".format(request.headers.get("Host", "localhost"))
    twiml_url = "{}/incoming-call".format(public_url)

    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url,
        )
        # Store lead data for this call
        active_leads[call.sid] = lead_data
        # Also store by phone number as fallback
        active_leads[to_number] = lead_data

        logger.info("Outbound call initiated: %s -> %s  SID=%s", TWILIO_PHONE_NUMBER, to_number, call.sid)
        logger.info("Lead data: %s %s - %s - fatturato: %s - budget: %s",
                     lead_data["nome"], lead_data["cognome"], lead_data["ruolo"],
                     lead_data["fatturato"], lead_data["budget"])
        return {"status": "ok", "call_sid": call.sid}
    except Exception as exc:
        logger.exception("Failed to initiate outbound call")
        return {"status": "error", "detail": str(exc)}, 500


@app.route("/calendly-webhook", methods=["POST"])
def calendly_webhook():
    """Receive webhook directly from Calendly or from n8n."""
    data = request.json if request.is_json else {}
    logger.info("Calendly webhook received - FULL DATA: %s", json.dumps(data, indent=2)[:2000])

    # Check if this is a native Calendly webhook (has "payload" key)
    payload = data.get("payload", {})
    if payload:
        # Native Calendly format
        # Check event type URI to filter
        event_type_uri = payload.get("event_type", {}).get("uri", "")
        event_type_name = payload.get("event_type", {}).get("name", "")
        # Also check the scheduled_event for event type
        scheduled_event = payload.get("scheduled_event", {})
        event_type_from_event = scheduled_event.get("event_type", "")
        logger.info("Event type name: '%s' URI: '%s' from_event: '%s'", event_type_name, event_type_uri, event_type_from_event)

        # SAFETY: Only process known event types
        # TEST TWILLIO: 04873ccb-e62c-49d8-8e31-1b357e19232d
        # ADV: e780bd22-cd3b-44ad-8f7a-7322ad9a23bf
        ALLOWED_EVENT_TYPES = [
            "04873ccb-e62c-49d8-8e31-1b357e19232d",  # TEST TWILLIO CHIAMATE
            "e780bd22-cd3b-44ad-8f7a-7322ad9a23bf",  # Consulenza Strategica Gratuita LinkedIn (adv)
        ]
        event_uri_check = event_type_uri + event_type_from_event
        is_allowed = any(eid in event_uri_check for eid in ALLOWED_EVENT_TYPES)
        if not is_allowed and event_type_name:
            is_allowed = "TEST TWILLIO" in event_type_name.upper() or "ADV" in event_type_name.upper()
        if not is_allowed:
            logger.info("BLOCKED: Event not in allowed list. URI: %s", event_uri_check)
            return {"status": "ignored", "reason": "not allowed event type"}

        # In Calendly v2 webhooks, invitee data is directly in payload
        # scheduled_event contains the event details
        invitee = payload  # payload IS the invitee
        event = payload.get("scheduled_event", {})

        # Extract phone from text_reminder_number or questions
        to_number = invitee.get("text_reminder_number", "")
        questions = invitee.get("questions_and_answers", [])

        # Extract form answers
        form_data = {
            "nome": invitee.get("first_name", ""),
            "cognome": invitee.get("last_name", ""),
            "email": invitee.get("email", ""),
            "cellulare": to_number,
            "data_consulenza": event.get("start_time", ""),
            "ora_consulenza": event.get("start_time", ""),
        }

        # Map question answers
        for qa in questions:
            q = qa.get("question", "").lower()
            a = qa.get("answer", "")
            if "ruolo" in q:
                form_data["ruolo"] = a
            elif "acquisisci" in q or "acquisire" in q:
                form_data["acquisizione_clienti"] = a
            elif "ottenere" in q or "vorresti" in q:
                form_data["obiettivi_linkedin"] = a
            elif "utilizzando" in q or "linkedin" in q and "business" in q:
                form_data["usa_linkedin"] = a
            elif "fatturato" in q:
                form_data["fatturato"] = a
            elif "budget" in q:
                form_data["budget"] = a
            elif "sito" in q:
                form_data["sito_web"] = a

        # If no phone from text_reminder, check questions for cellulare
        if not to_number:
            for qa in questions:
                if "cellulare" in qa.get("question", "").lower() or "telefono" in qa.get("question", "").lower():
                    to_number = qa.get("answer", "")
                    break

        # FILTRO RISPOSTE FORM: blocca lead che nelle risposte dicono di non prenotare
        BLOCK_PHRASES = [
            "non prenotare", "non prenotate", "non chiamare", "non chiamate",
            "guarda il video", "guardare il video", "guardate il video",
            "non mi interessa", "non sono interessat",
            "annulla", "cancella", "disdici",
            "non voglio", "non desidero",
            "solo il video", "solo video",
            "non posso investire", "non ancora nata",
            "trovare lavoro", "fare carriera",
        ]
        all_answers = " ".join(qa.get("answer", "") for qa in questions).lower()
        blocked_phrase = next((p for p in BLOCK_PHRASES if p in all_answers), None)
        if blocked_phrase:
            logger.info("BLOCKED LEAD: %s %s - form answer contains '%s'. Full answers: %s",
                        form_data.get("nome", ""), form_data.get("cognome", ""),
                        blocked_phrase, all_answers[:300])
            return {"status": "blocked", "reason": "Lead form answers indicate no call wanted", "phrase": blocked_phrase}

        logger.info("Lead: %s %s - Phone: %s", form_data["nome"], form_data["cognome"], to_number)
    else:
        # Simple format (from direct API call or n8n)
        form_data = data
        to_number = data.get("cellulare", "")

    if not to_number:
        logger.warning("No phone number found, cannot call")
        return {"status": "error", "detail": "No phone number"}, 400

    # Normalize Italian number
    to_number = to_number.replace(" ", "").replace("-", "")
    if to_number.startswith("3") and len(to_number) == 10:
        to_number = "+39" + to_number
    elif not to_number.startswith("+"):
        to_number = "+39" + to_number

    form_data["to"] = to_number
    form_data["cellulare"] = to_number

    # Trigger the call - pass correct Host header so make_call builds the right TwiML URL
    ws_host = PUBLIC_URL.replace("https://", "").replace("http://", "").rstrip("/")
    with app.test_request_context(
        "/make-call", method="POST",
        json=form_data, content_type="application/json",
        headers={"Host": ws_host}
    ):
        return make_call()


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def pcm16_to_mulaw(pcm_bytes):
    """Convert 16-bit signed PCM to G.711 mu-law."""
    return audioop.lin2ulaw(pcm_bytes, 2)


def mulaw_to_pcm16(mulaw_bytes):
    """Convert G.711 mu-law to 16-bit signed PCM."""
    return audioop.ulaw2lin(mulaw_bytes, 2)


# ---------------------------------------------------------------------------
# ElevenLabs TTS  (streaming, returns mu-law chunks)  -- synchronous version
# ---------------------------------------------------------------------------

def elevenlabs_tts_stream_sync(text):
    """Yield mu-law audio chunks from ElevenLabs streaming TTS (synchronous).

    We request output_format=ulaw_8000 directly so no conversion is needed.
    """
    url = ELEVENLABS_TTS_URL.format(voice_id=ELEVENLABS_VOICE_ID)
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    body = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }
    with httpx.Client(timeout=120.0) as client:
        with client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(chunk_size=640):
                if chunk:
                    yield chunk


# ---------------------------------------------------------------------------
# Claude conversation  -- synchronous version
# ---------------------------------------------------------------------------

class ConversationManager:
    """Maintains conversation history and queries Claude Haiku."""

    def __init__(self, system_prompt):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.system_prompt = system_prompt
        self.messages = []

    def get_response(self, user_text):
        self.messages.append({"role": "user", "content": user_text})
        logger.info("User said: %s", user_text)

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system=self.system_prompt,
                messages=self.messages,
            )
            assistant_text = response.content[0].text.strip()
            self.messages.append({"role": "assistant", "content": assistant_text})
            logger.info("Stefania says: %s", assistant_text)
            return assistant_text
        except Exception:
            logger.exception("Claude API error")
            return "Mi scusi, ho avuto un problema tecnico. Puo' ripetere?"


# ---------------------------------------------------------------------------
# WebSocket handler  (Twilio Media Stream <-> pipeline)
# ---------------------------------------------------------------------------

@sock.route("/media-stream")
def handle_media_stream(ws):
    """Handle a single Twilio Media Stream WebSocket connection.

    flask-sock handlers are synchronous (each runs in its own thread).
    We use a thread-safe queue for Deepgram transcripts and a background
    thread for the transcript -> Claude -> TTS -> Twilio pipeline.
    """
    logger.info("WebSocket connection opened")

    stream_sid = None
    call_from = None
    lead_data = None
    conversation = None  # initialized after we get call info
    stop_event = threading.Event()

    # --- Deepgram live transcription via raw WebSocket ---
    transcript_q = queue.Queue()
    dg_ready = threading.Event()

    dg_url = ("wss://api.deepgram.com/v1/listen"
              "?model=nova-2&language=it&encoding=mulaw&sample_rate=8000"
              "&channels=1&punctuate=true&interim_results=false&endpointing=300")

    dg_ws = websocket.WebSocket()
    try:
        dg_ws.connect(dg_url, header=["Authorization: Token {}".format(DEEPGRAM_API_KEY)])
        logger.info("Deepgram WebSocket connected")
        dg_ready.set()
    except Exception:
        logger.exception("Failed to connect to Deepgram")
        ws.close()
        return

    # Background thread to read Deepgram results
    def read_deepgram():
        while not stop_event.is_set():
            try:
                result_raw = dg_ws.recv()
                if not result_raw:
                    continue
                result = json.loads(result_raw)
                if result.get("is_final"):
                    transcript = result.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
                    if transcript:
                        logger.info("Deepgram final transcript: %s", transcript)
                        transcript_q.put(transcript)
            except Exception as e:
                if not stop_event.is_set():
                    logger.exception("Deepgram read error: %s", e)
                else:
                    logger.info("Deepgram read ended (stop_event set)")
                break

    dg_thread = threading.Thread(target=read_deepgram, daemon=True)
    dg_thread.start()

    # Lock for sending on the websocket (flask-sock ws is not thread-safe)
    ws_send_lock = threading.Lock()


    # --- Helper: send audio to Twilio ---
    def send_audio_to_twilio(audio_chunk):
        """Send a mu-law audio chunk back to Twilio via the media stream."""
        if not stream_sid:
            return
        payload = base64.b64encode(audio_chunk).decode("ascii")
        msg = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload},
        }
        with ws_send_lock:
            try:
                ws.send(json.dumps(msg))
            except Exception:
                logger.exception("Error sending audio to Twilio")

    # Flag to ignore input while AI is speaking
    is_speaking = threading.Event()

    # --- Helper: speak text via TTS -> Twilio ---
    def speak(text):
        """Convert text to speech and stream to Twilio."""
        logger.info("Speaking: %s", text)
        is_speaking.set()
        # Drain any transcripts that arrived while we prepare to speak
        while not transcript_q.empty():
            try:
                transcript_q.get_nowait()
            except queue.Empty:
                break
        try:
            chunk_count = 0
            for chunk in elevenlabs_tts_stream_sync(text):
                if stop_event.is_set():
                    logger.info("TTS interrupted by stop_event after %d chunks", chunk_count)
                    break
                send_audio_to_twilio(chunk)
                chunk_count += 1
            logger.info("TTS finished: %d chunks sent for: %s", chunk_count, text[:50])
        except Exception:
            logger.exception("TTS streaming error")
        finally:
            is_speaking.clear()
            # Drain transcripts that came in while speaking (echo/feedback)
            while not transcript_q.empty():
                try:
                    transcript_q.get_nowait()
                except queue.Empty:
                    break

    # --- Background thread: process transcripts ---
    def process_transcripts():
        """Read final transcripts, get Claude response, speak it."""
        buf = ""
        while not stop_event.is_set():
            try:
                text = transcript_q.get(timeout=1.0)

                # Wait for conversation to be initialized
                if conversation is None:
                    continue

                # Ignore input while AI is speaking (prevents echo/repeat)
                if is_speaking.is_set():
                    logger.info("Ignoring input while speaking: %s", text)
                    continue

                if buf:
                    buf = "{} {}".format(buf, text)
                else:
                    buf = text

                # Drain any additional quickly-arriving segments
                time.sleep(0.3)
                while not transcript_q.empty():
                    try:
                        extra = transcript_q.get_nowait()
                        buf = "{} {}".format(buf, extra)
                    except queue.Empty:
                        break

                if not buf:
                    continue

                user_input = buf.strip()
                buf = ""

                response_text = conversation.get_response(user_input)
                speak(response_text)

            except queue.Empty:
                # Timeout -- check if there's buffered text
                if buf:
                    user_input = buf.strip()
                    buf = ""
                    response_text = conversation.get_response(user_input)
                    speak(response_text)
                continue
            except Exception:
                logger.exception("Error in transcript processing")

    transcript_thread = threading.Thread(target=process_transcripts, daemon=True)
    transcript_thread.start()

    # --- Main receive loop ---
    opening_sent = False
    try:
        while True:
            try:
                raw_message = ws.receive()
            except Exception as e:
                logger.info("WebSocket receive error or connection closed: %s", e)
                break

            if raw_message is None:
                logger.info("WebSocket returned None - connection closed by remote")
                break

            try:
                data = json.loads(raw_message)
            except (json.JSONDecodeError, TypeError):
                continue

            event = data.get("event")

            if event == "connected":
                logger.info("Twilio Media Stream connected")

            elif event == "start":
                stream_sid = data["start"]["streamSid"]
                call_sid = data["start"].get("callSid", "")
                logger.info("Stream started: SID=%s  CallSID=%s", stream_sid, call_sid)

                # Look up lead data from active_leads
                lead_data = active_leads.get(call_sid, {})
                if not lead_data:
                    # Try by phone number
                    for key, val in active_leads.items():
                        if key.startswith("+"):
                            lead_data = val
                            break

                lead_name = "{} {}".format(
                    lead_data.get("nome", ""),
                    lead_data.get("cognome", "")
                ).strip() or "il lead"

                # Build personalized prompt with form data
                prompt = get_knowledge_prompt(
                    lead_name=lead_name,
                    appointment_date=lead_data.get("data_consulenza", ""),
                    appointment_time=lead_data.get("ora_consulenza", ""),
                )

                # Add form data context if available
                if lead_data.get("ruolo"):
                    prompt += "\n## RISPOSTE FORM CALENDLY (il lead ha gia' compilato queste info)"
                    prompt += "\n- Ruolo: {}".format(lead_data.get("ruolo", ""))
                    prompt += "\n- Come acquisisce clienti: {}".format(lead_data.get("acquisizione_clienti", ""))
                    prompt += "\n- Obiettivi LinkedIn: {}".format(lead_data.get("obiettivi_linkedin", ""))
                    prompt += "\n- Usa gia' LinkedIn: {}".format(lead_data.get("usa_linkedin", ""))
                    prompt += "\n- Sito web: {}".format(lead_data.get("sito_web", ""))
                    prompt += "\n- Fatturato azienda: {}".format(lead_data.get("fatturato", ""))
                    prompt += "\n- Budget disponibile: {}".format(lead_data.get("budget", ""))
                    prompt += "\n\nUSA QUESTE INFO per personalizzare la call. NON chiedere cose che sai gia'."
                    prompt += "\nRiferisciti a quello che ha scritto nel form per creare rapport."

                conversation = ConversationManager(prompt)
                logger.info("Lead data loaded: %s - ruolo: %s - budget: %s",
                            lead_name, lead_data.get("ruolo", "N/A"), lead_data.get("budget", "N/A"))

                # Personalize opening message
                first_name = lead_data.get("nome", "").strip()
                if not first_name:
                    first_name = "buongiorno"
                opening = "Ciao {}, sono Stefania del team LinkedIn di Davide Caiazzo.".format(first_name)

                # Send opening message in a separate thread so we don't block
                if not opening_sent:
                    opening_sent = True
                    threading.Thread(target=speak, args=(opening,), daemon=True).start()

            elif event == "media":
                # Forward raw mu-law audio to Deepgram
                payload = data["media"]["payload"]
                audio_bytes = base64.b64decode(payload)
                try:
                    dg_ws.send_binary(audio_bytes)
                except Exception:
                    logger.exception("Error sending audio to Deepgram")

            elif event == "stop":
                logger.info("Stream stopped")
                break

    except Exception:
        logger.exception("Error in WebSocket handler")
    finally:
        stop_event.set()
        transcript_thread.join(timeout=5.0)
        try:
            dg_ws.close()
        except Exception:
            pass
        logger.info("Media stream handler finished")


# ---------------------------------------------------------------------------
# Start server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  Twilio AI Setter Agent")
    logger.info("  HTTP + WS -> http://%s:%s", SERVER_HOST, SERVER_PORT)
    logger.info("  WebSocket route: /media-stream")
    if PUBLIC_URL:
        logger.info("  PUBLIC_URL: %s", PUBLIC_URL)
    logger.info("=" * 60)

    # flask-sock handles WebSocket upgrade on the same port as Flask
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
