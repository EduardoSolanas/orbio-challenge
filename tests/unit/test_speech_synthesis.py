"""Unit tests for TTS format negotiation, the input cap and PCM WAV assembly.

Covers the amended TTS contract (PC "TTS format and bounded synthesis
contract"): typed ``SynthesizedAudio``, configured ``response_format``, an
exact per-request character cap with ordered chunking, one attempt per chunk,
one shared deadline/permit, the aggregate 5 MiB cap and validated WAV assembly.
"""

from __future__ import annotations

import asyncio
import json
import struct
import wave
from dataclasses import replace
from io import BytesIO

import httpx
import pytest

from app.config import ServiceProfile, SpeechProfile, TextProfile
from app.errors import AppError
from app.providers import (
    SynthesizedAudio,
    assemble_wav,
    decode_pcm_wav,
    split_tts_text,
)
from tests.support.fakes import make_streaming_wav, make_wav
from tests.unit.provider_support import (
    LLM_PROFILE,
    RAG_PROFILE,
    STT_PROFILE,
    TTS_PROFILE,
    Clock,
    RoleTransport,
    SequenceClock,
    SleepRecorder,
    build_provider,
    streamed_response,
)

WAV_PROFILE = replace(TTS_PROFILE, audio_format="wav", max_input_chars=200)
MP3_PROFILE = replace(TTS_PROFILE, audio_format="mp3", max_input_chars=4000)
TTS_LIMIT = 5 * 1024 * 1024
ROLES = ("llm", "rag", "stt", "tts")


def profiles_with(tts: SpeechProfile) -> dict[str, ServiceProfile]:
    return {"llm": LLM_PROFILE, "rag": RAG_PROFILE, "stt": STT_PROFILE, "tts": tts}


def audio_responder(body: bytes, headers: dict[str, str] | None = None, status: int = 200):
    resolved = {"content-type": "audio/wav"} if headers is None else headers

    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        return streamed_response(body, status, resolved)

    return responder


def wav_geometry(data: bytes) -> tuple[int, int, int, bytes]:
    with wave.open(BytesIO(data)) as reader:
        channels, width, rate = (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
        )
        count = reader.getnframes()
        return channels, width, rate, reader.readframes(count)


def raw_wav(*, rate: int = 16000, channels: int = 1, width: int = 2, frames: int = 100) -> bytes:
    """A PCM WAV with explicit geometry, used for unsupported-format cases."""

    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"\x00" * frames * channels * width)
    return buffer.getvalue()


def sequence_responder(payloads: list[tuple[bytes, dict[str, str]]]):
    def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        body, headers = payloads[attempt - 1]
        return streamed_response(body, 200, headers)

    return responder


# ---------------------------------------------------------------------------
# Splitter: Unicode code points, exact preservation, configured limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("a", 200, ["a"]),
        ("a" * 199, 200, ["a" * 199]),
        ("a" * 200, 200, ["a" * 200]),
        ("a" * 201, 200, ["a" * 200, "a"]),
        ("a" * 4000, 200, ["a" * 200] * 20),
        ("a" * 4001, 200, ["a" * 200] * 20 + ["a"]),
    ],
)
def test_split_tts_text_counts_characters_and_keeps_order(text, limit, expected):
    assert split_tts_text(text, limit) == expected


def test_split_tts_text_preserves_unicode_and_whitespace_exactly():
    text = "  Grüße 👋🏽  \n\tZürich — naïve 日本語 "

    chunks = split_tts_text(text, 7)

    assert all(len(chunk) <= 7 for chunk in chunks)
    assert "".join(chunks) == text


def test_split_tts_text_never_produces_empty_chunks():
    assert split_tts_text("hello", 200) == ["hello"]


# ---------------------------------------------------------------------------
# WAV validation and assembly
# ---------------------------------------------------------------------------


