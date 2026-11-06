import sys, os, threading, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scrybe as s
calls = {}
def fake_http(url, *a, **k):
    calls[url] = calls.get(url, 0) + 1
    if "always-timeout" in url: raise s.NetError("timed out", retryable=True)
    if "flaky" in url and calls[url] < 3: raise s.NetError("timed out", retryable=True)
    if "gone" in url: raise s.NotFound(url)
    return b"img"
s.http_bytes = fake_http
issues = []
eng = s.Engine(s.Options(), lambda *ev: issues.append(ev) if ev[0]=="issue" else None, threading.Event(), {}, tempfile.mkdtemp())
p = s.Print.from_list([None]*len(s.Print.FIELDS))
d = s.Deck("t", [], tempfile.mkdtemp())
items = [s.DownloadItem(d, n, f"https://x/{n}.jpg", os.path.join(d.out_dir, n + ".jpg"), p, "front")
         for n in ("ok", "flaky", "always-timeout", "gone")]
order = []
orig = eng._fetch
def spy(it): order.append(it.label); return orig(it)
eng._fetch = spy
st = eng.download(items)
print("stats", st); print("fetch order", order); print("issues", [i[2:] for i in issues])
assert st == {"ok": 2, "skipped": 0, "failed": 2}
assert calls["https://x/flaky.jpg"] == 3 and calls["https://x/always-timeout.jpg"] == 3 and calls["https://x/gone.jpg"] == 1
assert order[:4] == ["ok", "flaky", "always-timeout", "gone"] or set(order[:4]) == {"ok","flaky","always-timeout","gone"}
# lookup queue requeue
n = {"a": 0}
def flaky_lookup():
    n["a"] += 1
    if n["a"] < 2: raise s.NetError("timed out", retryable=True)
seq = []
errs = eng.run_queue([("A", flaky_lookup), ("B", lambda: seq.append("B")), ("C", lambda: (_ for _ in ()).throw(s.NetError("timed out", True)))], "test")
print("lookup errors", errs, "A tries", n["a"])
assert n["a"] == 2 and "C" in errs and "after 3 attempts" in errs["C"]
print("retry tests passed")
