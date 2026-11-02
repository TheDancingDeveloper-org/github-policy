"""CI guardrails auditor (WI-3087).

One package, two entry points:

* ``ci-auditor lint`` - the static rules over one checkout; the policy gate
  (github-policy ``policy.yml``) runs it inside every repository.
* ``ci-auditor serve`` - the org-wide auditor: reads workflows, branch rules
  and recent runs for every repository, evaluates every rule minus the live
  exemptions, and serves Prometheus metrics plus one JSON log line per finding.
"""

__version__ = "1.0.0"
