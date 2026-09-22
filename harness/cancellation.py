"""Bridge a run's stop signal into providers that own cancelable request scopes."""
from __future__ import annotations

import threading
import uuid


def complete(provider, system, messages, schemas, *, on_text=None, cancelled=None):
    """Cancel only this caller's request, while preserving inherited budget authority.

    Providers without scoped cancellation retain their normal transport timeout
    and the harness's boundary checks. Never use a provider-wide cancel operation:
    a provider can serve more than one Mission concurrently.
    """
    cancel_for = getattr(provider, "cancel_for", None)
    authority = getattr(provider, "request_authority", None)
    if not cancelled or not callable(cancel_for) or not callable(authority):
        return provider.complete(system, messages, schemas, on_text=on_text)

    gate, settled = provider.current_request_authority()
    scope = provider.current_request_scope() or "call:" + uuid.uuid4().hex
    finished = threading.Event()

    def watch():
        while not finished.wait(0.05):
            try:
                if cancelled():
                    # Keep checking until completion. A stop may arrive just
                    # before the provider publishes its pending invocation.
                    cancel = cancel_for(scope)
                    # Claude SDK/CLI return a scope-bound callback, while some
                    # providers cancel directly. Calling the factory alone
                    # silently left the real Claude request running.
                    if callable(cancel):
                        cancel()
            except Exception:
                # Cancellation failure is not completion. The actual provider
                # call still owns its outcome and normal cleanup/error path.
                continue

    with authority(gate, settled, request_scope=scope):
        watcher = threading.Thread(target=watch, name="collie-request-cancel", daemon=True)
        watcher.start()
        try:
            return provider.complete(system, messages, schemas, on_text=on_text)
        finally:
            finished.set()
            watcher.join(timeout=1)
