# captionpack-api (JavaScript)

JavaScript client for the [Caption-Pack API](https://muse.ai/s/caption-pack-api-xfxt62ya0xcxlxhh) — social captions as a service. Zero dependencies, works in Node 18+ and modern browsers.

## Install

```bash
npm install captionpack-api
```

## Quickstart

```js
import { CaptionPackClient } from 'captionpack-api';

const client = new CaptionPackClient({ apiKey: 'cp_live_...' });

const pack = await client.captionPack({
  topic: 'password managers',
  audience: 'small business owners',
  tone: 'warm',
  platform: 'linkedin',
  count: 3,
});

console.log(pack.captions);
console.log('credits left:', client.creditsRemaining);
```

Get a key: mint a free 25-call key (no card) at
`GET https://caption-pack-api.onrender.com/v1/free-trial`,
or buy 500 calls for $10 via the checkout on the landing page.

## API

- `captionPack({ topic, audience, tone, platform, count? })` — 1 credit per call. Returns captions, hashtags, CTAs, char counts, plus `credits_used` / `credits_remaining`.
- `balance()` — free call. Returns `{ credits, key_id, tier, expires_at }`.
- `client.creditsRemaining` — last seen `X-Credits-Remaining` header value.

## Errors

Typed errors, all extending `CaptionPackError`:

| Class | When |
|---|---|
| `CaptionPackBadRequest` | 400 — bad request |
| `CaptionPackUnauthorized` | 401 — bad/missing key |
| `CaptionPackPaymentRequired` | 402 — out of credits (top up!) |
| `CaptionPackRateLimited` | 429 — retried once automatically, honoring `Retry-After` |
| `CaptionPackServerError` | 5xx |

```js
import { CaptionPackClient, CaptionPackPaymentRequired } from 'captionpack-api';
try {
  await client.captionPack({ topic: 'x', audience: 'y', tone: 'warm', platform: 'instagram' });
} catch (err) {
  if (err instanceof CaptionPackPaymentRequired) {
    console.log('Out of credits — top up at the landing page.');
  }
}
```

## License

MIT
