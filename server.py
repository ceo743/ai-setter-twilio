"""
Twilio AI Setter Voice Agent
-----------------------------
Flask + flask-sock server that orchestrates:
  Twilio Media Stream  ->  Deepgram STT  ->  Groq LLaMA 3  ->  ElevenLabs Turbo TTS  ->  Twilio

Everything runs on a single port so a single tunnel (serveo.net) works.
"""

import asyncio
import base64
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx
import websocket  # websocket-client for Deepgram raw WS
from dotenv import load_dotenv
from flask import Flask, Response, request
from flask_sock import Sock
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, VoiceResponse

from knowledge_base import get_knowledge_prompt
import re

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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
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
for _key_name in ["GROQ_API_KEY", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID"]:
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
# Transcript storage (in-memory, accessible via /transcript/<id>)
# ---------------------------------------------------------------------------
transcripts_store = {}


def save_transcript(entry, transcript_text):
    """Save transcript and return its public URL."""
    tid = uuid.uuid4().hex[:10]
    esito_map = {"qualificato": "Confermato", "non in target": "Non Confermato", "da confermare": "Da Confermare"}
    esito = esito_map.get(entry["status"], "Da Confermare")
    transcripts_store[tid] = {
        "nome": entry.get("nome", "N/A"),
        "cognome": entry.get("cognome", "N/A"),
        "phone": entry.get("phone", "N/A"),
        "ruolo": entry.get("ruolo", "N/A"),
        "obiettivi": entry.get("obiettivi", "N/A"),
        "esito": esito,
        "data_consulenza": entry.get("data_consulenza", "N/A"),
        "timestamp": entry.get("timestamp", ""),
        "transcript": transcript_text,
    }
    url = "{}/transcript/{}".format(PUBLIC_URL, tid)
    logger.info("Transcript saved: %s", url)
    return url


# ---------------------------------------------------------------------------
# Website scraping for lead intelligence
# ---------------------------------------------------------------------------
def scrape_website(url: str) -> str:
    """Scrape a prospect's website to extract business info before calling.
    Returns a short summary or empty string on failure."""
    if not url or url.strip() in ("", "-", "n/a", "nessuno", "no"):
        return ""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    try:
        logger.info("Scraping website: %s", url)
        resp = httpx.get(url, timeout=8, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; DCBot/1.0)"
        })
        if resp.status_code != 200:
            logger.warning("Website returned %s for %s", resp.status_code, url)
            return ""
        html = resp.text[:15000]  # limit to first 15k chars
        # Strip HTML tags to get text
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        text = text[:3000]  # limit text for Claude

        if len(text) < 50:
            return ""

        # Use Groq LLaMA to extract a brief summary
        groq_resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer {}".format(GROQ_API_KEY), "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": (
                    "Analizza questo testo dal sito web di un'azienda e rispondi in italiano con MAX 3 righe:\n"
                    "1. Di cosa si occupa l'azienda (settore e servizi principali)\n"
                    "2. Se vende a privati (B2C) o ad aziende (B2B)\n"
                    "3. Zona geografica se menzionata\n\n"
                    "Testo dal sito:\n" + text
                )}],
            },
            timeout=15.0,
        )
        groq_resp.raise_for_status()
        summary = groq_resp.json()["choices"][0]["message"]["content"].strip()
        logger.info("Website summary for %s: %s", url, summary)
        return summary
    except Exception as e:
        logger.warning("Failed to scrape %s: %s", url, e)
        return ""

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

# Call history for dashboard and transcripts
# Each entry: {phone, nome, cognome, ruolo, status, qualified, transcript, timestamp, data_consulenza}
call_history = []

# Davide's number for notifications
DAVIDE_PHONE = os.getenv("DAVIDE_PHONE", "+393478644733")

# ---------------------------------------------------------------------------
# Retry system for no-answer calls
# ---------------------------------------------------------------------------
# Intervals in seconds: 5min, 30min, 1h, 2h, 4h
RETRY_INTERVALS = [300, 1800, 3600, 7200, 14400]

# Track retry state per phone number: {phone: {"attempt": N, "form_data": {}, "answered": False}}
call_retries = {}
# Track which call_sids map to which phone number for status callback
call_sid_to_phone = {}


def parse_consultation_time(form_data):
    """Parse the consultation datetime from form data. Returns datetime or None."""
    date_str = form_data.get("data_consulenza", "")
    if not date_str:
        return None
    try:
        # Calendly format: 2026-03-30T15:00:00.000000Z
        if "T" in date_str:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            # Convert to Italian time (UTC+1 or UTC+2 for DST)
            dt = dt + timedelta(hours=2)  # CET/CEST approximation
            return dt.replace(tzinfo=None)
    except Exception:
        pass
    return None


TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "+15559199755")

