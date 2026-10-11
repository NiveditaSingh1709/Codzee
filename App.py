import os
import string

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


@app.route("/shorten", methods=["POST"])
def shorten():
    data = request.get_json()
    code = encode_base62(r.incr(COUNTER_KEY))
    r.hset(link_key(code), mapping={"url": data["url"], "clicks": 0})
    return jsonify({"short_code": code, "short_url": request.host_url + code}), 201


@app.route("/<code>")
def go(code):
    entry = r.hgetall(link_key(code))
    if not entry:
        return jsonify({"error": "not found"}), 404
    r.hincrby(link_key(code), "clicks", 1)
    return redirect(entry["url"])


if __name__ == "__main__":
    app.run(debug=True)
