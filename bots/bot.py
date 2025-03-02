#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import os
import sys
import wave
import json
from datetime import datetime
from typing import List, TypedDict, Optional

import aiohttp
import sentry_sdk
from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.services.rime import RimeTTSService
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.logger import FrameLogger
from pipecat.services.openai import OpenAILLMContext, OpenAILLMContextFrame, OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.processors.transcript_processor import TranscriptProcessor
from pipecat.processors.frameworks.rtvi import (
    RTVISpeakingProcessor,
    RTVIUserTranscriptionProcessor,
    RTVIBotTranscriptionProcessor,
    RTVIBotLLMProcessor,
    RTVIBotTTSProcessor,
    RTVIMetricsProcessor,
    FrameDirection
)
from pipecat.processors.metrics.sentry import SentryMetrics

from deepgram import LiveOptions
from pipecat.services.deepgram import DeepgramSTTService


load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

BASE_FILENAME = "/var/www/conversations/pipecat_conversation_"

sounds = {}
sound_files = [
    "clack-short.wav",
    "clack.wav",
    "clack-short-quiet.wav",
    "ding.wav",
    "ding2.wav",
]

script_dir = os.path.dirname(__file__)

for file in sound_files:
    # Build the full path to the sound file
    full_path = os.path.join(script_dir, "assets", file)
    # Get the filename without the extension to use as the dictionary key
    filename = os.path.splitext(os.path.basename(full_path))[0]
    # Open the sound and convert it to bytes
    with wave.open(full_path) as audio_file:
        sounds[file] = OutputAudioRawFrame(
            audio_file.readframes(-1), audio_file.getframerate(), audio_file.getnchannels()
        )


class WeatherData(TypedDict):
    temperature: float
    feels_like: float
    description: str
    humidity: int
    wind_speed: float

class NewsArticle(TypedDict):
    title: str
    description: str
    source: str
    url: str
    published_at: str

class NewsProcessor:
    """Handles news-related API calls using NewsData.io."""
    
    def __init__(self, api_key: str):
        self.base_url = "https://newsdata.io/api/1/latest"
        self.api_key = api_key
            
    async def get_latest_news(self, query: str, session: aiohttp.ClientSession) -> Optional[List[NewsArticle]]:
        """Fetch latest news articles for a given query."""
        params = {
            "apikey": self.api_key,
            "q": query,
            "language": "en"  # English articles only
        }
        
        try:
            async with session.get(self.base_url, params=params) as response:
                if response.status != 200:
                    logger.error(f"News API Error: {response.status}")
                    return None
                    
                data = await response.json()
                if not data.get("results"):
                    return None
                    
                articles = []
                for article in data["results"][:5]:  # Limit to top 5 articles
                    articles.append(NewsArticle(
                        title=article["title"],
                        description=article.get("description", "No description available"),
                        source=article["source_id"],
                        url=article["link"],
                        published_at=article["pubDate"]
                    ))
                
                return articles
        except Exception as e:
            logger.error(f"Error getting news: {e}")
            return None

class RecipeData(TypedDict):
    name: str
    category: str
    instructions: str
    ingredients: List[str]
    measurements: List[str]

class RecipeProcessor:
    """Handles recipe-related API calls using TheMealDB."""
    
    def __init__(self):
        self.base_url = "https://www.themealdb.com/api/json/v1/1"
            
    async def get_recipe(self, meal_name: str, session: aiohttp.ClientSession) -> Optional[RecipeData]:
        """Fetch recipe data for a given meal name."""
        params = {"s": meal_name}
        
        try:
            async with session.get(f"{self.base_url}/search.php", params=params) as response:
                if response.status != 200:
                    logger.error(f"Recipe API Error: {response.status}")
                    return None
                    
                data = await response.json()
                if not data["meals"]:
                    return None
                    
                meal = data["meals"][0]
                
                # Extract ingredients and measurements
                ingredients = []
                measurements = []
                for i in range(1, 21):  # TheMealDB has up to 20 ingredients
                    ing = meal.get(f"strIngredient{i}")
                    meas = meal.get(f"strMeasure{i}")
                    if ing and ing.strip():
                        ingredients.append(ing)
                        measurements.append(meas if meas and meas.strip() else "to taste")
                
                return RecipeData(
                    name=meal["strMeal"],
                    category=meal["strCategory"],
                    instructions=meal["strInstructions"],
                    ingredients=ingredients,
                    measurements=measurements
                )
        except Exception as e:
            logger.error(f"Error getting recipe: {e}")
            return None

