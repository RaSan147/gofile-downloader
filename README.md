# gofile-downloader

Download files from https://gofile.io

---

#### Requirements

- Python 3.10+
- `pip` (or `uv`)

---

#### Setup

```bash
./setup.sh
```

```bat
setup.bat
```

Or install directly:

```bash
python -m pip install .
```

---

#### Usage

```bash
python gofile_downloader.py https://gofile.io/d/contentid
```

If it has password:

```bash
python gofile_downloader.py https://gofile.io/d/contentid password
```

If you have a text file with multiple urls:

```text
https://gofile.io/d/contentid1
https://gofile.io/d/contentid2
https://gofile.io/d/contentid3
https://gofile.io/d/contentid4
```

```bash
python gofile_downloader.py my-urls.txt
```

If you specify a password, this password is used for all urls in the text file:

```bash
python gofile_downloader.py my-urls.txt password
```

You can also start interactive mode by running without arguments:

```bash
python gofile_downloader.py
```

Common CLI flags:

```bash
python gofile_downloader.py <target> [password] \
  --parallel-downloads 5 \
  --threads 5 \
  --speed-limit 5m \
  --interactive
```

- `--parallel-downloads`: how many files to download in parallel.
- `--threads`: max total batch threads (for url-list file input).
- `--speed-limit`: per-download speed limit in bytes/sec (supports `k`, `m`, `g`).

---

#### Resume and integrity behavior

- Files are written to a temporary `filename.part` file while downloading.
- A file is only considered complete when its size is validated against the expected remote size.
- Existing final files are validated first:
  - **Skip** when final size is complete (`>=` expected size).
  - **Verify/Resume** when final size is incomplete (moved to `.part` and resumed).
- Existing `.part` files are handled safely:
  - **Resume** if `.part` is smaller than expected size.
  - **Restart** if `.part` is larger than expected size (invalid partial).
  - **Finalize** if `.part` already matches expected size exactly.
- `.part` is renamed to the final filename only after successful completion and exact final size validation.
- If the server ignores `Range` on resume (`200 OK` instead of `206 Partial Content`), the downloader logs a restart message and safely restarts from byte `0`.
- On interruption (Ctrl+C/network errors), `.part` files are intentionally preserved so downloads can be resumed later.

---

#### Environment Variables

Create a `.env` file and set your desired configurations:

```env
# Specify where to download to (the path must exist already)
GF_DOWNLOAD_DIR="./downloads"

# Toggle manual file selection to download (1 for True)
GF_INTERACTIVE="1"

# Specify a specific account token
GF_TOKEN="your_account_token_here"

# Configure the maximum number of concurrent file downloads
GF_MAX_CONCURRENT_DOWNLOADS="5"

# Configure max batch threads (url-list file input)
GF_MAX_BATCH_THREADS="5"

# Configure per-download speed limit in bytes/sec (0 = unlimited)
GF_SPEED_LIMIT="0"

# Configure the number of retries on timeout
GF_MAX_RETRIES="5"

# Configure a timeout for connections (in seconds)
GF_TIMEOUT="15.0"

# Configure the number of bytes read per chunk
GF_CHUNK_SIZE="2097152"

# Specify browser user agent (defaults Mozilla/5.0)
GF_USERAGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64)..."
```
