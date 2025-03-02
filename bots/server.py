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
import socket

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


@app.get("/twilio_test")
async def twilio_test():
    """
    Simple endpoint to test if Twilio can reach the server.
    This helps verify connectivity before setting up the actual webhook.
    """
    logger.info("Twilio test endpoint accessed successfully")
    return {"status": "success", "message": "Twilio test endpoint is accessible"}


@app.get("/debug_server_info")
async def debug_server_info():
    """
    Enhanced endpoint to debug server configuration and connectivity issues.
    Returns detailed information about the server's network setup.
    """
    # Get hostname and local IP
    hostname = socket.gethostname()
    
    # Get all IP addresses
    ips = []
    try:
        # Try to get all IP addresses including public ones
        hostname_info = socket.gethostbyname_ex(hostname)
        ips = hostname_info[2]
    except Exception as e:
        ips = ["Error getting IPs: " + str(e)]
    
    # Try to get public IP
    public_ip = None
    try:
        # Simple way to get public IP - connect to external service
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        public_ip = s.getsockname()[0]
        s.close()
    except Exception as e:
        public_ip = f"Error getting public IP: {str(e)}"
    
    # Get all network interfaces
    interfaces = {}
    try:
        import netifaces
        for iface in netifaces.interfaces():
            try:
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    interfaces[iface] = addrs[netifaces.AF_INET]
            except Exception as e:
                interfaces[f"{iface}_error"] = str(e)
    except ImportError:
        interfaces = {"error": "netifaces package not installed"}
    
    # Get port status
    port_status = {}
    for port in [7860, 80, 443]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('0.0.0.0', port))
            s.close()
            port_status[str(port)] = "Available (not in use)"
        except Exception as e:
            port_status[str(port)] = f"In use or unavailable: {str(e)}"
    
    # Get server config
    server_config = {}
    if 'config' in globals():
        server_config = {
            "host": config.host,
            "port": config.port,
            "ssl_enabled": not config.no_ssl,
            "reload": config.reload,
            "log_level": config.log_level
        }
    
    return {
        "hostname": hostname,
        "ip_addresses": ips,
        "public_ip": public_ip,
        "interfaces": interfaces,
        "port_status": port_status,
        "env_vars": {
            "HOST": os.getenv("HOST", "0.0.0.0"),
            "PORT": os.getenv("FAST_API_PORT", "7860"),
        },
        "server_config": server_config,
        "api_url": f"http://{public_ip}:7860" if public_ip and not isinstance(public_ip, str) else "Unknown"
    }


@app.post("/twilio_webhook")
async def twilio_webhook(request: Request):
    """
    Handle Twilio webhook for incoming calls with more robust error handling.
    Uses TwiML to connect the caller to the Daily.co SIP endpoint.
    """
    try:
        # Log raw request first
        body = await request.body()
        form_data = await request.form()
        headers = dict(request.headers)
        
        logger.info(f"Twilio webhook raw request body: {body}")
        logger.info(f"Twilio webhook form data: {form_data}")
        logger.info(f"Twilio webhook headers: {headers}")
        
        # Try to extract fields, but don't require them to be present
        call_sid = form_data.get("CallSid", "unknown")
        from_number = form_data.get("From", "unknown")
        to_number = form_data.get("To", "unknown")
        call_status = form_data.get("CallStatus", "unknown")
        
        logger.info(f"Received Twilio webhook: CallSid={call_sid}, From={from_number}, To={to_number}, Status={call_status}")
        
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
        
        # Only start the bot if we have a valid SIP endpoint
        if room.config.sip_endpoint:
            # Get a token for the bot
            token = await daily_helpers["rest"].get_token(room.url)
            
            # Store SIP URI in environment for the bot
            env = os.environ.copy()
            env["DAILY_SIP_URI"] = room.config.sip_endpoint
            env["TWILIO_CALL_SID"] = call_sid
            
            # Start the dial-in bot with Twilio parameters - don't try to redirect call here
            logger.info(f"Starting Twilio dial-in bot with parameters: room_url={room.url}, token={token[:10]}..., call_sid={call_sid}")
            proc = subprocess.Popen(
                [sys.executable, "dial_in.py", "-u", room.url, "-t", token, "-i", call_sid, "-s", room.config.sip_endpoint],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                env=env
            )
            
            logger.info(f"Started Twilio dial-in bot with PID: {proc.pid}")
            
            # Track the process
            bot_procs[proc.pid] = (proc, room.url)
        
        # Return enhanced TwiML to better handle the connection to the SIP endpoint
        # We add more parameters to improve success rate and prevent early disconnection
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Please wait while we connect you to our AI assistant.</Say>
    <Play loop="3">https://demo.twilio.com/docs/classic.mp3</Play>
    <Dial action="/dial_status?call_sid={call_sid}" timeout="60" ringTone="us">
        <Sip username="daily-user" password="no-password" 
             statusCallbackEvent="initiated ringing answered completed" 
             statusCallback="http://49.13.226.238:7860/sip_status?call_sid={call_sid}">{room.config.sip_endpoint}</Sip>
    </Dial>
    <Say>We're having trouble connecting your call. Please try again later.</Say>
