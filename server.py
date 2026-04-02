"""
Twilio AI Setter Voice Agent
-----------------------------
Flask + flask-sock server that orchestrates:
  Twilio Media Stream  <->  OpenAI Realtime API (speech-to-speech, GPT-4o)

Audio flows directly between Twilio and OpenAI — no separate STT/TTS needed.
Everything runs on a single port.
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
from flask import Flask, Response, jsonify, request
from flask_sock import Sock
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, VoiceResponse

from knowledge_base import get_knowledge_prompt, check_lead_prefilter, _format_time_spoken, _format_date_spoken, _extract_time_from_iso
from setter_prompt import get_setter_prompt
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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
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
for _key_name in ["OPENAI_API_KEY", "TWILIO_ACCOUNT_SID"]:
    _val = os.getenv(_key_name)
    if not _val:
        raise RuntimeError("Missing env var: {}".format(_key_name))
    print("  {} = {}...".format(_key_name, _val[:15]))

# Also validate optional keys (for website scraping, etc.)
for _key_name in ["GROQ_API_KEY"]:
    _val = os.getenv(_key_name)
    if _val:
        print("  {} = {}...".format(_key_name, _val[:15]))

# OpenAI Realtime API
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2025-06-03"

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
            "data_consulenza": _format_date_spoken(event.get("start_time", "")),
            "ora_consulenza": _extract_time_from_iso(event.get("start_time", "")),
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

        # FILTRO RISPOSTE FORM: DISABILITATO — il pre-filtro Python (check_lead_prefilter)
        # gestisce B2C e cerca-lavoro durante la chiamata, non serve bloccare qui.
        # I lead vengono sempre chiamati e filtrati in conversazione.
        all_answers = " ".join(qa.get("answer", "") for qa in questions).lower()
        logger.info("Lead form answers: %s", all_answers[:300])

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

    if answered_by in ("machine_end_beep", "fax"):
        # Only hang up on confirmed voicemail beep or fax — machine_start has too many false positives
        logger.info("AMD: Confirmed voicemail/fax for SID=%s, hanging up", call_sid)
        try:
            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.calls(call_sid).update(status="completed")
        except Exception:
            logger.exception("AMD: Failed to hang up call %s", call_sid)
    else:
        logger.info("AMD: AnsweredBy=%s for SID=%s — continuing call (not hanging up)", answered_by, call_sid)

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


@app.route("/test-response", methods=["POST"])
def test_response():
    """Test Groq responses without making a phone call — uses the REAL knowledge_base prompt.

    POST JSON with:
      - nome, data_consulenza, ora_consulenza: lead info
      - ruolo, acquisizione_clienti, obiettivi_linkedin, usa_linkedin,
        sito_web, fatturato, budget: Calendly form fields
      - messages: list of {"role": "user"|"assistant", "content": "..."}
      - OR message: single string (shortcut for one user message)

    Returns the AI response with word count and token info.
    """
    data = request.json or {}

    # Build prompt using the REAL knowledge_base (same as actual calls)
    nome = data.get("nome", "Marco")
    data_consulenza = data.get("data_consulenza", "2 aprile 2026")
    ora_consulenza = data.get("ora_consulenza", "15:00")

    prompt = get_knowledge_prompt(
        lead_name=nome,
        appointment_date=data_consulenza,
        appointment_time=ora_consulenza,
    )

    # Append Calendly form data (same as WebSocket handler)
    ruolo = data.get("ruolo", "")
    if ruolo:
        prompt += "\n## RISPOSTE FORM CALENDLY (il lead ha gia' compilato queste info)"
        prompt += "\n- Ruolo: {}".format(ruolo)
        prompt += "\n- Come acquisisce clienti: {}".format(data.get("acquisizione_clienti", ""))
        prompt += "\n- Obiettivi LinkedIn: {}".format(data.get("obiettivi_linkedin", ""))
        prompt += "\n- Usa gia' LinkedIn: {}".format(data.get("usa_linkedin", ""))
        prompt += "\n- Sito web: {}".format(data.get("sito_web", ""))
        prompt += "\n- Fatturato azienda: {}".format(data.get("fatturato", ""))
        prompt += "\n- Budget disponibile: {}".format(data.get("budget", ""))

    # --- PRE-FILTRO PYTHON: B2C puro e cerca-lavoro ---
    rejection_msg = check_lead_prefilter(
        ruolo=ruolo,
        obiettivi=data.get("obiettivi_linkedin", ""),
    )

    conv = ConversationManager(prompt)

    # Support single message or conversation
    messages = data.get("messages", [])
    if not messages and data.get("message"):
        messages = [{"role": "user", "content": data["message"]}]

    if not messages:
        # Default test conversation
        messages = [
            {"role": "user", "content": "Pronto?"},
        ]

    responses = []
    opening_done = False
    rejected = False

    for msg in messages:
        if msg["role"] == "user":
            if rejected:
                # Call already closed, skip
                continue

            if rejection_msg and opening_done:
                # After opening, serve rejection message instead of LLM
                responses.append({
                    "user": msg["content"],
                    "stefania": rejection_msg,
                    "word_count": len(rejection_msg.split()),
                    "ok": True,
                    "prefilter": "rejected",
                })
                rejected = True
                continue

            resp = conv.get_response(msg["content"])
            word_count = len(resp.split())
            responses.append({
                "user": msg["content"],
                "stefania": resp,
                "word_count": word_count,
                "ok": word_count <= 15,
            })
            opening_done = True
        elif msg["role"] == "assistant":
            # Inject assistant message into history
            conv.messages.append({"role": "assistant", "content": msg["content"]})

    return jsonify({
        "responses": responses,
        "total_turns": len(responses),
        "prompt_used": "knowledge_base",
        "form_data": bool(ruolo),
        "prefilter_rejection": bool(rejection_msg),
    })


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "version": "v6.18-noise-reduction"}


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
# Groq LLaMA conversation  -- kept for /test-response endpoint only
# ---------------------------------------------------------------------------

class ConversationManager:
    """Maintains conversation history and queries Groq LLaMA 3. Used by /test-response only."""

    def __init__(self, system_prompt):
        self.system_prompt = system_prompt
        self.messages = []
        self.transcript_log = []

    def get_response(self, user_text):
        self.messages.append({"role": "user", "content": user_text})
        self.transcript_log.append(("Lead", user_text))
        try:
            groq_messages = [{"role": "system", "content": self.system_prompt}] + self.messages
            resp = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer {}".format(GROQ_API_KEY), "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "max_tokens": 60, "temperature": 0.7, "messages": groq_messages},
                timeout=10.0,
            )
            resp.raise_for_status()
            assistant_text = resp.json()["choices"][0]["message"]["content"].strip()
            self.messages.append({"role": "assistant", "content": assistant_text})
            self.transcript_log.append(("Stefania", assistant_text))
            return assistant_text
        except Exception:
            logger.exception("Groq API error")
            return "Mi scusi, problema tecnico."


# ---------------------------------------------------------------------------
# WebSocket handler  (Twilio Media Stream <-> OpenAI Realtime API)
# ---------------------------------------------------------------------------

def build_realtime_prompt(lead_data):
    """Build a compact system prompt for OpenAI Realtime API.

    Keep it SHORT (<3000 chars) so GPT-4o can focus on LISTENING.
    The full knowledge_base prompt is too long for real-time speech.
    """
    nome = lead_data.get("nome", "").strip()
    cognome = lead_data.get("cognome", "").strip()
    lead_name = "{} {}".format(nome, cognome).strip() or "il lead"
    first_name = nome or "buongiorno"
    ruolo = lead_data.get("ruolo", "")
    obiettivi = lead_data.get("obiettivi_linkedin", "")
    fatturato = lead_data.get("fatturato", "")
    budget = lead_data.get("budget", "")
    data_consulenza = lead_data.get("data_consulenza", "")
    ora_consulenza = lead_data.get("ora_consulenza", "")
    website_info = lead_data.get("website_info", "")

    # Format appointment time spoken
    ora_spoken = ""
    if ora_consulenza:
        ora_spoken = _format_time_spoken(ora_consulenza)

    prompt = """## RUOLO
