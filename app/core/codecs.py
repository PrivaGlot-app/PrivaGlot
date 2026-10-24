"""
Audio codecs at the kernel boundary.

Channels hand over raw bytes in whatever format their platform produces
(browser WebM/Opus, Feishu opus, ...) and get 16 kHz mono WAV back for the
ASR engine; the reverse direction (WAV → ogg/opus) serves voice-message
channels such as Feishu. Everything runs through ffmpeg in a worker thread
so the event loop is never blocked by a conversion.

The WAV helpers (duration / merge / split) exist for the voice-message
channels: a whole teacher turn is one voice bar, chunked when it exceeds
the platform's practical voice length.
"""
import asyncio
import os
import shutil
import struct
import subprocess
import tempfile

from ..logging_config import log as logger

FFMPEG_TIMEOUT = 10


def _require_ffmpeg():
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "本机没有找到 ffmpeg，无法把录音转成可识别的格式。"
            "Windows 用户请重新运行「一键启动.bat」自动安装；"
            "手动安装的用户请到 https://www.gyan.dev/ffmpeg/builds/ 下载并把 bin 目录加入 PATH。"
        )


def _run_ffmpeg(args: list, timeout: int = FFMPEG_TIMEOUT) -> None:
    """Run one ffmpeg command; RuntimeError with the stderr tail on failure."""
    _require_ffmpeg()
    proc = subprocess.run(
        ["ffmpeg", *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {proc.returncode}"
        raise RuntimeError(f"录音转换失败（ffmpeg 返回 {proc.returncode}）：{tail[:200]}")


async def to_wav_16k(data: bytes, ext: str = ".webm") -> bytes:
    """Normalize any channel-native recording to 16 kHz mono WAV.

    `ext` is only a demuxer hint for the temp file name. Feishu voice files
    are opus in a container that ffmpeg usually sniffs correctly; if the
    sniff fails we retry with explicit demuxers before giving up.
    """
    ext = ext if ext.startswith(".") else f".{ext}"

    def _convert() -> bytes:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(data)
            tmp_in = f.name
        # a distinct output path: when ext is already ".wav" a naive
        # in-place replace would point ffmpeg's input and output at the
        # same file and fail with a truncated-read error
        tmp_out = tmp_in + "-out.wav"
        try:
            attempts = ["", "-f opus", "-f ogg"]
            last_error: RuntimeError | None = None
            for fmt in attempts:
                args = (fmt.split() if fmt else []) + [
                    "-i", tmp_in, "-ar", "16000", "-ac", "1", "-f", "wav",
                    "-y", tmp_out]
                try:
                    _run_ffmpeg(args)
                    with open(tmp_out, "rb") as f:
                        return f.read()
                except RuntimeError as e:
                    last_error = e
            raise last_error or RuntimeError("录音转换失败")
        finally:
            for p in [tmp_in, tmp_out]:
                if os.path.exists(p):
                    os.unlink(p)

    return await asyncio.to_thread(_convert)


def wav_duration_seconds(wav_bytes: bytes) -> float:
    """Duration of a WAV buffer from its header; assumes 24 kHz/16-bit mono
    when the header cannot be parsed."""
    try:
        if len(wav_bytes) >= 44 and wav_bytes[:4] == b"RIFF":
            byte_rate = struct.unpack("<I", wav_bytes[28:32])[0]
            if byte_rate > 0:
                return round((len(wav_bytes) - 44) / byte_rate, 1)
    except Exception:
        pass
    return round(len(wav_bytes) / (24000 * 2), 1)


def merge_wavs(wavs: list[bytes]) -> bytes:
    """Concatenate TTS sentence WAVs into one WAV (re-encoding to plain PCM
    so differing source parameters cannot corrupt the result)."""
    if not wavs:
        return b""
    if len(wavs) == 1:
        return wavs[0]

    def _merge() -> bytes:
        with tempfile.TemporaryDirectory() as td:
            list_path = os.path.join(td, "list.txt")
            lines = []
            for i, chunk in enumerate(wavs):
                part = os.path.join(td, f"part{i:03d}.wav")
                with open(part, "wb") as f:
                    f.write(chunk)
                lines.append(f"file '{part}'")
            with open(list_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            out = os.path.join(td, "merged.wav")
            _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path,
                         "-c:a", "pcm_s16le", "-y", out], timeout=30)
            with open(out, "rb") as f:
                return f.read()

    return _merge()


def split_wav(wav_bytes: bytes, max_seconds: float) -> list[bytes]:
    """Split one WAV into consecutive parts of at most `max_seconds`."""
    total = wav_duration_seconds(wav_bytes)
    if total <= max_seconds:
        return [wav_bytes]

    def _split() -> list[bytes]:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.wav")
            with open(src, "wb") as f:
                f.write(wav_bytes)
            parts = []
            start = 0.0
            i = 0
            while start < total - 0.01:
                dur = min(max_seconds, total - start)
                out = os.path.join(td, f"seg{i:03d}.wav")
                _run_ffmpeg(["-i", src, "-ss", f"{start:.2f}", "-t",
                             f"{dur:.2f}", "-c:a", "pcm_s16le", "-y", out])
                with open(out, "rb") as f:
                    parts.append(f.read())
                start += dur
                i += 1
            return parts

    return _split()


def wav_to_opus(wav_bytes: bytes) -> bytes:
    """WAV → ogg/opus (16 kHz mono), the voice-message format Feishu expects."""
    _require_ffmpeg()

    def _convert() -> bytes:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.wav")
            out = os.path.join(td, "out.opus")
            with open(src, "wb") as f:
                f.write(wav_bytes)
            _run_ffmpeg(["-i", src, "-c:a", "libopus", "-b:a", "24k",
                         "-ar", "16000", "-ac", "1", "-y", out])
            with open(out, "rb") as f:
                return f.read()

    return _convert()


async def wav_to_opus_async(wav_bytes: bytes) -> bytes:
    return await asyncio.to_thread(wav_to_opus, wav_bytes)