def test_decode_pcm_wav_returns_geometry_and_frames():
    audio = make_wav(rate=16000, channels=1, seconds=0.5, peak=900)

    channels, width, rate, frames = decode_pcm_wav(audio)

    assert (channels, width, rate) == (1, 2, 16000)
    assert len(frames) == 8000 * 2  # 0.5 s at 16 kHz, one channel, 16-bit
    assert frames == wav_geometry(audio)[3]


def test_decode_pcm_wav_accepts_a_single_frame():
    audio = raw_wav(frames=1)

    channels, width, rate, frames = decode_pcm_wav(audio)

    assert (channels, width, rate) == (1, 2, 16000)
    assert frames == b"\x00\x00"


@pytest.mark.parametrize(
    ("audio", "message"),
    [
        (b"", "not a decodable PCM WAV"),
        (b"RIFF\x00\x00\x00\x00WAVE", "not a decodable PCM WAV"),
        (b"ID3-mp3-bytes", "not a decodable PCM WAV"),
        (b"\x00" * 64, "not a decodable PCM WAV"),
        (raw_wav(channels=3), "unsupported WAV geometry"),
        (raw_wav(width=1), "unsupported WAV geometry"),
        (raw_wav(width=4), "unsupported WAV geometry"),
        (raw_wav(rate=7999), "unsupported WAV sample rate"),
        (raw_wav(rate=48001), "unsupported WAV sample rate"),
        (raw_wav(frames=0), "empty or truncated WAV frames"),
        (make_wav()[:-8], "empty or truncated WAV frames"),
    ],
    ids=[
        "empty",
        "header-only",
        "mp3",
        "junk",
        "three-channels",
        "eight-bit",
        "thirty-two-bit",
        "rate-below-window",
        "rate-above-window",
        "no-frames",
        "truncated-data",
    ],
)
def test_decode_pcm_wav_reports_the_exact_rejection_reason(audio, message):
    with pytest.raises(ValueError) as caught:
        decode_pcm_wav(audio)

    assert str(caught.value) == message


@pytest.mark.parametrize("rate", [8000, 48000])
def test_decode_pcm_wav_accepts_the_sample_rate_window(rate):
    assert decode_pcm_wav(make_wav(rate=rate))[2] == rate


@pytest.mark.parametrize("channels", [1, 2])
def test_decode_pcm_wav_accepts_mono_and_stereo(channels):
    assert decode_pcm_wav(make_wav(channels=channels))[0] == channels


# ---------------------------------------------------------------------------
# Streaming WAV: Groq Orpheus sends 0xFFFFFFFF RIFF/data sizes
# ---------------------------------------------------------------------------


def test_decode_pcm_wav_accepts_groq_streaming_unspecified_sizes():
    audio = make_streaming_wav(rate=24000, frames=1600)

    channels, width, rate, frames = decode_pcm_wav(audio)

    assert (channels, width, rate) == (1, 2, 24000)
    assert frames == b"\x00" * (1600 * 2)


@pytest.mark.parametrize(
    "overrides",
    [
        {"riff_size": "finite"},
        {"data_size": "finite"},
        {"with_list": False},
    ],
    ids=["finite-riff-size", "finite-data-size", "no-list-chunk"],
)
def test_decode_pcm_wav_accepts_streaming_size_combinations(overrides):
    audio = make_streaming_wav(frames=800)
    if overrides.get("riff_size") == "finite":
        audio = make_streaming_wav(frames=800, riff_size=len(audio) - 8)
    elif overrides.get("data_size") == "finite":
        audio = make_streaming_wav(frames=800, data_size=800 * 2)
    else:
        audio = make_streaming_wav(frames=800, with_list=False)

    channels, width, rate, frames = decode_pcm_wav(audio)

    assert (channels, width, rate) == (1, 2, 24000)
    assert frames == b"\x00" * (800 * 2)


