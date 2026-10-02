# captionpack — Python client for the Caption-Pack API

Turn a topic, audience, and tone into ready-to-post social captions + hashtags, as JSON. Built for developers and AI agents.

## Install

```bash
pip install captionpack
```

(Get your API key at the [Caption-Pack site](https://muse.ai/s/caption-pack-api-xfxt62ya0xcxlxhh) — $10 for 500 calls.)

## Quickstart

```python
from captionpack import Client

client = Client(api_key="cp_live_...")
pack = client.caption_pack(topic="morning routines", audience="busy parents")
print(pack.captions[0].body, pack.hashtags)
```

You can also set the `CAPTIONPACK_API_KEY` environment variable instead of passing `api_key=`.

## Usage

```python
from captionpack import (
    Client,
    AuthenticationError,
    InsufficientCreditsError,
    RateLimitedError,
)

client = Client()  # reads CAPTIONPACK_API_KEY

# Generate captions (1 credit per call)
pack = client.caption_pack(
    topic="password managers",
    audience="small business owners",
    tone="warm",            # warm | bold | professional | playful
    platform="linkedin",    # instagram | linkedin | x | tiktok
    count=5,                # 1-10
    include_hashtags=True,
)
for caption in pack.captions:
    print(caption.hook, "|", caption.body, "|", caption.cta)
print(pack.hashtags)
print("credits left:", pack.credits_remaining)  # from X-Credits-Remaining

# Check balance
balance = client.balance()
print(balance.credits)

# Handle errors explicitly
try:
    pack = client.caption_pack(topic="x", audience="y")
except InsufficientCreditsError as e:
    print("Out of credits — top up:", e.top_up_url)
except RateLimitedError:
    print("Slow down — 60 requests/minute per key.")
except AuthenticationError:
    print("Bad API key.")
```

## Behavior notes

- One automatic retry on HTTP 429 (with backoff, honoring `Retry-After`).
- `X-Credits-Remaining` is surfaced on every `caption_pack` result.
- Point at a local/dev server with `Client(api_key=..., base_url="http://localhost:8000")`.

## Development

```bash
pip install -e ".[dev]"   # or: pip install -e . && pip install pytest
# integration tests need the API source; set CAPTIONPACK_TEST_API_DIR
CAPTIONPACK_TEST_API_DIR=/path/to/caption-pack-api pytest
```
