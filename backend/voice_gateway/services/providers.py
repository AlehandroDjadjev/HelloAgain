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

from voice_gateway.services.memory import conversation_memory

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[2] / ".voice_models"
BACKEND_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_STT_LANGUAGE = "bg-BG"
DEFAULT_GOOGLE_SPEECH_API_VERSION = "v1"
DEFAULT_GOOGLE_SPEECH_MODEL = "latest_long"
DEFAULT_GOOGLE_SPEECH_LOCATION = "global"
DEFAULT_GOOGLE_SPEECH_RECOGNIZER = "_"
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_SYSTEM_PROMPT = (
    "You are HelloAgain, a warm real-time voice assistant. "
    "You are speaking with an older adult, so use calm, respectful, everyday language. "
    "Avoid emojis, slang, jargon, and difficult words unless the user clearly asks for them. "
    "Keep replies concise, natural, easy to understand, and easy to speak aloud. "
    "Reply in the same language as the user's most recent message unless asked otherwise. "
    "Prefer short spoken-friendly responses."
)
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


def _clean_env_value(raw_value: str) -> str:
    return raw_value.strip().strip("'\"")


def _read_env_value(key: str, env_path: Optional[Path] = None) -> str:
    direct_value = os.environ.get(key, "")
    if direct_value.strip():
        return _clean_env_value(direct_value)

    resolved_env_path = env_path or BACKEND_ENV_PATH

    try:
        env_text = resolved_env_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return _clean_env_value(value)

    return ""


