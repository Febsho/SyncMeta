"""The synchronization engine: baselines, planning, safety and execution.

Split out of ``sync_service.py`` and ``cross_sync.py`` deliberately. Those two
answer "copy this category from A to B"; this package answers the harder
question underneath it — *what actually changed since we last agreed*, which is
the only thing that can tell a user's edit apart from a provider hiccup.

Landing incrementally. What exists today:

* ``models``      — the shared vocabulary: fetch outcomes, item states, baselines
* ``state_store`` — per-route baselines, persisted per profile, advanced only by
                    a run that actually succeeded
* ``planner``     — membership: what changed, not what differs
* ``history``     — watch events: union semantics and three layers of dedupe
* ``progress``    — resume points: never rewind somebody's playback
* ``safety``      — refuse a plan more likely to be a bug than an intention
* ``executor``    — perform a plan and report each action's fate
* ``ownership``   — whether any other route still requires an item
"""
