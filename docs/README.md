# YouTubeDB Documentation

Welcome to the technical documentation for **YouTubeDB (formerly StaticDB)** — an educational append-only database that stores records as TV static frames in YouTube videos.

---

## Documentation Index

| Document | Description |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Comprehensive system architecture, data pipelines (write & read), and component interactions. |
| [ENCODING_AND_COMPRESSION.md](ENCODING_AND_COMPRESSION.md) | The science of surviving YouTube: grayscale encoding, Reed-Solomon ECC, and temporal frame repetition. |
| [METADATA_AND_RECOVERY.md](METADATA_AND_RECOVERY.md) | Disaster recovery, the self-reconstructing index, Superblock format, and YouTube description manifests. |
| [API_SPECIFICATION.md](API_SPECIFICATION.md) | Complete REST API endpoint contracts, schemas, headers, status codes, and error formats. |
| [OPERATIONS_GUIDE.md](OPERATIONS_GUIDE.md) | Step-by-step developer guide: local setup, video uploading, HD gatekeeper verification, and calibration. |

---

## Architectural Summary

YouTubeDB treats YouTube as an append-only block storage medium:
1. **Records are packed and encoded** into fixed-size chunks protected by Reed-Solomon forward error correction.
2. **Bits are rendered as physical pixel blocks** in 1080p video frames with temporal repetition to collapse video compression bitrates.
3. **An index tracks offsets**, mapping record IDs to specific video timestamps.
4. **Queries download only the relevant sub-second video slice** using `yt-dlp --download-sections`, decode the frames in-memory, and return JSON in 5–10 seconds.
