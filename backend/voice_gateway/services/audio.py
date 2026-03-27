import io
import math
import wave
from dataclasses import dataclass, field
from typing import Optional


DEFAULT_VAD_FRAME_MS = 30
DEFAULT_VAD_MIN_SPEECH_MS = 180
DEFAULT_VAD_MAX_SILENCE_MS = 540
DEFAULT_VAD_PADDING_MS = 150


@dataclass
class PreparedAudio:
    audio_bytes: bytes
    content_type: Optional[str]
    original_duration_ms: Optional[int] = None
    processed_duration_ms: Optional[int] = None
    vad_applied: bool = False
    speech_detected: bool = False
    warnings: list[str] = field(default_factory=list)


def _decode_sample(chunk: bytes, sample_width: int) -> int:
    if sample_width == 1:
        return chunk[0] - 128
    return int.from_bytes(chunk, byteorder="little", signed=True)


def _encode_sample(sample: int, sample_width: int) -> bytes:
    if sample_width == 1:
        return bytes([max(0, min(255, sample + 128))])

    min_value = -(1 << (sample_width * 8 - 1))
    max_value = (1 << (sample_width * 8 - 1)) - 1
    clipped = max(min_value, min(max_value, int(sample)))
    return clipped.to_bytes(sample_width, byteorder="little", signed=True)


def _duration_ms(frame_count: int, sample_rate: int) -> int:
    if sample_rate <= 0:
        return 0
    return int((frame_count / sample_rate) * 1000)