class WeatherProcessor:
    """Handles weather-related API calls using Open-Meteo."""
    
    def __init__(self):
        self.base_url = "https://api.open-meteo.com/v1/forecast"
            
    async def get_weather(self, lat: float, lon: float, session: aiohttp.ClientSession) -> Optional[WeatherData]:
        """Fetch current weather data for given coordinates."""
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code"
        }
        
        try:
            async with session.get(self.base_url, params=params) as response:
                if response.status != 200:
                    logger.error(f"Weather API Error: {response.status}")
                    return None
                    
                data = await response.json()
                current = data["current"]
                
                # Convert WMO weather codes to descriptions
                weather_code = current["weather_code"]
                description = self._get_weather_description(weather_code)
                
                return WeatherData(
                    temperature=current["temperature_2m"],
                    feels_like=current["apparent_temperature"],
                    description=description,
                    humidity=current["relative_humidity_2m"],
                    wind_speed=current["wind_speed_10m"]
                )
        except Exception as e:
            logger.error(f"Error getting weather: {e}")
            return None
            
    def _get_weather_description(self, code: int) -> str:
        """Convert WMO weather codes to human-readable descriptions."""
        codes = {
            0: "clear sky",
            1: "mainly clear",
            2: "partly cloudy",
            3: "overcast",
            45: "foggy",
            48: "depositing rime fog",
            51: "light drizzle",
            53: "moderate drizzle",
            55: "dense drizzle",
            61: "slight rain",
            63: "moderate rain",
            65: "heavy rain",
            71: "slight snow",
            73: "moderate snow",
            75: "heavy snow",
            77: "snow grains",
            80: "slight rain showers",
            81: "moderate rain showers",
            82: "violent rain showers",
            85: "slight snow showers",
            86: "heavy snow showers",
            95: "thunderstorm",
            96: "thunderstorm with slight hail",
            99: "thunderstorm with heavy hail"
        }
        return codes.get(code, "unknown weather condition")

