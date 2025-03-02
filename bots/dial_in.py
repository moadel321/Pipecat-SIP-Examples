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
    """Function the bot can call to terminate the call upon completion of a voicemail message."""
    await llm.queue_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
    await result_callback("Goodbye")


async def main(
    room_url: str,
    token: str,
    call_id: str,
    sip_uri: str,
    detect_voicemail: bool,
    dialout_number: Optional[str],
):
    # With Twilio integration, we don't need DailyDialinSettings
    # Instead, we'll handle the call forwarding when on_dialin_ready fires
    
    transport = DailyTransport(
        room_url,
        token,
        "Chatbot",
        DailyParams(
            api_url=daily_api_url,
            api_key=daily_api_key,
            dialin_settings=None,  # Not needed for Twilio integration
            audio_in_enabled=True,
            audio_out_enabled=True,
            camera_out_enabled=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            transcription_enabled=True,
        ),
    )

    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        voice_id="aEO01A4wXwd1O8GPgGlF",
    )

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

    messages = [
        {
            "role": "system",
            "content": """You are an elementary teacher having a phone conversation with a student. You are friendly, patient, and encouraging.
            Follow these guidelines for our interaction:

            ### Conversation Style:
            - Keep your responses concise and clear - perfect for phone conversations
            - Use a warm, encouraging tone suitable for elementary students
            - Speak naturally without any special characters or formatting (this is a phone call)
            - Listen carefully to the student's questions and provide helpful explanations
            - If you don't understand something, politely ask for clarification

            ### Teaching Approach:
            - Break down complex concepts into simple, understandable parts
            - Use real-world examples that students can relate to
            - Encourage critical thinking through thoughtful questions
            - Provide positive reinforcement for good questions and understanding
            - Be patient and offer to explain things in different ways if needed

            ### Call Management:
            - Start the call by saying: "Hello! I'm your AI teaching assistant. What would you like to learn about today?"
            - If the student wants to end the call, say: "Thank you for learning with me today! Have a wonderful day!"
            - Then call `terminate_call` to end the session
            - Keep the conversation engaging but focused on learning
            - If there's silence for too long, gently prompt the student with a question

            Remember: You're having a real phone conversation, so keep your responses natural and conversational while maintaining an educational focus."""
        }
    ]

    context = OpenAILLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

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

    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    # Twilio integration: Handle call forwarding when Daily is ready
    @transport.event_handler("on_dialin_ready")
    async def on_dialin_ready(transport, cdata):
        logger.info(f"Daily SIP endpoint ready: {sip_uri}")
        # Only try to update the Twilio call if we have an environment variable
        # indicating we should do so - this will be determined by the server.py file
        # which knows the call state better
        
        # For most cases, we'll use the TwiML in the server.py webhook response
        # to connect the call, but this is kept as a fallback
        twilio_call_sid = os.environ.get("TWILIO_CALL_SID")
        if twilio_call_sid and twilio_call_sid == call_id:
            try:
                # Check call status before attempting to redirect
                call_instance = twilio_client.calls(call_id).fetch()
                logger.info(f"Current call status: {call_instance.status}")
                
                # Only try to redirect if call is in-progress
                if call_instance.status == "in-progress":
                    logger.info(f"Redirecting Twilio call: {call_id} to SIP URI: {sip_uri}")
                    call = twilio_client.calls(call_id).update(
                        twiml=f"<Response><Dial><Sip>{sip_uri}</Sip></Dial></Response>"
                    )
                    logger.info(f"Successfully redirected call: {call.sid}")
                else:
                    logger.info(f"Not redirecting call with status: {call_instance.status}")
                    # For calls that aren't in-progress, we rely on the TwiML already sent from server.py
            except Exception as e:
                logger.error(f"Error with Twilio call: {str(e)}")
                # Don't raise exception here, as it will crash the bot
                # Instead, log the error and continue - the server.py TwiML should handle the connection
        else:
            logger.info(f"No matching Twilio call ID found in environment, using server-side TwiML")

    if dialout_number:
        logger.debug("Dialout number detected; doing dialout")

        # Configure some handlers for dialing out
        @transport.event_handler("on_joined")
        async def on_joined(transport, data):
            logger.debug(f"Joined; starting dialout to: {dialout_number}")
            await transport.start_dialout({"phoneNumber": dialout_number})

        @transport.event_handler("on_dialout_connected")
        async def on_dialout_connected(transport, data):
            logger.debug(f"Dial-out connected: {data}")

        @transport.event_handler("on_dialout_answered")
        async def on_dialout_answered(transport, data):
            logger.debug(f"Dial-out answered: {data}")

        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            await transport.capture_participant_transcription(participant["id"])
            # unlike the dialin case, for the dialout case, the caller will speak first. Presumably
            # they will answer the phone and say "Hello?" Since we've captured their transcript,
            # That will put a frame into the pipeline and prompt an LLM completion, which is how the
            # bot will then greet the user.
    elif detect_voicemail:
        logger.debug("Detect voicemail example. You can test this in example in Daily Prebuilt")

        # For the voicemail detection case, we do not want the bot to answer the phone. We want it to wait for the voicemail
        # machine to say something like 'Leave a message after the beep', or for the user to say 'Hello?'.
        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            await transport.capture_participant_transcription(participant["id"])
    else:
        logger.debug("No dialout number; assuming dial-in")

        # Different handlers for dial-in
        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            await transport.capture_participant_transcription(participant["id"])
            # For the dial-in case, we want the bot to answer the phone and greet the user. We
            # can prompt the bot to speak by putting the context into the pipeline.
            await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        await task.cancel()

    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipecat Twilio ChatBot")
    parser.add_argument("-u", type=str, help="Room URL")
    parser.add_argument("-t", type=str, help="Token")
    parser.add_argument("-i", type=str, help="Twilio Call SID")
    parser.add_argument("-s", type=str, help="Daily SIP URI")
    parser.add_argument("-v", action="store_true", help="Detect voicemail")
    parser.add_argument("-o", type=str, help="Dialout number", default=None)
    config = parser.parse_args()

    asyncio.run(main(config.u, config.t, config.i, config.s, config.v, config.o))