def test_decode_pcm_wav_accepts_streaming_stereo_at_the_rate_window():
    audio = make_streaming_wav(channels=2, rate=48000, frames=480)

    channels, width, rate, frames = decode_pcm_wav(audio)

    assert (channels, width, rate) == (2, 2, 48000)
    assert frames == b"\x00" * (480 * 2 * 2)


def float_format_wav() -> bytes:
    audio = bytearray(make_streaming_wav(with_list=False))
    audio[20:22] = struct.pack("<H", 3)  # WAVE_FORMAT_IEEE_FLOAT
    return bytes(audio)


def streaming_list_size_wav() -> bytes:
    audio = bytearray(make_streaming_wav())
    audio[40:44] = struct.pack("<L", 0xFFFFFFFF)  # LIST size unspecified
    return bytes(audio)


def test_decode_pcm_wav_accepts_streaming_with_an_odd_sized_padded_chunk():
    audio = make_streaming_wav(with_list=False)
    fmt_chunk = audio[12:36]
    odd_chunk = b"LIST" + struct.pack("<L", 1) + b"x" + b"\x00"
    audio = b"RIFF\xff\xff\xff\xffWAVE" + odd_chunk + fmt_chunk + audio[36:]

    channels, width, rate, frames = decode_pcm_wav(audio)

    assert (channels, width, rate) == (1, 2, 24000)
    assert frames == b"\x00" * (1600 * 2)


def test_decode_pcm_wav_accepts_streaming_with_many_empty_chunks():
    empty_chunks = b"".join(b"JUNK" + struct.pack("<L", 0) for _ in range(3300))
    audio = (
        b"RIFF\xff\xff\xff\xffWAVE"
        + empty_chunks
        + make_streaming_wav(with_list=False)[12:]
    )

    channels, width, rate, frames = decode_pcm_wav(audio)

    assert (channels, width, rate) == (1, 2, 24000)
    assert frames == b"\x00" * (1600 * 2)


@pytest.mark.parametrize(
    ("audio", "message"),
    [
        (make_streaming_wav(frames=0), "empty or truncated WAV frames"),
        (make_streaming_wav(frames=100) + b"\x00", "empty or truncated WAV frames"),
        (make_streaming_wav(frames=100, data_size=202), "empty or truncated WAV frames"),
        (make_streaming_wav(channels=3), "unsupported WAV geometry"),
        (make_streaming_wav(width=1), "unsupported WAV geometry"),
        (make_streaming_wav(width=4), "unsupported WAV geometry"),
        (make_streaming_wav(rate=7999), "unsupported WAV sample rate"),
        (make_streaming_wav(rate=48001), "unsupported WAV sample rate"),
        (
            make_streaming_wav(with_list=False)[:36],
            "not a decodable PCM WAV",
        ),
        (b"RIFF\xff\xff\xff\xffWAVEfmt ", "not a decodable PCM WAV"),
        (
            make_streaming_wav(with_list=False)[:20] + b"\x00" * 4,
            "not a decodable PCM WAV",
        ),
        (
            b"RIFF\xff\xff\xff\xffWAVE"
            + b"data\x04\x00\x00\x00\x00\x00\x00\x00",
            "not a decodable PCM WAV",
        ),
        (float_format_wav(), "not a decodable PCM WAV"),
        (streaming_list_size_wav(), "not a decodable PCM WAV"),
        (make_streaming_wav(channels=0), "not a decodable PCM WAV"),
        (make_streaming_wav(width=0), "not a decodable PCM WAV"),
        (make_streaming_wav(width=2, bits=17), "empty or truncated WAV frames"),
        (make_streaming_wav(width=2, bits=0xFFFF), "empty or truncated WAV frames"),
        (b"", "not a decodable PCM WAV"),
        (b"ID3-mp3-bytes", "not a decodable PCM WAV"),
        (b"RIFF\xff\xff\xff\xffWAVE", "not a decodable PCM WAV"),
        (
            b"RIFF\xff\xff\xff\xffXXXX" + make_streaming_wav()[12:],
            "not a decodable PCM WAV",
        ),
        (
            b"XXXX\xff\xff\xff\xffWAVE" + make_streaming_wav()[12:],
            "not a decodable PCM WAV",
        ),
    ],
    ids=[
        "empty-data",
        "incomplete-frame",
        "declared-data-exceeds-buffer",
        "three-channels",
        "eight-bit",
        "thirty-two-bit",
        "rate-below-window",
        "rate-above-window",
        "fmt-only",
        "truncated-header",
        "truncated-fmt-chunk",
        "data-before-fmt",
        "float-format",
        "streaming-list-size",
        "zero-channels",
        "zero-bits",
        "seventeen-bits",
        "signed-overflow-bits",
        "empty",
        "mp3",
        "header-only",
        "wrong-wave-tag",
        "wrong-riff-tag",
    ],
)
def test_decode_pcm_wav_rejects_invalid_streaming_audio(audio, message):
    with pytest.raises(ValueError) as caught:
        decode_pcm_wav(audio)

    assert str(caught.value) == message