class IntakeProcessor:
    def __init__(self, context: OpenAILLMContext):
        print("Initializing context from IntakeProcessor")
        self.weather_processor = WeatherProcessor()
        self.recipe_processor = RecipeProcessor()
        self.news_processor = NewsProcessor(os.getenv("NEWSDATA_API_KEY"))
        context.add_message(
            {
                "role": "system",
                "content": """You are Jessica, a friendly and helpful AI personal assistant. Avoid special characters and markdown formatting. Your goal is to help users with various tasks while maintaining a conversational and engaging tone. Keep your responses concise but warm.

Here's how you should use your available tools:

1. Weather Information:
   - When a user asks about the weather for a city, convert the city name to its coordinates
   - Use the get_weather function with the exact latitude and longitude
   - Format: latitude (-90 to 90), longitude (-180 to 180)
   - Example: London = 51.5074, -0.1278

2. Recipe Information:
   - When a user asks for a recipe, use the get_recipe function with the meal name
   - If the recipe isn't found, suggest trying a different meal name
   - Help format the recipe nicely in your response

3. News Information:
   - When a user asks for news about a topic, use the get_news function with their query
   - Present the news articles in a clear, organized format
   - Include titles, brief descriptions, and sources
   - Offer to search for more news if needed

4. Identity Verification:
   - When a user provides their birthday in any format
   - Use the verify_birthday function to confirm their identity
   - If the format is unclear, politely ask for clarification

Remember to:
- Be conversational and friendly, using a natural tone
- Ask clarifying questions when needed
- Acknowledge user inputs before processing them
- Handle errors gracefully with helpful suggestions
- Keep the conversation flowing naturally
- Never output asterisks or other formatting or special characters
- You are talking to the user over a phone call, so don't use markdown formatting

Start by warmly introducing yourself and asking how you can help today. You can help with weather information, recipes, news updates, or assist with other tasks as they come up.
Never include asterisks or any markdown formatting in your output

"""
            
            }
        )
        context.set_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "save_conversation",
                        "description": "Save the current conversation. Use this function to persist the current conversation to external storage.",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "verify_birthday",
                        "description": "Use this function to verify the user has provided their correct birthday.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "birthday": {
                                    "type": "string",
                                    "description": "The user's birthdate, including the year. The user can provide it in any format, ",
                                }
                            },
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the current weather using coordinates",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "lat": {
                                    "type": "number",
                                    "description": "Latitude of the location (-90 to 90)"
                                },
                                "lon": {
                                    "type": "number",
                                    "description": "Longitude of the location (-180 to 180)"
                                }
                            },
                            "required": ["lat", "lon"]
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "get_recipe",
                        "description": "Get recipe instructions for a meal",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "meal_name": {
                                    "type": "string",
                                    "description": "The name of the meal to get the recipe for"
                                }
                            },
                            "required": ["meal_name"]
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "get_news",
                        "description": "Get the latest news articles about a topic",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "The topic or keywords to search for in the news"
                                }
                            },
                            "required": ["query"]
                        },
                    },
                }
            ]
        )

    async def verify_birthday(
        self, function_name, tool_call_id, args, llm, context, result_callback
    ):
        if args["birthday"] == "1983-01-01":
            context.set_tools(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "list_prescriptions",
                            "description": "Once the user has provided a list of their prescription medications, call this function.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "prescriptions": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "medication": {
                                                    "type": "string",
                                                    "description": "The medication's name",
                                                },
                                                "dosage": {
                                                    "type": "string",
                                                    "description": "The prescription's dosage",
                                                },
                                            },
                                        },
                                    }
                                },
                            },
                        },
                    }
                ]
            )
            # It's a bit weird to push this to the LLM, but it gets it into the pipeline
            # await llm.push_frame(sounds["ding2.wav"], FrameDirection.DOWNSTREAM)
            # We don't need the function call in the context, so just return a new
            # system message and let the framework re-prompt
            await result_callback(
                [
                    {
                        "role": "system",
                        "content": "Next, thank the user for confirming their identity, then ask the user to list their current prescriptions. Each prescription needs to have a medication name and a dosage. Do not call the list_prescriptions function with any unknown dosages.",
                    }
                ]
            )
        else:
            # The user provided an incorrect birthday; ask them to try again
            await result_callback(
                [
                    {
                        "role": "system",
                        "content": "The user provided an incorrect birthday. Ask them for their birthday again. When they answer, call the verify_birthday function.",
                    }
                ]
            )

    async def start_prescriptions(self, function_name, llm, context):
        print("!!! doing start prescriptions")
        # Move on to allergies
        context.set_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "list_allergies",
                        "description": "Once the user has provided a list of their allergies, call this function.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "allergies": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {
                                                "type": "string",
                                                "description": "What the user is allergic to",
                                            }
                                        },
                                    },
                                }
                            },
                        },
                    },
                }
            ]
        )
        context.add_message(
            {
                "role": "system",
                "content": "Next, ask the user if they have any allergies. Once they have listed their allergies or confirmed they don't have any, call the list_allergies function.",
            }
        )
        print("!!! about to await llm process frame in start prescrpitions")
        await llm.queue_frame(OpenAILLMContextFrame(context), FrameDirection.DOWNSTREAM)
        print("!!! past await process frame in start prescriptions")

    async def start_allergies(self, function_name, llm, context):
        print("!!! doing start allergies")
        # Move on to conditions
        context.set_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "list_conditions",
                        "description": "Once the user has provided a list of their medical conditions, call this function.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "conditions": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {
                                                "type": "string",
                                                "description": "The user's medical condition",
                                            }
                                        },
                                    },
                                }
                            },
                        },
                    },
                },
            ]
        )
        context.add_message(
            {
                "role": "system",
                "content": "Now ask the user if they have any medical conditions the doctor should know about. Once they've answered the question, call the list_conditions function.",
            }
        )
        await llm.queue_frame(OpenAILLMContextFrame(context), FrameDirection.DOWNSTREAM)

    async def start_conditions(self, function_name, llm, context):
        print("!!! doing start conditions")
        # Move on to visit reasons
        context.set_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "list_visit_reasons",
                        "description": "Once the user has provided a list of the reasons they are visiting a doctor today, call this function.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "visit_reasons": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {
                                                "type": "string",
                                                "description": "The user's reason for visiting the doctor",
                                            }
                                        },
                                    },
                                }
                            },
                        },
                    },
                }
            ]
        )
        context.add_message(
            {
                "role": "system",
                "content": "Finally, ask the user the reason for their doctor visit today. Once they answer, call the list_visit_reasons function.",
            }
        )
        await llm.queue_frame(OpenAILLMContextFrame(context), FrameDirection.DOWNSTREAM)

    async def start_visit_reasons(self, function_name, llm, context):
        print("!!! doing start visit reasons")
        # move to finish call
        context.set_tools([])
        context.add_message(
            {"role": "system", "content": "Now, thank the user and end the conversation."}
        )
        await llm.queue_frame(OpenAILLMContextFrame(context), FrameDirection.DOWNSTREAM)

    async def save_data(self, function_name, tool_call_id, args, llm, context, result_callback):
        logger.info(f"!!! Saving data: {args}")
        # Since this is supposed to be "async", returning None from the callback
        # will prevent adding anything to context or re-prompting
        await result_callback(None)

    async def get_weather(
        self, function_name, tool_call_id, args, llm, context, result_callback
    ):
        """Handle weather requests."""
        async with aiohttp.ClientSession() as session:
            weather = await self.weather_processor.get_weather(args["lat"], args["lon"], session)
            
            if not weather:
                await result_callback(
                    [
                        {
                            "role": "system",
                            "content": "I couldn't get the weather data for these coordinates. Please try again later.",
                        }
                    ]
                )
                return
                
            await result_callback(
                [
                    {
                        "role": "system",
                        "content": f"The current weather is {weather['description']} with a temperature of {weather['temperature']}°C (feels like {weather['feels_like']}°C). The humidity is {weather['humidity']}% and wind speed is {weather['wind_speed']} meters per second.",
                    }
                ]
            )

    async def get_recipe(
        self, function_name, tool_call_id, args, llm, context, result_callback
    ):
        """Handle recipe requests."""
        async with aiohttp.ClientSession() as session:
            recipe = await self.recipe_processor.get_recipe(args["meal_name"], session)
            
            if not recipe:
                await result_callback(
                    [
                        {
                            "role": "system",
                            "content": f"I couldn't find a recipe for {args['meal_name']}. Would you like to try searching for a different meal?",
                        }
                    ]
                )
                return
                
            # Format the recipe nicely
            ingredients_list = [f"- {m} {i}" for i, m in zip(recipe["ingredients"], recipe["measurements"])]
            ingredients_text = "\n".join(ingredients_list)
            
            await result_callback(
                [
                    {
                        "role": "system",
                        "content": f"""Here's the recipe for {recipe['name']} (Category: {recipe['category']}):

Ingredients:
{ingredients_text}

Instructions:
{recipe['instructions']}

Would you like me to help you find another recipe?""",
                    }
                ]
            )

    async def get_news(
        self, function_name, tool_call_id, args, llm, context, result_callback
    ):
        """Handle news requests."""
        async with aiohttp.ClientSession() as session:
            articles = await self.news_processor.get_latest_news(args["query"], session)
            
            if not articles:
                await result_callback(
                    [
                        {
                            "role": "system",
                            "content": f"I couldn't find any news articles about '{args['query']}'. Would you like to try a different topic?",
                        }
                    ]
                )
                return
            
            # Add a system message to instruct the LLM to summarize the articles
            context.add_message({
                "role": "system",
                "content": """Please summarize the following news articles in a concise and engaging way. For each article:
1. Keep the title and source
2. Provide a 1-2 sentence summary of the key points
3. Avoid technical jargon and keep it conversational
4. Include the URL for more details"""
            })
            
            # Format articles for the LLM to summarize
            articles_text = "\n\n".join([
                f"Title: {article['title']}\n"
                f"Source: {article['source']}\n"
                f"Description: {article['description']}\n"
                f"URL: {article['url']}"
                for article in articles
            ])
            
            # Add the articles as user message for the LLM to process
            context.add_message({
                "role": "user",
                "content": articles_text
            })
            
            # Let the LLM process and summarize
            await llm.queue_frame(OpenAILLMContextFrame(context), FrameDirection.DOWNSTREAM)
            
            # Add final instruction for response format
            await result_callback(
                [
                    {
                        "role": "system",
                        "content": f"After providing the summarized news about {args['query']}, ask if they would like to explore another topic."
                    }
                ]
            )

    async def save_conversation(
        self, function_name, tool_call_id, args, llm, context, result_callback
    ):
        """Save the current conversation to a file."""
        timestamp = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
        filename = f"{BASE_FILENAME}{timestamp}.json"
        logger.debug(f"writing conversation to {filename}\n{json.dumps(context.messages, indent=4)}")
        try:
            with open(filename, "w") as file:
                messages = context.get_messages_for_persistent_storage()
                # remove the last message, which is the instruction we just gave to save the conversation
                messages.pop()
                json.dump(messages, file, indent=2)
            await result_callback(
                [
                    {
                        "role": "system",
                        "content": f"I've saved our conversation to {filename}. Is there anything else I can help you with?",
                    }
                ]
            )
        except Exception as e:
            logger.error(f"Error saving conversation: {e}")
            await result_callback(
                [
                    {
                        "role": "system",
                        "content": "I apologize, but I encountered an error while trying to save our conversation. Would you like to try again?",
                    }
                ]
            )


