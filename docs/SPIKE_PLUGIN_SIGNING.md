# Spike: Ed25519 attestation for plugin package digests (KEL-29)

- **Status:** spike, not merged behavior. Prototype module `skills/signing.py`
  is imported by nothing in the production path; `skills/loader.py`,
  `skills/cli.py`, and `skills/manifest.schema.json` are untouched.
- **What this proves:** an operator Ed25519 key can sign the exact digest
  string the CLI pins, and a verifier holding only the public key can check
  that signature offline.
- **What this does not do:** change boot behavior, add a `signature` field to
  any pin, or introduce a crypto dependency. Those are team decisions, listed
  as follow-ups below.
- **Crypto honesty:** the venv has only `jsonschema` and `httpx`. The spike
  uses a small pure-Python RFC 8032 Ed25519 implementation, isolated behind
  two internal functions (`_sign_raw`, `_verify_raw`). It is **not
  constant-time** and not audited. The intended production dependency is
  `cryptography` (`Ed25519PrivateKey`/`Ed25519PublicKey`) or PyNaCl
  (`nacl.signing.SigningKey`); both speak the same 64-byte detached-signature
  format, so the swap is a two-function change.

---

## 1. The gap: the pin is self-certified

Today's chain (KEL-25, `docs/SKILL_SPEC.md` §15.1):

1. `python -m skills.cli install <package>` validates the package, computes
   its SHA-256 digest, and pins `{name, version, digest}` in
   `plugins.lock.json`.
2. At boot the loader recomputes the digest and refuses to start on a
   mismatch.

That proves **unchanged-since-install**. It does not prove
**written-by-a-trusted-publisher**: the digest is a pure function of the
package bytes, so anyone who can write a package into `KELP_PLUGIN_ROOT` —
or replace it and re-pin it — can produce a package whose digest matches its
own pin. The pin certifies the disk, not the author.

## 2. Trusted publishing path

- **Who signs:** the operator, at deploy time, on the build host — the same
  trust boundary that already runs `skills.cli`. Signing is an operator act,
  never a model-facing or chat-reachable one.
- **What is signed:** the digest **string** returned by
  `skills.loader.package_digest` — `sha256:<64 hex>` — as UTF-8 bytes. Not
  the package bytes, not a re-hash, not the manifest alone. Signing the
  digest string means the signature composes with the existing pin for free:
  the loader already computes exactly this string, so "verify the signature"
  and "check the pin" compare the same value.
- **Where the private key lives:** outside the package tree and outside the
  container image (the image deliberately ships no secrets, `docs/DEPLOY.md`
  §4). An operator key file on the build host, or a secret manager the
  operator pulls from; never `KELP_PLUGIN_ROOT`.
- **Where the public key lives:** a deploy asset provisioned on the runtime
  host, referenced by an env path — the spike uses
  `KELP_PLUGIN_SIGNING_PUBLIC_KEY` pointing at a 64-hex-char key file. The
  loader-style rule is fail-closed: an unset variable or a missing file is a
  hard error, never "skip verification".

Round trip (run against a real package fixture, the same shape
`tests/test_plugins.py` builds):

```python
from skills import signing
from skills.loader import inspect_plugin_package

signing.write_keypair("operator.key", "operator.pub")     # hex text files
package = inspect_plugin_package("path/to/plugin")        # real digest
signature = signing.sign_package("path/to/plugin", "operator.key")
assert signing.verify_package(package.digest, signature, "operator.pub") is True
assert signing.verify_package("sha256:" + "0" * 64, signature, "operator.pub") is False
```

Measured on this spike: sign+verify of a two-file package round-trips
`True`; a tampered digest or a flipped signature bit verifies `False`.

## 3. How signing composes with the existing pin

The pin stays the runtime check; the signature is the author attestation on
top of it. `plugins.lock.json` keeps `digest` and gains one additive field:

```json
{
  "version": 1,
  "plugins": {
    "plugins.echo": {
      "name": "plugins.echo",
      "version": "0.1.0",
      "digest": "sha256:ecbd7bc5cb59140cd158929a3f85d2fca73727812057dc431a50aaaf9b722ee1",
      "signature": "c626dcdc11ddc95dc5c8a7866b68311e…"
    }
  }
}
```

The loader **would** verify, in this order, before pinning takes effect:

1. Compute the package digest (existing code path).
2. Check the digest against the pin (existing code path — unchanged).
3. Check the signature over that digest string against the trusted public
   key (new; fails boot like any other violation, naming the plugin).

Ordering matters: the digest check stays first so a tampered package is
reported as a digest mismatch (the existing, tested message) even when a
signature field is present. A missing `signature` field on a pinned plugin
would be a boot violation once enforcement lands — an old lock file is not
grandfathered, because "attested" is the point of the feature. `read_plugin_lock`
would validate the signature field's shape (`128 hex chars`) the same way it
validates the digest today.

What each mechanism then guarantees:

| Mechanism | Proves | Fails when |
|---|---|---|
| Digest pin | bytes on disk are the bytes that were installed | any on-disk mutation |
| Signature | the installed digest was signed by the operator key | package replaced **and** re-pinned by anything that is not the operator key |

The signature closes the re-pin hole: an attacker who swaps a package and
rewrites `plugins.lock.json` still cannot produce a valid signature, because
they do not hold the private key.

## 4. Key distribution and rotation

- **Provisioning:** the public key is a deploy asset, like `deploy/kelp.env`
  is today. The runtime host gets the key file and
  `KELP_PLUGIN_SIGNING_PUBLIC_KEY=/path/to/operator.pub` in the environment
  the service already reads (`EnvironmentFile` / compose env). The key file
  lives under the persistent volume or a read-only mount, not inside
  `KELP_PLUGIN_ROOT`, so a package-writer cannot replace the trust anchor.
- **Rotation:** generate a new operator keypair, re-sign every currently
  pinned digest with the new private key, and deploy the new public key
  alongside the re-signed lock file in the same restart. There is no key-ID
  field in this spike; if overlapping keys are ever needed (verify against
  old and new during a transition window), that is an additive
  `signatures: [...]` list change, not a format break.
- **On a rotated key:** packages whose pins were signed by the old key fail
  verification after the new public key lands. The remedy is **re-pin**:
  re-run the signing step over the installed packages (their digests are
  unchanged, so the packages themselves are untouched) and rewrite the lock
  file. This is deliberate — rotation is an operator workflow with an
  operator-sized blast radius, not a silent fallback. A `--force` reinstall
  also re-signs, since install is the only act that produces a new pin.

## 5. What is out of scope for the spike

Named follow-ups, in the order they would matter:

- **Trusted-boot chain.** The public key itself is currently provisioned by
  the same channel that deploys everything else; nothing anchors it. A real
  chain would pin the key (or its fingerprint) in the image build or a
  measured boot mechanism, so the key file on disk cannot be swapped either.
- **Registry service.** No remote source of packages, signatures, or keys is
  assumed. Install remains a local operator act against a local file; a
  registry would add fetch, mirror, and availability questions this spike
  does not answer.
- **Per-plugin revocation.** There is no revocation list and no expiry on a
  signature. Uninstalling a plugin removes its pin; revoking a *publisher*
  today means rotating the key (§4) and re-pinning everything.

## 6. Why not enforced today

Enforcement changes boot behavior: with a signature check in
`_load_plugins`, every existing deployment without a provisioned key fails to
start, and every lock file written before this feature fails verification.
That is a breaking rollout decision — it needs the dependency decision
(`cryptography` vs PyNaCl), the key-provisioning runbook, and a migration
path for existing `plugins.lock.json` files. The spike deliberately leaves
`skills/loader.py` and `skills/cli.py` untouched so the round trip can be
evaluated without any of those commitments. `git status` on this branch shows
exactly two new files: `skills/signing.py` and this document.

## 7. Prototype status

- `skills/signing.py` — pure-Python Ed25519 (RFC 8032) behind
  `_sign_raw`/`_verify_raw`; hex-text key files; `sign_package` /
  `verify_package` round-trip over the real `package_digest` string;
  `load_public_key_from_env` fails closed on a missing variable, file, or
  malformed key.
- Not production-grade: no constant-time arithmetic, no side-channel
  hardening, no PEM/PKCS#8, no key IDs, no audit trail of who signed what.
  Each of those is a reason the production version wants `cryptography` (or
  PyNaCl) plus the key-provisioning runbook in §4 — not a reason to keep the
  pure-Python backend.
- Checks on this spike: `ruff check skills/signing.py` clean, `mypy
  skills/signing.py` clean, full suite 1045 passed (unchanged from `main`).