Sei Stefania, assistente telefonica del team LinkedIn di Davide Caiazzo (DC Academy).
Stai chiamando {lead_name} che ha prenotato una consulenza strategica gratuita con Davide.
Il tuo obiettivo: pre-qualificare il lead e confermare o annullare la consulenza.

## PERSONALITA' E TONO
- Personalita': Cordiale, empatica, genuinamente interessata alla persona al telefono.
- Tono: Caldo, professionale, mai invadente. Come una collega che chiama per aiutare.
- Lunghezza: MASSIMO 1-2 frasi per turno. Poi FERMATI.
- Velocita': Parla in modo naturale e scorrevole, non troppo veloce ne' troppo lento.
- NON ripetere mai la stessa frase due volte. Varia le tue risposte.
- NON includere effetti sonori o espressioni onomatopeiche.
- Rispondi SOLO con il testo parlato. Niente asterischi, parentesi, o descrizioni di azioni.

## LINGUA
La conversazione sara' SOLO in italiano. NON rispondere MAI in altre lingue, anche se il lead parla in un'altra lingua.

## AUDIO NON CHIARO
Se l'audio del lead non e' chiaro (rumore di fondo, silenzio, incomprensibile), chiedi chiarimento:
- "Mi scusi, non ho sentito bene. Puo' ripetere?"
- Se il silenzio continua: "{first_name}, mi sente? E' ancora in linea?"
- Se continua ancora: "Sembra ci siano problemi di linea. La richiamo. Arrivederci!"

