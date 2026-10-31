"""Finding model shared by every analyzer."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

SEVERITIES = ["critical", "high", "medium", "low", "info"]
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class Finding:
    rule: str                      # rule id, see kb.RULES
    severity: str                  # critical|high|medium|low|info
    scope: str                     # plan|compare|settings|health|statements
    subject: str                   # query name, setting name, table name ...
    message: str                   # one-line, concrete (numbers included)
    evidence: List[str] = field(default_factory=list)
    side: Optional[str] = None     # old|new|both|None
    node_ids: List[int] = field(default_factory=list)   # node ids in the plan of `side`
    old_node_ids: List[int] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0             # tie-breaker inside a severity (bigger = more important)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def bump(sev: str, steps: int = 1) -> str:
    return SEVERITIES[max(0, SEV_RANK[sev] - steps)]


def lower(sev: str, steps: int = 1) -> str:
    return SEVERITIES[min(len(SEVERITIES) - 1, SEV_RANK[sev] + steps)]


def sort_findings(fs: List[Finding]) -> List[Finding]:
    return sorted(fs, key=lambda f: (SEV_RANK[f.severity], -f.score, f.rule))


def fmt_num(x: Optional[float]) -> str:
    if x is None:
        return "-"
    if abs(x) >= 1e9:
        return f"{x/1e9:.2f}G"
    if abs(x) >= 1e6:
        return f"{x/1e6:.2f}M"
    if abs(x) >= 1e4:
        return f"{x/1e3:.1f}k"
    if float(x).is_integer():
        return f"{int(x)}"
    return f"{x:.2f}"


def fmt_ms(ms: Optional[float]) -> str:
    if ms is None:
        return "-"
    if ms >= 3600_000:
        return f"{ms/3600_000:.2f} h"
    if ms >= 60_000:
        return f"{ms/60_000:.1f} min"
    if ms >= 1000:
        return f"{ms/1000:.2f} s"
    return f"{ms:.1f} ms"
