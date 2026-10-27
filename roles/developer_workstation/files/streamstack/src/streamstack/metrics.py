"""A tiny Prometheus text exposition (format 0.0.4): families, counters, gauges and histograms, standard library only.

Two kinds of metric live side by side:

* stateful instruments (`Counter`, `Histogram`) that the program updates as things happen, and
* one-shot `Family` objects a collector builds from current state at scrape time.

`render(families)` turns both into the text a Prometheus server scrapes. Names and label names are checked when a
family is built, label values are escaped, and a family never renders twice, so the output always parses.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
LABEL = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
TYPES = ("counter", "gauge", "histogram")
DEFAULT_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
Labels = Mapping[str, str]


def escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def escape_value(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def number(value: float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def _labels(labels: Labels) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{escape_value(str(v))}"' for k, v in labels.items()) + "}"


@dataclass
class Family:
    """One metric name with its help text, type and samples (suffix, labels, value)."""

    name: str
    kind: str
    help: str
    samples: list[tuple[str, dict[str, str], float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not NAME.fullmatch(self.name):
            raise ValueError(f"{self.name!r} is not a valid metric name")
        if self.kind not in TYPES:
            raise ValueError(f"{self.kind!r} is not a metric type")

    def add(self, value: float, labels: Labels | None = None, suffix: str = "") -> Family:
        labels = dict(labels or {})
        for key in labels:
            if not LABEL.fullmatch(key) or key.startswith("__"):
                raise ValueError(f"{key!r} is not a valid label name")
        self.samples.append((suffix, {k: str(v) for k, v in labels.items()}, float(value)))
        return self

    def render(self) -> str:
        lines = [f"# HELP {self.name} {escape_help(self.help)}", f"# TYPE {self.name} {self.kind}"]
        for suffix, labels, value in self.samples:
            lines.append(f"{self.name}{suffix}{_labels(labels)} {number(value)}")
        return "\n".join(lines) + "\n"


def gauge(name: str, help_text: str, value: float | None = None, labels: Labels | None = None) -> Family:
    family = Family(name, "gauge", help_text)
    if value is not None:
        family.add(value, labels)
    return family


def render(families: Iterable[Family]) -> str:
    """The exposition text; a family with no samples is left out and a repeated name is a programming error."""
    seen: set[str] = set()
    out: list[str] = []
    for family in families:
        if family.name in seen:
            raise ValueError(f"metric {family.name} is exposed twice")
        seen.add(family.name)
        if family.samples:
            out.append(family.render())
    return "".join(out)


class Counter:
    """A monotonically increasing value per label set."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = (), max_series: int = 200) -> None:
        self.name, self.help, self.label_names = name, help_text, label_names
        self.max_series = max_series
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(str(labels.get(n, "")) for n in self.label_names)
        with self._lock:
            if key not in self._values and len(self._values) >= self.max_series:
                key = tuple("other" for _ in self.label_names)  # bounded: a bug can never grow memory without limit
            self._values[key] = self._values.get(key, 0.0) + amount

    def collect(self) -> Family:
        family = Family(self.name, "counter", self.help)
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            family.add(value, dict(zip(self.label_names, key, strict=True)))
        return family


class Histogram:
    """Observations in cumulative buckets, with a sum and a count, per label set."""

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
        max_series: int = 100,
    ) -> None:
        if list(buckets) != sorted(set(buckets)):
            raise ValueError("buckets must be increasing")
        self.name, self.help, self.label_names, self.buckets = name, help_text, label_names, buckets
        self.max_series = max_series
        self._series: dict[tuple[str, ...], list[float]] = {}  # per key: bucket counts..., sum, count
        self._lock = threading.Lock()

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(str(labels.get(n, "")) for n in self.label_names)
        with self._lock:
            if key not in self._series and len(self._series) >= self.max_series:
                key = tuple("other" for _ in self.label_names)
            row = self._series.setdefault(key, [0.0] * (len(self.buckets) + 2))
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    row[i] += 1
            row[-2] += value
            row[-1] += 1

    def collect(self) -> Family:
        family = Family(self.name, "histogram", self.help)
        with self._lock:
            series = sorted((k, list(v)) for k, v in self._series.items())
        for key, row in series:
            base = dict(zip(self.label_names, key, strict=True))
            for bound, count in zip(self.buckets, row, strict=False):
                family.add(count, {**base, "le": number(bound)}, "_bucket")
            family.add(row[-1], {**base, "le": "+Inf"}, "_bucket")
            family.add(row[-2], base, "_sum")
            family.add(row[-1], base, "_count")
        return family
