# Pipecat AI Teaching Assistant with Twilio Integration

A voice-enabled AI teaching assistant that students can call using their phone. Built with Pipecat, Daily.co, and Twilio.

## Overview

This project implements an educational AI assistant that students can access through a simple phone call. It combines:
- Twilio for phone call handling
- Daily.co for SIP (Session Initiation Protocol) communication
- OpenAI GPT-4 for natural language understanding
- ElevenLabs for voice synthesis

## Features

- **Phone-Based Access**: Students can call a Twilio number to connect with the AI teacher
- **Real-Time Voice Interaction**: Natural conversation with the AI through voice
- **Robust Call Handling**:
  - Automatic call connection
  - Graceful error handling
  - Call status monitoring
  - Session management

## Technical Architecture

### SIP Integration Flow
1. User calls Twilio number
2. Twilio webhook triggers Daily.co room creation
3. Daily.co provides SIP endpoint
4. Pipecat bot connects to Daily.co room
5. Two-way audio established through SIP

### Components
- `server.py`: FastAPI server handling Twilio webhooks and room management
- `dial_in.py`: Core bot implementation with educational AI logic
- `requirements.txt`: Project dependencies

## Setup

### Prerequisites
```bash
# Required environment variables
DAILY_API_KEY=your_daily_api_key
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
OPENAI_API_KEY=your_openai_api_key
ELEVENLABS_API_KEY=your_elevenlabs_api_key
ELEVENLABS_VOICE_ID=your_voice_id
```

### Installation
```bash
# Clone the repository
git clone [repository-url]

# Install dependencies
pip install -r requirements.txt

# Start the server
python server.py
```

### Twilio Configuration
1. Get a Twilio phone number
2. Set webhook URL in Twilio:
   - Voice webhook: `http://your-server:7860/twilio_webhook`
   - Method: HTTP POST
   - Content-Type: `application/x-www-form-urlencoded`

## API Endpoints

### Twilio Integration
- `/twilio_webhook`: Handles incoming calls
- `/dial_status`: Monitors call status
- `/sip_status`: Tracks SIP connection status

### Debug Endpoints
- `/twilio_test`: Test Twilio connectivity
- `/debug_server_info`: Server configuration info

## Call Flow

1. **Call Initiation**
   ```
   User Phone → Twilio → Server Webhook
   ```

2. **Room Setup**
   ```
   Server → Daily.co API → Create Room with SIP
   ```

3. **Bot Initialization**
   ```
   Server → Start Bot → Connect to Daily Room
   ```

4. **Call Connection**
   ```
   Twilio ←→ Daily.co SIP ←→ Bot
   ```


## License

BSD 2-Clause License

