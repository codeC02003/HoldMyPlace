"""Hold My Place: a claim queue for post-payment out-of-stock lines.

When a paid delivery line goes out of stock, the money is refunded and the
demand is discarded. This package models keeping the demand: a claim queue
allocated first-in-first-out, filtered by a deadline the member declares
themselves, fulfilled by whichever mode is cheapest given the slack that
deadline allows.

The package is a simulation, not a production system. Every economic input is
declared in `holdmyplace.domain.economics.Assumptions` with its provenance
marked, because the conclusion depends far more on those numbers than on any
code here.
"""

__all__ = ["domain", "sim"]
