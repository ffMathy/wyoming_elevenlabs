import asyncio
import logging
import wave

from elevenlabs import NOT_GIVEN
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler
from wyoming.tts import Synthesize

from . import __version__
from .compatibility import CustomAsyncElevenLabs, TtsVoiceModel
from .utilities import NamedBytesIO

_LOGGER = logging.getLogger(__name__)

DEFAULT_AUDIO_WIDTH = 2  # 16-bit audio
DEFAULT_AUDIO_CHANNELS = 1  # Mono audio
DEFAULT_ASR_AUDIO_RATE = 16000  # Hz (Wyoming default)
TTS_AUDIO_RATE = 24000  # Hz (ElevenLabs spec)
TTS_CHUNK_SIZE = 2048  # Magical guess :)

class ElevenLabsEventHandler(AsyncEventHandler):
    def __init__(
        self,
        *args,
        stt_client: CustomAsyncElevenLabs,
        tts_client: CustomAsyncElevenLabs,
        client_lock: asyncio.Lock,
        asr_models: list[AsrModel],
        stt_temperature: float | None = None,
        stt_prompt: str | None = None,
        tts_voices: list[TtsVoiceModel],
        tts_speed: float | None = None,
        tts_instructions: str | None = None,
        **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)

        self._client_lock = client_lock

        self._stt_client = stt_client
        self._stt_temperature = stt_temperature
        self._stt_prompt = stt_prompt

        self._tts_client = tts_client
        self._tts_speed = tts_speed
        self._tts_instructions = tts_instructions

        self._wyoming_info = Info(
            asr=[
                AsrProgram(
                    name="elevenlabs",
                    description="ElevenLabs-Compatible Proxy",
                    attribution=Attribution(
                        name="Rory Eckel",
                        url="https://github.com/roryeckel/wyoming-elevenlabs/",
                    ),
                    installed=True,
                    version=__version__,
                    models=asr_models
                )
            ],
            tts=[
                TtsProgram(
                    name="elevenlabs",
                    description="ElevenLabs-Compatible Proxy",
                    attribution=Attribution(
                        name="Rory Eckel",
                        url="https://github.com/roryeckel/wyoming-elevenlabs/",
                    ),
                    installed=True,
                    version=__version__,
                    voices=tts_voices
                )
            ]
        )

        # State for current transcription
        self._wav_buffer: NamedBytesIO | None = None
        self._wav_write_buffer: wave.Wave_write | None = None
        self._is_recording: bool = False
        self._current_asr_model: AsrModel | None = None

    async def handle_event(self, event: Event) -> bool:
        """
        Handle incoming events
        https://github.com/rhasspy/rhasspy3/blob/master/docs/wyoming.md#events-types
        """
        if AudioChunk.is_type(event.type):
            # Non-logging because spammy
            await self._handle_audio_chunk(AudioChunk.from_event(event))
            return True

        _LOGGER.debug("Incoming event type %s", event.type)

        if Transcribe.is_type(event.type):
            return await self._handle_transcribe(Transcribe.from_event(event))

        if AudioStart.is_type(event.type):
            sample_rate = DEFAULT_ASR_AUDIO_RATE
            audio_width = DEFAULT_AUDIO_WIDTH
            audio_channels = DEFAULT_AUDIO_CHANNELS
            if event.data:
                if 'rate' in event.data:
                    sample_rate = event.data['rate']
                if 'width' in event.data:
                    audio_width = event.data['width']
                if 'channels' in event.data:
                    audio_channels = event.data['channels']
            await self._handle_audio_start(sample_rate, audio_width, audio_channels)
            return True

        if AudioStop.is_type(event.type):
            await self._handle_audio_stop()
            return True

        if Synthesize.is_type(event.type):
            return await self._handle_synthesize(Synthesize.from_event(event))

        if Describe.is_type(event.type):
            await self.write_event(self._wyoming_info.event())
            return True

        _LOGGER.warning("Unhandled event type: %s", event.type)
        return False

    async def _handle_transcribe(self, transcribe: Transcribe) -> bool:
        """Handle transcription request"""
        self._current_asr_model = self._get_asr_model(transcribe.name)
        if self._current_asr_model:
            if self._is_asr_language_supported(transcribe.language, self._current_asr_model):
                return True
            self._log_unsupported_asr_language(transcribe.name, transcribe.language)
        else:
            self._log_unsupported_asr_model(transcribe.name)
        return False

    async def _handle_audio_start(self, sample_rate: int, audio_width: int, audio_channels: int) -> None:
        """Handle start of audio stream"""
        self._is_recording = True
        self._wav_buffer = NamedBytesIO(name='recording.wav')
        self._wav_write_buffer = wave.open(self._wav_buffer, "wb")
        self._wav_write_buffer.setnchannels(audio_channels)
        self._wav_write_buffer.setsampwidth(audio_width)
        self._wav_write_buffer.setframerate(sample_rate)
        _LOGGER.info("Recording started at %d Hz, %d channels, %d bytes per sample",
                     sample_rate, audio_channels, audio_width)

    async def _handle_audio_chunk(self, chunk: AudioChunk) -> None:
        """Handle audio chunk"""
        if self._is_recording and chunk.audio and self._wav_write_buffer:
            self._wav_write_buffer.writeframes(chunk.audio)
        else:
            _LOGGER.warning("Problem handling audio chunk")

    async def _handle_audio_stop(self) -> None:
        """Handle end of audio stream and perform transcription"""
        if not self._is_recording or not self._wav_buffer:
            _LOGGER.warning("Received audio stop event without recording")
            return

        self._is_recording = False

        try:
            # Close the WAV file
            if self._wav_write_buffer:
                self._wav_write_buffer.close()
                self._wav_write_buffer = None

            # Reset buffer position to start
            self._wav_buffer.seek(0)

            # Send to ElevenLabs for transcription
            async with self._client_lock:
                result = await self._stt_client.audio.transcriptions.create(
                    file=self._wav_buffer,
                    model=self._current_asr_model.name,
                    temperature=self._stt_temperature or NOT_GIVEN,
                    prompt=self._stt_prompt or NOT_GIVEN
                )

            if result.text:
                _LOGGER.info(f"Successfully transcribed: {result.text}")

                # Send transcript event
                transcript = Transcript(
                    text=result.text
                )
                await self.write_event(transcript.event())
            else:
                _LOGGER.warning("Received empty transcription result")

        except Exception as e:
            _LOGGER.exception("Error during transcription: %s", e)
        finally:
            self._wav_buffer.close()
            self._wav_buffer = None

    def _get_asr_model(self, model_name: str | None = None) -> AsrModel | None:
        """Get an ASR model by name or None"""
        for program in self._wyoming_info.asr:
            for model in program.models:
                if model.name == model_name or not model_name:
                    return model
        return None

    def _log_unsupported_asr_model(self, model_name: str | None = None):
        """Log an unsupported ASR model"""
        if model_name:
            _LOGGER.warning("Unsupported ASR model: %s", model_name)
        else:
            _LOGGER.warning("No ASR models specified")

    def _is_asr_language_supported(self, language: str, model: AsrModel) -> bool:
        """Check if a language is supported by an ASR model"""
        return not model.languages or language in model.languages

    def _log_unsupported_asr_language(self, model_name: str, language: str):
        """Log an unsupported ASR language"""
        _LOGGER.error("Unsupported ASR model %s for language %s", model_name, language)

    def _get_voice(self, name: str | None = None) -> TtsVoiceModel | None:
        """Get a TTS voice by name or None"""
        for program in self._wyoming_info.tts:
            for voice in program.voices:
                if not name or voice.name == name:
                    return voice
        return None

    def _is_tts_language_supported(self, language: str, voice: TtsVoice) -> bool:
        """Check if a language is supported by a TTS voice"""
        return not voice.languages or language in voice.languages

    def _validate_tts_language(self, language: str | None, voice: TtsVoice) -> bool:
        """Validate if a language is supported by a TTS voice. Returns True if supported. If no language is specified, also returns True."""
        if language and not self._is_tts_language_supported(language, voice):
            _LOGGER.error(f"Language {language} is not supported for voice {voice.name}. Available languages: {voice.languages}")
            return False
        return True

    def _log_unsupported_voice(self, requested_voice: str | None) -> None:
        """Log an error message if a voice is not supported"""
        if requested_voice:
            _LOGGER.error(f"Voice {requested_voice} is not supported. Available voices: {[voice.name for program in self._wyoming_info.tts for voice in program.voices]}")
        else:
            _LOGGER.error("No TTS voices specified")

    async def _handle_synthesize(self, synthesize: Synthesize) -> bool:
        """Handle text-to-speech synthesis request"""
        try:
            _LOGGER.debug("Handling synthesize request %s", synthesize)

            if synthesize.voice:
                requested_voice = synthesize.voice.name
                requested_language = synthesize.voice.language
            else:
                requested_voice = None
                requested_language = None

            # Validate voice against self._wyoming_info
            voice = self._get_voice(requested_voice)
            if voice:
                if not self._validate_tts_language(requested_language, voice):
                    return False
            else:
                self._log_unsupported_voice(requested_voice)
                return False

            async with self._client_lock, self._tts_client.audio.speech.with_streaming_response.create(
                model=voice.model_name,
                voice=voice.name,
                input=synthesize.text,
                speed=self._tts_speed or NOT_GIVEN,
                instructions=self._tts_instructions or NOT_GIVEN
            ) as response:

                    # Send audio start with required audio parameters
                    await self.write_event(
                        AudioStart(
                            rate=TTS_AUDIO_RATE,
                            width=DEFAULT_AUDIO_WIDTH,
                            channels=DEFAULT_AUDIO_CHANNELS
                        ).event()
                    )

                    # Stream the audio in chunks
                    timestamp = 0
                    samples_per_chunk = TTS_CHUNK_SIZE // DEFAULT_AUDIO_WIDTH  # bytes per sample
                    timestamp_increment = (samples_per_chunk / TTS_AUDIO_RATE) * 1000  # ms

                    async for chunk in response.iter_bytes(chunk_size=TTS_CHUNK_SIZE):
                        await self.write_event(
                            AudioChunk(
                                audio=chunk,
                                rate=TTS_AUDIO_RATE,
                                width=DEFAULT_AUDIO_WIDTH,
                                channels=DEFAULT_AUDIO_CHANNELS,
                                timestamp=int(timestamp)
                            ).event()
                        )
                        timestamp += timestamp_increment

                    # Send audio stop
                    await self.write_event(AudioStop(timestamp=timestamp).event())

                    _LOGGER.debug("Successfully synthesized: %s", synthesize.text[:100])
                    return True

        except Exception as e:
            _LOGGER.exception("Error during synthesis: %s", e)
            return False

    async def stop(self) -> None:
        """Stop the handler and close the clients"""
        await super().stop()
        self._stt_client.close()
        self._tts_client.close()