@pytest.mark.parametrize(
    "audio",
    [
        raw_wav(rate=7999),
        raw_wav(rate=48001),
        raw_wav(channels=3),
        raw_wav(width=1),
        raw_wav(width=4),
        raw_wav(frames=0),
        make_wav()[:-8],
        b"",
        b"RIFF\x00\x00\x00\x00WAVE",
        b"ID3-mp3-bytes",
        b"\x00" * 64,
    ],
    ids=[
        "rate-below-window",
        "rate-above-window",
        "three-channels",
        "eight-bit",
        "thirty-two-bit",
        "no-frames",
        "truncated-data",
        "empty",
        "header-only",
        "mp3",
        "junk",
    ],
)
def test_decode_pcm_wav_rejects_invalid_or_unsupported_audio(audio):
    with pytest.raises(ValueError):
        decode_pcm_wav(audio)


def test_assemble_wav_writes_one_header_and_appends_frames_in_order():
    first = make_wav(rate=16000, seconds=0.1, peak=1000)
    second = make_wav(rate=16000, seconds=0.2, peak=500)
    first_geometry = decode_pcm_wav(first)
    second_geometry = decode_pcm_wav(second)

    assembled = assemble_wav([first_geometry, second_geometry])

    channels, width, rate, frames = wav_geometry(assembled)
    assert (channels, width, rate) == (1, 2, 16000)
    assert frames == first_geometry[3] + second_geometry[3]
    assert assembled[:4] == b"RIFF" and assembled[8:12] == b"WAVE"
    assert len(assembled) == 44 + len(frames)


def test_assemble_wav_accepts_a_single_chunk_unchanged():
    part = decode_pcm_wav(make_wav(seconds=0.1))

    assert wav_geometry(assemble_wav([part]))[3] == part[3]


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ([], "no WAV chunks to assemble"),
        (
            [decode_pcm_wav(make_wav()), decode_pcm_wav(make_wav(rate=22050))],
            "mismatched WAV geometry between chunks",
        ),
        (
            [(1, 2, 16000, b"\x00" * (TTS_LIMIT + 1))],
            "assembled WAV exceeds the response cap",
        ),
    ],
    ids=["no-chunks", "geometry-mismatch", "over-cap"],
)
def test_assemble_wav_reports_the_exact_rejection_reason(parts, message):
    with pytest.raises(ValueError) as caught:
        assemble_wav(parts)

    assert str(caught.value) == message


def test_assemble_wav_accepts_frames_exactly_at_the_cap():
    frames = b"\x00" * TTS_LIMIT

    assembled = assemble_wav([(1, 2, 16000, frames)])

    assert len(assembled) == 44 + TTS_LIMIT


