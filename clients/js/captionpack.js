/**
 * captionpack-api — JavaScript client for the Caption-Pack API.
 *
 * Social captions as a service: POST a topic, audience and tone;
 * get platform-ready captions + hashtags as JSON. 1 credit per call.
 *
 * Zero dependencies. Works in Node 18+ and modern browsers.
 *
 * @example
 * import { CaptionPackClient } from 'captionpack-api';
 * const client = new CaptionPackClient({ apiKey: 'cp_live_...' });
 * const pack = await client.captionPack({
 *   topic: 'password managers',
 *   audience: 'small business owners',
 *   tone: 'warm',
 *   platform: 'linkedin',
 *   count: 3,
 * });
 */

const DEFAULT_BASE_URL = 'https://caption-pack-api.onrender.com';

class CaptionPackError extends Error {
  constructor(message, { status, code, retryAfter } = {}) {
    super(message);
    this.name = this.constructor.name;
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

class CaptionPackBadRequest extends CaptionPackError {}
class CaptionPackUnauthorized extends CaptionPackError {}
class CaptionPackPaymentRequired extends CaptionPackError {}
class CaptionPackRateLimited extends CaptionPackError {}
class CaptionPackServerError extends CaptionPackError {}

function errorFor(status, body) {
  const message =
    (body && (body.detail || body.message)) || `Caption-Pack API error ${status}`;
  const code = body && body.code;
  if (status === 400) return new CaptionPackBadRequest(message, { status, code });
  if (status === 401) return new CaptionPackUnauthorized(message, { status, code });
  if (status === 402) return new CaptionPackPaymentRequired(message, { status, code });
  if (status === 429) return new CaptionPackRateLimited(message, { status, code });
  return new CaptionPackServerError(message, { status, code });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class CaptionPackClient {
  /**
   * @param {object} opts
   * @param {string} opts.apiKey - your Caption-Pack API key (cp_live_... / cp_free_...)
   * @param {string} [opts.baseUrl] - override for testing
   */
  constructor({ apiKey, baseUrl = DEFAULT_BASE_URL } = {}) {
    if (!apiKey) throw new CaptionPackError('apiKey is required');
    this.apiKey = apiKey;
    this.baseUrl = baseUrl.replace(/\/$/, '');
    /** Credits remaining, from the last X-Credits-Remaining header seen. */
    this.creditsRemaining = null;
  }

  async _request(path, { method = 'GET', body } = {}, _retried = false) {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });

    const remaining = res.headers.get('x-credits-remaining');
    if (remaining !== null) this.creditsRemaining = Number(remaining);

    // One retry on 429, honoring Retry-After.
    if (res.status === 429 && !_retried) {
      const retryAfter = Number(res.headers.get('retry-after') || '1');
      await sleep(Math.max(0, retryAfter) * 1000);
      return this._request(path, { method, body }, true);
    }

    let data = null;
    try {
      data = await res.json();
    } catch {
      // non-JSON error body; fall through to generic error
    }
    if (!res.ok) throw errorFor(res.status, data);
    return data;
  }

  /**
   * Generate a caption pack. 1 credit per call.
   * @param {object} req - { topic, audience, tone, platform, count? }
   */
  captionPack(req) {
    return this._request('/v1/caption-pack', { method: 'POST', body: req });
  }

  /** Check credit balance. Free call. */
  balance() {
    return this._request('/v1/balance');
  }
}

export {
  CaptionPackClient,
  CaptionPackError,
  CaptionPackBadRequest,
  CaptionPackUnauthorized,
  CaptionPackPaymentRequired,
  CaptionPackRateLimited,
  CaptionPackServerError,
  DEFAULT_BASE_URL,
};
