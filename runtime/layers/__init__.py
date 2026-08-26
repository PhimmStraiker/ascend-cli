"""
runtime.layers — composable adapter layers (the L2/L3/L5 building blocks).

An adapter config is one choice per orthogonal layer (see
``docs/CAPABILITY_MATRIX.md``). This package implements the layers that carry
behaviour beyond raw transport:

  * :mod:`layers.identity` — Layer 5, *who* is calling (:class:`IdentityManager`).
  * :mod:`layers.auth`     — Layer 2 + Layer 3, *how* a request is authorized
    (:class:`AuthProvider`) and *how* credentials stay valid
    (:class:`AuthLifecycle`).

Design rules honoured throughout:

  * **Pure where possible.** :class:`IdentityManager` and all secret-reference
    resolution are deterministic functions over their config — importable and
    unit-testable with no network.
  * **No inline secrets.** Every credential value is taken from an environment
    reference (``{"value_ref": "env:MY_TOKEN"}`` or the bare string
    ``"env:MY_TOKEN"``). A literal secret in a config is refused.
  * **Lazy network.** Nothing in this package performs I/O at import time.
    :class:`AuthProvider` only touches the network when
    :meth:`AuthProvider.materialize` is called.
"""
from .identity import (
    IdentityManager,
    IdentityError,
    resolve_identity,
)
from .auth import (
    AuthProvider,
    AuthMaterial,
    AuthLifecycle,
    AuthError,
    resolve_secret_ref,
)

__all__ = [
    "IdentityManager",
    "IdentityError",
    "resolve_identity",
    "AuthProvider",
    "AuthMaterial",
    "AuthLifecycle",
    "AuthError",
    "resolve_secret_ref",
]