</Response>"""
        
        logger.info(f"Returning enhanced TwiML response to Twilio to connect to SIP endpoint: {room.config.sip_endpoint}")
        return Response(content=twiml, media_type="application/xml")
    except Exception as e:
        logger.error(f"Error in twilio_webhook: {str(e)}", exc_info=True)
        # Return a more Twilio-friendly error response
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>We're sorry, but there was an error connecting to our service. Please try again later.</Say>
    <Play>https://demo.twilio.com/docs/classic.mp3</Play>
</Response>"""
        return Response(content=twiml, media_type="application/xml")


@app.post("/dial_status")
async def dial_status(request: Request):
    """Handle the status callback from Twilio's Dial verb"""
    try:
        form_data = await request.form()
        
        dial_call_status = form_data.get("DialCallStatus", "unknown")
        call_sid = form_data.get("call_sid", "unknown")
        
        logger.info(f"Received Dial status callback: CallSid={call_sid}, DialCallStatus={dial_call_status}")
        
        # Return TwiML based on the dial status
        if dial_call_status == "completed":
            # Call was successful and completed normally
            return Response(
                content="<?xml version='1.0' encoding='UTF-8'?><Response><Say>Thank you for your call. Goodbye.</Say></Response>",
                media_type="application/xml"
            )
        elif dial_call_status in ["busy", "no-answer", "failed", "canceled"]:
            # Call failed to connect
            logger.error(f"SIP call failed with status: {dial_call_status}")
            return Response(
                content="<?xml version='1.0' encoding='UTF-8'?><Response><Say>We're sorry, but there was an error connecting to our AI assistant. Please try again later.</Say></Response>",
                media_type="application/xml"
            )
        else:
            # Any other status
            return Response(
                content="<?xml version='1.0' encoding='UTF-8'?><Response><Say>Your call has ended. Thank you.</Say></Response>",
                media_type="application/xml"
            )
    except Exception as e:
        logger.error(f"Error in dial_status: {str(e)}")
        return Response(
            content="<?xml version='1.0' encoding='UTF-8'?><Response><Say>An error occurred. Goodbye.</Say></Response>",
            media_type="application/xml"
        )


@app.post("/sip_status")
async def sip_status(request: Request):
    """Handle status callbacks from the SIP connection"""
    try:
        form_data = await request.form()
        logger.info(f"SIP status callback: {dict(form_data)}")
        return {"status": "received"}
    except Exception as e:
        logger.error(f"Error in sip_status: {str(e)}")
        return {"status": "error", "message": str(e)}


