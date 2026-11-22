# YouTubeDB Operations & Runbook

## 1. Prerequisites & Environment Setup

### System Requirements:
- **Operating System:** Windows, macOS, or Linux
- **Python:** 3.10+ (tested on Python 3.12)
- **FFmpeg:** Installed and available on system path (or inside `.venv/Scripts`)

### Setup:
```bash
# 1. Clone repository
git clone https://github.com/toxicbishop/youtube-as-a-database.git
cd youtube-as-a-database

# 2. Create virtual environment
python -m venv .venv

# 3. Activate environment (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# 4. Install dependencies
pip install -r requirements-dev.txt
```

---

## 2. Generating & Seeding Data

Generate fake customer records and encode them into an MP4 video segment:

```bash
# Generate 50 test customers (small 80 KiB video, ~8 frames)
python seed.py --count 50

# Generate 1,000 customers (~1.5 MiB video, 154 frames)
python seed.py --count 1000
```

The output video will be saved in:
`data/videos/seg_0001.mp4`

---

## 3. Uploading to YouTube

1. Go to **[YouTube Studio](https://studio.youtube.com)** using a **throwaway channel**.
2. Click **Create** $\rightarrow$ **Upload Videos**.
3. Select `data/videos/seg_0001.mp4`.
4. Set the following options:
   - **Title:** `YouTubeDB - Segment 0001`
   - **Visibility:** **Unlisted** (important: do NOT set to Private, or `yt-dlp` cannot access it without cookies).
   - **Audience:** Not Made for Kids.
5. In the **Description**, paste the generated JSON manifest (from `python store.py manifest --segment seg_0001`).
6. Click **Publish**.

---

## 4. Registering the Video (HD Gatekeeper)

After publishing, YouTube processes the SD stream (360p) in seconds, but HD (720p/1080p) takes 5–15 minutes. 

Register the video with:
```bash
python store.py register --segment seg_0001 --video-id <YOUTUBE_VIDEO_ID_OR_URL>
```

### The HD Gatekeeper:
If 1080p is still processing on YouTube, `store.py` will output:
```text
⏳ Stream 1080p is not yet available (YouTube is still re-encoding).
Available formats: 360p (SD).
Please wait 5-10 minutes and re-run this command once HD is ready.
```
Only once the 1080p stream is verified live will the segment be activated for queries.

---

## 5. Starting the Service

### Local Mode (Reads directly from `data/videos/`):
```bash
uvicorn api:app --reload
```

### YouTube Mode (Reads from YouTube via `yt-dlp`):
```powershell
$env:STATICDB_SOURCE="youtube"
uvicorn api:app --reload
```

Open your browser to:
👉 **`http://localhost:8000`**

Or query via command line:
```bash
curl http://localhost:8000/customer/7
```

---

## 6. Running the Test Suite

Run the full pytest suite (including simulated compression resilience):
```bash
pytest -v
```

All 42 tests will execute:
- `tests/test_codec.py`: Verifies bit rasterization, RS codeword integrity, and CRC calculations.
- `tests/test_store.py`: Verifies SQLite transaction indexing, segment boundaries, and offset math.
- `tests/test_video.py`: Simulates harsh FFmpeg recompression (down to 480p at CRF 36) to verify ECC recovery.
- `tests/test_api.py`: Validates FastAPI endpoints and web routes.
