# Encoding and Video Compression Engineering

## 1. The Challenge: Surviving YouTube's Lossy Pipeline

YouTube re-encodes all uploaded media using aggressive video compression algorithms (H.264, VP9, and AV1). These encoders are designed for human eyes, which tolerate loss of high-frequency details, slight color bleeding, and macroblock smoothing. Databases, however, require bit-for-bit exactness.

This document outlines the engineering solutions implemented to survive YouTube's lossy compression.

---

## 2. Grayscale vs. RGB (Chroma Subsampling)

Many early projects attempted to pack 3 bits per pixel using RGB color channels (e.g. Red, Green, Blue). **This always fails reliably.**

### Why RGB Fails:
YouTube converts all uploads to **YUV 4:2:0 chroma subsampling**:
- **Luminance ($Y$)** is retained at full resolution ($1920 \times 1080$).
- **Chrominance ($U, V$)** is downsampled by half in both horizontal and vertical dimensions ($960 \times 540$).

Color channels suffer $>13\%$ distortion, creating catastrophic bit error rates across color boundaries. By contrast, pure **black-and-white (grayscale)** relies solely on the $Y$ channel, exhibiting $<2\%$ distortion under standard H.264 profiles.

---

## 3. The Bitrate Cap & Temporal Frame Repetition

### The Problem
Pure random black-and-white static has maximum spatial and temporal entropy. When every frame is unique noise:
- Modern codecs cannot find motion vectors or reuse macroblocks.
- YouTube's bitrate limiter (typically capped around 2.5–4.5 Mbps for 1080p) chokes.
- The encoder aggressively blurs and quantizes the image, destroying up to 90% of frames.

### The Solution: Temporal Frame Holding ($k \times$)
Instead of advancing the data on every single video frame, **each unique static frame is repeated $k$ times** (default: $3\times$ or $5\times$):

```
Frame 0 (Copy 1) ──► Frame 0 (Copy 2) ──► Frame 0 (Copy 3) ──► Frame 1 (Copy 1) ...
```

1. **Collapsing Inter-Frame Bitrates:** Consecutive duplicate frames yield identical frames where inter-frame delta is zero. Modern P-frames and B-frames compress to almost zero bytes.
2. **Surviving Bitrate Caps:** The encoder has enough bits allocated to render the keyframe sharply.
3. **Redundant Reading:** The decoder only needs **any 1 of the $k$ frames** to decode successfully.

---

## 4. Reed-Solomon Forward Error Correction (FEC)

Even with large blocks and grayscale encoding, high-frequency DCT quantization artifacts will flip occasional bits. We employ **Reed-Solomon GF(256)** error correction.

### Frame Layout (1080p with $8 \times 8$ blocks):
- **Raster:** $1920 \times 1080$ pixels
- **Blocks:** $240 \times 135 = 32,400\text{ bits} = 4,050\text{ raw bytes}$
- **Codewords:** 15 codewords per frame ($N=255$ bytes each).
- **Parity ($nsym$):** 32 bytes per codeword (can correct up to 16 byte errors per codeword, or 240 byte errors per frame).
- **Usable Data:** $15 \times 223 = 3,345\text{ bytes per frame}$.
- **Zero Padding:** $4,050 - (15 \times 255) = 225\text{ spare bytes}$.

### 2D Byte Interleaving
Compression artifacts do not flip bits at random—they cluster into $16 \times 16$ or $32 \times 32$ macroblock smears. If contiguous bytes were placed sequentially on the screen, a single macroblock artifact could corrupt 30 consecutive bytes, exceeding the Reed-Solomon threshold (16 bytes).

**We interleave bytes across all 15 codewords:**
- Byte 0 $\rightarrow$ Codeword 0
- Byte 1 $\rightarrow$ Codeword 1
- ...
- Byte 14 $\rightarrow$ Codeword 14
- Byte 15 $\rightarrow$ Codeword 0

A localized burst of corruption is evenly distributed across all 15 codewords, allowing Reed-Solomon to repair it cleanly.

---

## 5. Self-Describing Frame Header & CRC32

Every frame begins with a 10-byte binary header:

| Field | Type | Size | Description |
|---|---|---|---|
| `frame_no` | Big-Endian `uint32` | 4 bytes | Sequential frame index in the segment |
| `payload_len` | Big-Endian `uint16` | 2 bytes | Exact length of payload data |
| `crc32` | Big-Endian `uint32` | 4 bytes | CRC32 checksum of the decoded payload |

Because frames identify themselves, the reader does not need sample-accurate video cuts. When `yt-dlp` seeks to a keyframe and yields overlapping or adjacent frames, the reader discards invalid CRCs and correctly identifies the target frame number.

---

## 6. The Calibration Suite (`calibrate.py`)

To systematically determine optimal parameters for any video host, the calibration suite renders a short test video sweeping across parameter dimensions:

$$\text{Matrix} = \text{Block Sizes } [6\times 6, 8\times 8, 10\times 10] \times \text{Repeats } [1\times, 3\times, 5\times] \times \text{Parity } [16, 32, 48]$$

The analysis tool downloads the encoded test video, computes the bit error rate (BER) and RS repair counts for each slice, and outputs recommended production settings.
