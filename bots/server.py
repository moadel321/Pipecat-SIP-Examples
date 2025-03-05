#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import argparse
import os
import subprocess
import sys
from contextlib import asynccontextmanager
import time

import aiohttp
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from dotenv import load_dotenv
from loguru import logger
import http.client as http_client
import logging

from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams, DailyRoomProperties, DailyRoomSipParams

MAX_BOTS_PER_ROOM = 1

# Bot sub-process dict for status reporting and concurrency control
bot_procs = {}

# Dictionary to store bot type by room URL
room_bot_types = {}

# Constants
MAX_SESSION_TIME = 30 * 60  # 30 minutes in seconds

daily_helpers = {}

load_dotenv(override=True)

# Remove existing logger handlers and set up new configuration
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="DEBUG"  # Default level, will be updated from command line if specified
)

# Set up HTTP client logging (can be made more verbose)
http_client.HTTPConnection.debuglevel = 1
logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)
requests_log = logging.getLogger("urllib3")
requests_log.setLevel(logging.DEBUG)
requests_log.propagate = True


def cleanup():
    # Clean up function, just to be extra safe
    for entry in bot_procs.values():
        proc = entry[0]
        proc.terminate()
        proc.wait()


async def update_pinless_call(call_id, call_domain, sip_uri):
    """
    Forward an incoming call to a SIP URI using Daily's pinlessCallUpdate endpoint.
    
    Args:
        call_id (str): The ID of the incoming call
        call_domain (str): The domain of the incoming call
        sip_uri (str): The SIP URI to forward the call to
    
    Returns:
        dict: The response from the Daily API
    """
    try:
        daily_api_key = os.getenv("DAILY_API_KEY", "").strip()
        daily_api_url = os.getenv("DAILY_API_URL", "https://api.daily.co/v1").strip()
        
        url = f"{daily_api_url}/dialin/pinlessCallUpdate"
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {daily_api_key}"
        }
        
        data = {
            "callId": call_id,
            "callDomain": call_domain,
            "sipUri": sip_uri
        }
        
        logger.info(f"Updating pinless call - URL: {url}")
        logger.info(f"Payload: {data}")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers) as response:
                if response.status >= 400:
                    error_text = await response.text()
                    logger.error(f"Error updating pinless call: {error_text}, Status: {response.status}")
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to update pinless call: {error_text}"
                    )
                
                result = await response.json()
                logger.info(f"Successfully updated pinless call: {result}")
                return result
    except Exception as e:
        logger.error(f"Error in update_pinless_call: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update pinless call: {str(e)}"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting application...")
    aiohttp_session = aiohttp.ClientSession()
    
    daily_api_key = os.getenv("DAILY_API_KEY", "").strip()
    if not daily_api_key:
        logger.error("No Daily API key found")
        raise Exception("DAILY_API_KEY environment variable is required")

    logger.debug(f"Initializing Daily REST helper with API key: {daily_api_key[:6]}...{daily_api_key[-4:]}")
    
    daily_helpers["rest"] = DailyRESTHelper(
        daily_api_key=daily_api_key,
        daily_api_url="https://api.daily.co/v1",
        aiohttp_session=aiohttp_session,
    )
    logger.info("Application startup complete")
    yield
    logger.info("Shutting down application...")
    await aiohttp_session.close()
    cleanup()
    logger.info("Application shutdown complete")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"message": "Bot server is running"}