# Add a new helper endpoint for verifying TwiML
@app.get("/test_twiml")
async def test_twiml():
    """Test endpoint that returns a simple TwiML response to verify formatting"""
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>This is a test TwiML response.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


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
    parser.add_argument("--port-80", action="store_true", help="Run on port 80 (requires root/admin)")
    parser.add_argument("--reload", action="store_true", help="Reload code on change")
    parser.add_argument("--ssl-cert", type=str, default=default_ssl_cert, help="Path to SSL certificate file")
    parser.add_argument("--ssl-key", type=str, default=default_ssl_key, help="Path to SSL key file")
    parser.add_argument("--no-ssl", action="store_true", default=True, help="Disable SSL/HTTPS (default: True)")
    parser.add_argument("--use-ssl", action="store_true", help="Enable SSL/HTTPS (overrides no-ssl)")
    parser.add_argument("--log-level", type=str, default=default_log_level, 
                        choices=["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"],
                        help="Set the logging level (TRACE is most verbose)")
    parser.add_argument("--http-debug", type=int, default=1, choices=[0, 1, 2], 
                        help="HTTP client debug level (0=off, 1=basic, 2=verbose)")

    config = parser.parse_args()
    
    # If --use-ssl is specified, override the --no-ssl setting
    if config.use_ssl:
        config.no_ssl = False
    
    # Set port to 80 if --port-80 flag is used
    if config.port_80:
        config.port = 80
        logger.warning("Running on port 80, which requires root/admin privileges")

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
    
    # Detect public IP for better URL display
    public_ip = config.host
    if public_ip == "0.0.0.0":
        try:
            # Try to get a more useful IP address for URLs
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            public_ip = s.getsockname()[0]
            s.close()
        except:
            # If we can't determine a better IP, use localhost as fallback
            public_ip = "127.0.0.1"
            logger.warning(f"Could not determine server's public IP. Using {public_ip} for URLs")
    
    # Print Twilio webhook URL information with the detected IP
    protocol = "http" if config.no_ssl else "https"
    port_info = f":{config.port}" if config.port != (80 if protocol == "http" else 443) else ""
    webhook_url = f"{protocol}://{public_ip}{port_info}/twilio_webhook"
    test_url = f"{protocol}://{public_ip}{port_info}/twilio_test"
    debug_url = f"{protocol}://{public_ip}{port_info}/debug_server_info"
    
    logger.info(f"✨ Use this webhook URL in Twilio: {webhook_url}")
    logger.info(f"👉 Test Twilio connectivity by accessing: {test_url}")
    logger.info(f"🔍 Debug server configuration at: {debug_url}")
    logger.info(f"ℹ️ If using your IP without a domain name, ensure port {config.port} is publicly accessible")
    
    if config.no_ssl:
        logger.info(f"Starting server WITHOUT SSL on http://{config.host}:{config.port}")
        print(f"To join a test room, visit http://{public_ip}:{config.port}/")
        # Explicitly set host to 0.0.0.0 if it's not already
        host_to_use = "0.0.0.0" if config.host == "localhost" or config.host == "127.0.0.1" else config.host
        if host_to_use != config.host:
            logger.warning(f"Changed host from {config.host} to {host_to_use} to accept external connections")
        
        uvicorn.run(
            "server:app",
            host=host_to_use,
            port=config.port,
            reload=config.reload,
            log_level=uvicorn_log_level,
        )
    else:
        # Check if the certificate files exist
        if not os.path.exists(config.ssl_cert):
            logger.error(f"SSL certificate not found at {config.ssl_cert}")
            logger.warning("Falling back to HTTP mode without SSL")
            print(f"SSL certificate not found. Running without SSL.")
            
            # Fall back to HTTP
            host_to_use = "0.0.0.0" if config.host == "localhost" or config.host == "127.0.0.1" else config.host
            uvicorn.run(
                "server:app",
                host=host_to_use,
                port=config.port,
                reload=config.reload,
                log_level=uvicorn_log_level,
            )
        elif not os.path.exists(config.ssl_key):
            logger.error(f"SSL key not found at {config.ssl_key}")
            logger.warning("Falling back to HTTP mode without SSL")
            print(f"SSL key not found. Running without SSL.")
            
            # Fall back to HTTP
            host_to_use = "0.0.0.0" if config.host == "localhost" or config.host == "127.0.0.1" else config.host
            uvicorn.run(
                "server:app",
                host=host_to_use,
                port=config.port,
                reload=config.reload,
                log_level=uvicorn_log_level,
            )
        else:
            logger.info(f"Starting server with SSL on https://{config.host}:{config.port}")
            print(f"To join a test room, visit https://{public_ip}:{config.port}/")
            uvicorn.run(
                "server:app",
                host=config.host,
                port=config.port,
                reload=config.reload,
                ssl_keyfile=config.ssl_key,
                ssl_certfile=config.ssl_cert,
                log_level=uvicorn_log_level,
            )
