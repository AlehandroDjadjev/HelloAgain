import abc
import base64
import importlib.util
import io
import json
import logging
import os
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[2] / ".voice_models"
DEFAULT_STT_LANGUAGE = "bg-BG"
DEFAULT_GOOGLE_SPEECH_MODEL = "latest_long"
DEFAULT_GOOGLE_SPEECH_HINTS = (
    "български, здравей, помощ, сметка, баланс, превод, моля, искам, "
    "искам помощ, проверка на сметка, банков баланс"
)
DEFAULT_PIPER_MODEL_PATH = DEFAULT_MODEL_DIR / "bg_BG-dimitar-medium.onnx"
DEFAULT_PIPER_CONFIG_PATH = DEFAULT_MODEL_DIR / "bg_BG-dimitar-medium.onnx.json"
DEFAULT_PIPER_MODEL_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "bg/bg_BG/dimitar/medium/bg_BG-dimitar-medium.onnx?download=true"
)
DEFAULT_PIPER_CONFIG_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "bg/bg_BG/dimitar/medium/bg_BG-dimitar-medium.onnx.json?download=true"
)


class ProviderNotReadyError(RuntimeError):
    pass


class SpeechToTextProvider(abc.ABC):
    @abc.abstractmethod
    def transcribe(self, audio_data: bytes, language: Optional[str] = None):
        raise NotImplementedError

    @abc.abstractmethod
    def status(self) -> str:
        raise NotImplementedError


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    def generate_reply(self, prompt: str, session_id: str, user_id: str):
        raise NotImplementedError

    @abc.abstractmethod
    def status(self) -> str:
        raise NotImplementedError


class TextToSpeechProvider(abc.ABC):
    @abc.abstractmethod
    def synthesize(self, text: str, voice_id: Optional[str] = None):
        raise NotImplementedError

    @abc.abstractmethod
    def status(self) -> str:
        raise NotImplementedError


