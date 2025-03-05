# Pipecat AI Teaching Assistant with Twilio Integration

A voice-enabled AI teaching assistant that students can call using their phone. Built with Pipecat, Daily.co, and Twilio.

## Purpose

This repository serves as an example to help users understand how Pipecat integrates with Twilio for both outbound and inbound dialing. It demonstrates how to set up and manage voice interactions using Pipecat, Daily.co, and Twilio.

## File Descriptions

- **`dial_out.py`**: Handles outbound calls initiated by the server. It sets up the Pipecat bot to dial out to a specified phone number using Twilio's SIP integration.
- **`dial_in.py`**: Manages inbound calls received via Twilio. It connects incoming calls to a Pipecat bot through Daily.co's SIP endpoint.
- **`server.py`**: A FastAPI server that manages Twilio webhooks, Daily.co room creation, and orchestrates the dial-in and dial-out processes.

## Prerequisites

### Environment Variables

Create a `.env` file in the root directory with the following variables:

```bash
# Daily Configuration
DAILY_API_KEY=your_daily_key
DAILY_DIALOUT_ROOM=your_room_name

# Twilio Configuration
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_SIP_DOMAIN=daily-twilio-integration  # Must match SIP Domain name

# AI Services
OPENAI_API_KEY=your_openai_api_key
ELEVENLABS_API_KEY=your_elevenlabs_api_key
ELEVENLABS_VOICE_ID=your_voice_id
```

## Installation

```bash
# Clone the repository
git clone [repository-url]

# Install dependencies
pip install -r requirements.txt

# Start the server
python server.py
```

## Integration Setup

### 1. Daily Room Configuration

Create rooms with dial-out enabled:
```python
# In server.py when creating rooms
properties=DailyRoomProperties(
    enable_dialout=True,
    sip_mode="dial-in",
    video=False,
    # ... other properties
)
```

### 2. Twilio Configuration Steps

#### A. Phone Number Setup
1. Purchase a Twilio phone number from the Twilio Console
2. Note the phone number as it will be used as the `callerId` in your webhook

#### B. SIP Domain Setup
1. Navigate to Twilio Console > Voice > SIP Domains
2. Create a new SIP domain (e.g., `daily-twilio-integration.sip.twilio.com`)
3. Configure Voice Settings:
   - Request URL: `https://your-server.com/twilio_webhook`
   - HTTP Method: POST
   - Content-Type: `application/x-www-form-urlencoded`

### 3. Webhook Implementation

#### A. Handler Code
Example AWS Lambda handler (Node.js):
```javascript
exports.handler = async (event) => {
  const params = event.queryStringParameters;
  const toNumber = params.To.match(/sip:(.*)@/)[1];
  
  return {
    statusCode: 200,
    headers: {
      "Content-Type": "application/xml",
      "Access-Control-Allow-Origin": "*"
    },
    body: `
      <Response>
        <Dial answerOnBridge="true" callerId="+[YOUR_TWILIO_NUMBER]">
          <Number>${toNumber}</Number>
        </Dial>
      </Response>
    `
  };
};
```

#### B. Requirements
- Must handle SIP URI parsing from Twilio request
- Must return valid TwiML response
- Caller ID must be a verified Twilio number
- Enable CORS headers for cross-domain requests

### 4. Integration Points

1. SIP URI Formatting:
   ```python
   # In dial_out.py
   sip_uri = f"sip:+{phone_number}@{os.getenv('TWILIO_SIP_DOMAIN')}.sip.twilio.com"
   ```

2. Webhook Security:
   - Validate Twilio requests using request signature
   - Implement error handling for SIP parsing
   - Set appropriate CORS headers

3. Call Handling:
   - Implement answerOnBridge behavior
   - Handle different call status updates (ringing, answered, completed)
   - Manage call termination and cleanup

## API Usage

### Daily Dial-out API

Use Daily's REST API to start dial-out:
```bash
curl -H "Content-Type: application/json" \
     -H "Authorization: Bearer $DAILY_API_KEY" \
     -XPOST -d '{
        "sipUri": "sip:+[PHONE_NUMBER]@$TWILIO_SIP_DOMAIN.sip.twilio.com",
        "displayName": "+[PHONE_NUMBER]", 
        "video": false
     }' \
     https://api.daily.co/v1/rooms/[ROOM_NAME]/dialOut/start
```

### Server Endpoints

#### Trigger Outbound Call
```bash
curl -X POST http://your-server:7860/dial_out \
-H "Content-Type: application/json" \
-d '{"phoneNumber": "+1234567890", "detectVoicemail": false}'
```

#### Trigger Inbound Call
Inbound calls are automatically managed by Twilio's webhook configuration.

## Running the Server

To run the server locally:
```bash
python server.py
```

Ensure your server is accessible to Twilio by using tools like ngrok if necessary.

## License

BSD 2-Clause License

