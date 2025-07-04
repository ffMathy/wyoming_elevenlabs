import argparse
import asyncio
import logging
import os
from functools import partial

from wyoming.server import AsyncServer

from . import __version__
from .compatibility import (
    CustomAsyncElevenLabs,
    ElevenLabsBackend,
    asr_model_to_string,
    create_asr_models,
    create_tts_voices,
    tts_voice_to_string,
)
from .handler import ElevenLabsEventHandler


def configure_logging(level):
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f'Invalid log level: {level}')
    logging.basicConfig(level=numeric_level)

async def main():
    env_stt_backend = os.getenv("STT_BACKEND")
    env_tts_backend = os.getenv("TTS_BACKEND")
    parser = argparse.ArgumentParser()

    # General configuration
    parser.add_argument(
        "--uri",
        default=os.getenv("WYOMING_URI","tcp://0.0.0.0:10300"),
        help="This Wyoming Server URI"
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("WYOMING_LOG_LEVEL", "INFO"),
        help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    )
    parser.add_argument(
        "--languages",
        nargs="+",
        default=os.getenv("WYOMING_LANGUAGES", "en").split(),
        help="List of languages supported by BOTH STT AND TTS (example: en, fr)"
    )

    # STT configuration
    parser.add_argument(
        "--stt-elevenlabs-key",
        required=False,
        default=os.getenv("STT_ELEVENLABS_KEY", None),
        help="ElevenLabs API key for speech-to-text"
    )
    parser.add_argument(
        "--stt-elevenlabs-url",
        default=os.getenv("STT_ELEVENLABS_URL", "https://api.elevenlabs.com/v1"),
        help="Custom ElevenLabs API base URL for STT"
    )
    parser.add_argument(
        "--stt-models",
        nargs='+',  # Use nargs to accept multiple values
        default=os.getenv("STT_MODELS", 'gpt-4o-transcribe gpt-4o-mini-transcribe whisper-1').split(),
        help="List of STT model identifiers"
    )
    parser.add_argument(
        "--stt-backend",
        type=ElevenLabsBackend,
        required=False,
        choices=list(ElevenLabsBackend),
        default=ElevenLabsBackend[env_stt_backend] if env_stt_backend else None,
        help="Backend for speech-to-text (ELEVENLABS, SPEACHES, KOKORO_FASTAPI, or None)"
    )
    parser.add_argument(
        "--stt-temperature",
        type=float,
        default=float(os.getenv("STT_TEMPERATURE")) if os.getenv("STT_TEMPERATURE") else None,
        help="Sampling temperature for speech-to-text (0.0 to 1.0, default is None for ElevenLabs default)"
    )
    parser.add_argument(
        "--stt-prompt",
        default=os.getenv("STT_PROMPT", None),
        help="Optional prompt for STT requests (ElevenLabs createTranscription API)."
    )

    # TTS configuration
    parser.add_argument(
        "--tts-elevenlabs-key",
        required=False,
        default=os.getenv("TTS_ELEVENLABS_KEY", None),
        help="ElevenLabs API key for text-to-speech"
    )
    parser.add_argument(
        "--tts-elevenlabs-url",
        default=os.getenv("TTS_ELEVENLABS_URL", "https://api.elevenlabs.com/v1"),
        help="Custom ElevenLabs API base URL for TTS"
    )
    parser.add_argument(
        "--tts-models",
        nargs='+',
        default=os.getenv("TTS_MODELS", 'gpt-4o-mini-tts tts-1-hd tts-1').split(),
        help="List of TTS model identifiers"
    )
    parser.add_argument(
        "--tts-voices",
        nargs='+',
        default=os.getenv("TTS_VOICES", '').split(),
        required=False,
        help="List of available TTS voices"
    )
    parser.add_argument(
        "--tts-backend",
        type=ElevenLabsBackend,
        required=False,
        choices=list(ElevenLabsBackend),
        default=ElevenLabsBackend[env_tts_backend] if env_tts_backend else None,
        help="Backend for text-to-speech (ELEVENLABS, SPEACHES, KOKORO_FASTAPI, or None)"
    )
    parser.add_argument(
        "--tts-speed",
        type=float,
        default=float(os.getenv("TTS_SPEED")) if os.getenv("TTS_SPEED") else None,
        help="Speed of the TTS output (0.25 to 4.0, default is None for ElevenLabs default)"
    )
    parser.add_argument(
        "--tts-instructions",
        default=os.getenv("TTS_INSTRUCTIONS", None),
        help="Optional instructions for TTS requests (ElevenLabs createSpeech API)."
    )

    args = parser.parse_args()

    configure_logging(args.log_level)
    _logger = logging.getLogger(__name__)

    _logger.info("Starting Wyoming ElevenLabs %s", __version__)

    # Create factories and clients
    if args.stt_backend is None:
        _logger.debug("STT backend is None, autodetecting...")
        stt_factory = CustomAsyncElevenLabs.create_autodetected_factory()
    else:
        stt_factory = CustomAsyncElevenLabs.create_backend_factory(args.stt_backend)
    stt_client = await stt_factory(api_key=args.stt_elevenlabs_key, base_url=args.stt_elevenlabs_url)
    _logger.debug("Detected STT backend: %s", stt_client.backend)

    if args.tts_backend is None:
        _logger.debug("TTS backend is None, autodetecting...")
        tts_factory = CustomAsyncElevenLabs.create_autodetected_factory()
    else:
        tts_factory = CustomAsyncElevenLabs.create_backend_factory(args.tts_backend)
    tts_client = await tts_factory(api_key=args.tts_elevenlabs_key, base_url=args.tts_elevenlabs_url)
    _logger.debug("Detected TTS backend: %s", tts_client.backend)

    asr_models = create_asr_models(args.stt_models, args.stt_elevenlabs_url, args.languages)

    if args.tts_voices:
        # If TTS_VOICES is set, use that
        tts_voices = create_tts_voices(args.tts_models, args.tts_voices, args.tts_elevenlabs_url, args.languages)
    else:
        # Otherwise, list supported voices via defaults
        tts_voices = await tts_client.list_supported_voices(args.tts_models, args.languages)

    # Log everything available
    if asr_models:
        _logger.info("*** ASR Models ***\n%s", "\n".join(asr_model_to_string(x) for x in asr_models))
    else:
        _logger.warning("No ASR models specified")

    if tts_voices:
        _logger.info("*** TTS Voices ***\n%s", "\n".join(tts_voice_to_string(x) for x in tts_voices))
    else:
        _logger.warning("No TTS models specified")

    # Create server
    server = AsyncServer.from_uri(args.uri)

    # Run server
    _logger.info("Starting server at %s", args.uri)
    await server.run(
        partial(
            ElevenLabsEventHandler,
            stt_client=stt_client,
            tts_client=tts_client,
            client_lock=asyncio.Lock(),
            asr_models=asr_models,
            stt_temperature=args.stt_temperature,
            tts_voices=tts_voices,
            tts_speed=args.tts_speed,
            tts_instructions=args.tts_instructions,
            stt_prompt=args.stt_prompt
        )
    )

asyncio.run(main())
