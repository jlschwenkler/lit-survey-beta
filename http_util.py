"""
http_util.py — shared HTTP settings for the scholarly-API calls.

Two things every networking script in this pipeline needs, centralized here so
they are configured (and audited) in ONE place:

1. CONTACT EMAIL for the "polite pool".
   OpenAlex and Crossref ask you to identify yourself with a `mailto=` so they
   can route you to faster, more reliable infrastructure and contact you if your
   crawler misbehaves. It is NOT a secret and NOT authentication — just courtesy.
   Set it to a real address you control:

       export CROSSREF_MAILTO="you@example.com"

   If unset, a placeholder is used (which still works, but please set your own).

2. TLS VERIFICATION.
   By DEFAULT this tool verifies TLS certificates (the correct, secure behavior).
   Some networks (e.g. corporate TLS-interception proxies, or certain macOS
   Python builds with a stale cert bundle) break verification. ONLY on such a
   network, you may opt out with:

       export INSECURE_TLS=1

   That disables certificate checking for this tool's requests. Do not set it on
   an untrusted network. It is off by default for a reason.
"""

import os
import requests

# The polite-pool contact. Real but non-secret; override via env.
MAILTO = os.environ.get("CROSSREF_MAILTO", "you@example.com")

# Default: verify TLS. Opt out only on networks that break verification.
VERIFY_TLS = os.environ.get("INSECURE_TLS", "") not in ("1", "true", "True")

if not VERIFY_TLS:
    # Suppress the (now expected) noise when the user has deliberately opted out.
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def make_session(extra_headers=None):
    """A requests.Session pre-configured with the polite User-Agent and the
    project-wide TLS verification policy."""
    s = requests.Session()
    s.verify = VERIFY_TLS
    s.headers["User-Agent"] = f"litreview-pipeline (mailto:{MAILTO})"
    if extra_headers:
        s.headers.update(extra_headers)
    return s
