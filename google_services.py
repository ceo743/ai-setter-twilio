"""
Google Workspace automation for Stefania AI Setter.
Uses a service account with domain-wide delegation to:
- Send emails via Gmail API (as academy@davidecaiazzo.it)
- Create calendar events (on ceo@davidecaiazzo.it)
- Duplicate Google Docs templates and fill with lead data
"""

import json
import logging
import os
import base64
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger("google-services")

# Scopes for domain-wide delegation
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

SENDER_EMAIL = "academy@davidecaiazzo.it"
IMPERSONATE_EMAIL = "ceo@davidecaiazzo.it"
CALENDAR_ID = "ceo@davidecaiazzo.it"
DOC_TEMPLATE_ID = "144O6QWLfnq-EPtvTmn2eQCUhXrxGBR8GeBF1yARedGg"

# Email content per outcome
EMAIL_CONFIGS = {
    "confermato": {
        "subject": "{nome}, la tua consulenza strategica è confermata",
        "body": (
            "Ciao {nome},\n\n"
            "la tua sessione strategica con Davide Caiazzo è confermata "
            "per il {data} alle {ora}.\n\n"
            "Collegati da qui:\n"
            "{meet_link}\n\n"
            "Davide ha riservato questo slot esclusivamente per te. "
            "Per ottenere il massimo dalla sessione, tieni a mente "
            "il tuo obiettivo principale su LinkedIn e i numeri chiave "
            "del tuo business.\n\n"
            "La sessione parte in orario, collegati qualche minuto prima.\n\n"
            "A presto,\n"
            "Il team di Davide Caiazzo"
        ),
    },
    "budget_basso": {
        "subject": "{nome}, inizia a generare contatti su LinkedIn",
        "body": (
            "Ciao {nome},\n\n"
            "come anticipato, ecco la risorsa per iniziare a ottenere "
            "risultati concreti su LinkedIn:\n"
            "https://bit.ly/primo_passo_dc\n\n"
            "Chi ha applicato questo metodo ha iniziato a ricevere "
            "richieste di contatto qualificate già nelle prime settimane.\n\n"
            "Quando vorrai accelerare i risultati, saremo qui.\n\n"
            "A presto,\n"
            "Il team di Davide Caiazzo"
        ),
    },
    "non_puo_investire": {
        "subject": "{nome}, una risorsa per il tuo business su LinkedIn",
        "body": (
            "Ciao {nome},\n\n"
            "grazie per la conversazione.\n\n"
            "Ti lascio un accesso diretto alle risorse che usiamo con "
            "i nostri clienti per generare contatti qualificati su LinkedIn:\n"
            "https://clienti.davidecaiazzo.it\n\n"
            "Buon lavoro,\n"
            "Il team di Davide Caiazzo"
        ),
    },
    "cerca_lavoro": {
        "subject": "{nome}, LinkedIn per trovare lavoro",
        "body": (
            "Ciao {nome},\n\n"
            "come promesso, ecco la guida per usare LinkedIn "
            "nella ricerca di lavoro:\n"
            "https://bit.ly/lavoro_linkedin\n\n"
            "Dentro trovi un metodo pratico per farti notare "
            "dai recruiter e dalle aziende giuste.\n\n"
            "Buon lavoro,\n"
            "Il team di Davide Caiazzo"
        ),
    },
}


def _get_credentials():
    """Load service account credentials from env var."""
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set")
        return None
    try:
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        return creds
    except Exception:
        logger.exception("Failed to load service account credentials")
        return None


def _get_gmail_service():
    creds = _get_credentials()
    if not creds:
        return None
    delegated = creds.with_subject(IMPERSONATE_EMAIL)
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


def _get_calendar_service():
    creds = _get_credentials()
    if not creds:
        return None
    delegated = creds.with_subject(IMPERSONATE_EMAIL)
    return build("calendar", "v3", credentials=delegated, cache_discovery=False)


