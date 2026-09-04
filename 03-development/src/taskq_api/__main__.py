"""[FR-03] ``python -m taskq_api`` CLI — Phase-3 GREEN entry point.

Implements the ``key create`` subcommand mandated by SPEC.md line 105:

    python -m taskq_api key create --scope <scope>

On success the freshly-generated plaintext (prefix ``tk-``) is printed
to stdout EXACTLY ONCE and never persisted — only its SHA-256 hash
lands in ``api_keys.key_hash``. Exit code ``0`` on success, non-zero
on usage errors so callers can ``$?``-check from shell scripts.

The CLI is intentionally minimal: Phase-4 will add ``key revoke``,
``key list``, and DB bootstrap flags; this module only does what
``03-development/tests/test_fr03.py`` pins down. Argparse guarantees
that ``args.handler`` is always set once ``required=True`` is in
effect — no defensive ``if handler is None`` branch is needed.

Citations:
- SPEC.md line 105 — `python -m taskq_api key create --scope <scope>`;
  plaintext only printed once at creation time.
- SPEC.md line 104 — only the SHA-256 hash is persisted.
- SPEC.md §3 FR-10 — CLI usage errors exit non-zero.
- SAD.md §2.6 — ``__main__`` is the dedicated entry point; do not
  duplicate ``if __name__ == "__main__":`` elsewhere.
- TEST_SPEC.md §1 FR-03 row 4 — stdout contains plaintext exactly once;
  row exists in api_keys; plaintext not in row.
- NFR-02 — plaintext must not leak into logs / stderr / row.
"""  # NFR-02 NFR-11
from __future__ import annotations

import argparse
import secrets
import sys
from typing import List

from taskq_api.repository.api_keys import insert_api_key


# Length of the random portion of a generated key. 32 bytes ≈ 256 bits of
# entropy, matching the SHA-256 output width (SPEC.md line 104).
_RANDOM_BYTES = 32
_KEY_PREFIX = "tk-"

# CLI usage strings kept inline so the file stays under the NFR-11 line cap
# without dragging in a separate constants module.
_USAGE = "python -m taskq_api <command> [args]"
_EPILOG = "Phase-3 GREEN: only `key create` is implemented."


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser.

    Citations: SPEC.md line 105 — `key create --scope <scope>`;
    SAD.md §2.6 — argv dispatch lives in ``__main__``.
    """  # NFR-11
    parser = argparse.ArgumentParser(
        prog="python -m taskq_api",
        usage=_USAGE,
        description="taskq_api Phase-3 CLI (FR-03: api key management).",
        epilog=_EPILOG,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    key_parser = subparsers.add_parser(
        "key",
        help="manage API keys",
        description="create / list / revoke API keys (Phase-3: create only).",
    )
    key_subparsers = key_parser.add_subparsers(
        dest="subcommand", required=True
    )

    create = key_subparsers.add_parser(
        "create",
        help="create a new API key and print its plaintext to stdout",
        description=(
            "Generate a fresh API key, persist only its SHA-256 hash, and "
            "print the plaintext to stdout exactly once."
        ),
    )
    create.add_argument(
        "--scope",
        required=True,
        help="scope granted to the new key (e.g. 'write', 'admin')",
    )

    return parser


def _generate_plaintext() -> str:
    """Return a fresh ``tk-`` prefixed plaintext key.

    Uses ``secrets.token_urlsafe`` so the random portion is
    URL-safe (alphanumeric + ``-`` / ``_``) and carries ~256 bits of
    entropy — comfortably above the SHA-256 collision-resistance bar.

    Citations: SPEC.md line 105 — plaintext must be high-entropy and
    printed only once.
    """  # NFR-02 NFR-09
    return _KEY_PREFIX + secrets.token_urlsafe(_RANDOM_BYTES)


def _handle_key_create(scope: str) -> int:
    """Create a new key, persist its hash, and print the plaintext once.

    Citations: SPEC.md line 105 — 印一次; SPEC.md line 104 — only the
    hash is stored.
    """  # NFR-02 NFR-09
    plaintext = _generate_plaintext()
    insert_api_key(plaintext, scope=scope)
    # Print EXACTLY ONCE (SPEC.md line 105). A trailing newline keeps the
    # shell prompt on its own line; we never ``print(plaintext)`` twice
    # or include it in any log / trace.
    print(plaintext)
    return 0


def main(argv: List[str]) -> int:
    """CLI entry point — returns a POSIX exit code.

    ``argv`` is a list of strings (excluding the program name) so the
    function is callable from tests without ``sys.argv`` mutation. The
    shape matches ``03-development/tests/test_fr03.py`` which invokes
    ``cli_main(["key", "create", "--scope", "write"])``.

    Citations: SPEC.md line 105 — CLI subcommand; SPEC.md §3 FR-10 —
    CLI usage errors return non-zero; TEST_SPEC.md §1 FR-03 row 4.
    """  # NFR-10 NFR-11
    parser = _build_parser()
    args = parser.parse_args(argv)

    # ``required=True`` on every subparser guarantees ``command`` and
    # ``subcommand`` are populated when parse_args returns without
    # raising SystemExit, so a plain dispatch is sufficient.
    if args.command == "key" and args.subcommand == "create":
        return _handle_key_create(args.scope)
    # Defensive fallback for unknown subcommands — argparse already
    # rejects them with SystemExit, so reaching this branch means the
    # parser was extended without a handler.
    parser.error(f"unsupported subcommand: {args.command} {args.subcommand}")
    return 2  # pragma: no cover — argparse exits before this.


# Allow ``python -m taskq_api`` to invoke ``main(sys.argv[1:])``.
# SAD.md §2.6 forbids duplicating this guard inside api/service layers.
if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