# WhatsApp Content Template SIDs (approved by Meta)
WA_TEMPLATE_PRIMO_TENTATIVO = "HX66c6c02faa67d23e446cf69d07394f36"
WA_TEMPLATE_ULTIMO_TENTATIVO = "HX99ee0607803dae0fbf3b7358734d08cf"
WA_TEMPLATE_REMINDER = "HX4aac79d57bc31b19c77f44667f876ff1"

# Track opted-out numbers (STOP)
opted_out_numbers = set()


def schedule_reminder(phone_number, form_data):
    """Schedule a WhatsApp reminder 2 minutes before the consultation."""
    consultation_dt = parse_consultation_time(form_data)
    if not consultation_dt:
        logger.info("REMINDER: No consultation time for %s, skipping", phone_number)
        return

    reminder_time = consultation_dt - timedelta(minutes=2)
    delay = (reminder_time - datetime.now()).total_seconds()

    if delay <= 0:
        logger.info("REMINDER: Consultation already started for %s, skipping", phone_number)
        return

    first_name = form_data.get("nome", "").strip() or "buongiorno"
    meeting_link = form_data.get("meeting_link", "")

    if not meeting_link:
        logger.info("REMINDER: No meeting link for %s, skipping", phone_number)
        return

    logger.info("REMINDER: Scheduled for %s in %d min (at %s)", phone_number, delay // 60, reminder_time)

    def send_reminder():
        if phone_number in opted_out_numbers:
            return
        variables = {"1": first_name, "2": meeting_link}
        send_whatsapp_template(phone_number, WA_TEMPLATE_REMINDER, variables)
        logger.info("REMINDER: Sent to %s", phone_number)

    timer = threading.Timer(delay, send_reminder)
    timer.daemon = True
    timer.start()


def send_whatsapp_template(phone_number, content_sid, variables):
    """Send a WhatsApp template message via Twilio Content API."""
    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            to="whatsapp:{}".format(phone_number),
            from_="whatsapp:{}".format(TWILIO_WHATSAPP_NUMBER),
            content_sid=content_sid,
            content_variables=json.dumps(variables),
        )
        logger.info("WHATSAPP: Sent template to %s - SID=%s Status=%s", phone_number, msg.sid, msg.status)
    except Exception:
        logger.exception("WHATSAPP: Failed to send to %s", phone_number)


