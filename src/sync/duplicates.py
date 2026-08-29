"""Finding — and only on request, removing — plays that were recorded twice.

Earlier versions matched plays on the exact second. Services stamp the same
viewing minutes apart, so one watch became one play per service and grew on
every run. That is fixed going forward, but the plays already written are still
there, and nothing in the sync engine will ever remove them: history is a union,
so a duplicate looks exactly like a rewatch it should preserve.

The distinction this module draws is the same one the planner draws, applied
backwards over what is already stored:

    two plays of one episode within the match window   -> one viewing, recorded twice
    two plays of one episode weeks apart               -> two viewings

Scanning is free and safe. Repair is not: deleting a play record cannot be
undone, and the evidence separating a duplicate from a genuine rewatch is a
timestamp window, not a certainty. So the scan is the default and the repair has
to be asked for explicitly — and even then it keeps the earliest play of each
cluster, because that is the one closest to when the viewing actually happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..providers import PLAY_MATCH_WINDOW_SECONDS, item_key, watched_at_epoch


@dataclass
class DuplicateCluster:
    """Several stored plays that are almost certainly one viewing."""

    key: str
    title: str
    media_type: str
    season: object = None
    episode: object = None
    #: Every play in the cluster, earliest first.
    plays: list = field(default_factory=list)

    @property
    def keep(self):
        """The play to keep: the earliest, being closest to the viewing."""
        return self.plays[0] if self.plays else None

    @property
    def redundant(self) -> list:
        return self.plays[1:]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "media_type": self.media_type,
            "season": self.season,
            "episode": self.episode,
            "count": len(self.plays),
            "keep": (self.keep or {}).get("watched_at"),
            "redundant": [play.get("watched_at") for play in self.redundant],
        }


@dataclass
class DuplicateReport:
    clusters: list = field(default_factory=list)
    rows_scanned: int = 0
    episodes_scanned: int = 0
    window_seconds: int = PLAY_MATCH_WINDOW_SECONDS

    @property
    def redundant_plays(self) -> int:
        return sum(len(cluster.redundant) for cluster in self.clusters)

    def to_dict(self, limit: int = 200) -> dict:
        return {
            "rows_scanned": self.rows_scanned,
            "episodes_scanned": self.episodes_scanned,
            "affected_episodes": len(self.clusters),
            "redundant_plays": self.redundant_plays,
            "window_seconds": self.window_seconds,
            "clusters": [cluster.to_dict() for cluster in self.clusters[:limit]],
            "truncated": max(0, len(self.clusters) - limit),
        }


def scan(rows, *, window: int | None = None) -> DuplicateReport:
    """Group stored history into clusters that look like one viewing each.

    Rows with no usable timestamp are ignored rather than clustered: they carry
    watched *state*, so there is nothing to compare and nothing to delete.
    """
    window = PLAY_MATCH_WINDOW_SECONDS if window is None else max(0, int(window))
    report = DuplicateReport(window_seconds=window)

    by_episode: dict[str, list] = {}
    for row in rows or []:
        report.rows_scanned += 1
        epoch = watched_at_epoch(row.get("watched_at"))
        if epoch is None:
            continue
        by_episode.setdefault(item_key(row), []).append((epoch, row))

    report.episodes_scanned = len(by_episode)
    for key, entries in sorted(by_episode.items()):
        entries.sort(key=lambda pair: pair[0])
        cluster: list = []
        clusters: list[list] = []
        last_epoch = None
        for epoch, row in entries:
            # Chained against the previous play, not the first: three records a
            # few minutes apart in sequence are still one viewing.
            if last_epoch is not None and epoch - last_epoch > window:
                clusters.append(cluster)
                cluster = []
            cluster.append(row)
            last_epoch = epoch
        if cluster:
            clusters.append(cluster)

        for group in clusters:
            if len(group) < 2:
                continue
            sample = group[0]
            report.clusters.append(DuplicateCluster(
                key=key,
                title=str(sample.get("title") or "Unknown"),
                media_type=str(sample.get("media_type") or ""),
                season=sample.get("season"),
                episode=sample.get("episode"),
                plays=list(group),
            ))
    return report


def redundant_plays(report: DuplicateReport) -> list:
    """Every play a repair would delete, keeping the earliest of each cluster."""
    return [play for cluster in report.clusters for play in cluster.redundant]
