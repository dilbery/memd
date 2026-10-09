import threading
import time

from memd import control


def test_password_hashing_is_capped_at_the_configured_concurrency(monkeypatch):
    running, peak, lock = [0], [0], threading.Lock()
    real = control.hashlib.scrypt

    def counting(*args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return real(*args, **{**kwargs, "n": 2, "maxmem": 0})

    monkeypatch.setattr(control.hashlib, "scrypt", counting)
    monkeypatch.setattr(control, "_scrypt_gate", threading.BoundedSemaphore(2))
    threads = [threading.Thread(target=control.password_hash, args=("a long password",))
               for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 2