## CONTESTO
- Dati lead: {lead_name}, ruolo: {ruolo}, obiettivi: {obiettivi}
- Fatturato: {fatturato}, budget: {budget}
- Consulenza prenotata: {data_consulenza} {ora_spoken}
{website_section}
- DC Academy insegna a professionisti B2B a usare LinkedIn per trovare clienti
- Due percorsi: COACHING (insegniamo a usare LinkedIn) o GESTIONE (gestiamo noi il profilo)
- La consulenza e' gratuita, la fa Davide Caiazzo (223mila follower LinkedIn)
- NON chiedere informazioni che hai gia' dai dati sopra

## FLUSSO CONVERSAZIONE

FASE 1 - APERTURA
- ASPETTA che il lead parli ("Pronto?", "Si?", "Chi e'?", "Ciao") PRIMA di dire qualsiasi cosa
- NON parlare finche' il lead non ha detto qualcosa
- Quando il lead risponde, presentati: "Ciao {first_name}, sono Stefania del team LinkedIn di Davide Caiazzo!"
- Poi spiega: "La chiamo per la consulenza che ha prenotato. Devo farle un paio di domande veloci per preparare al meglio la call con Davide."
- NON USARE SEMPRE QUESTA FRASE, VARIA
- Uscita: Il lead accetta di rispondere

FASE 2 - FILTRO
- Se il ruolo e' parrucchiere, estetista, ristorante, negozio, bar, palestra o altra attivita' B2C pura:
  "Il nostro metodo funziona per chi vende ad aziende. Per la sua attivita' le abbiamo mandato risorse via email. Buona giornata!"
- Se l'obiettivo e' "trovare lavoro" o "cerco impiego" o il ruolo e' "disoccupato":
  "Noi lavoriamo con chi vuole trovare clienti. Per la ricerca lavoro le abbiamo mandato risorse via email. In bocca al lupo!"
- In entrambi i casi: call FINITA, NON fare altre domande.