@app.post("/start_bot")
async def start_bot(request: Request):
    try:
        data = await request.json()
        logger.info(f"Received SIP webhook: {data}")
        
        # Extract callId and callDomain
        call_id = data.get("callId")
        call_domain = data.get("callDomain")
        
        if not call_id or not call_domain:
            logger.error("Missing callId or callDomain in request")
            raise HTTPException(status_code=400, detail="Missing callId or callDomain")
            
        # Create SIP-enabled room
        properties = DailyRoomProperties(
            sip=DailyRoomSipParams(
                display_name="sip-dialin",
                video=False,
                sip_mode="dial-in",
                num_endpoints=1
            )
        )
        
        params = DailyRoomParams(properties=properties)
        room = await daily_helpers["rest"].create_room(params=params)
        logger.info(f"Created room: {room.url} with SIP endpoint: {room.config.sip_endpoint}")
        
        # Get a token for the bot
        token = await daily_helpers["rest"].get_token(room.url)
        
        # Start the dial-in bot
        logger.info(f"Starting dial-in bot with parameters: room_url={room.url}, token={token[:10]}..., call_id={call_id}, call_domain={call_domain}")
        proc = subprocess.Popen(
            [sys.executable, "dial_in.py", "-u", room.url, "-t", token, "-i", call_id, "-d", call_domain],
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        
        logger.info(f"Started dial-in bot with PID: {proc.pid}")
        
        # Track the process
        bot_procs[proc.pid] = (proc, room.url)
        
        # Forward the call using our custom update_pinless_call function
        await update_pinless_call(call_id, call_domain, room.config.sip_endpoint)
        
        return {"status": "success", "message": "SIP call forwarded successfully", "room_url": room.url, "sip_endpoint": room.config.sip_endpoint}
    except Exception as e:
        logger.error(f"Error in start_bot: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/start_agent")
async def start_agent(request: Request):
    print(f"!!! Creating room")
    room = await daily_helpers["rest"].create_room(DailyRoomParams())
    print(f"!!! Room URL: {room.url}")
    # Ensure the room property is present
    if not room.url:
        raise HTTPException(
            status_code=500,
            detail="Missing 'room' property in request data. Cannot start agent without a target room!",
        )

    # Check if there is already an existing process running in this room
    num_bots_in_room = sum(
        1 for proc in bot_procs.values() if proc[1] == room.url and proc[0].poll() is None
    )
    if num_bots_in_room >= MAX_BOTS_PER_ROOM:
        raise HTTPException(status_code=500, detail=f"Max bot limited reach for room: {room.url}")

    # Get the token for the room
    token = await daily_helpers["rest"].get_token(room.url)

    if not token:
        raise HTTPException(status_code=500, detail=f"Failed to get token for room: {room.url}")

    # Spawn a new agent, and join the user session
    # Note: this is mostly for demonstration purposes (refer to 'deployment' in README)
    try:
        proc = subprocess.Popen(
            [f"python3 -m bot -u {room.url} -t {token}"],
            shell=True,
            bufsize=1,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        bot_procs[proc.pid] = (proc, room.url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start subprocess: {e}")

    return RedirectResponse(room.url)


@app.get("/status/{pid}")
def get_status(pid: int):
    # Look up the subprocess
    proc = bot_procs.get(pid)

    # If the subprocess doesn't exist, return an error
    if not proc:
        raise HTTPException(status_code=404, detail=f"Bot with process id: {pid} not found")

    # Check the status of the subprocess
    if proc[0].poll() is None:
        status = "running"
    else:
        status = "finished"

    return JSONResponse({"bot_id": pid, "status": status})


@app.post("/connect")
async def rtvi_connect(request: Request):
    """RTVI connect endpoint that creates a room and returns connection credentials."""
    try:
        # Get raw request body first
        raw_body = await request.body()
        logger.info(f"Raw request body: {raw_body.decode()}")
        
        # Get request data
        data = await request.json()
        
        # Enhanced request logging
        logger.info("=== Incoming Request Details ===")
        logger.info(f"Headers: {dict(request.headers)}")
        logger.info(f"Method: {request.method}")
        logger.info(f"Raw JSON data: {data}")
        logger.info(f"botType in request: {'botType' in data}")
        
        # Extract botType from request or get from stored room type
        if 'botType' in data:
            bot_type = data["botType"].lower()
            logger.info(f"Using botType from request: {bot_type}")
        else:
            # Try to get bot type from room URL if this is a reconnection
            daily_room_url = data.get("room_url")
            bot_type = room_bot_types.get(daily_room_url, "intake")
            logger.info(f"Using stored botType: {bot_type}")
        
        logger.info("==============================")
        
        logger.info(f"=== Starting new connection request ===")
        logger.info(f"Final bot type being used: {bot_type}")
        
        # Get the Daily API key from environment
        daily_api_key = os.getenv("DAILY_API_KEY")
        if not daily_api_key:
            logger.error("DAILY_API_KEY environment variable is not set")
            raise HTTPException(
                status_code=500, 
                detail="DAILY_API_KEY environment variable is not set"
            )

        # Create a new room with expiry time
        try:
            # Define room expiry time (e.g., 30 minutes)
            ROOM_EXPIRY_TIME = 10 * 60  # 10 minutes in seconds
            
            # Create room properties with expiry time
            room_properties = DailyRoomProperties(
                exp=time.time() + ROOM_EXPIRY_TIME,  # Room expires in 10 minutes
                start_audio_off=False,
                start_video_off=True,
                eject_at_room_exp=True,  # Eject participants when room expires
                enable_prejoin_ui=False  #  Skip the prejoin UI
            )
            
            # Create room parameters with properties
            room_params = DailyRoomParams(
                privacy="public",
                properties=room_properties
            )
            
            # Create the room
            room = await daily_helpers["rest"].create_room(room_params)
            daily_room_url = room.url
            logger.info(f"Created room: {daily_room_url} with expiry in {ROOM_EXPIRY_TIME/60} minutes")
            
            # Store the bot type for this room
            room_bot_types[daily_room_url] = bot_type
            
        except Exception as e:
            logger.error(f"Failed to create room: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create room: {str(e)}"
            )

        # Get a token for the room
        logger.info("Attempting to get room token...")
        try:
            token = await daily_helpers["rest"].get_token(daily_room_url)
            logger.debug(f"Token received: {token[:10]}...")
        except Exception as e:
            logger.error(f"Failed to get room token: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to generate room token: {str(e)}"
            )

        if not token:
            logger.error("Token generation succeeded but no token was returned")
            raise HTTPException(
                status_code=500,
                detail="Failed to generate room token - no token returned"
            )

        # Set these as environment variables for the bot process
        env = os.environ.copy()
        env["DAILY_ROOM_URL"] = daily_room_url
        env["DAILY_API_KEY"] = daily_api_key
        env["DAILY_ROOM_TOKEN"] = token

        logger.info(f"=== Starting bot process ===")
        logger.info(f"Bot type: {bot_type}")
        logger.info(f"Room URL: {daily_room_url}")
        
        # Create a new bot process with the updated environment
        try:
            # Select the appropriate bot script based on type
            if bot_type == "movie":
                bot_script = "movie_bot.py"
            elif bot_type == "shawarma":
                bot_script = "shawarma_bot.py"
            elif bot_type == "simple":
                bot_script = "simple.py"
            else:
                bot_script = "bot.py"
                
            logger.info(f"Selected bot script: {bot_script}")
            
            proc = subprocess.Popen(
                [sys.executable, bot_script],
                env=env,
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            logger.info(f"Bot process started successfully with PID: {proc.pid}")
        except Exception as e:
            logger.error(f"Failed to start bot process: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to start bot: {str(e)}"
            )
        
        # Store the process info
        bot_procs[proc.pid] = (proc, daily_room_url)
        
        # Return the authentication bundle in format expected by DailyTransport
        response_data = {
            "room_url": daily_room_url,
            "token": token
        }
        logger.info(f"Returning connection data for room: {daily_room_url}")
        return response_data

    except Exception as e:
        logger.error(f"Unexpected error in rtvi_connect: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/dial_in")
async def handle_dial_in(request: Request) -> JSONResponse:
    """Handle incoming SIP dial-in calls."""
    logger.info("=== Received dial-in request ===")
    
    # Use specified room URL, or create a new one if not specified
    room_url = os.getenv("DAILY_SAMPLE_ROOM_URL", None)
    
    # Get the dial-in properties from the request
    try:
        data = await request.json()
        if "test" in data:
            # Pass through any webhook checks
            return JSONResponse({"test": True})
            
        logger.info(f"Dial-in request data: {data}")
        
        detect_voicemail = data.get("detectVoicemail", False)
        call_id = data.get("callId", None)
        call_domain = data.get("callDomain", None)
        dialout_number = data.get("dialoutNumber", None)
        
        if not call_id or not call_domain:
            logger.error("Missing callId or callDomain in request")
            raise HTTPException(
                status_code=400, 
                detail="Missing callId or callDomain in request"
            )
            
    except Exception as e:
        logger.error(f"Error parsing request: {str(e)}")
        raise HTTPException(
            status_code=400, 
            detail=f"Error parsing request: {str(e)}"
        )
    
    logger.info(f"Processing dial-in call - ID: {call_id}, Domain: {call_domain}")
    
    # Create a room with SIP settings if needed
    try:
        if not room_url:
            # Create base properties with SIP settings
            properties = DailyRoomProperties(
                sip=DailyRoomSipParams(
                    display_name="dial-in-user", 
                    video=False, 
                    sip_mode="dial-in", 
                    num_endpoints=1
                )
            )
            
            # Only enable dialout if dialoutNumber is provided
            if dialout_number:
                properties.enable_dialout = True
                
            params = DailyRoomParams(properties=properties)
            
            logger.info("Creating new room with SIP settings...")
            room = await daily_helpers["rest"].create_room(params=params)
            room_url = room.url
        else:
            # Check if passed room URL exists
            try:
                room = await daily_helpers["rest"].get_room_from_url(room_url)
            except Exception as e:
                logger.error(f"Room not found: {room_url}, Error: {str(e)}")
                raise HTTPException(status_code=404, detail=f"Room not found: {room_url}")
        
        logger.info(f"Daily room: {room.url} with SIP endpoint: {room.config.sip_endpoint}")
        
        # Get a token for the bot to join the session
        token = await daily_helpers["rest"].get_token(room.url, MAX_SESSION_TIME)
        
        if not token:
            logger.error("Failed to get token for room")
            raise HTTPException(
                status_code=500, 
                detail="Failed to get token for room"
            )
            
        # Start the dial-in bot
        logger.info("Starting dial-in bot...")
        
        # Set command with appropriate arguments
        bot_cmd = f"{sys.executable} dial_in_bot.py -u {room.url} -t {token} -i {call_id} -d {call_domain}"
        
        if detect_voicemail:
            bot_cmd += " -v"
            
        if dialout_number:
            bot_cmd += f" -o {dialout_number}"
            
        logger.info(f"Executing command: {bot_cmd}")
        
        try:
            proc = subprocess.Popen(
                bot_cmd,
                shell=True,
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            logger.info(f"Dial-in bot process started with PID: {proc.pid}")
            
            # Track the process
            bot_procs[proc.pid] = (proc, room.url)
            
        except Exception as e:
            logger.error(f"Failed to start dial-in bot: {str(e)}")
            raise HTTPException(
                status_code=500, 
                detail=f"Failed to start dial-in bot: {str(e)}"
            )
            
        # Return the SIP URI for the call
        return JSONResponse({
            "room_url": room.url,
            "sipUri": room.config.sip_endpoint
        })
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Unexpected error in handle_dial_in: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/twilio_webhook")
async def twilio_webhook(
    request: Request,
    CallSid: str = Form(...),
    From: str = Form(None),
    To: str = Form(None),
    CallStatus: str = Form(None),
):
    """
    Handle Twilio webhook for incoming calls.
    
    This endpoint will:
    1. Create a Daily room with SIP capabilities
    2. Get a token for the bot
    3. Start the dial-in bot with Twilio parameters
    4. Return TwiML to put the call in a waiting state
    """
    try:
        logger.info(f"Received Twilio webhook: CallSid={CallSid}, From={From}, To={To}, Status={CallStatus}")
        
        # Create a Daily room with SIP capabilities
        properties = DailyRoomProperties(
            sip=DailyRoomSipParams(
                display_name="twilio-caller",
                video=False,
                sip_mode="dial-in",
                num_endpoints=1
            )
        )
        
        params = DailyRoomParams(properties=properties)
        room = await daily_helpers["rest"].create_room(params=params)
        logger.info(f"Created room: {room.url} with SIP endpoint: {room.config.sip_endpoint}")
        
        # Get a token for the bot
        token = await daily_helpers["rest"].get_token(room.url)
        
        # Start the dial-in bot with Twilio parameters
        # Note: Using the CallSid and SIP URI instead of callId and callDomain
        logger.info(f"Starting Twilio dial-in bot with parameters: room_url={room.url}, token={token[:10]}..., call_sid={CallSid}, sip_uri={room.config.sip_endpoint}")
        proc = subprocess.Popen(
            [sys.executable, "dial_in.py", "-u", room.url, "-t", token, "-i", CallSid, "-s", room.config.sip_endpoint],
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        
        logger.info(f"Started Twilio dial-in bot with PID: {proc.pid}")
        
        # Track the process
        bot_procs[proc.pid] = (proc, room.url)
        
        # Return TwiML to put the call in a waiting state
        # The bot will update this call with the SIP URI when ready
        twiml = f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say>Please wait while we connect you to our AI assistant.</Say>
            <Play loop="10">https://demo.twilio.com/docs/classic.mp3</Play>
        </Response>
        """
        
        return Response(content=twiml, media_type="application/xml")
    except Exception as e:
        logger.error(f"Error in twilio_webhook: {str(e)}")
        # Return a more Twilio-friendly error
        twiml = f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say>We're sorry, but there was an error connecting to our service. Please try again later.</Say>
        </Response>
        """
        return Response(content=twiml, media_type="application/xml")


@app.post("/dial_out")
async def handle_dial_out(request: Request) -> JSONResponse:
    """Handle outbound calls to phone numbers."""
    logger.info("=== Received dial-out request ===")
    
    # Get the dial-out properties from the request
    try:
        data = await request.json()
        if "test" in data:
            # Pass through any webhook checks
            return JSONResponse({"test": True})
            
        logger.info(f"Dial-out request data: {data}")
        
        phone_number = data.get("phoneNumber", None)
        detect_voicemail = data.get("detectVoicemail", False)
        
        if not phone_number:
            logger.error("Missing phoneNumber in request")
            raise HTTPException(
                status_code=400, 
                detail="Missing phoneNumber in request"
            )
            
    except Exception as e:
        logger.error(f"Error parsing request: {str(e)}")
        raise HTTPException(
            status_code=400, 
            detail=f"Error parsing request: {str(e)}"
        )
    
    logger.info(f"Processing dial-out request to phone number: {phone_number}")
    
    # Create a room with SIP settings
    try:
        # Create base properties with SIP and dialout enabled
        # Always create a new room for dial-out to ensure it has dialout capabilities
        properties = DailyRoomProperties(
            sip=DailyRoomSipParams(
                display_name="dial-out-bot", 
                video=False, 
                sip_mode="dial-in",  # Changed from "direct" to "dial-in" as required by Daily.co API
                num_endpoints=1
            ),
            enable_dialout=True
        )
            
        params = DailyRoomParams(properties=properties)
        
        logger.info("Creating new room with SIP and dialout settings...")
        room = await daily_helpers["rest"].create_room(params=params)
        room_url = room.url
        
        logger.info(f"Created Daily room: {room.url}")
        
        # Get a token for the bot to join the session
        token = await daily_helpers["rest"].get_token(room.url, MAX_SESSION_TIME)
        
        if not token:
            logger.error("Failed to get token for room")
            raise HTTPException(
                status_code=500, 
                detail="Failed to get token for room"
            )
            
        # Start the dial-out bot
        logger.info("Starting dial-out bot...")
        
        # Set command with appropriate arguments
        bot_cmd = f"{sys.executable} dial_out.py -u {room.url} -t {token} -p {phone_number}"
        
        if detect_voicemail:
            bot_cmd += " -v"
            
        logger.info(f"Executing command: {bot_cmd}")
        
        try:
            proc = subprocess.Popen(
                bot_cmd,
                shell=True,
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            logger.info(f"Dial-out bot process started with PID: {proc.pid}")
            
            # Track the process
            bot_procs[proc.pid] = (proc, room.url)
            room_bot_types[room.url] = "dial_out"
            
        except Exception as e:
            logger.error(f"Failed to start dial-out bot: {str(e)}")
            raise HTTPException(
                status_code=500, 
                detail=f"Failed to start dial-out bot: {str(e)}"
            )
            
        # Return the room information
        return JSONResponse({
            "status": "success",
            "message": f"Outbound call initiated to {phone_number}",
            "room_url": room.url,
            "pid": proc.pid
        })
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Unexpected error in handle_dial_out: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    default_host = os.getenv("HOST", "0.0.0.0")
    default_port = int(os.getenv("FAST_API_PORT", "7860"))
    
    # Default certificate paths for Let's Encrypt
    default_ssl_cert = os.getenv("SSL_CERT", "/etc/letsencrypt/live/chatbotmailer.com/fullchain.pem")
    default_ssl_key = os.getenv("SSL_KEY", "/etc/letsencrypt/live/chatbotmailer.com/privkey.pem")
    
    # Default log level
    default_log_level = os.getenv("LOG_LEVEL", "DEBUG")

    parser = argparse.ArgumentParser(description="Daily patient-intake FastAPI server")
    parser.add_argument("--host", type=str, default=default_host, help="Host address")
    parser.add_argument("--port", type=int, default=default_port, help="Port number")
    parser.add_argument("--reload", action="store_true", help="Reload code on change")
    parser.add_argument("--ssl-cert", type=str, default=default_ssl_cert, help="Path to SSL certificate file")
    parser.add_argument("--ssl-key", type=str, default=default_ssl_key, help="Path to SSL key file")
    parser.add_argument("--no-ssl", action="store_true", help="Disable SSL/HTTPS")
    parser.add_argument("--log-level", type=str, default=default_log_level, 
                        choices=["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"],
                        help="Set the logging level (TRACE is most verbose)")
    parser.add_argument("--http-debug", type=int, default=1, choices=[0, 1, 2], 
                        help="HTTP client debug level (0=off, 1=basic, 2=verbose)")

    config = parser.parse_args()
    
    # Update logger configuration with specified log level
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=config.log_level
    )
    
    # Set HTTP client debug level
    http_client.HTTPConnection.debuglevel = config.http_debug
    
    # Log configuration details
    logger.info(f"Starting server with log level: {config.log_level}")
    logger.info(f"HTTP debug level: {config.http_debug}")
    
    # Update other loggers based on our log level
    if config.log_level == "TRACE":
        # Set urllib3 and requests to DEBUG for TRACE level
        requests_log.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Set the log configuration for uvicorn
    log_config = None
    if config.log_level in ["TRACE", "DEBUG"]:
        uvicorn_log_level = "debug"  # More detailed uvicorn logs
    elif config.log_level == "INFO":
        uvicorn_log_level = "info"
    elif config.log_level in ["WARNING", "ERROR", "CRITICAL"]:
        uvicorn_log_level = "warning"
    else:
        uvicorn_log_level = "info"  # Default
    
    if config.no_ssl:
        logger.info(f"Starting server WITHOUT SSL on http://{config.host}:{config.port}")
        print(f"To join a test room, visit http://{config.host}:{config.port}/")
        uvicorn.run(
            "server:app",
            host=config.host,
            port=config.port,
            reload=config.reload,
            log_level=uvicorn_log_level,
        )
    else:
        # Check if the certificate files exist
        if not os.path.exists(config.ssl_cert):
            logger.error(f"SSL certificate not found at {config.ssl_cert}")
            print(f"SSL certificate not found. Run with --no-ssl to disable HTTPS or provide valid certificate paths.")
            sys.exit(1)
            
        if not os.path.exists(config.ssl_key):
            logger.error(f"SSL key not found at {config.ssl_key}")
            print(f"SSL key not found. Run with --no-ssl to disable HTTPS or provide valid certificate paths.")
            sys.exit(1)
            
        logger.info(f"Starting server with SSL on https://{config.host}:{config.port}")
        print(f"To join a test room, visit https://{config.host}:{config.port}/")
        uvicorn.run(
            "server:app",
            host=config.host,
            port=config.port,
            reload=config.reload,
            ssl_keyfile=config.ssl_key,
            ssl_certfile=config.ssl_cert,
            log_level=uvicorn_log_level,
        )