def _get_drive_service():
    creds = _get_credentials()
    if not creds:
        return None
    delegated = creds.with_subject(IMPERSONATE_EMAIL)
    return build("drive", "v3", credentials=delegated, cache_discovery=False)


def _get_docs_service():
    creds = _get_credentials()
    if not creds:
        return None
    delegated = creds.with_subject(IMPERSONATE_EMAIL)
    return build("docs", "v1", credentials=delegated, cache_discovery=False)


def send_email(to_email, email_type, lead_data, meet_link=""):
    """Send email to lead based on outcome type.
    email_type: 'confermato', 'budget_basso', 'non_puo_investire', 'cerca_lavoro'
    """
    config = EMAIL_CONFIGS.get(email_type)
    if not config:
        logger.warning("Unknown email type: %s", email_type)
        return False

    nome = lead_data.get("nome", "")
    data_consulenza = lead_data.get("data_consulenza", "")
    ora_consulenza = lead_data.get("ora_consulenza", "")

    subject = config["subject"].format(nome=nome)
    body = config["body"].format(
        nome=nome,
        data=data_consulenza,
        ora=ora_consulenza,
        meet_link=meet_link,
    )

    try:
        service = _get_gmail_service()
        if not service:
            return False

        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = to_email
        msg["from"] = "DC Academy <{}>".format(SENDER_EMAIL)
        msg["subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(
            userId=IMPERSONATE_EMAIL,
            body={"raw": raw},
        ).execute()
        logger.info("Email sent to %s (type: %s)", to_email, email_type)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to_email)
        return False


def create_calendar_event(lead_data, doc_url=""):
    """Create calendar event 15 min before consultation time.
    Title: 'Conferma consulenza strategica [Nome Cognome]'
    """
    data_str = lead_data.get("data_consulenza", "")
    ora_str = lead_data.get("ora_consulenza", "")
    nome = lead_data.get("nome", "")
    cognome = lead_data.get("cognome", "")

    if not data_str or not ora_str:
        logger.warning("No consultation date/time, skipping calendar event")
        return None

    try:
        # Parse date — try common formats
        dt = None
        for fmt in ["%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%d %B %Y"]:
            try:
                if fmt in ("%Y-%m-%d", "%d %B %Y"):
                    dt = datetime.strptime(data_str, fmt)
                    # Add time
                    h, m = ora_str.split(":")[:2]
                    dt = dt.replace(hour=int(h), minute=int(m))
                else:
                    dt = datetime.strptime("{} {}".format(data_str, ora_str), fmt)
                break
            except ValueError:
                continue

        if not dt:
            logger.warning("Could not parse date: %s %s", data_str, ora_str)
            return None

        # Event starts 15 min BEFORE the actual meeting
        event_start = dt - timedelta(minutes=15)
        event_end = dt + timedelta(minutes=45)

        description = "Consulenza strategica LinkedIn con {} {}".format(nome, cognome)
        if doc_url:
            description += "\n\nDoc preparazione: {}".format(doc_url)

        event_body = {
            "summary": "Conferma consulenza strategica {} {}".format(nome, cognome),
            "description": description,
            "start": {
                "dateTime": event_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "Europe/Rome",
            },
            "end": {
                "dateTime": event_end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "Europe/Rome",
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 15},
                ],
            },
        }

        service = _get_calendar_service()
        if not service:
            return None

        event = service.events().insert(
            calendarId=CALENDAR_ID, body=event_body
        ).execute()
        logger.info("Calendar event created: %s (id: %s)", event.get("summary"), event.get("id"))
        return event.get("htmlLink")
    except Exception:
        logger.exception("Failed to create calendar event")
        return None