FASE 3 - DISCOVERY (una domanda alla volta, FERMATI e ascolta)
- "Come ci ha conosciuto?" — Ascolta, commenta brevemente
- Se hai info dal sito web: "Ho dato un'occhiata al vostro sito e ho visto che vi occupate di" e dici il settore specifico dai dati (es. "consulenza fiscale", "formazione"). Se non hai info dal sito: "Mi racconta brevemente di cosa si occupa?"
- "Chi e' il suo cliente ideale? A che tipo di aziende si rivolge?" (SALTA se lo sai gia' dal sito o dalla risposta precedente)
- "Ho visto che come obiettivo ha indicato di {obiettivi}." Riformula sempre in terza persona (es. "posizionarmi" diventa "posizionarsi"). Approfondisci: se cerca clienti chiedi che tipo, se cerca partner chiedi quali. Se cerca lavoro: "In realta' questa chiamata e' pensata per chi cerca clienti, per il suo caso dovrebbe contattare la mia collega tramite il link ricevuto via email." e chiudi.
- Se dal sito capisci la zona: "Dal sito mi sembra che lavoriate a livello [nazionale/regionale/locale] o mi sbaglio?" Se non hai info dal sito: "Lavora solo nella sua zona o anche a livello nazionale?" Se lavora solo in zona molto ristretta, chiudi: "Le dico la verita', LinkedIn funziona meglio per chi ha un pubblico piu' ampio. Probabilmente non riusciremmo ad aiutarla." Ascolta e se non ti convince del contrario non confermarlo, ma gentilmente.
- Se hai il budget dal form: "Ho visto che ha indicato {budget} come investimento potenziale. E' lei che prende la decisione o deve confrontarsi con qualcuno?"
  Se NON hai il budget: "Se Davide le propone un percorso, e' lei che decide o deve sentire qualcun altro?"
  Se dice "devo sentire il socio": "Puo' coinvolgerlo nella consulenza? Cosi' Davide parla direttamente con chi decide."
- IMPORTANTE: Se il lead fa una domanda, RISPONDI PRIMA alla sua domanda
- Uscita: Hai le informazioni per qualificare

CHECKLIST MENTALE (prima di confermare, servono almeno 3 GO su 4):
1. B2B? 2. Budget >= 1500 euro? 3. Decisore? 4. Zona geografica almeno regionale?
Se 2+ NO-GO: "Per la sua situazione le abbiamo mandato risorse via email. Quando le circostanze saranno piu' favorevoli, ci ricontatti. Buona giornata!"

FASE 4 - CHIUSURA
Se qualificato (fai UN passo alla volta, FERMATI dopo ogni frase):
- Turno A: "Perfetto {first_name}! Sulla base di quello che mi ha detto, la consulenza con Davide e' assolutamente in linea. Davide analizzera' il suo profilo e le dara' una strategia concreta."
- Turno B: "Ha ricevuto la mail con il link di Google Meet per collegarsi?" — Se no: "Lo segnalo subito ai colleghi che gliela rimanderanno a stretto giro."
- Turno C: "Le chiedo la massima puntualita' perche' la consulenza sara' direttamente con Davide Caiazzo che ha un calendario abbastanza pieno e se non iniziamo puntuali non riusciremo ad aiutarla al meglio. Ci vediamo il {data_consulenza} {ora_spoken}. Grazie e buona giornata!" (dopo le 17 dire "buona serata")
- Non qualificato: "Per la sua situazione le abbiamo mandato risorse via email. Ci ricontatti quando vuole. Buona giornata!"
- Uscita: "buona giornata/serata" = call finita

## OBIEZIONI
- "Non ho tempo" -> "Capisco, l'unico problema e' che se non riesco a farle queste due domandine veloci saro' costretta ad assegnarle un altro consulente, ci metto davvero due minuti contati. Possiamo procedere?"
- "Quanto costa?" -> "I dettagli li vedra' con Davide. Il mio ruolo e' prepararle una call utile."
- "Non mi interessa" -> "Capisco, cosa e' cambiato rispetto a quando ha prenotato?"
- "Ho gia' speso con un'agenzia" -> "Capisco. Ma avevano i risultati che ha Davide su LinkedIn e centinaia di testimonianze? Ha visto i 3 video degli imprenditori dalla pagina dove ha prenotato la consulenza?"
- "Non ho tempo per LinkedIn" -> "Abbiamo un servizio dove il nostro team gestisce completamente il suo profilo. Lei non deve dedicare neanche un minuto."
- "Sto parlando con un'altra agenzia" -> "Ottimo, significa che ha capito l'importanza di LinkedIn. Le consiglio di fare la consulenza con Davide prima di firmare: potra' confrontare le proposte. I risultati di Davide con 223mila follower parlano da soli."
- "Solo pagina aziendale" -> "Si puo' fare, pero' su LinkedIn i profili personali ottengono 10 volte piu' visibilita'. Davide le spieghera' come far lavorare entrambi."
- "Magari piu' avanti" -> "Capisco. Cosa cambiera' tra qualche mese? I suoi concorrenti stanno gia' costruendo la loro presenza. Vuole che la ricontattiamo fra quanto?"

## REGOLE
- MAI dire "ti rubo tempo"
- MAI dire "perfetto" dopo qualcosa di negativo, usa "capisco"
- NON ripetere il saluto iniziale, ti sei gia' presentata
- NON usare frasi goffe o meccaniche per passare da una domanda all'altra. Sii naturale.
- Dopo "buona giornata/serata" la call e' FINITA""".format(
        first_name=first_name,
        lead_name=lead_name,
        ruolo=ruolo or "non specificato",
        obiettivi=obiettivi or "non specificati",
        fatturato=fatturato or "non specificato",
        budget=budget or "non specificato",
        data_consulenza=data_consulenza or "da definire",
        ora_spoken=ora_spoken,
        website_section="\n- Info sito web: {}\n".format(website_info) if website_info else "",
    )
    return prompt, first_name