async def main():
    async with aiohttp.ClientSession() as session:
        # Get the room URL from environment variables
        daily_room_url = os.getenv("DAILY_ROOM_URL")
        if not daily_room_url:
            logger.error("DAILY_ROOM_URL environment variable is not set")
            sys.exit(1)

        logger.info(f"Using room URL: {daily_room_url}")

        # Initialize Sentry
        sentry_sdk.init(
            dsn=os.getenv("SENTRY_DSN"),
            traces_sample_rate=1.0,
        )

        # Configure the Daily transport
        transport = DailyTransport(
            daily_room_url,
            os.getenv("DAILY_ROOM_TOKEN"),
            "Chatbot",
            DailyParams(
                audio_out_enabled=True,
                vad_enabled=True,
                transcription_enabled=True,
                vad_analyzer=SileroVADAnalyzer(
                    sample_rate=16000,
                    params=VADParams(
                        threshold=0.5,
                        min_speech_duration_ms=250,
                        min_silence_duration_ms=100
                    )
                ),
                vad_audio_passthrough=True,
            ),
        )

        tts = RimeTTSService(
            api_key=os.getenv("RIME_API_KEY", ""),
            voice_id="Abbie",
            metrics=SentryMetrics(),
        )

        stt = DeepgramSTTService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            live_options=LiveOptions(
                language="en-US",
                model="nova-2-general"
            ),
            metrics=SentryMetrics(),
        )

        llm = OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"), 
            model="gpt-4o-mini",
            metrics=SentryMetrics(),
        )

        messages = []
        context = OpenAILLMContext(messages=messages)
        context_aggregator = llm.create_context_aggregator(context)

        # Initialize RTVI processors
        rtvi_speaking = RTVISpeakingProcessor()
        rtvi_user_transcription = RTVIUserTranscriptionProcessor()
        rtvi_bot_transcription = RTVIBotTranscriptionProcessor()
        rtvi_bot_llm = RTVIBotLLMProcessor()
        rtvi_bot_tts = RTVIBotTTSProcessor(direction=FrameDirection.DOWNSTREAM)
        rtvi_metrics = RTVIMetricsProcessor()

        # Initialize transcript processor for context
        transcript = TranscriptProcessor()

        intake = IntakeProcessor(context)
        llm.register_function("verify_birthday", intake.verify_birthday)
        llm.register_function("get_weather", intake.get_weather)
        llm.register_function("save_conversation", intake.save_conversation)
        llm.register_function(
            "list_prescriptions", intake.save_data, start_callback=intake.start_prescriptions
        )
        llm.register_function(
            "list_allergies", intake.save_data, start_callback=intake.start_allergies
        )
        llm.register_function(
            "list_conditions", intake.save_data, start_callback=intake.start_conditions
        )
        llm.register_function(
            "list_visit_reasons", intake.save_data, start_callback=intake.start_visit_reasons
        )
        llm.register_function(
            "get_recipe", intake.get_recipe
        )
        llm.register_function(
            "get_news", intake.get_news
        )

        fl = FrameLogger("LLM Output")

        pipeline = Pipeline(
            [
                transport.input(),  # Transport input
                rtvi_speaking,  # Speaking state
                stt,  # STT
                rtvi_user_transcription,  # Process user transcripts for RTVI
                transcript.user(),  # Process user messages for context
                context_aggregator.user(),  # User responses
                llm,  # LLM
                rtvi_bot_llm,  # Process LLM responses for RTVI
                tts,  # TTS
                rtvi_bot_tts,  # Process TTS for RTVI
                rtvi_bot_transcription,  # Process bot transcripts for RTVI
                transport.output(),  # Transport output
                transcript.assistant(),  # Process assistant messages for context
                context_aggregator.assistant(),  # Assistant responses
                rtvi_metrics,  # Collect metrics
            ]
        )

        task = PipelineTask(pipeline, PipelineParams(allow_interruptions=True, enable_metrics=True))

        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            await transport.capture_participant_transcription(participant["id"])
            print(f"Context is: {context}")
            await task.queue_frames([OpenAILLMContextFrame(context)])

        @transport.event_handler("on_participant_left")
        async def on_participant_left(transport, participant, reason):
            logger.info(f"Participant {participant['id']} left: {reason}")
            
            # Save the conversation before ending
            timestamp = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
            filename = f"{BASE_FILENAME}{timestamp}.json"
            logger.debug(f"Saving final conversation to {filename}")
            
            try:
                with open(filename, "w") as file:
                    messages = context.get_messages_for_persistent_storage()
                    json.dump(messages, file, indent=2)
                logger.info(f"Successfully saved conversation to {filename}")
            except Exception as e:
                logger.error(f"Error saving conversation on participant left: {e}")
            
            # Cancel the pipeline task to stop processing
            await task.cancel()

        runner = PipelineRunner()

        await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
