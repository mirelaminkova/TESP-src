"""Sends requests to the gateway with a claimed client address (issue #44).

Run inside a container on the stack's network, where X-Real-IP must not be
trusted (check-oauth.sh runs it in tesp-api's container):

    docker exec -i tesp-api python - URL COUNT CLAIM [USER:PASSWORD] < real-ip-probe.py

COUNT requests go out at once. CLAIM is the X-Real-IP value, or "each" for a
different address per request (203.0.113.N). Prints how many got each HTTP
status, as JSON: {"401": 60, "429": 40}.
"""

import base64
import json
import sys
import threading
import urllib.error
import urllib.request
from collections import Counter

url, count, claim = sys.argv[1], int(sys.argv[2]), sys.argv[3]
basic = sys.argv[4] if len(sys.argv) > 4 else None
statuses = Counter()
lock = threading.Lock()
start = threading.Barrier(count)


def one(i):
    headers = {"X-Real-IP": f"203.0.113.{i % 250 + 1}" if claim == "each" else claim}
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
    request = urllib.request.Request(url, headers=headers)
    start.wait()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as e:
        status = e.code
    except OSError as e:
        status = type(e).__name__
    with lock:
        statuses[str(status)] += 1


threads = [threading.Thread(target=one, args=(i,)) for i in range(count)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print(json.dumps(dict(statuses), sort_keys=True))