@sock.route("/media-stream")
def handle_media_stream(ws):
    """Handle Twilio Media Stream by bridging audio to/from OpenAI Realtime API.

    SINGLE-THREAD model for OpenAI WebSocket:
      - openai_thread: owns the OpenAI WS exclusively — both recv and send
      - Main thread: receives from Twilio WS, queues messages for openai_thread
    This eliminates ALL concurrent access to the OpenAI WebSocket.
    """
    logger.info("WebSocket connection opened")

    stream_sid = None
    lead_data = None
    stop_event = threading.Event()
    farewell_detected = [False]
    farewell_complete = [False]  # True after farewell response.done
    hangup_timer = [None]  # fallback timer reference
    transcript_log = []  # [(role, text), ...]
    ws_send_lock = threading.Lock()

    # Track current response for interruption handling
    last_assistant_item_id = [None]
    response_start_timestamp = [None]
    latest_media_timestamp = [0]

    # Gate: don't forward audio to OpenAI until opening message starts playing
    opening_audio_started = threading.Event()

    # Queue for messages TO OpenAI (main thread -> openai_thread)
    openai_send_queue = queue.Queue()

    # --- Connect to OpenAI Realtime API ---
    openai_ws = websocket.WebSocket()

    try:
        openai_ws.connect(
            OPENAI_REALTIME_URL,
            header=[
                "Authorization: Bearer {}".format(OPENAI_API_KEY),
                "OpenAI-Beta: realtime=v1",
            ],
        )
        logger.info("OpenAI Realtime API connected")
    except Exception:
        logger.exception("Failed to connect to OpenAI Realtime API")
        ws.close()
        return

    # --- Helper: queue message for OpenAI (called from main thread) ---
    def send_to_openai(msg):
        openai_send_queue.put(msg)

    # --- Helper: send to Twilio WS (called from openai_thread) ---
    def send_to_twilio(msg_dict):
        with ws_send_lock:
            try:
                ws.send(json.dumps(msg_dict))
            except Exception:
                logger.exception("Error sending to Twilio")

    # Track when audio was last committed (for response timeout safety net)
    last_committed_time = [0.0]
    waiting_for_response = [False]

    # --- SINGLE background thread: owns OpenAI WS exclusively ---
    def openai_loop():
        """Single thread that handles ALL OpenAI WebSocket I/O.
        - Sends queued messages (audio, config, etc.)
        - Receives events (audio deltas, transcripts, etc.)
        No other thread touches openai_ws.
        """
        while not stop_event.is_set():
            # 1) Drain send queue — send all pending messages
            drained = 0
            while drained < 200:  # cap to avoid starving recv
                try:
                    msg = openai_send_queue.get_nowait()
                    openai_ws.send(json.dumps(msg))
                    drained += 1
                except queue.Empty:
                    break
                except Exception:
                    if stop_event.is_set():
                        return
                    logger.exception("Error sending to OpenAI")

            # Safety net: if we committed audio but no response came in 8s, force one
            if waiting_for_response[0] and (time.time() - last_committed_time[0]) > 8.0:
                logger.warning("SAFETY: No response 8s after commit — forcing response.create")
                try:
                    openai_ws.send(json.dumps({"type": "response.create"}))
                except Exception:
                    pass
                waiting_for_response[0] = False

            # 2) Try to receive one event (short timeout)
            openai_ws.settimeout(0.05)  # 50ms
            try:
                result_raw = openai_ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException:
                logger.warning("OpenAI WebSocket closed")
                break
            except Exception as e:
                if stop_event.is_set():
                    break
                logger.warning("OpenAI recv error: %s", e)
                continue

            if not result_raw:
                continue

            try:
                event = json.loads(result_raw)
            except (json.JSONDecodeError, TypeError):
                continue

            event_type = event.get("type", "")

            # --- Handle OpenAI events ---
            if event_type == "session.created":
                logger.info("OpenAI session created: %s", event.get("session", {}).get("id", ""))

            elif event_type == "session.updated":
                logger.info("OpenAI session configured OK")

            elif event_type == "response.audio.delta":
                delta = event.get("delta", "")
                if delta and stream_sid:
                    send_to_twilio({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": delta},
                    })
                    if not opening_audio_started.is_set():
                        opening_audio_started.set()
                        logger.info("Opening audio started — user audio enabled")
                    if response_start_timestamp[0] is None:
                        response_start_timestamp[0] = latest_media_timestamp[0]
                    # Response is being generated — clear the waiting flag
                    waiting_for_response[0] = False
                item_id = event.get("item_id", "")
                if item_id:
                    last_assistant_item_id[0] = item_id

            elif event_type == "response.audio.done":
                logger.info("AI audio complete")
                if stream_sid:
                    send_to_twilio({"event": "mark", "streamSid": stream_sid, "mark": {"name": "ai-done"}})

            elif event_type == "response.audio_transcript.done":
                transcript = event.get("transcript", "")
                if transcript:
                    logger.info("Stefania: %s", transcript)
                    transcript_log.append(("Stefania", transcript))
                    lower = transcript.lower()
                    if "buona giornata" in lower or "buona serata" in lower or "in bocca al lupo" in lower:
                        logger.info("HANGUP: farewell detected — waiting for audio to finish")
                        farewell_detected[0] = True

            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript:
                    logger.info("Lead: %s", transcript)
                    transcript_log.append(("Lead", transcript))

            elif event_type == "conversation.item.input_audio_transcription.failed":
                logger.warning("Transcription failed: %s", event.get("error", {}).get("message", ""))

            elif event_type == "input_audio_buffer.committed":
                # Audio buffer committed — OpenAI should auto-generate response (server VAD)
                logger.info("Audio committed — waiting for response")
                last_committed_time[0] = time.time()
                waiting_for_response[0] = True

            elif event_type == "input_audio_buffer.speech_started":
                if not opening_audio_started.is_set():
                    logger.info("Speech before opening — ignored")
                    continue
                logger.info("Barge-in: user speaking")
                if stream_sid:
                    send_to_twilio({"event": "clear", "streamSid": stream_sid})
                # Only truncate if AI is currently speaking
                item_id = last_assistant_item_id[0]
                if item_id and response_start_timestamp[0] is not None:
                    elapsed = latest_media_timestamp[0] - response_start_timestamp[0]
                    elapsed_ms = max(0, int(elapsed))
                    try:
                        openai_ws.send(json.dumps({
                            "type": "conversation.item.truncate",
                            "item_id": item_id,
                            "content_index": 0,
                            "audio_end_ms": elapsed_ms,
                        }))
                        logger.info("Truncated (item=%s, %dms)", item_id, elapsed_ms)
                    except Exception:
                        logger.exception("Error truncating")
                else:
                    logger.info("Barge-in but no active AI audio — no truncation needed")
                response_start_timestamp[0] = None
                last_assistant_item_id[0] = None

            elif event_type == "input_audio_buffer.speech_stopped":
                logger.info("User speech stopped")
                # After farewell is complete, lead's goodbye = last speaker → close
                if farewell_complete[0]:
                    logger.info("HANGUP: lead spoke after farewell — closing in 3s")
                    if hangup_timer[0]:
                        hangup_timer[0].cancel()
                    t = threading.Timer(3.0, lambda: stop_event.set())
                    t.daemon = True
                    t.start()
                    hangup_timer[0] = t

            elif event_type == "response.done":
                resp = event.get("response", {})
                st = resp.get("status", "")
                if st == "failed":
                    logger.error("Response FAILED: %s", resp.get("status_details", {}))
                elif st == "cancelled":
                    logger.info("Response cancelled (barge-in)")
                response_start_timestamp[0] = None
                waiting_for_response[0] = False
                # After farewell response completes: block new responses, start fallback
                if farewell_detected[0] and st == "completed":
                    farewell_complete[0] = True
                    logger.info("HANGUP: farewell response done — waiting for lead goodbye (15s fallback)")
                    t = threading.Timer(15.0, lambda: stop_event.set())
                    t.daemon = True
                    t.start()
                    hangup_timer[0] = t

            elif event_type == "error":
                err = event.get("error", {})
                logger.error("OpenAI error: %s %s %s", err.get("type", ""), err.get("code", ""), err.get("message", ""))

            elif event_type == "response.created":
                # Block new responses after farewell — Stefania should NOT talk after goodbye
                if farewell_complete[0]:
                    logger.info("Blocking post-farewell response")
                    send_to_openai({"type": "response.cancel"})

            elif event_type not in (
                "response.output_item.added", "response.output_item.done",
                "response.content_part.added", "response.content_part.done",
                "response.audio_transcript.delta", "conversation.item.created",
                "rate_limits.updated",
            ):
                logger.info("OpenAI event: %s", event_type)

    openai_thread = threading.Thread(target=openai_loop, daemon=True)
    openai_thread.start()

    # --- Main loop: receive from Twilio, queue to OpenAI ---
    session_configured = False
    try:
        while not stop_event.is_set():
            try:
                raw_message = ws.receive()
            except Exception as e:
                logger.info("Twilio WS closed: %s", e)
                break

            if raw_message is None:
                logger.info("Twilio WS closed by remote")
                break

            try:
                data = json.loads(raw_message)
            except (json.JSONDecodeError, TypeError):
                continue

            event = data.get("event")

            if event == "connected":
                logger.info("Twilio stream connected")

            elif event == "start":
                stream_sid = data["start"]["streamSid"]
                call_sid = data["start"].get("callSid", "")
                logger.info("Stream start: SID=%s CallSID=%s", stream_sid, call_sid)

                # Look up lead data
                lead_data = active_leads.get(call_sid, {})
                if not lead_data:
                    for key, val in active_leads.items():
                        if key.startswith("+"):
                            lead_data = val
                            break

                # Build compact prompt
                system_prompt, first_name = build_realtime_prompt(lead_data)

                logger.info("Lead: %s %s - ruolo: %s - budget: %s",
                            lead_data.get("nome", ""), lead_data.get("cognome", ""),
                            lead_data.get("ruolo", "N/A"), lead_data.get("budget", "N/A"))

                # Configure OpenAI session
                send_to_openai({
                    "type": "session.update",
                    "session": {
                        "modalities": ["text", "audio"],
                        "instructions": system_prompt,
                        "voice": "coral",
                        "input_audio_format": "g711_ulaw",
                        "output_audio_format": "g711_ulaw",
                        "input_audio_noise_reduction": {"type": "near_field"},
                        "input_audio_transcription": {"model": "whisper-1"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "low",
                        },
                        "temperature": 0.8,
                        "max_response_output_tokens": "inf",
                    },
                })
                session_configured = True
                logger.info("Session configured (%d chars prompt)", len(system_prompt))

                # No manual opening — prompt tells Stefania to wait for lead's "Pronto?"
                logger.info("Session ready — waiting for lead to speak (%s)", first_name)

            elif event == "media":
                payload = data["media"]["payload"]
                timestamp = int(data["media"].get("timestamp", "0"))
                latest_media_timestamp[0] = timestamp

                if session_configured:
                    send_to_openai({
                        "type": "input_audio_buffer.append",
                        "audio": payload,
                    })

            elif event == "mark":
                pass  # marks handled silently

            elif event == "stop":
                logger.info("Stream stopped")
                break

    except Exception:
        logger.exception("Error in media stream handler")
    finally:
        stop_event.set()
        openai_thread.join(timeout=5.0)
        try:
            if openai_ws.connected:
                openai_ws.close()
        except Exception:
            pass

        # Save call to history and notify Davide
        if lead_data and transcript_log:
            phone = lead_data.get("cellulare", "")
            transcript_text = "\n".join(
                "{}: {}".format(role, text) for role, text in transcript_log
            )
            full_text = " ".join(t for _, t in transcript_log).lower()
            non_target_phrases = ["non sono interessat", "non mi interessa", "non fa per me",
                                  "non e' il momento", "non è il momento", "non ho tempo", "non ho bisogno"]
            if any(phrase in full_text for phrase in non_target_phrases):
                status = "non in target"
            elif "conferm" in full_text and "consulenz" in full_text:
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
            logger.info("Call saved: %s %s - %s", entry["nome"], entry["cognome"], entry["status"])

            if status == "qualificato" and phone:
                schedule_reminder(phone, lead_data)

            if transcript_text:
                transcript_url = save_transcript(entry, transcript_text)
                esito_map = {"qualificato": "Confermato", "non in target": "Non Confermato", "da confermare": "Da Confermare"}
                esito = esito_map.get(entry["status"], "Da Confermare")
                summary = "📞 CALL COMPLETATA\n{} {} - {}\nRuolo: {}\nEsito: {}\n\n📄 Trascrizione:\n{}".format(
                    entry["nome"], entry["cognome"], entry["phone"],
                    entry["ruolo"] or "N/A", esito, transcript_url,
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