@pytest.mark.parametrize(
    "second",
    [
        make_wav(rate=22050),
        make_wav(channels=2),
    ],
    ids=["rate-mismatch", "channel-mismatch"],
)
def test_assemble_wav_rejects_mismatched_geometry(second):
    with pytest.raises(ValueError):
        assemble_wav([decode_pcm_wav(make_wav()), decode_pcm_wav(second)])


# ---------------------------------------------------------------------------
# Format negotiation: configured response_format, accepted media types
# ---------------------------------------------------------------------------


async def test_wav_profile_requests_wav_and_returns_typed_wav_audio():
    text = "A" * 201
    payloads = [
        (make_wav(seconds=0.1), {"content-type": "audio/wav"}),
        (make_wav(seconds=0.2), {"content-type": "audio/wav"}),
    ]
    provider, recorders = build_provider(
        {"tts": sequence_responder(payloads)}, profiles=profiles_with(WAV_PROFILE)
    )

    result = await provider.synthesize(text)

    assert isinstance(result, SynthesizedAudio)
    assert result.media_type == "audio/wav"
    assert [json.loads(request.content) for request in recorders["tts"].requests] == [
        {
            "model": "tts-model",
            "voice": "alloy",
            "input": "A" * 200,
            "response_format": "wav",
        },
        {
            "model": "tts-model",
            "voice": "alloy",
            "input": "A",
            "response_format": "wav",
        },
    ]
    assert wav_geometry(result.data)[3] == (
        wav_geometry(payloads[0][0])[3] + wav_geometry(payloads[1][0])[3]
    )


async def test_wav_profile_at_the_cap_uses_a_single_request():
    provider, recorders = build_provider(
        {"tts": audio_responder(make_wav(seconds=0.1))}, profiles=profiles_with(WAV_PROFILE)
    )

    result = await provider.synthesize("B" * 200)

    assert len(recorders["tts"].requests) == 1
    assert json.loads(recorders["tts"].requests[0].content)["input"] == "B" * 200
    assert result.media_type == "audio/wav"


async def test_mp3_profile_requests_mp3_and_returns_typed_mp3_audio():
    provider, recorders = build_provider(
        {"tts": audio_responder(b"ID3-mp3", {"content-type": "audio/mpeg"})},
        profiles=profiles_with(MP3_PROFILE),
    )

    result = await provider.synthesize("Saved locally.")

    assert result == SynthesizedAudio(data=b"ID3-mp3", media_type="audio/mpeg")
    assert json.loads(recorders["tts"].requests[0].content) == {
        "model": "tts-model",
        "voice": "alloy",
        "input": "Saved locally.",
        "response_format": "mp3",
    }


@pytest.mark.parametrize(
    "content_type",
    [
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "Audio/WAV",
        "audio/wav; charset=binary",
    ],
)
async def test_wav_profile_accepts_the_wav_media_types(content_type: str):
    audio = make_wav(seconds=0.1)
    provider, _ = build_provider(
        {"tts": audio_responder(audio, {"content-type": content_type})},
        profiles=profiles_with(WAV_PROFILE),
    )

    assert (await provider.synthesize("hello")).data == audio