def schedule_retry(phone_number):
    """Schedule the next retry call for a lead."""
    retry_info = call_retries.get(phone_number)
    if not retry_info:
        return

    attempt = retry_info["attempt"]
    form_data = retry_info["form_data"]
    first_name = form_data.get("nome", "").strip() or "buongiorno"
    data_consulenza = form_data.get("data_consulenza", "")

    # Format consultation date for WhatsApp message
    data_display = ""
    consultation_dt = parse_consultation_time(form_data)
    if consultation_dt:
        data_display = consultation_dt.strftime("%d/%m alle %H:%M")

    # After first failed attempt: send WhatsApp template
    if attempt == 0:
        variables = {"1": first_name, "2": data_display or "prossimi giorni"}
        threading.Thread(
            target=send_whatsapp_template,
            args=(phone_number, WA_TEMPLATE_PRIMO_TENTATIVO, variables),
            daemon=True
        ).start()

    if attempt >= len(RETRY_INTERVALS):
        # All retries exhausted: send final WhatsApp template
        variables = {"1": first_name, "2": data_display or "prossimi giorni"}
        threading.Thread(
            target=send_whatsapp_template,
            args=(phone_number, WA_TEMPLATE_ULTIMO_TENTATIVO, variables),
            daemon=True
        ).start()
        logger.info("RETRY: Max attempts reached for %s (%d attempts) - final WhatsApp sent", phone_number, attempt + 1)
        return

    # Check if we're past the consultation time
    consultation_dt = parse_consultation_time(retry_info["form_data"])
    if consultation_dt:
        delay = RETRY_INTERVALS[attempt]
        retry_time = datetime.now() + timedelta(seconds=delay)
        if retry_time >= consultation_dt:
            logger.info("RETRY: Skipping retry for %s - would be after consultation at %s",
                        phone_number, consultation_dt)
            return

    delay = RETRY_INTERVALS[attempt]
    logger.info("RETRY: Scheduling attempt %d for %s in %d seconds (%d min)",
                attempt + 2, phone_number, delay, delay // 60)

    def do_retry():
        if phone_number in opted_out_numbers:
            logger.info("RETRY: Cancelled for %s - opted out (STOP)", phone_number)
            return
        retry_info = call_retries.get(phone_number)
        if not retry_info or retry_info.get("answered"):
            logger.info("RETRY: Cancelled for %s - already answered", phone_number)
            return

        # Check consultation time again at execution time
        consultation_dt = parse_consultation_time(retry_info["form_data"])
        if consultation_dt and datetime.now() >= consultation_dt:
            logger.info("RETRY: Cancelled for %s - past consultation time", phone_number)
            return

        retry_info["attempt"] += 1
        form_data = retry_info["form_data"]
        logger.info("RETRY: Calling %s - attempt %d", phone_number, retry_info["attempt"] + 1)

        # Make the call
        try:
            public_url = PUBLIC_URL.rstrip("/")
            twiml_url = "{}/incoming-call".format(public_url)
            status_url = "{}/call-status".format(public_url)
            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            call = client.calls.create(
                to=phone_number,
                from_=TWILIO_PHONE_NUMBER,
                url=twiml_url,
                status_callback=status_url,
                status_callback_event=["completed", "no-answer", "busy", "failed", "canceled"],
            )
            active_leads[call.sid] = form_data
            active_leads[phone_number] = form_data
            call_sid_to_phone[call.sid] = phone_number
            logger.info("RETRY: Call initiated SID=%s", call.sid)
        except Exception:
            logger.exception("RETRY: Failed to call %s", phone_number)

    timer = threading.Timer(delay, do_retry)
    timer.daemon = True
    timer.start()


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

    # Scrape website before calling to gather business intelligence
    sito = lead_data.get("sito_web", "")
    if sito:
        website_info = scrape_website(sito)
        if website_info:
            lead_data["website_info"] = website_info
            logger.info("Website intelligence gathered for %s: %s", sito, website_info[:200])

    if PUBLIC_URL:
        public_url = PUBLIC_URL.rstrip("/")
    else:
        public_url = "https://{}".format(request.headers.get("Host", "localhost"))
    twiml_url = "{}/incoming-call".format(public_url)

    try:
        status_url = "{}/call-status".format(public_url)
        amd_url = "{}/amd-status".format(public_url)
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url,
            status_callback=status_url,
            status_callback_event=["completed", "no-answer", "busy", "failed", "canceled"],
            machine_detection="Enable",
            async_amd=True,
            async_amd_status_callback=amd_url,
            async_amd_status_callback_method="POST",
        )
        # Store lead data for this call
        active_leads[call.sid] = lead_data
        # Also store by phone number as fallback
        active_leads[to_number] = lead_data
        # Map call SID to phone for status callback
        call_sid_to_phone[call.sid] = to_number

        # Initialize retry tracking if first attempt
        if to_number not in call_retries:
            call_retries[to_number] = {
                "attempt": 0,
                "form_data": lead_data,
                "answered": False,
            }

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
            # "e780bd22-cd3b-44ad-8f7a-7322ad9a23bf",  # Consulenza Strategica Gratuita LinkedIn (adv) - DISABILITATO
        ]
        event_uri_check = event_type_uri + event_type_from_event
        is_allowed = any(eid in event_uri_check for eid in ALLOWED_EVENT_TYPES)
        if not is_allowed and event_type_name:
            is_allowed = "TEST TWILLIO" in event_type_name.upper()
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

        # Extract meeting link from event location
        location = event.get("location", {})
        meeting_link = location.get("join_url", "") or location.get("location", "")
        if not meeting_link:
            # Try from event itself
            meeting_link = event.get("join_url", "")

        # Extract form answers
        form_data = {
            "nome": invitee.get("first_name", ""),
            "cognome": invitee.get("last_name", ""),
            "email": invitee.get("email", ""),
            "cellulare": to_number,
            "data_consulenza": event.get("start_time", ""),
            "ora_consulenza": event.get("start_time", ""),
            "meeting_link": meeting_link,
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


@app.route("/call-status", methods=["POST"])
def call_status():
    """Twilio status callback - detect no-answer and schedule retry."""
    call_sid = request.form.get("CallSid", "")
    call_status = request.form.get("CallStatus", "")
    phone_number = call_sid_to_phone.get(call_sid, "")

    logger.info("CALL STATUS: SID=%s Status=%s Phone=%s", call_sid, call_status, phone_number)

    if not phone_number:
        return {"status": "ok"}

    if call_status == "completed":
        # Lead answered - mark as answered, no more retries
        if phone_number in call_retries:
            call_retries[phone_number]["answered"] = True
            logger.info("RETRY: Lead %s answered - no more retries", phone_number)
    elif call_status in ("no-answer", "busy", "failed", "canceled"):
        # Lead didn't answer - schedule retry
        logger.info("RETRY: Lead %s did not answer (status: %s)", phone_number, call_status)
        schedule_retry(phone_number)

    return {"status": "ok"}


@app.route("/amd-status", methods=["POST"])
def amd_status():
    """Answering Machine Detection callback. Hang up if voicemail detected."""
    call_sid = request.form.get("CallSid", "")
    answered_by = request.form.get("AnsweredBy", "")
    logger.info("AMD: SID=%s AnsweredBy=%s", call_sid, answered_by)

    if answered_by in ("machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"):
        # It's a voicemail/machine - hang up immediately
        logger.info("AMD: Voicemail detected for SID=%s, hanging up", call_sid)
        try:
            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.calls(call_sid).update(status="completed")
        except Exception:
            logger.exception("AMD: Failed to hang up call %s", call_sid)

    return {"status": "ok"}


@app.route("/whatsapp-incoming", methods=["POST"])
def whatsapp_incoming():
    """Handle incoming WhatsApp replies. If lead asks to be called, trigger call."""
    from_number = request.form.get("From", "").replace("whatsapp:", "")
    body = request.form.get("Body", "").lower().strip()
    logger.info("WHATSAPP IN: From=%s Body='%s'", from_number, body)

    # Check for STOP / opt-out
    stop_keywords = ["stop", "basta", "non contattare", "non chiamare", "cancella"]
    if any(kw in body for kw in stop_keywords):
        opted_out_numbers.add(from_number)
        if from_number in call_retries:
            call_retries[from_number]["answered"] = True  # Stop retries
        logger.info("WHATSAPP IN: Lead %s opted out (STOP)", from_number)
        # Send confirmation
        try:
            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.messages.create(
                to="whatsapp:{}".format(from_number),
                from_="whatsapp:{}".format(TWILIO_WHATSAPP_NUMBER),
                body="Ricevuto. Non la contatteremo piu'. Se cambia idea, puo' sempre riprenotare. Buona giornata!"
            )
        except Exception:
            logger.exception("WHATSAPP IN: Failed to send STOP confirmation")
        return {"status": "ok"}

    # Check if this person has pending retry data
    retry_info = call_retries.get(from_number)
    if not retry_info:
        logger.info("WHATSAPP IN: No retry data for %s, ignoring", from_number)
        return {"status": "ok"}

    # Check if lead wants to be called
    call_keywords = ["chiamami", "chiama", "chiamare", "richiamare", "richiama",
                      "si chiama", "ok chiama", "va bene", "chiamatemi"]
    wants_call = any(kw in body for kw in call_keywords)

    if wants_call:
        logger.info("WHATSAPP IN: Lead %s wants to be called NOW", from_number)
        retry_info["answered"] = True  # Stop scheduled retries

        form_data = retry_info["form_data"]
        # Trigger call immediately
        try:
            public_url = PUBLIC_URL.rstrip("/")
            twiml_url = "{}/incoming-call".format(public_url)
            status_url = "{}/call-status".format(public_url)
            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            call = client.calls.create(
                to=from_number,
                from_=TWILIO_PHONE_NUMBER,
                url=twiml_url,
                status_callback=status_url,
                status_callback_event=["completed", "no-answer", "busy", "failed", "canceled"],
            )
            active_leads[call.sid] = form_data
            active_leads[from_number] = form_data
            call_sid_to_phone[call.sid] = from_number
            logger.info("WHATSAPP IN: Call triggered for %s - SID=%s", from_number, call.sid)
        except Exception:
            logger.exception("WHATSAPP IN: Failed to call %s", from_number)

    return {"status": "ok"}


@app.route("/transcript/<tid>", methods=["GET"])
def view_transcript(tid):
    """View full transcript of a call."""
    data = transcripts_store.get(tid)
    if not data:
        return "Trascrizione non trovata", 404
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trascrizione - {nome} {cognome}</title>
<style>body{{font-family:sans-serif;max-width:700px;margin:20px auto;padding:0 15px;background:#f5f5f5}}
.card{{background:#fff;border-radius:8px;padding:20px;margin-bottom:15px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.esito{{display:inline-block;padding:4px 12px;border-radius:4px;color:#fff;font-weight:bold}}
.confermato{{background:#4CAF50}}.non-confermato{{background:#f44336}}
pre{{white-space:pre-wrap;word-wrap:break-word;background:#f9f9f9;padding:15px;border-radius:6px;font-size:14px;line-height:1.6}}</style></head>
<body><div class="card"><h2>{nome} {cognome}</h2>
<p><span class="esito {esito_class}">{esito}</span></p>
<p><b>Telefono:</b> {phone}<br><b>Ruolo:</b> {ruolo}<br><b>Obiettivo:</b> {obiettivi}<br>
<b>Data consulenza:</b> {data_consulenza}<br><b>Chiamata:</b> {timestamp}</p></div>
<div class="card"><h3>Trascrizione completa</h3><pre>{transcript}</pre></div></body></html>""".format(
        nome=data["nome"], cognome=data["cognome"], phone=data["phone"],
        ruolo=data["ruolo"], obiettivi=data["obiettivi"], esito=data["esito"],
        esito_class="confermato" if data["esito"] == "Confermato" else "non-confermato",
        data_consulenza=data["data_consulenza"], timestamp=data["timestamp"],
        transcript=data["transcript"].replace("<", "&lt;").replace(">", "&gt;"),
    )


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Simple dashboard showing all calls, status, and transcripts."""
    rows = ""
    for i, c in enumerate(reversed(call_history)):
        status_color = "#4CAF50" if c["status"] == "qualificato" else "#f44336"
        transcript_id = "transcript_{}".format(i)
        rows += """
        <tr>
            <td>{timestamp}</td>
            <td>{nome} {cognome}</td>
            <td>{phone}</td>
            <td>{ruolo}</td>
            <td>{obiettivi}</td>
            <td><span style="background:{color};color:white;padding:3px 8px;border-radius:4px">{status}</span></td>
            <td><button onclick="document.getElementById('{tid}').style.display=document.getElementById('{tid}').style.display==='none'?'block':'none'" style="cursor:pointer;background:#2196F3;color:white;border:none;padding:5px 10px;border-radius:4px">Vedi</button>
                <pre id="{tid}" style="display:none;white-space:pre-wrap;max-width:500px;background:#1a1a2e;padding:10px;border-radius:4px;margin-top:5px">{transcript}</pre></td>
        </tr>""".format(
            timestamp=c["timestamp"],
            nome=c["nome"], cognome=c["cognome"],
            phone=c["phone"], ruolo=c["ruolo"] or "-",
            obiettivi=c.get("obiettivi", "") or "-",
            color=status_color, status=c["status"],
            tid=transcript_id,
            transcript=c["transcript"] or "Nessuna trascrizione"
        )

    # Stats
    total = len(call_history)
    qualified = sum(1 for c in call_history if c["status"] == "qualificato")
    not_qualified = total - qualified

    # Retry stats
    active_retries = sum(1 for r in call_retries.values() if not r.get("answered") and r["attempt"] < len(RETRY_INTERVALS))
    stopped = len(opted_out_numbers)

    html = """<!DOCTYPE html>
<html><head>
<title>Stefania AI Setter - Dashboard</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f23; color: #e0e0e0; margin: 0; padding: 20px; }}
    h1 {{ color: #fff; margin-bottom: 5px; }}
    .subtitle {{ color: #888; margin-bottom: 30px; }}
    .stats {{ display: flex; gap: 20px; margin-bottom: 30px; flex-wrap: wrap; }}
    .stat {{ background: #1a1a2e; padding: 20px; border-radius: 10px; min-width: 150px; }}
    .stat-number {{ font-size: 36px; font-weight: bold; }}
    .stat-label {{ color: #888; margin-top: 5px; }}
    .green {{ color: #4CAF50; }}
    .red {{ color: #f44336; }}
    .blue {{ color: #2196F3; }}
    .orange {{ color: #FF9800; }}
    table {{ width: 100%; border-collapse: collapse; background: #1a1a2e; border-radius: 10px; overflow: hidden; }}
    th {{ background: #16213e; padding: 12px; text-align: left; color: #888; font-size: 12px; text-transform: uppercase; }}
    td {{ padding: 12px; border-top: 1px solid #2a2a4a; }}
    tr:hover {{ background: #16213e; }}
    .refresh {{ color: #2196F3; text-decoration: none; }}
</style>
</head><body>
<h1>Stefania AI Setter</h1>
<p class="subtitle">DC Academy - Dashboard Chiamate <a href="/dashboard" class="refresh">Aggiorna</a></p>

<div class="stats">
    <div class="stat"><div class="stat-number blue">{total}</div><div class="stat-label">Totale chiamate</div></div>
    <div class="stat"><div class="stat-number green">{qualified}</div><div class="stat-label">Qualificati</div></div>
    <div class="stat"><div class="stat-number red">{not_qualified}</div><div class="stat-label">Non qualificati</div></div>
    <div class="stat"><div class="stat-number orange">{active_retries}</div><div class="stat-label">Retry attivi</div></div>
    <div class="stat"><div class="stat-number red">{stopped}</div><div class="stat-label">STOP (opt-out)</div></div>
</div>

<table>
<tr><th>Data/Ora</th><th>Nome</th><th>Telefono</th><th>Ruolo</th><th>Obiettivo</th><th>Esito</th><th>Trascrizione</th></tr>
{rows}
</table>

{empty}
</body></html>""".format(
        total=total, qualified=qualified, not_qualified=not_qualified,
        active_retries=active_retries, stopped=stopped,
        rows=rows,
        empty='<p style="text-align:center;color:#888;margin-top:40px">Nessuna chiamata ancora</p>' if not rows else ""
    )
    return Response(html, mimetype="text/html")


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
# Groq LLaMA conversation  -- synchronous version
# ---------------------------------------------------------------------------

class ConversationManager:
    """Maintains conversation history and queries Groq LLaMA 3."""

    def __init__(self, system_prompt):
        # Force Italian language in system prompt
        self.system_prompt = system_prompt + "\n\nREGOLE ASSOLUTE:\n1. Rispondi SEMPRE e SOLO in italiano. MAI una parola in inglese.\n2. Rispondi in MAX 2 frasi brevi e concise. Questa è una telefonata, non un'email. Sii naturale e diretta."
        self.messages = []
        self.transcript_log = []  # [(role, text), ...]

    def get_response(self, user_text):
        self.messages.append({"role": "user", "content": user_text})
        self.transcript_log.append(("Lead", user_text))
        logger.info("User said: %s", user_text)

        try:
            groq_messages = [{"role": "system", "content": self.system_prompt}] + self.messages
            resp = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer {}".format(GROQ_API_KEY), "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": 80,
                    "temperature": 0.7,
                    "messages": groq_messages,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            assistant_text = resp.json()["choices"][0]["message"]["content"].strip()
            self.messages.append({"role": "assistant", "content": assistant_text})
            self.transcript_log.append(("Stefania", assistant_text))
            logger.info("Stefania says: %s", assistant_text)
            return assistant_text
        except Exception:
            logger.exception("Groq API error")
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
    # PROVEN WORKING: logs show raw WebSocket produced transcripts (12:06 call).
    # WebSocketApp did NOT work (on_message not called during call).
    # Using raw WebSocket + blocking recv in dedicated thread + send_binary from main thread.
    # Added KeepAlive every 5s to prevent NET-0001 timeout.
    transcript_q = queue.Queue()
    dg_ready = threading.Event()
    dg_ws_container = [None]

    dg_url = ("wss://api.deepgram.com/v1/listen"
              "?model=nova-2&language=it&encoding=mulaw&sample_rate=8000"
              "&channels=1&punctuate=true&interim_results=false&endpointing=300")

    try:
        dg = websocket.WebSocket()
        dg.connect(dg_url, header=["Authorization: Token {}".format(DEEPGRAM_API_KEY)])
        dg_ws_container[0] = dg
        logger.info("Deepgram WebSocket connected")
        dg_ready.set()
    except Exception:
        logger.exception("Failed to connect to Deepgram")
        ws.close()
        return

    # Background thread: read Deepgram results (blocking recv, no timeout)
    def read_deepgram():
        while not stop_event.is_set():
            try:
                dg = dg_ws_container[0]
                if dg is None:
                    time.sleep(0.5)
                    continue
                result_raw = dg.recv()
                if not result_raw:
                    continue
                result = json.loads(result_raw)
                msg_type = result.get("type", "unknown")
                if msg_type == "Results":
                    is_final = result.get("is_final", False)
                    transcript = (result.get("channel", {})
                                  .get("alternatives", [{}])[0]
                                  .get("transcript", ""))
                    if is_final and transcript:
                        logger.info("Deepgram final transcript: %s", transcript)
                        transcript_q.put(transcript)
                elif msg_type == "Metadata":
                    logger.info("Deepgram metadata: request_id=%s", result.get("request_id", "N/A"))
            except Exception as e:
                if stop_event.is_set():
                    break
                logger.warning("Deepgram read error: %s", e)
                time.sleep(0.5)

    dg_thread = threading.Thread(target=read_deepgram, daemon=True)
    dg_thread.start()

    # KeepAlive thread — sends {"type": "KeepAlive"} every 5s to prevent NET-0001 timeout
    def deepgram_keepalive():
        while not stop_event.is_set():
            time.sleep(5)
            try:
                dg = dg_ws_container[0]
                if dg and dg.connected:
                    dg.send(json.dumps({"type": "KeepAlive"}))
            except Exception:
                pass

    ka_thread = threading.Thread(target=deepgram_keepalive, daemon=True)
    ka_thread.start()

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
    playback_end_estimate = [0.0]  # when Twilio finishes playing audio (for silence timer)
    audio_packet_count = [0]  # counter for audio packets sent to Deepgram
    mark_counter = [0]  # counter for unique mark names
    pending_mark = [None]  # name of the mark we're waiting for from Twilio

    # --- Helper: send mark to Twilio (to know when audio finishes playing) ---
    def send_mark_to_twilio(mark_name):
        """Send a mark event to Twilio. Twilio will echo it back when audio finishes playing."""
        if not stream_sid:
            return
        msg = {
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": mark_name},
        }
        with ws_send_lock:
            try:
                ws.send(json.dumps(msg))
                logger.info("Mark sent to Twilio: %s", mark_name)
            except Exception:
                logger.exception("Error sending mark to Twilio")

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
        total_audio_bytes = 0
        speak_start = time.time()
        try:
            chunk_count = 0
            for chunk in elevenlabs_tts_stream_sync(text):
                if stop_event.is_set():
                    logger.info("TTS interrupted by stop_event after %d chunks", chunk_count)
                    break
                send_audio_to_twilio(chunk)
                total_audio_bytes += len(chunk)
                chunk_count += 1
            logger.info("TTS finished: %d chunks, %d bytes for: %s", chunk_count, total_audio_bytes, text[:50])
        except Exception:
            logger.exception("TTS streaming error")
        finally:
            # Send a mark to Twilio — it will echo it back when audio finishes playing
            mark_counter[0] += 1
            mark_name = "tts-end-{}".format(mark_counter[0])
            pending_mark[0] = mark_name
            send_mark_to_twilio(mark_name)

            # Fallback: estimate playback duration in case mark never arrives
            # mulaw 8000Hz = 8000 bytes/sec
            streaming_elapsed = time.time() - speak_start
            playback_duration = total_audio_bytes / 8000.0
            remaining_playback = max(0, playback_duration - streaming_elapsed)
            playback_end_estimate[0] = time.time() + remaining_playback
            logger.info("TTS playback estimate: %.1fs total, %.1fs remaining. Waiting for mark: %s",
                        playback_duration, remaining_playback, mark_name)

            # Fallback timer: if mark doesn't arrive within estimated time + 3s, clear anyway
            def _fallback_clear():
                fallback_wait = remaining_playback + 3.0
                time.sleep(fallback_wait)
                if is_speaking.is_set() and pending_mark[0] == mark_name:
                    logger.warning("Mark %s never arrived, clearing is_speaking via fallback after %.1fs", mark_name, fallback_wait)
                    is_speaking.clear()
                    playback_end_estimate[0] = time.time()
                    while not transcript_q.empty():
                        try:
                            transcript_q.get_nowait()
                        except queue.Empty:
                            break
            threading.Thread(target=_fallback_clear, daemon=True).start()

    # --- Background thread: process transcripts ---
    def process_transcripts():
        """Read final transcripts, get Claude response, speak it."""
        buf = ""
        last_activity = time.time()
        silence_warning_sent = False
        SILENCE_WARNING_SECS = 15  # Ask "mi sente?" after 15s silence
        SILENCE_HANGUP_SECS = 30   # Hang up after 30s total silence

        while not stop_event.is_set():
            try:
                text = transcript_q.get(timeout=1.0)

                # Reset silence tracking on any input
                last_activity = time.time()
                silence_warning_sent = False

                # Wait for conversation to be initialized
                if conversation is None:
                    continue

                # Ignore input while AI is speaking (prevents echo/repeat)
                if is_speaking.is_set():
                    logger.info("Ignoring input while speaking: %s", text)
                    continue

                logger.info("Transcript received (is_speaking=OFF): %s", text)

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
                last_activity = time.time()

                # Auto-hangup after farewell
                if "buona giornata" in response_text.lower() or "buona serata" in response_text.lower():
                    logger.info("HANGUP: Detected 'buona giornata' in response, closing call in 3s")
                    time.sleep(3)
                    stop_event.set()
                    break

            except queue.Empty:
                # Timeout -- check if there's buffered text
                if buf:
                    user_input = buf.strip()
                    buf = ""
                    response_text = conversation.get_response(user_input)
                    speak(response_text)
                    last_activity = time.time()

                    # Auto-hangup after farewell
                    if "buona giornata" in response_text.lower() or "buona serata" in response_text.lower():
                        logger.info("HANGUP: Detected 'buona giornata' in response, closing call in 3s")
                        time.sleep(3)
                        stop_event.set()
                        break

                    continue

                # Silence timeout check (only after opening message)
                if conversation and not is_speaking.is_set():
                    # Use the LATER of last_activity and playback_end_estimate
                    # so silence timer doesn't fire while audio is still playing
                    silence_start = max(last_activity, playback_end_estimate[0])
                    silence_duration = time.time() - silence_start

                    if silence_duration >= SILENCE_HANGUP_SECS and silence_warning_sent:
                        # Too long silence after warning - hang up
                        logger.info("SILENCE: Hanging up after %ds of silence", int(silence_duration))
                        speak("Non la sento piu'. La richiamero' tra poco. Buona giornata!")
                        stop_event.set()
                        break

                    elif silence_duration >= SILENCE_WARNING_SECS and not silence_warning_sent:
                        # First warning
                        lead_first = lead_data.get("nome", "").strip() if lead_data else ""
                        if lead_first:
                            speak("{}, e' ancora in linea? Mi sente bene?".format(lead_first))
                        else:
                            speak("E' ancora in linea? Mi sente bene?")
                        silence_warning_sent = True
                        last_activity = time.time()  # Reset to give them time to respond

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
                    # Add website intelligence if available
                    website_info = lead_data.get("website_info", "")
                    if website_info:
                        prompt += "\n\n## INFO DAL SITO WEB DEL PROSPECT (hai gia' analizzato il loro sito)"
                        prompt += "\n{}".format(website_info)
                        prompt += "\nUSA queste info per dimostrare che ti sei preparata. Di': 'Ho dato un'occhiata al vostro sito e ho visto che vi occupate di...' NON chiedere di cosa si occupa se lo sai gia'."
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

            elif event == "mark":
                # Twilio confirms audio playback finished
                mark_name = data.get("mark", {}).get("name", "")
                logger.info("Mark received from Twilio: %s (pending: %s)", mark_name, pending_mark[0])
                if mark_name and mark_name == pending_mark[0]:
                    pending_mark[0] = None
                    # Grace period: wait 500ms for echo tail to die out
                    def _mark_clear(mn=mark_name):
                        time.sleep(0.5)
                        is_speaking.clear()
                        playback_end_estimate[0] = time.time()
                        # Drain any echo transcripts
                        while not transcript_q.empty():
                            try:
                                transcript_q.get_nowait()
                            except queue.Empty:
                                break
                        logger.info("is_speaking cleared after mark %s + 500ms grace", mn)
                    threading.Thread(target=_mark_clear, daemon=True).start()

            elif event == "media":
                # ALWAYS forward audio to Deepgram (gating breaks the STT connection)
                # Echo is handled by discarding TRANSCRIPTS while is_speaking, not audio
                payload = data["media"]["payload"]
                audio_bytes = base64.b64decode(payload)
                audio_packet_count[0] += 1
                if audio_packet_count[0] % 500 == 1:
                    logger.info("Audio packets sent to Deepgram: %d", audio_packet_count[0])
                if len(audio_bytes) == 0:
                    continue
                try:
                    dg = dg_ws_container[0]
                    if dg and dg.connected:
                        dg.send_binary(audio_bytes)
                except Exception as e:
                    logger.warning("Error sending audio to Deepgram: %s", e)

            elif event == "stop":
                logger.info("Stream stopped")
                break

    except Exception:
        logger.exception("Error in WebSocket handler")
    finally:
        stop_event.set()
        transcript_thread.join(timeout=5.0)
        try:
            dg = dg_ws_container[0]
            if dg and dg.connected:
                dg.send(json.dumps({"type": "CloseStream"}))
                time.sleep(0.3)
                dg.close()
        except Exception:
            pass

        # Save call to history and notify Davide
        if conversation and lead_data:
            phone = lead_data.get("cellulare", "")
            transcript_text = "\n".join(
                "{}: {}".format(role, text) for role, text in conversation.transcript_log
            )
            # Determine call outcome
            full_text = " ".join(t for _, t in conversation.transcript_log).lower()
            non_target_phrases = ["non sono interessat", "non mi interessa", "non fa per me", "non e' il momento", "non è il momento", "non ho tempo", "non ho bisogno"]
            if any(phrase in full_text for phrase in non_target_phrases):
                status = "non in target"
            elif "confermo la consulenza" in full_text:
                status = "qualificato"
            else:
                status = "da confermare"

            entry = {
                "phone": phone,
                "nome": lead_data.get("nome", ""),
                "cognome": lead_data.get("cognome", ""),
                "ruolo": lead_data.get("ruolo", ""),
                "obiettivi": lead_data.get("obiettivi_linkedin", ""),
                "status": status,
                "transcript": transcript_text,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "data_consulenza": lead_data.get("data_consulenza", ""),
            }
            call_history.append(entry)
            logger.info("Call saved to history: %s %s - %s", entry["nome"], entry["cognome"], entry["status"])

            # Schedule pre-consultation reminder if qualified
            if status == "qualificato" and phone:
                schedule_reminder(phone, lead_data)

            # Save transcript and notify Davide via WhatsApp with link
            if transcript_text:
                transcript_url = save_transcript(entry, transcript_text)
                esito_map = {"qualificato": "Confermato", "non in target": "Non Confermato", "da confermare": "Da Confermare"}
                esito = esito_map.get(entry["status"], "Da Confermare")
                summary = "📞 CALL COMPLETATA\n{} {} - {}\nRuolo: {}\nEsito: {}\n\n📄 Trascrizione completa:\n{}".format(
                    entry["nome"], entry["cognome"], entry["phone"],
                    entry["ruolo"] or "N/A",
                    esito,
                    transcript_url,
                )
                def notify_davide():
                    try:
                        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                        client.messages.create(
                            to="whatsapp:{}".format(DAVIDE_PHONE),
                            from_="whatsapp:{}".format(TWILIO_WHATSAPP_NUMBER),
                            body=summary[:1600],
                        )
                        logger.info("Notifica WhatsApp a Davide inviata")
                    except Exception:
                        logger.exception("Errore notifica Davide")
                threading.Thread(target=notify_davide, daemon=True).start()

        logger.info("Media stream handler finished")


# ---------------------------------------------------------------------------
# Start server
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Keep-alive ping (prevents Render free tier from sleeping)
# ---------------------------------------------------------------------------
def keep_alive():
    """Ping own /health endpoint every 10 minutes to prevent Render sleep."""
    while True:
        time.sleep(600)  # 10 minutes
        try:
            httpx.get("{}/health".format(PUBLIC_URL), timeout=10.0)
            logger.info("KEEPALIVE: Ping sent")
        except Exception:
            logger.warning("KEEPALIVE: Ping failed")

keepalive_thread = threading.Thread(target=keep_alive, daemon=True)
keepalive_thread.start()


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
