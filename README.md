# Twilio AI Setter Voice Agent

Voice agent that handles inbound/outbound Twilio calls to pre-qualify leads using Claude Haiku as the conversation brain, Deepgram for real-time Italian speech-to-text, and ElevenLabs for text-to-speech.

## Architecture

```
Twilio Call  <-->  Twilio Media Stream (WebSocket)
                        |
                   server.py
                   /    |    \
          Deepgram   Claude    ElevenLabs
          (STT)      (Brain)   (TTS)
```

**Audio flow:**
1. Twilio sends mu-law 8kHz audio via WebSocket
2. Raw mu-law is forwarded to Deepgram for real-time Italian transcription
3. Final transcripts are sent to Claude Haiku with the setter system prompt
4. Claude's response is sent to ElevenLabs TTS (output: mu-law 8kHz)
5. TTS audio is streamed back to Twilio via the same WebSocket

## Setup

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env` and fill in your API keys:

- **TWILIO_ACCOUNT_SID** / **TWILIO_AUTH_TOKEN** - from twilio.com/console
- **TWILIO_PHONE_NUMBER** - your Twilio phone number (E.164 format)
- **ANTHROPIC_API_KEY** - from console.anthropic.com
- **ELEVENLABS_API_KEY** / **ELEVENLABS_VOICE_ID** - from elevenlabs.io
- **DEEPGRAM_API_KEY** - from console.deepgram.com

### 3. Expose your server publicly

Twilio needs to reach your server. Use ngrok:

```bash
ngrok http 8080
```

Take note of the `https://xxxx.ngrok.io` URL.

### 4. Configure Twilio

1. Go to your Twilio phone number configuration
2. Set the **Voice webhook** (incoming calls) to: `https://xxxx.ngrok.io/incoming-call` (POST)
3. Update the WebSocket URL in the TwiML: the server auto-generates it from the Host header, so ngrok should work out of the box

### 5. Run the server

```bash
python server.py
```

The server starts:
- **HTTP** on port 8080 (Flask - Twilio webhooks)
- **WebSocket** on port 8081 (Media Stream handler)

### 6. Make an outbound call

```bash
curl -X POST http://localhost:8080/make-call
```

Or to a custom number:

```bash
curl -X POST http://localhost:8080/make-call \
  -H "Content-Type: application/json" \
  -d '{"to": "+391234567890"}'
```

## Ports

| Service    | Port | Purpose                     |
|------------|------|-----------------------------|
| Flask HTTP | 8080 | Twilio webhooks, API        |
| WebSocket  | 8081 | Twilio Media Stream handler |

## Customization

- Edit `setter_prompt.py` to change the conversation script
- Modify `OPENING_MESSAGE` in `server.py` for a different greeting
- Pass `lead_name` and `appointment_date` to `get_setter_prompt()` for per-call personalization