@pytest.mark.parametrize(
    "content_type",
    ["audio/mpeg", "audio/mp3", "text/plain", "application/json", "", None],
)
async def test_wav_profile_rejects_non_wav_media_types(content_type):
    headers = {} if content_type is None else {"content-type": content_type}
    provider, recorders = build_provider(
        {"tts": audio_responder(make_wav(), headers)},
        profiles=profiles_with(WAV_PROFILE),
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 1


async def test_mp3_profile_rejects_wav_media_type():
    provider, recorders = build_provider(
        {"tts": audio_responder(make_wav(), {"content-type": "audio/wav"})},
        profiles=profiles_with(MP3_PROFILE),
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 1


# ---------------------------------------------------------------------------
# Chunking failure rules: >20 chunks, mismatched geometry, partial discard
# ---------------------------------------------------------------------------


async def test_more_than_twenty_chunks_is_rejected_before_any_paid_call():
    profile = replace(WAV_PROFILE, max_input_chars=1)
    provider, recorders = build_provider(
        {"tts": audio_responder(make_wav())}, profiles=profiles_with(profile)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 21)

    assert caught.value.code == "provider_configuration"
    assert caught.value.retryable is False
    assert recorders["tts"].requests == []


async def test_mp3_profile_with_a_smaller_cap_is_rejected_before_any_paid_call():
    profile = replace(MP3_PROFILE, max_input_chars=200)
    provider, recorders = build_provider(
        {"tts": audio_responder(b"ID3")}, profiles=profiles_with(profile)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 201)

    assert caught.value.code == "provider_configuration"
    assert recorders["tts"].requests == []


async def test_mismatched_chunk_geometry_is_invalid_and_discards_partial_audio():
    payloads = [
        (make_wav(rate=16000, seconds=0.1), {"content-type": "audio/wav"}),
        (make_wav(rate=22050, seconds=0.1), {"content-type": "audio/wav"}),
    ]
    provider, recorders = build_provider(
        {"tts": sequence_responder(payloads)}, profiles=profiles_with(WAV_PROFILE)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 201)

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 2


async def test_invalid_wav_chunk_is_invalid_provider_response():
    payloads = [
        (make_wav(seconds=0.1), {"content-type": "audio/wav"}),
        (b"not-a-wav", {"content-type": "audio/wav"}),
    ]
    provider, recorders = build_provider(
        {"tts": sequence_responder(payloads)}, profiles=profiles_with(WAV_PROFILE)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 201)

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 2


async def test_empty_wav_body_is_invalid_provider_response():
    provider, recorders = build_provider(
        {"tts": audio_responder(b"")}, profiles=profiles_with(WAV_PROFILE)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 1


async def test_a_later_chunk_failure_discards_earlier_audio():
    payloads = [
        (make_wav(seconds=0.1), {"content-type": "audio/wav"}),
        (b'{"error": "unavailable"}', {"content-type": "application/json"}),
    ]
    provider, recorders = build_provider(
        {"tts": sequence_responder(payloads)}, profiles=profiles_with(WAV_PROFILE)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 201)

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 2


async def test_aggregate_raw_bytes_above_five_mib_is_rejected():
    payloads = [
        (make_wav(seconds=0.1), {"content-type": "audio/wav"}),
        (b"x" * TTS_LIMIT, {"content-type": "audio/wav"}),
    ]
    provider, recorders = build_provider(
        {"tts": sequence_responder(payloads)}, profiles=profiles_with(WAV_PROFILE)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 201)

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 2


async def test_compressed_wav_success_is_rejected_without_decompression():
    import gzip

    provider, recorders = build_provider(
        {
            "tts": audio_responder(
                gzip.compress(make_wav()),
                {"content-type": "audio/wav", "content-encoding": "gzip"},
            )
        },
        profiles=profiles_with(WAV_PROFILE),
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("hello")

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 1


async def test_twenty_chunks_at_the_cap_are_accepted():
    provider, recorders = build_provider(
        {"tts": audio_responder(make_wav(seconds=0.1))}, profiles=profiles_with(WAV_PROFILE)
    )

    result = await provider.synthesize("A" * 4000)

    assert len(recorders["tts"].requests) == 20
    assert result.media_type == "audio/wav"
    assert len(wav_geometry(result.data)[3]) == 20 * 1600 * 2


def wav_of_size(total: int) -> bytes:
    """A valid mono 16 kHz PCM WAV whose encoded size is exactly ``total`` bytes."""

    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00" * (total - 44))
    return buffer.getvalue()


async def test_aggregate_raw_bytes_exactly_at_the_cap_are_accepted():
    payloads = [
        (wav_of_size(1044), {"content-type": "audio/wav"}),
        (wav_of_size(TTS_LIMIT - 1044), {"content-type": "audio/wav"}),
    ]
    provider, recorders = build_provider(
        {"tts": sequence_responder(payloads)}, profiles=profiles_with(WAV_PROFILE)
    )

    result = await provider.synthesize("A" * 201)

    assert len(recorders["tts"].requests) == 2
    assert 0 < len(result.data) <= TTS_LIMIT


async def test_aggregate_raw_bytes_above_the_cap_with_valid_wav_chunks_is_rejected():
    payloads = [
        (wav_of_size(1044), {"content-type": "audio/wav"}),
        (wav_of_size(TTS_LIMIT + 40 - 1044), {"content-type": "audio/wav"}),
    ]
    provider, recorders = build_provider(
        {"tts": sequence_responder(payloads)}, profiles=profiles_with(WAV_PROFILE)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 201)

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 2


async def test_raw_byte_total_accumulates_across_three_chunks():
    sizes = [
        3 * 1024 * 1024,
        1024 * 1024,
        TTS_LIMIT + 40 - 3 * 1024 * 1024 - 1024 * 1024,
    ]
    payloads = [(wav_of_size(size), {"content-type": "audio/wav"}) for size in sizes]
    provider, recorders = build_provider(
        {"tts": sequence_responder(payloads)}, profiles=profiles_with(WAV_PROFILE)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 401)

    assert caught.value.code == "invalid_provider_response"
    assert len(recorders["tts"].requests) == 3


# ---------------------------------------------------------------------------
# Attempts, deadline and permit across chunks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_each_chunk_is_a_single_attempt_without_retry(status: int):
    sleeps = SleepRecorder()
    provider, recorders = build_provider(
        {"tts": audio_responder(b"", {"content-type": "text/plain"}, status=status)},
        profiles=profiles_with(WAV_PROFILE),
        sleep=sleeps,
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 201)

    assert caught.value.code == "provider_unavailable"
    assert len(recorders["tts"].requests) == 1
    assert sleeps.calls == []


async def test_chunks_share_one_operation_deadline():
    provider, recorders = build_provider(
        {"tts": audio_responder(make_wav(seconds=0.1))},
        profiles=profiles_with(WAV_PROFILE),
        clock=SequenceClock([1000.0, 1001.0, 1020.0]),
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("A" * 201)

    assert caught.value.code == "provider_timeout"
    assert len(recorders["tts"].requests) == 1


async def test_one_permit_is_held_across_every_chunk():
    entered_second = asyncio.Event()
    release_second = asyncio.Event()
    calls = 0

    async def responder(request: httpx.Request, attempt: int) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered_second.set()
            await release_second.wait()
        return streamed_response(make_wav(seconds=0.1), 200, {"content-type": "audio/wav"})

    provider, _ = build_provider(
        {"tts": responder}, profiles=profiles_with(WAV_PROFILE), capacity=1
    )

    task = asyncio.create_task(provider.synthesize("A" * 201))
    await asyncio.wait_for(entered_second.wait(), timeout=2)

    with pytest.raises(AppError) as caught:
        await provider.synthesize("B")
    assert caught.value.code == "provider_busy"

    release_second.set()
    result = await task
    assert result.media_type == "audio/wav"


async def test_empty_text_is_rejected_without_a_wire_call():
    provider, recorders = build_provider(
        {"tts": audio_responder(make_wav())}, profiles=profiles_with(WAV_PROFILE)
    )

    with pytest.raises(AppError) as caught:
        await provider.synthesize("")

    assert caught.value.code == "invalid_provider_response"
    assert recorders["tts"].requests == []


def test_synthesized_audio_is_immutable():
    audio = SynthesizedAudio(data=b"bytes", media_type="audio/wav")

    with pytest.raises(Exception):
        audio.data = b"other"  # type: ignore[misc]
