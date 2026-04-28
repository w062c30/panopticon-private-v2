from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    market_id: str
    cluster_id: str
    kelly_fraction: float


def allocate_kelly_with_correlation(
    *,
    proposed_kelly: float,
    cluster_id: str,
    inventory: list[Position],
    correlation: float | None = None,
    rho_matrix: float | None = None,
    fallback_rho_when_unknown: float = 0.95,
    threshold: float = 0.85,
) -> float:
    """
    Share Kelly budget when inventory in the same cluster is highly correlated.
    Prefer ``rho_matrix`` from DB rolling correlation; else ``correlation``; else fallback.
    """
    related = [p for p in inventory if p.cluster_id == cluster_id]
    if not related:
        return proposed_kelly

    if rho_matrix is not None:
        rho = rho_matrix
    elif correlation is not None:
        rho = correlation
    else:
        rho = fallback_rho_when_unknown

    if rho < threshold:
        return proposed_kelly

    total_existing = sum(p.kelly_fraction for p in related)
    budget = max(0.0, 0.25 - total_existing)
    return min(proposed_kelly, budget / 2)
