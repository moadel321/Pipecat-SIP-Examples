import argparse
import asyncio
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from loguru import logger
from openai.types.chat import ChatCompletionToolParam
from twilio.rest import Client

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndTaskFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_services import LLMService
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

daily_api_key = os.getenv("DAILY_API_KEY", "")
daily_api_url = os.getenv("DAILY_API_URL", "https://api.daily.co/v1")

# Twilio client setup
twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_client = Client(twilio_account_sid, twilio_auth_token)


async def terminate_call(
    function_name, tool_call_id, args, llm: LLMService, context, result_callback
):
    """Function the bot can call to terminate the call upon completion."""
    await llm.queue_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
    await result_callback("Goodbye")


async def main(
    room_url: str,
    token: str,
    phone_number: str,
    detect_voicemail: bool = False,
):
    """
    Main function to handle outbound calls via Daily and Twilio.
    
    Args:
        room_url: Daily room URL
        token: Daily room token (owner token with dial-out permissions)
        phone_number: The phone number to dial out to
        detect_voicemail: Whether to enable voicemail detection
    """
    logger.info(f"Initializing dial-out bot to call {phone_number}")
    
    # Configure the Daily transport
    transport = DailyTransport(
        room_url,
        token,  # This should be an owner token with dial-out permissions
        "Outbound Bot",
        DailyParams(
            api_url=daily_api_url,
            api_key=daily_api_key,
            audio_in_enabled=True,
            audio_out_enabled=True,
            camera_out_enabled=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            transcription_enabled=True,
        ),
    )

    # Configure TTS service
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        voice_id="aEO01A4wXwd1O8GPgGlF",
    )

    # Configure LLM service
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")
    llm.register_function("terminate_call", terminate_call)
    tools = [
        ChatCompletionToolParam(
            type="function",
            function={
                "name": "terminate_call",
                "description": "Terminate the call",
            },
        )
    ]

    # Create the system prompt for the bot
    messages = [
        {
            "role": "system",
            "content": "You are an outbound calling assistant. Your goal is to engage the person who answers the call in a friendly and sustained conversation. When the call is answered, start by greeting the person: 'Hello, this is an automated call from Pipecat. I'm calling to see if you have a few moments to chat.' Maintain a conversational tone, ask follow-up questions as needed, and do not abruptly end the call. Only end the call if the person indicates they want to finish by politely saying, 'Thank you for your time. Have a great day!', and then terminate the call."
        }
    ]

    # Set up the OpenAI context and aggregator
    context = OpenAILLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    # Create the pipeline
    pipeline = Pipeline(
        [
            transport.input(),
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    # Configure the pipeline task
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    # Set up event handlers for outbound calling
    @transport.event_handler("on_joined")
    async def on_joined(transport, data):
        logger.info(f"Joined Daily room; initiating dial-out to: {phone_number}")
        # Start the dial-out process to the specified phone number
        # The phone number should be prefixed with + for international format
        # For example: +12025550123 for a US number
        try:
            formatted_phone = phone_number if phone_number.startswith("+") else f"+{phone_number}"
            logger.info(f"Initiating dial-out to formatted number: {formatted_phone}")
            
            # Format the SIP URI according to Twilio's requirements
            # Use the format: sip:{phone_number}@{domain}.sip.twilio.com
            twilio_domain = os.getenv("TWILIO_SIP_DOMAIN", "")
            sip_uri = f"sip:{formatted_phone}@{twilio_domain}.sip.twilio.com"
            logger.info(f"Using SIP URI for dialout: {sip_uri}")
            
            # Pass the properly formatted SIP URI for Twilio integration
            await transport.start_dialout({
                "sipUri": sip_uri,
                "displayName": formatted_phone,
                "video": False
            })
        except Exception as e:
            logger.error(f"Error initiating dial-out: {str(e)}")
            # If dial-out fails, we should end the task
            await task.cancel()

    @transport.event_handler("on_dialout_connected")
    async def on_dialout_connected(transport, data):
        logger.info(f"Dial-out connected: {data}")

    @transport.event_handler("on_dialout_answered")
    async def on_dialout_answered(transport, data):
        logger.info(f"Dial-out answered: {data}")
        
    @transport.event_handler("on_dialout_error")
    async def on_dialout_error(transport, data):
        logger.error(f"Dial-out error occurred: {data}")
        await task.cancel()
        
    @transport.event_handler("on_dialout_stopped")
    async def on_dialout_stopped(transport, data):
        logger.info(f"Dial-out stopped: {data}")

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(f"First participant joined: {participant['id']}")
        await transport.capture_participant_transcription(participant["id"])
        
        # For outbound calls, we want the bot to speak first after the call is answered
        # This will prompt the bot to introduce itself
        if not detect_voicemail:
            await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.info(f"Participant left: {participant['id']}, reason: {reason}")
        await task.cancel()

    # Run the pipeline
    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipecat Outbound Calling Bot")
    parser.add_argument("-u", type=str, help="Room URL")
    parser.add_argument("-t", type=str, help="Token")
    parser.add_argument("-p", type=str, help="Phone number to call")
    parser.add_argument("-v", action="store_true", help="Detect voicemail")
    config = parser.parse_args()

    asyncio.run(main(config.u, config.t, config.p, config.v)) 