def duplicate_doc_template(lead_data):
    """Duplicate the consultation template doc and fill with lead data.
    Returns the URL of the new doc.
    """
    nome = lead_data.get("nome", "")
    cognome = lead_data.get("cognome", "")

    try:
        drive = _get_drive_service()
        docs = _get_docs_service()
        if not drive or not docs:
            return None

        # 1. Copy the template
        new_title = "Consulenza {} {} - {}".format(
            nome, cognome, datetime.now().strftime("%d/%m/%Y")
        )
        copied = drive.files().copy(
            fileId=DOC_TEMPLATE_ID,
            body={"name": new_title},
        ).execute()
        new_doc_id = copied["id"]
        logger.info("Doc template duplicated: %s (id: %s)", new_title, new_doc_id)

        # 2. Replace placeholders in the doc
        replacements = {
            "{{NOME}}": nome,
            "{{COGNOME}}": cognome,
            "{{RUOLO}}": lead_data.get("ruolo", ""),
            "{{OBIETTIVI}}": lead_data.get("obiettivi_linkedin", ""),
            "{{ACQUISIZIONE}}": lead_data.get("acquisizione_clienti", ""),
            "{{FATTURATO}}": lead_data.get("fatturato", ""),
            "{{BUDGET}}": lead_data.get("budget", ""),
            "{{SITO_WEB}}": lead_data.get("sito_web", ""),
            "{{DATA_CONSULENZA}}": lead_data.get("data_consulenza", ""),
            "{{ORA_CONSULENZA}}": lead_data.get("ora_consulenza", ""),
            "{{TELEFONO}}": lead_data.get("cellulare", ""),
            "{{EMAIL}}": lead_data.get("email", ""),
        }

        requests_list = []
        for placeholder, value in replacements.items():
            if value:
                requests_list.append({
                    "replaceAllText": {
                        "containsText": {
                            "text": placeholder,
                            "matchCase": True,
                        },
                        "replaceText": value,
                    }
                })

        if requests_list:
            docs.documents().batchUpdate(
                documentId=new_doc_id,
                body={"requests": requests_list},
            ).execute()
            logger.info("Doc placeholders replaced for %s %s", nome, cognome)

        doc_url = "https://docs.google.com/document/d/{}/edit".format(new_doc_id)
        return doc_url
    except Exception:
        logger.exception("Failed to duplicate doc template")
        return None


def handle_post_call_automation(status, lead_data, transcript_text=""):
    """Main entry point: called after a call ends.
    Handles email, calendar event, and doc duplication based on outcome.

    Only sends email if lead explicitly asked for it during the call.
    """
    nome = lead_data.get("nome", "")
    cognome = lead_data.get("cognome", "")
    email = lead_data.get("email", "")
    logger.info("Post-call automation for %s %s (status: %s, email: %s)",
                nome, cognome, status, email)

    if not email:
        logger.info("No email for lead, skipping email automation")

    transcript_lower = transcript_text.lower() if transcript_text else ""

    if status == "qualificato":
        # 1. Duplicate doc template
        doc_url = duplicate_doc_template(lead_data)

        # 2. Create calendar event (with doc link)
        create_calendar_event(lead_data, doc_url=doc_url or "")

        # 3. Send confirmation email with Meet link (only if lead asked)
        if email and _lead_asked_for_email(transcript_lower):
            meet_link = "https://meet.google.com/landing"  # placeholder
            send_email(email, "confermato", lead_data, meet_link=meet_link)

    elif status == "non in target" and email:
        # Determine sub-type from transcript
        if _lead_asked_for_email(transcript_lower):
            if "budget" in transcript_lower and any(w in transcript_lower for w in ["basso", "meno", "poco"]):
                send_email(email, "budget_basso", lead_data)
            elif any(w in transcript_lower for w in ["lavoro", "assunzione", "posizione"]):
                send_email(email, "cerca_lavoro", lead_data)
            else:
                send_email(email, "non_puo_investire", lead_data)


def _lead_asked_for_email(transcript_lower):
    """Check if lead explicitly asked for email/resources during the call."""
    email_request_phrases = [
        "mand", "invi", "mail", "email", "e-mail",
        "risorse", "link", "materiale",
    ]
    return any(phrase in transcript_lower for phrase in email_request_phrases)
