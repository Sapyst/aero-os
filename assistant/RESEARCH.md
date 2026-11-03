# Aero Assistant Voice Call: Research & Architectural Plan (Part 1)

## 1. Executive Summary & Hardware Context
- **Operating System:** Arch Linux x86_64
- **Audio Server:** PipeWire 1.6.10 with WirePlumber
- **GPU Acceleration:** NVIDIA GeForce RTX 5070 Laptop GPU + AMD Radeon 890M (Vulkan backends)
- **Audio Endpoints:**
  - Default Input (Mic): `fifine Microphone Analog Stereo` (alsa source 83)
  - Default Output (Speaker): `H848 Wireless headset Analog Stereo` (alsa sink 70)

---

## 2. Speech-to-Text (STT) Comparison

| Dimension | `whisper.cpp` (`whisper-cli` / `whisper-server`) | `janas-server --speech-model` |
| :--- | :--- | :--- |
| **License** | MIT | GPL-3.0 |
| **Availability** | Installed natively via Arch package (`whisper-cpp 1.9.5-1`) | Installed in `~/.local/share/aero-assistant/janas/janas/janas-server` |
| **Model Format** | GGML (`.bin`) | JNS (`.jns`) converted via `hf2jns_whisper` |
| **Installed Model** | `ggml-large-v3-turbo-q5_0.bin` (548 MB) in `~/.local/share/arch-quilly/takery/models/gpu-whisper-turbo/` | None currently converted |
| **GPU Acceleration** | Native Vulkan on RTX 5070 (`libggml-vulkan.so`) | Vulkan / CPU via Janas engine |
| **Inference Latency** | **360 ms – 610 ms** (measured on local RTX 5070) | ~400 ms – 700 ms |
| **Language Detection** | Built-in (`-dl`, `-l auto`): 97% confidence on PT-BR, 99% on EN | Supported via OpenAI `/v1/audio/transcriptions` API |
| **Memory Footprint** | **0 MB** when call closed; ~570 MB VRAM when running | Resides with Janas daemon if unified, or 0 MB if on-demand |
| **Structured Output** | Native `-oj` produces JSON with detected language, confidence, timestamps | Standard OpenAI transcription JSON |

**STT Recommendation:**
Use `whisper.cpp` (`whisper-cli` or lightweight ephemeral process during active call). It runs in ~400ms on the local RTX 5070 GPU, delivers 97–99% language detection accuracy for Portuguese and English, and completely terminates when the call ends, leaving **0 MB resident memory overhead**.

---

## 3. Text-to-Speech (TTS) Comparison

| Dimension | `espeak-ng` | `Piper` (piper-tts) | `Kokoro` (Kokoro-82M) |
| :--- | :--- | :--- | :--- |
| **License** | GPL-3.0 | MIT | Apache-2.0 |
| **Status on Machine** | Installed (`/usr/bin/espeak-ng 1.52.0-1`) | Available via Steam/Proton shared libs or standalone binary | Experimental Python / ONNX dependency |
| **Voices for PT / EN** | `pt-br`, `pt`, `en-us`, `en-gb` built-in | `pt_BR-faber-medium`, `en_US-lessac-medium` | Strong EN, nascent/unstable PT-BR |
| **Model / Disk Size** | ~5 MB total system package | ~30–50 MB per ONNX voice file | ~350 MB model file |
| **Synthesis Latency** | **8 ms – 10 ms** (ultra-low latency) | ~120 ms – 250 ms | ~300 ms – 800 ms (CPU) |
| **Memory Usage** | **< 10 MB RAM** | ~60 MB – 100 MB RAM | ~400 MB – 800 MB RAM |
| **Naturalness / Tone** | Synthetic / robotic formant synthesis | High naturalness, neural acoustic model | Very high naturalness (primarily English) |

**TTS Recommendation:**
Adopt a tiered offline engine:
1. Primary built-in offline engine: `espeak-ng` (`pt-br` and `en-us`). It is guaranteed installed, has instant **8ms latency**, zero extra disk footprint, and requires no downloads.
2. High-fidelity neural voice provider: `Piper` voice models cataloged in `models.json` with user consent download flow.

---

## 4. Voice Activity Detection (VAD) Evaluation

| Approach | Latency | RAM | Noise Immunity | Implementation Details |
| :--- | :--- | :--- | :--- | :--- |
| **Energy / RMS Hysteresis** | < 0.1 ms | 0 MB | Good in domestic / desk environments | Frame RMS calculation (16kHz, 20ms chunks) with speech onset and hangover decay. Zero external dependencies. |
| **Silero VAD (ONNX)** | ~5 ms | ~2 MB | Excellent across all noise profiles | Lightweight 2MB ONNX model. Supported natively by Janas and Whisper. |

**VAD Recommendation:**
Hybrid strategy: Fast energy-based RMS frame gating for real-time microphone visualization (sphere effect) and speech onset detection, paired with Whisper's internal VAD / silence segmentation to slice coherent spoken utterances.

---

## 5. Audio Architecture (PipeWire Integration)

- **Capture:**
  `pw-record --rate 16000 --channels 1 --format s16 <pipe/wav>`
  Streams from `@DEFAULT_SOURCE@` (Fifine Microphone) in 16kHz 16-bit mono PCM directly compatible with STT.
- **Playback:**
  `pw-play <wav>`
  Outputs to `@DEFAULT_SINK@` (H848 Headset).
- **Barge-in Protection:**
  When assistant begins speaking (`pw-play`), audio output is active. An acoustic echo loop is prevented by:
  1. Setting assistant output stream property `media.role=assistant`.
  2. Muting microphone analysis or subtracting playback signal while assistant audio stream is playing.
  3. Barge-in detection: user speech exceeding an elevated speech-energy threshold immediately aborts `pw-play` via `SIGTERM` / `SIGKILL` and switches state back to listening.
- **Ducking:**
  `wpctl` / `pactl` stream volume adjustment: ducking non-assistant sink inputs during playback and restoring immediately afterward.

---

## 6. Language Routing Safety Policy

- **Language Model Scope:** The LLM operates **strictly in English**. It only receives English and only responds in English.
- **Portuguese Hard Separation:**
  1. Audio transcribed with language detection.
  2. If detected language is `pt`, or confidence is < 0.85, or vocabulary contains Portuguese particles/words, request is routed exclusively to **Portuguese Rule Engine**.
  3. Under NO circumstance does a Portuguese request ever reach `janas-server` / the LLM.
  4. If no rule matches, the request is passed directly to the deterministic **"Cannot Answer" Composer** in Portuguese.
