"""Keeps the LLM from being the bottleneck: a shared rate limit and an answer cache.

NVIDIA's API allows about 30 requests a minute per key. Every running check
shares that budget, so each LLM call first takes a slot in a DynamoDB counter:

    slots      at most LLM_RPM // 6 calls in each 10-second window (4 for the
               default 26 a minute), so any 60 seconds hold at most 28 calls.
               A call that finds the window full waits for the next one instead
               of failing; it gives up only when the check's deadline is near.
    cache      the answer to an identical request (same model, prompt and text)
               is stored for 30 days and reused. A repeated check then needs no
               request at all, and always gets the same answer.

    cool-down  after NVIDIA answers "busy" (HTTP 429), every check skips that model
               for a while (see llm.py) instead of each finding out by itself.

All live in one table (key "key", TTL attribute "expiresAt"): "slot#<name>#<window>"
counters, "cool#<name>" markers and "answer#<sha256>" answers. Each model has its
own gate, named after it. Without a table (tests, local runs) nothing is limited,
cached or cooled down.
"""

import hashlib
import json
import os
import time

WINDOW_SECONDS = 10
CACHE_DAYS = 30
CACHE_GENERATION = "2"  # raise to stop reusing every stored answer (after a prompt or model change)


class Gate:
    def __init__(self, table=None, rpm=None, clock=time.time, sleep=time.sleep, name="llm"):
        self.table, self.name = table, name
        rpm = int(rpm or os.environ.get("LLM_RPM", "26"))
        self.per_window = max(1, rpm * WINDOW_SECONDS // 60)
        self.clock, self.sleep = clock, sleep
        self.waited = 0.0  # seconds spent waiting for slots, for the check's timings

    @staticmethod
    def key(*parts):
        text = "\x1f".join(str(p) for p in (CACHE_GENERATION, *parts))
        return "answer#" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    def cached(self, key):
        """The stored answer for this request, or None."""
        if self.table is None:
            return None
        try:
            item = self.table.get_item(Key={"key": key}).get("Item")
        except Exception as e:  # the cache is an optimisation: never fail a check over it
            print(f"LLM cache read failed: {type(e).__name__}")
            return None
        return json.loads(item["answer"]) if item else None

    def remember(self, key, answer):
        if self.table is None:
            return
        try:
            self.table.put_item(Item={"key": key, "answer": json.dumps(answer),
                                      "expiresAt": int(self.clock()) + CACHE_DAYS * 86400})
        except Exception as e:
            print(f"LLM cache write failed: {type(e).__name__}")

    def cool_down(self, seconds):
        """Marks this model as rate-limited for `seconds`, for every check."""
        if self.table is None:
            return
        until = int(self.clock() + seconds)
        try:
            self.table.put_item(Item={"key": f"cool#{self.name}", "until": until, "expiresAt": until + 3600})
        except Exception as e:
            print(f"LLM cool-down write failed: {type(e).__name__}")

    def cooling(self):
        """Whether this model was rate-limited a moment ago."""
        if self.table is None:
            return False
        try:
            item = self.table.get_item(Key={"key": f"cool#{self.name}"}).get("Item")
        except Exception:
            return False
        return bool(item) and int(item.get("until", 0)) > self.clock()

    def acquire(self, deadline):
        """Takes a slot for one request. Returns False if none frees up before `deadline`."""
        if self.table is None:
            return True
        while True:
            now = self.clock()
            window = int(now // WINDOW_SECONDS)
            try:
                self.table.update_item(
                    Key={"key": f"slot#{self.name}#{window}"},
                    UpdateExpression="ADD calls :one SET expiresAt = :expires",
                    ConditionExpression="attribute_not_exists(calls) OR calls < :limit",
                    ExpressionAttributeValues={":one": 1, ":limit": self.per_window,
                                               ":expires": int(now) + 3600})
                return True
            except Exception as e:
                if "ConditionalCheckFailed" not in type(e).__name__ + str(e):
                    print(f"LLM slot counter failed: {type(e).__name__}; going ahead")
                    return True  # never block a check because the counter is unavailable
            wait = (window + 1) * WINDOW_SECONDS - now + 0.05
            if now + wait > deadline:
                return False
            self.sleep(wait)
            self.waited += wait
