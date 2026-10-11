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
COUNTER_KEY = "url:counter"


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

ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,30}$")

def is_valid_url(value):
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def claim_code(code, url):
    """Atomically claim a code; returns False if already taken."""
    return bool(r.hsetnx(link_key(code), "url", url))

@app.route("/shorten", methods=["POST"])
def shorten():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    if not is_valid_url(url):
        return jsonify({"error": "a valid http(s) url is required"}), 400

    expires_in = data.get("expires_in")
    if expires_in is not None and (
        not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0
    ):
        return jsonify({"error": "expires_in must be a positive integer"}), 400

    alias = data.get("alias")
    if alias:
        if not ALIAS_PATTERN.match(alias):
            return jsonify({"error": "invalid alias"}), 400
        if not claim_code(alias, url):
            return jsonify({"error": "alias already taken"}), 409
        code = alias
    else:
        # A custom alias could equal a future counter value, so verify the claim
        while True:
            code = encode_base62(r.incr(COUNTER_KEY))
            if claim_code(code, url):
                break

    r.hset(link_key(code), "clicks", 0)
    if expires_in:
        r.expire(link_key(code), expires_in)
    return jsonify({"short_code": code, "short_url": request.host_url + code}), 201



@app.route("/stats/<code>")
def stats(code):
    entry = r.hgetall(link_key(code))
    if not entry:
        return jsonify({"error": "not found"}), 404
    ttl = r.ttl(link_key(code))
    return jsonify({
        "url": entry["url"],
        "clicks": int(entry["clicks"]),
        "expires_in": ttl if ttl >= 0 else None,
    })


@app.route("/<code>")
def go(code):
    entry = r.hgetall(link_key(code))
    if not entry:
        return jsonify({"error": "not found"}), 404
    r.hincrby(link_key(code), "clicks", 1)
    return redirect(entry["url"])


if __name__ == "__main__":
    app.run(debug=True)