class GoogleCloudSpeechSTTProvider(SpeechToTextProvider):
    def __init__(
        self,
        language: Optional[str] = None,
        model: Optional[str] = None,
        phrase_hints: Optional[list[str]] = None,
    ):
        self.default_language = language or os.environ.get(
            "GOOGLE_CLOUD_SPEECH_LANGUAGE",
            DEFAULT_STT_LANGUAGE,
        )
        self.api_key = os.environ.get("GOOGLE_STT_API_KEY", "").strip().strip("'\"")
        configured_model = model or os.environ.get(
            "GOOGLE_CLOUD_SPEECH_MODEL",
            DEFAULT_GOOGLE_SPEECH_MODEL,
        )
        self.model = self._normalize_model_name(configured_model)
        self.phrase_hints = phrase_hints or [
            phrase.strip()
            for phrase in os.environ.get(
                "GOOGLE_CLOUD_SPEECH_HINTS",
                DEFAULT_GOOGLE_SPEECH_HINTS,
            ).split(",")
            if phrase.strip()
        ]
        self._client = None
        self._project_id: Optional[str] = None
        self._load_error: Optional[str] = None

    def _speech_package_available(self) -> bool:
        try:
            from google.cloud import speech  # noqa: F401
        except ModuleNotFoundError:
            return False
        return True

    def _resolve_credentials(self):
        from google.auth import default
        from google.oauth2 import service_account

        credentials_path = os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS",
            "",
        ).strip()
        if credentials_path:
            credentials_file = Path(credentials_path)
            if not credentials_file.exists():
                raise FileNotFoundError(
                    "GOOGLE_APPLICATION_CREDENTIALS points to a missing file: "
                    f"{credentials_file}",
                )

            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_file),
            )
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or getattr(
                credentials,
                "project_id",
                None,
            )
            return credentials, project_id

        credentials, detected_project = default()
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or detected_project
        return credentials, project_id

    def _normalize_model_name(self, model_name: str) -> str:
        normalized = model_name.strip()
        if normalized in {"long", "latest_long"}:
            return "default"
        if normalized in {"short", "latest_short"}:
            return "command_and_search"
        return normalized

    def _build_rest_request_body(
        self,
        audio_data: bytes,
        language: str,
        audio_metadata: Optional[dict] = None,
    ) -> dict:
        body = {
            "config": {
                "languageCode": language,
                "model": self.model,
                "enableAutomaticPunctuation": True,
                "maxAlternatives": 3,
            },
            "audio": {
                "content": base64.b64encode(audio_data).decode("ascii"),
            },
        }

        if audio_metadata and audio_metadata.get("sample_width") == 2:
            body["config"]["encoding"] = "LINEAR16"
            body["config"]["sampleRateHertz"] = audio_metadata["sample_rate"]
            body["config"]["audioChannelCount"] = audio_metadata["channels"]

        if self.phrase_hints:
            body["config"]["speechContexts"] = [
                {"phrases": self.phrase_hints, "boost": 15.0},
            ]

        return body

    def _transcribe_via_api_key(
        self,
        audio_data: bytes,
        language: str,
        audio_metadata: Optional[dict] = None,
    ):
        request_body = self._build_rest_request_body(
            audio_data,
            language,
            audio_metadata=audio_metadata,
        )
        query = urllib.parse.urlencode({"key": self.api_key})
        url = f"https://speech.googleapis.com/v1/speech:recognize?{query}"
        payload = json.dumps(request_body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw_response = response.read()
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(error_body)
                error_message = parsed.get("error", {}).get("message", error_body)
            except json.JSONDecodeError:
                error_message = error_body or str(exc)
            raise RuntimeError(
                f"Google STT request failed: {exc.code} {error_message}",
            ) from exc

        parsed_response = json.loads(raw_response.decode("utf-8"))
        transcripts = [
            result["alternatives"][0]["transcript"].strip()
            for result in parsed_response.get("results", [])
            if result.get("alternatives")
        ]
        return " ".join(part for part in transcripts if part).strip()

    def _ensure_client(self):
        if self.api_key:
            self._project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "api-key"
            return

        if self._client is not None or self._load_error is not None:
            return

        try:
            from google.cloud import speech

            credentials, self._project_id = self._resolve_credentials()
            if not self._project_id:
                raise ProviderNotReadyError(
                    "Google Cloud Speech-to-Text needs GOOGLE_CLOUD_PROJECT or "
                    "a service-account JSON with a project_id.",
                )

            self._client = speech.SpeechClient(credentials=credentials)
        except Exception as exc:
            self._load_error = str(exc)
            logger.exception("Failed to load Google Cloud Speech client")

    def _normalize_transcript(self, text: str) -> str:
        cleaned = " ".join(text.split()).strip()
        if not cleaned:
            return cleaned

        if cleaned[-1] not in ".!?":
            cleaned = f"{cleaned}."

        return cleaned[0].upper() + cleaned[1:]

    def _downmix_pcm_to_mono(
        self,
        frames: bytes,
        sample_width: int,
        channels: int,
    ) -> bytes:
        if channels <= 1:
            return frames

        frame_size = sample_width * channels
        mono_frames = bytearray()

        for offset in range(0, len(frames), frame_size):
            frame = frames[offset : offset + frame_size]
            if len(frame) < frame_size:
                break

            samples = []
            for channel_index in range(channels):
                start = channel_index * sample_width
                chunk = frame[start : start + sample_width]

                if sample_width == 1:
                    samples.append(chunk[0] - 128)
                else:
                    samples.append(
                        int.from_bytes(chunk, byteorder="little", signed=True),
                    )

            average = int(sum(samples) / len(samples))
            if sample_width == 1:
                mono_frames.append(max(0, min(255, average + 128)))
            else:
                min_value = -(1 << (8 * sample_width - 1))
                max_value = (1 << (8 * sample_width - 1)) - 1
                average = max(min_value, min(max_value, average))
                mono_frames.extend(
                    average.to_bytes(
                        sample_width,
                        byteorder="little",
                        signed=True,
                    ),
                )

        return bytes(mono_frames)

    def _read_wav_metadata(self, audio_data: bytes) -> Optional[dict]:
        try:
            with wave.open(io.BytesIO(audio_data), "rb") as wav_reader:
                return {
                    "channels": wav_reader.getnchannels(),
                    "sample_width": wav_reader.getsampwidth(),
                    "sample_rate": wav_reader.getframerate(),
                    "frame_count": wav_reader.getnframes(),
                    "compression": wav_reader.getcomptype(),
                }
        except wave.Error:
            return None

    def _prepare_audio_for_google(self, audio_data: bytes) -> tuple[bytes, Optional[dict]]:
        metadata = self._read_wav_metadata(audio_data)
        if metadata is None:
            return audio_data, None

        try:
            with wave.open(io.BytesIO(audio_data), "rb") as wav_reader:
                if wav_reader.getnchannels() == 1:
                    return audio_data, metadata

                if wav_reader.getcomptype() != "NONE":
                    logger.warning(
                        "Skipping mono conversion for compressed WAV input with codec %s",
                        wav_reader.getcomptype(),
                    )
                    return audio_data, metadata

                sample_width = wav_reader.getsampwidth()
                frame_rate = wav_reader.getframerate()
                frame_count = wav_reader.getnframes()
                channels = wav_reader.getnchannels()
                frames = wav_reader.readframes(frame_count)

            mono_frames = self._downmix_pcm_to_mono(
                frames,
                sample_width,
                channels,
            )
            output = io.BytesIO()
            with wave.open(output, "wb") as wav_writer:
                wav_writer.setnchannels(1)
                wav_writer.setsampwidth(sample_width)
                wav_writer.setframerate(frame_rate)
                wav_writer.writeframes(mono_frames)
            return output.getvalue(), {
                **metadata,
                "channels": 1,
                "sample_rate": frame_rate,
                "sample_width": sample_width,
                "frame_count": len(mono_frames) // sample_width,
            }
        except wave.Error:
            return audio_data, metadata

    def transcribe(self, audio_data: bytes, language: Optional[str] = None):
        from voice_gateway.domain.contracts import TranscriptionResult

        self._ensure_client()
        if not self.api_key and self._client is None:
            raise ProviderNotReadyError(
                "Google Cloud Speech-to-Text is not ready. Check "
                "GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_STT_API_KEY in backend/.env.",
            )

        chosen_language = language or self.default_language
        prepared_audio, audio_metadata = self._prepare_audio_for_google(audio_data)
        if self.api_key:
            text = self._transcribe_via_api_key(
                prepared_audio,
                chosen_language,
                audio_metadata=audio_metadata,
            )
            source = "google_cloud_speech_v1_rest"
            auth_mode = "api_key"
        else:
            from google.cloud import speech

            config_kwargs = {
                "language_code": chosen_language,
                "model": self.model,
                "enable_automatic_punctuation": True,
                "max_alternatives": 3,
                "speech_contexts": [
                    speech.SpeechContext(
                        phrases=self.phrase_hints,
                        boost=15.0,
                    ),
                ],
                "metadata": speech.RecognitionMetadata(
                    interaction_type=(
                        speech.RecognitionMetadata.InteractionType.DICTATION
                    ),
                    microphone_distance=(
                        speech.RecognitionMetadata.MicrophoneDistance.NEARFIELD
                    ),
                    original_media_type=(
                        speech.RecognitionMetadata.OriginalMediaType.AUDIO
                    ),
                    recording_device_type=(
                        speech.RecognitionMetadata.RecordingDeviceType.SMARTPHONE
                    ),
                ),
            }
            if audio_metadata and audio_metadata.get("sample_width") == 2:
                config_kwargs.update(
                    encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                    sample_rate_hertz=audio_metadata["sample_rate"],
                    audio_channel_count=audio_metadata["channels"],
                )
            config = speech.RecognitionConfig(**config_kwargs)
            audio = speech.RecognitionAudio(content=prepared_audio)
            response = self._client.recognize(config=config, audio=audio, timeout=120)

            text = " ".join(
                result.alternatives[0].transcript.strip()
                for result in response.results
                if result.alternatives
            ).strip()
            source = "google_cloud_speech_v1"
            auth_mode = "service_account"

        text = self._normalize_transcript(text)
        if not text:
            raise ValueError(
                "Google STT did not produce a transcript. Make sure the recording "
                "contains audible speech and try again.",
            )

        return TranscriptionResult(
            text=text,
            source=source,
            warnings=[
                f"transcription_language={chosen_language}",
                f"transcription_model={self.model}",
                f"transcription_project={self._project_id}",
                f"transcription_auth={auth_mode}",
            ],
        )

    def status(self) -> str:
        if self._client is not None:
            return f"ready: {self._project_id}/{self.model}"
        if self.api_key:
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "api-key"
            return f"configured: {project_id}/{self.model}/api_key"
        if self._load_error is not None:
            return f"unavailable: {self._load_error}"
        if not self._speech_package_available():
            return "unavailable: package_missing"

        try:
            _, project_id = self._resolve_credentials()
            return f"configured: {project_id}/{self.model}"
        except Exception as exc:
            return f"unavailable: {exc}"


class PlaceholderQwenLLMProvider(LLMProvider):
    def generate_reply(self, prompt: str, session_id: str, user_id: str):
        from voice_gateway.domain.contracts import LLMResult

        message = (
            "Qwen is not connected yet, so this is a placeholder reply. "
            f"I heard: {prompt}"
        )
        return LLMResult(
            text=message,
            source="qwen_placeholder",
            warnings=[
                "Qwen is not configured yet. Replace PlaceholderQwenLLMProvider "
                "when the model is available.",
            ],
        )

    def status(self) -> str:
        return "placeholder"


class PiperTTSProvider(TextToSpeechProvider):
    def __init__(
        self,
        model_path: Optional[str] = None,
        config_path: Optional[str] = None,
        model_url: Optional[str] = None,
        config_url: Optional[str] = None,
    ):
        self.model_path = Path(
            model_path or os.environ.get("PIPER_MODEL_PATH", DEFAULT_PIPER_MODEL_PATH),
        )
        self.config_path = Path(
            config_path or os.environ.get(
                "PIPER_CONFIG_PATH",
                DEFAULT_PIPER_CONFIG_PATH,
            ),
        )
        self.model_url = model_url or os.environ.get(
            "PIPER_MODEL_URL",
            DEFAULT_PIPER_MODEL_URL,
        )
        self.config_url = config_url or os.environ.get(
            "PIPER_CONFIG_URL",
            DEFAULT_PIPER_CONFIG_URL,
        )
        self._voice = None
        self._load_error: Optional[str] = None

    def _ensure_voice(self):
        # Return early if already loaded or if there's an error
        if self._voice is not None or self._load_error is not None:
            return

        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)

            # Download the model if it doesn't exist
            if not self.model_path.exists():
                logger.info("Downloading Piper model to %s", self.model_path)
                urllib.request.urlretrieve(self.model_url, self.model_path)

            # Download the config if it doesn't exist
            if not self.config_path.exists():
                logger.info("Downloading Piper config to %s", self.config_path)
                urllib.request.urlretrieve(self.config_url, self.config_path)

            # Try to load the model
            from piper.voice import PiperVoice

            logger.info("Loading Piper voice model from %s", self.model_path)
            self._voice = PiperVoice.load(
                str(self.model_path),
                config_path=str(self.config_path),
            )

            logger.info("Piper voice model loaded successfully.")

        except Exception as exc:
            # Catch exceptions and log the error
            self._load_error = str(exc)
            logger.exception("Failed to load Piper voice: %s", exc)

    def _normalize_text(self, text: str) -> str:
        # Normalize the input text
        cleaned = unicodedata.normalize("NFC", " ".join(text.split())).strip()
        if cleaned and cleaned[-1] not in ".!?":
            cleaned = f"{cleaned}."
        return cleaned

    def synthesize(self, text: str, voice_id: Optional[str] = None):
        from voice_gateway.domain.contracts import SpeechSynthesisResult

        # Ensure the voice model is loaded
        self._ensure_voice()

        # If the voice is not ready, raise an error
        if self._voice is None:
            raise ProviderNotReadyError(
                "Piper is not ready. Install the dependency and verify the "
                "voice model can load.",
            )

        # Normalize the text
        normalized_text = self._normalize_text(text)

        if not normalized_text:
            raise ValueError("Text is required for speech synthesis.")

        # Try to synthesize speech
        try:
            chunks = list(self._voice.synthesize(normalized_text))

            if not chunks:
                raise ProviderNotReadyError(
                    "Piper did not generate audio for the provided text.",
                )

            # Write audio chunks to WAV format
            wav_io = io.BytesIO()
            with wave.open(wav_io, "wb") as wav_file:
                first_chunk = chunks[0]
                wav_file.setframerate(first_chunk.sample_rate)
                wav_file.setsampwidth(first_chunk.sample_width)
                wav_file.setnchannels(first_chunk.sample_channels)

                for chunk in chunks:
                    wav_file.writeframes(chunk.audio_int16_bytes)

            # Get the audio bytes
            audio_bytes = wav_io.getvalue()

            # If the file is too small, log the warning and raise an error
            if len(audio_bytes) <= 44:
                raise ProviderNotReadyError("Piper returned an empty WAV payload.")
            
            logger.info("Audio synthesis completed successfully.")

            return SpeechSynthesisResult(
                audio_bytes=audio_bytes,
                source="piper",
                mime_type="audio/wav",
            )

        except Exception as exc:
            # Log and raise errors during synthesis
            logger.exception("Error during speech synthesis: %s", exc)
            raise ProviderNotReadyError(f"Synthesis failed: {exc}")

    def status(self) -> str:
        # Check the status of the voice model
        if self._voice is not None:
            return "ready"
        if self._load_error is not None:
            return f"unavailable: {self._load_error}"
        try:
            piper_available = importlib.util.find_spec("piper") is not None
        except ModuleNotFoundError:
            piper_available = False

        if not piper_available:
            return "unavailable: package_missing"
        if self.model_path.exists() and self.config_path.exists():
            return "configured"
        return "configured: model_will_download_on_first_use"