class SpeechToTextProvider(abc.ABC):
    @abc.abstractmethod
    def transcribe(
        self,
        audio_data: bytes,
        language: Optional[str] = None,
        content_type: Optional[str] = None,
    ):
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
        self.api_key = _read_env_value("GOOGLE_STT_API_KEY")
        self.api_version = os.environ.get(
            "GOOGLE_STT_API_VERSION",
            DEFAULT_GOOGLE_SPEECH_API_VERSION,
        ).strip().lower()
        configured_model = model or os.environ.get(
            "GOOGLE_CLOUD_SPEECH_MODEL",
            DEFAULT_GOOGLE_SPEECH_MODEL,
        )
        self.model = self._normalize_model_name(configured_model)
        self.location = os.environ.get(
            "GOOGLE_CLOUD_SPEECH_LOCATION",
            DEFAULT_GOOGLE_SPEECH_LOCATION,
        ).strip()
        self.recognizer = os.environ.get(
            "GOOGLE_CLOUD_SPEECH_RECOGNIZER",
            DEFAULT_GOOGLE_SPEECH_RECOGNIZER,
        ).strip()
        self.phrase_hints = phrase_hints or [
            phrase.strip()
            for phrase in os.environ.get(
                "GOOGLE_CLOUD_SPEECH_HINTS",
                DEFAULT_GOOGLE_SPEECH_HINTS,
            ).split(",")
            if phrase.strip()
        ]
        self._project_id: Optional[str] = None

    def _normalize_model_name(self, model_name: str) -> str:
        normalized = model_name.strip()
        if self.api_version == "v2":
            if normalized == "latest_long":
                return "long"
            if normalized == "latest_short":
                return "short"
            return normalized
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
        content_type: Optional[str] = None,
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
        elif content_type:
            inferred_encoding = self._infer_rest_encoding(content_type)
            if inferred_encoding:
                body["config"]["encoding"] = inferred_encoding

        if self.phrase_hints:
            body["config"]["speechContexts"] = [
                {"phrases": self.phrase_hints, "boost": 15.0},
            ]

        return body

    def _infer_rest_encoding(self, content_type: str) -> Optional[str]:
        normalized = content_type.split(";")[0].strip().lower()
        if normalized in {"audio/flac", "audio/x-flac"}:
            return "FLAC"
        if normalized in {"audio/ogg", "audio/opus"}:
            return "OGG_OPUS"
        if normalized == "audio/webm":
            return "WEBM_OPUS"
        if normalized in {"audio/wav", "audio/wave", "audio/x-wav"}:
            return "LINEAR16"
        return None

    def _transcribe_via_api_key(
        self,
        audio_data: bytes,
        language: str,
        audio_metadata: Optional[dict] = None,
        content_type: Optional[str] = None,
    ):
        if self.api_version == "v2":
            return self._transcribe_v2_via_rest(
                audio_data,
                language,
                audio_metadata=audio_metadata,
                content_type=content_type,
            )
        request_body = self._build_rest_request_body(
            audio_data,
            language,
            audio_metadata=audio_metadata,
            content_type=content_type,
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

    def _build_v2_rest_request_body(
        self,
        audio_data: bytes,
        language: str,
        audio_metadata: Optional[dict] = None,
        content_type: Optional[str] = None,
    ) -> dict:
        config: dict[str, object] = {
            "languageCodes": [language],
            "model": self.model,
            "features": {
                "enableAutomaticPunctuation": True,
                "maxAlternatives": 3,
            },
        }

        if audio_metadata and audio_metadata.get("sample_width") == 2:
            config["explicitDecodingConfig"] = {
                "encoding": "LINEAR16",
                "sampleRateHertz": audio_metadata["sample_rate"],
                "audioChannelCount": audio_metadata["channels"],
            }
        elif content_type:
            inferred_encoding = self._infer_rest_encoding(content_type)
            if inferred_encoding:
                config["explicitDecodingConfig"] = {
                    "encoding": inferred_encoding,
                }
            else:
                config["autoDecodingConfig"] = {}
        else:
            config["autoDecodingConfig"] = {}

        return {
            "config": config,
            "content": base64.b64encode(audio_data).decode("ascii"),
        }

    def _transcribe_v2_via_rest(
        self,
        audio_data: bytes,
        language: str,
        audio_metadata: Optional[dict] = None,
        content_type: Optional[str] = None,
    ) -> str:
        if not self._project_id:
            self._project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "api-key"

        if not self._project_id or self._project_id == "api-key":
            raise ProviderNotReadyError(
                "Google STT v2 needs GOOGLE_CLOUD_PROJECT set in Backend/.env.",
            )

        recognizer = self.recognizer or DEFAULT_GOOGLE_SPEECH_RECOGNIZER
        location = self.location or DEFAULT_GOOGLE_SPEECH_LOCATION
        request_body = self._build_v2_rest_request_body(
            audio_data,
            language,
            audio_metadata=audio_metadata,
            content_type=content_type,
        )
        query = urllib.parse.urlencode({"key": self.api_key})
        url = (
            "https://speech.googleapis.com/v2/projects/"
            f"{self._project_id}/locations/{location}/recognizers/{recognizer}:recognize?{query}"
        )
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
                f"Google STT v2 request failed: {exc.code} {error_message}",
            ) from exc

        parsed_response = json.loads(raw_response.decode("utf-8"))
        transcripts = [
            result["alternatives"][0]["transcript"].strip()
            for result in parsed_response.get("results", [])
            if result.get("alternatives")
        ]
        return " ".join(part for part in transcripts if part).strip()

    def _ensure_api_key(self):
        if not self.api_key:
            raise ProviderNotReadyError(
                "Google Cloud Speech-to-Text is not ready. Set GOOGLE_STT_API_KEY "
                "in Backend/.env.",
            )
        self._project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "api-key"

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

    def transcribe(
        self,
        audio_data: bytes,
        language: Optional[str] = None,
        content_type: Optional[str] = None,
    ):
        from voice_gateway.domain.contracts import TranscriptionResult

        self._ensure_api_key()

        chosen_language = language or self.default_language
        prepared_audio, audio_metadata = self._prepare_audio_for_google(audio_data)
        text = self._transcribe_via_api_key(
            prepared_audio,
            chosen_language,
            audio_metadata=audio_metadata,
            content_type=content_type,
        )
        source = "google_cloud_speech_v1_rest"
        auth_mode = "api_key"

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
        if not self.api_key:
            return "unavailable: api_key_missing"
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "api-key"
        if self.api_version == "v2" and project_id == "api-key":
            return "unavailable: google_cloud_project_missing_for_v2"
        return (
            f"configured: {self.api_version}:{project_id}/"
            f"{self.location}/{self.recognizer}/{self.model}/api_key"
        )


class OpenAILLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ):
        self.api_key = (
            api_key
            or _read_env_value("OPENAI_LLM_API_KEY")
            or _read_env_value("LLM_API_KEY")
            or _read_env_value("OPEN_AI_KEY")
            or ""
        ).strip().strip("'\"")
        self.model = (
            model
            or _read_env_value("OPENAI_LLM_MODEL")
            or _read_env_value("OPENAI_MODEL")
            or DEFAULT_OPENAI_MODEL
        ).strip()
        self.base_url = (
            base_url
            or _read_env_value("OPENAI_BASE_URL")
            or DEFAULT_OPENAI_BASE_URL
        ).rstrip("/")
        self.system_prompt = (
            system_prompt
            or _read_env_value("VOICE_GATEWAY_SYSTEM_PROMPT")
            or DEFAULT_OPENAI_SYSTEM_PROMPT
        ).strip()
        self.timeout = int(_read_env_value("OPENAI_TIMEOUT_SECONDS") or "60")

    def _normalize_reply(self, text: str) -> str:
        return " ".join(text.split()).strip()

    def _request_openai(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: Optional[dict] = None,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
        }
        if isinstance(response_format, dict) and response_format:
            payload["response_format"] = response_format
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_response = response.read()
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(error_body)
                error_message = parsed.get("error", {}).get("message", error_body)
            except json.JSONDecodeError:
                error_message = error_body or str(exc)
            raise RuntimeError(
                f"OpenAI request failed: {exc.code} {error_message}",
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI request error: {exc.reason}") from exc

        parsed_response = json.loads(raw_response.decode("utf-8"))
        try:
            content = parsed_response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenAI returned an unexpected response shape.") from exc
        return str(content).strip()

    def generate_reply_with_messages(
        self,
        *,
        messages: list[dict[str, str]],
        session_id: str,
        user_id: str,
        system_prompt: Optional[str] = None,
        include_history: bool = True,
        history_prompt: Optional[str] = None,
        store_history: bool = True,
        response_format: Optional[dict] = None,
    ):
        from voice_gateway.domain.contracts import LLMResult

        if not self.api_key:
            raise ProviderNotReadyError(
                "OpenAI is not ready. Set OPENAI_LLM_API_KEY in Backend/.env.",
            )

        payload_messages: list[dict[str, str]] = []
        normalized_system_prompt = " ".join((system_prompt or "").split()).strip()
        if normalized_system_prompt:
            payload_messages.append(
                {"role": "system", "content": normalized_system_prompt},
            )

        if include_history:
            payload_messages.extend(conversation_memory.get_history(user_id, session_id))

        for raw_message in messages:
            if not isinstance(raw_message, dict):
                continue
            role = str(raw_message.get("role", "")).strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = self._normalize_reply(str(raw_message.get("content", "")))
            if not content:
                continue
            payload_messages.append({"role": role, "content": content})

        if not payload_messages:
            raise ValueError("At least one message is required for OpenAI generation.")

        message = self._normalize_reply(
            self._request_openai(
                payload_messages,
                response_format=response_format,
            )
        )
        if not message:
            raise ValueError("OpenAI returned an empty response.")

        if store_history:
            prompt_for_history = self._normalize_reply(
                history_prompt
                or next(
                    (
                        str(item.get("content", ""))
                        for item in reversed(messages)
                        if str(item.get("role", "")).strip().lower() == "user"
                    ),
                    "",
                ),
            )
            if prompt_for_history:
                conversation_memory.append_turn(
                    user_id=user_id,
                    session_id=session_id,
                    user_text=prompt_for_history,
                    assistant_text=message,
                )

        return LLMResult(
            text=message,
            source="openai_chat_completions",
            warnings=[f"llm_model={self.model}"],
        )

    def generate_reply(self, prompt: str, session_id: str, user_id: str):
        return self.generate_reply_with_messages(
            system_prompt=self.system_prompt,
            messages=[{"role": "user", "content": prompt.strip()}],
            session_id=session_id,
            user_id=user_id,
            include_history=True,
            history_prompt=prompt,
            store_history=True,
        )

    def status(self) -> str:
        if not self.api_key:
            return "unavailable: api_key_missing"
        return f"configured: {self.model}"


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
        if self._voice is not None or self._load_error is not None:
            return

        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)

            if not self.model_path.exists():
                logger.info("Downloading Piper model to %s", self.model_path)
                urllib.request.urlretrieve(self.model_url, self.model_path)

            if not self.config_path.exists():
                logger.info("Downloading Piper config to %s", self.config_path)
                urllib.request.urlretrieve(self.config_url, self.config_path)

            from piper.voice import PiperVoice

            logger.info("Loading Piper voice model from %s", self.model_path)
            self._voice = PiperVoice.load(
                str(self.model_path),
                config_path=str(self.config_path),
            )

            logger.info("Piper voice model loaded successfully.")

        except Exception as exc:
            self._load_error = str(exc)
            logger.exception("Failed to load Piper voice: %s", exc)

    def _normalize_text(self, text: str) -> str:
        cleaned = unicodedata.normalize("NFC", " ".join(text.split())).strip()
        if cleaned and cleaned[-1] not in ".!?":
            cleaned = f"{cleaned}."
        return cleaned

    def synthesize(self, text: str, voice_id: Optional[str] = None):
        from voice_gateway.domain.contracts import SpeechSynthesisResult

        self._ensure_voice()

        if self._voice is None:
            raise ProviderNotReadyError(
                "Piper is not ready. Install the dependency and verify the "
                "voice model can load.",
            )

        normalized_text = self._normalize_text(text)

        if not normalized_text:
            raise ValueError("Text is required for speech synthesis.")

        try:
            chunks = list(self._voice.synthesize(normalized_text))

            if not chunks:
                raise ProviderNotReadyError(
                    "Piper did not generate audio for the provided text.",
                )

            wav_io = io.BytesIO()
            with wave.open(wav_io, "wb") as wav_file:
                first_chunk = chunks[0]
                wav_file.setframerate(first_chunk.sample_rate)
                wav_file.setsampwidth(first_chunk.sample_width)
                wav_file.setnchannels(first_chunk.sample_channels)

                for chunk in chunks:
                    wav_file.writeframes(chunk.audio_int16_bytes)

            audio_bytes = wav_io.getvalue()

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