def _wav_to_mono_samples(
    audio_bytes: bytes,
) -> tuple[list[int], int, int, int, int, str]:
    with wave.open(io.BytesIO(audio_bytes), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        frame_count = reader.getnframes()
        compression = reader.getcomptype()
        frames = reader.readframes(frame_count)

    if compression != "NONE":
        raise ValueError("Compressed WAV audio is not supported for VAD.")

    if sample_width not in {1, 2, 4}:
        raise ValueError(f"Unsupported WAV sample width for VAD: {sample_width}.")

    frame_size = sample_width * channels
    samples: list[int] = []
    for offset in range(0, len(frames), frame_size):
        frame = frames[offset : offset + frame_size]
        if len(frame) < frame_size:
            break

        total = 0
        for channel_index in range(channels):
            start = channel_index * sample_width
            total += _decode_sample(frame[start : start + sample_width], sample_width)
        samples.append(int(total / channels))

    return samples, sample_rate, sample_width, channels, frame_count, compression


def _sample_energy(samples: list[int]) -> float:
    if not samples:
        return 0.0

    squared_sum = 0
    for sample in samples:
        squared_sum += sample * sample
    return math.sqrt(squared_sum / len(samples))


def _detect_speech_window(
    samples: list[int],
    sample_rate: int,
    sample_width: int,
    frame_ms: int = DEFAULT_VAD_FRAME_MS,
    min_speech_ms: int = DEFAULT_VAD_MIN_SPEECH_MS,
    max_silence_ms: int = DEFAULT_VAD_MAX_SILENCE_MS,
    padding_ms: int = DEFAULT_VAD_PADDING_MS,
) -> Optional[tuple[int, int]]:
    if not samples or sample_rate <= 0:
        return None

    chunk_size = max(1, int(sample_rate * frame_ms / 1000))
    chunks = [
        samples[index : index + chunk_size]
        for index in range(0, len(samples), chunk_size)
        if samples[index : index + chunk_size]
    ]
    if not chunks:
        return None

    energies = [_sample_energy(chunk) for chunk in chunks]
    peak_energy = max(energies, default=0.0)
    if peak_energy <= 0:
        return None

    sorted_energies = sorted(energies)
    percentile_index = max(0, min(len(sorted_energies) - 1, int(len(sorted_energies) * 0.2)))
    noise_floor = sorted_energies[percentile_index]
    amplitude_floor = {
        1: 4.0,
        2: 420.0,
        4: 1_500_000.0,
    }
    threshold = max(
        noise_floor * 3.2,
        peak_energy * 0.14,
        amplitude_floor.get(sample_width, 420.0),
    )

    min_speech_frames = max(1, math.ceil(min_speech_ms / frame_ms))
    max_silence_frames = max(1, math.ceil(max_silence_ms / frame_ms))
    padding_frames = max(0, math.ceil(padding_ms / frame_ms))

    consecutive_voice = 0
    silence_after_voice = 0
    speech_started = False
    start_chunk = 0
    last_voice_chunk = 0

    for index, energy in enumerate(energies):
        is_voice = energy >= threshold

        if not speech_started:
            if is_voice:
                consecutive_voice += 1
                if consecutive_voice >= min_speech_frames:
                    speech_started = True
                    first_voice_chunk = index - consecutive_voice + 1
                    start_chunk = max(0, first_voice_chunk - padding_frames)
                    last_voice_chunk = index
            else:
                consecutive_voice = 0
            continue

        if is_voice:
            last_voice_chunk = index
            silence_after_voice = 0
            continue

        silence_after_voice += 1
        if silence_after_voice >= max_silence_frames:
            end_chunk = min(len(chunks), last_voice_chunk + 1 + padding_frames)
            return start_chunk * chunk_size, min(len(samples), end_chunk * chunk_size)

    if not speech_started:
        return None

    end_chunk = min(len(chunks), last_voice_chunk + 1 + padding_frames)
    return start_chunk * chunk_size, min(len(samples), end_chunk * chunk_size)


def _samples_to_wav_bytes(
    samples: list[int],
    sample_rate: int,
    sample_width: int,
) -> bytes:
    frame_bytes = bytearray()
    for sample in samples:
        frame_bytes.extend(_encode_sample(sample, sample_width))

    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(bytes(frame_bytes))
    return output.getvalue()


def prepare_audio_for_stt(
    audio_bytes: bytes,
    content_type: Optional[str] = None,
) -> PreparedAudio:
    if not audio_bytes:
        raise ValueError("Audio is required.")

    normalized_content_type = (content_type or "").split(";")[0].strip().lower() or None

    wav_content_types = {"audio/wav", "audio/wave", "audio/x-wav"}
    if normalized_content_type and normalized_content_type not in wav_content_types:
        return PreparedAudio(
            audio_bytes=audio_bytes,
            content_type=normalized_content_type,
            warnings=[
                "vad_skipped=non_wav_input",
            ],
        )

    try:
        samples, sample_rate, sample_width, _, frame_count, _ = _wav_to_mono_samples(
            audio_bytes,
        )
    except (wave.Error, ValueError):
        return PreparedAudio(
            audio_bytes=audio_bytes,
            content_type=normalized_content_type or "application/octet-stream",
            warnings=[
                "vad_skipped=wav_decode_failed",
            ],
        )

    original_duration_ms = _duration_ms(frame_count, sample_rate)
    speech_window = _detect_speech_window(samples, sample_rate, sample_width)
    if speech_window is None:
        raise ValueError("No speech detected in the uploaded audio.")

    start_sample, end_sample = speech_window
    trimmed_samples = samples[start_sample:end_sample]
    trimmed_audio = _samples_to_wav_bytes(trimmed_samples, sample_rate, sample_width)
    processed_duration_ms = _duration_ms(len(trimmed_samples), sample_rate)

    return PreparedAudio(
        audio_bytes=trimmed_audio,
        content_type="audio/wav",
        original_duration_ms=original_duration_ms,
        processed_duration_ms=processed_duration_ms,
        vad_applied=True,
        speech_detected=True,
        warnings=[
            "vad_applied=true",
            f"original_duration_ms={original_duration_ms}",
            f"speech_duration_ms={processed_duration_ms}",
        ],
    )
