import os
import re
import string
from urllib.parse import urlparse

import redis
from flask import Flask, jsonify, request, redirect

app = Flask(__name__)
r = redis.Redis.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    decode_responses=True,
)

BASE62 = string.digits + string.ascii_letters
COUNTER_KEY = "meta:url_counter"
MAX_EXPIRY_SECONDS = 60 * 60 * 24 * 365  # one year
MAX_URL_LENGTH = 2048
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,30}$")

# Atomically create a link with its click counter and optional TTL.
# Returns 1 if created, 0 if the key already exists.
CREATE_LINK = r.register_script("""
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
redis.call('HSET', KEYS[1], 'url', ARGV[1], 'clicks', 0)
if tonumber(ARGV[2]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 1
""")

# Atomically count a click and return the URL, or nil if the link is gone.
VISIT_LINK = r.register_script("""
local url = redis.call('HGET', KEYS[1], 'url')
if not url then
  return false
end
redis.call('HINCRBY', KEYS[1], 'clicks', 1)
return url
""")


def encode_base62(n):
    if n == 0:
        return BASE62[0]
    out = []
    while n:
        n, rem = divmod(n, 62)
        out.append(BASE62[rem])
    return "".join(reversed(out))


def link_key(code):
    return f"url:{code}"


def is_valid_url(value):
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def create_link(code, url, expires_in):
    """Create a link atomically; returns False if the code is already taken."""
    return bool(CREATE_LINK(keys=[link_key(code)], args=[url, expires_in or 0]))


@app.route("/shorten", methods=["POST"])
def shorten():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "a JSON object body is required"}), 400

    url = data.get("url")
    if not is_valid_url(url):
        return jsonify({"error": "a valid http(s) url is required"}), 400

    expires_in = data.get("expires_in")
    if expires_in is not None and (
        not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or not 0 < expires_in <= MAX_EXPIRY_SECONDS
    ):
        return jsonify({"error": "expires_in must be between 1 and 31536000 seconds"}), 400

    alias = data.get("alias")
    if alias is not None:
        if not isinstance(alias, str) or not ALIAS_PATTERN.fullmatch(alias):
            return jsonify({"error": "invalid alias"}), 400
        if not create_link(alias, url, expires_in):
            return jsonify({"error": "alias already taken"}), 409
        code = alias
    else:
        # A custom alias may equal a future counter value, so retry until a free code is claimed
        while True:
            code = encode_base62(r.incr(COUNTER_KEY))
            if create_link(code, url, expires_in):
                break

    return jsonify({"short_code": code, "short_url": request.host_url + code}), 201


@app.route("/stats/<code>")
def stats(code):
    key = link_key(code)
    pipe = r.pipeline(transaction=True)
    pipe.hgetall(key)
    pipe.ttl(key)
    entry, ttl = pipe.execute()
    if not entry or "url" not in entry:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "url": entry["url"],
        "clicks": int(entry.get("clicks", 0)),
        "expires_in": ttl if ttl >= 0 else None,
    })


@app.route("/<code>")
def go(code):
    url = VISIT_LINK(keys=[link_key(code)])
    if not url:
        return jsonify({"error": "not found"}), 404
    return redirect(url)

@app.errorhandler(redis.RedisError)
def handle_redis_error(exc):
    app.logger.error("Redis error: %s", exc, exc_info=exc)
    return jsonify({"error": "service temporarily unavailable"}), 503


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
