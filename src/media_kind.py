"""Classifying an item as movie / show / anime / anime movie.

The app has only ever modelled TMDB's two namespaces, ``movie`` and ``tv``.
That is not enough to filter a library the way people actually think about it:
an anime film and a live-action film are both ``movie``, and a seasonal anime
and a drama are both ``tv``, yet nobody browsing a watchlist considers those
the same thing.

So "anime" is treated as an orthogonal *flag* on top of the TMDB namespace,
never as a third namespace. That ordering matters — SIMKL does model anime as
its own type, and taking it at face value is exactly how anime films ended up
stored as fake one-episode TV. The namespace comes from TMDB (or from Fribb's
mapping key, which agrees with TMDB); the anime flag comes from evidence that
has nothing to do with the namespace:

* an anime-native id on the item (AniList, MAL, AniDB), or
* the source explicitly calling it anime (SIMKL's ``anime`` type), or
* a hit in the Fribb anime mapping, which only contains anime.

The four resulting kinds are what the Library filters on.
"""

from __future__ import annotations

KIND_MOVIE = "movie"
KIND_SHOW = "show"
KIND_ANIME = "anime"
KIND_ANIME_MOVIE = "anime_movie"

ALL_KINDS = (KIND_MOVIE, KIND_SHOW, KIND_ANIME, KIND_ANIME_MOVIE)

KIND_LABELS = {
    KIND_MOVIE: "Movies",
    KIND_SHOW: "Shows",
    KIND_ANIME: "Anime",
    KIND_ANIME_MOVIE: "Anime movies",
}

#: Item fields that, when set, prove the item is anime. None of these exist for
#: a non-anime title, so their presence alone is sufficient.
_ANIME_ID_FIELDS = ("anilist_id", "mal_id", "anidb_id")


def normalize_namespace(value: object) -> str:
    """Reduce any provider's media-type spelling to ``movie`` or ``tv``.

    ``anime`` deliberately maps to ``tv`` here rather than to a kind of its own:
    this function answers "which TMDB namespace", and the anime-ness is carried
    separately by :func:`is_anime`. Callers that need the namespace for an anime
    *film* must pass the mapped TMDB media type, not the source's word for it.
    """
    text = str(value or "").strip().lower()
    if text in {"movie", "film", "movies"}:
        return "movie"
    return "tv"


def is_anime(item: dict) -> bool:
    """Whether ``item`` is anime, from ids and source labelling only."""
    if not isinstance(item, dict):
        return False
    for field in _ANIME_ID_FIELDS:
        if item.get(field):
            return True
    ids = item.get("ids")
    if isinstance(ids, dict):
        for field in ("anilist", "mal", "anidb"):
            if ids.get(field):
                return True
    if bool(item.get("is_anime")):
        return True
    # SIMKL is the one provider that reports anime as its own media type, and
    # it is right about *that* even though it is wrong about the namespace.
    for field in ("simkl_type", "source_media_type", "provider_media_type"):
        if str(item.get(field) or "").strip().lower() == "anime":
            return True
    return False


def classify(item: dict, *, namespace: str | None = None) -> str:
    """Return one of :data:`ALL_KINDS` for ``item``.

    ``namespace`` overrides the item's own media type, for callers that have
    already resolved the authoritative TMDB namespace (the Fribb mapping key,
    for instance, which is more trustworthy than the entry's own ``type``).
    """
    if not isinstance(item, dict):
        return KIND_SHOW
    space = normalize_namespace(
        namespace if namespace is not None else item.get("media_type")
    )
    anime = is_anime(item)
    if space == "movie":
        return KIND_ANIME_MOVIE if anime else KIND_MOVIE
    return KIND_ANIME if anime else KIND_SHOW


def matches_filter(kind: str, selected: object) -> bool:
    """Whether ``kind`` passes a UI filter selection.

    An empty selection means "everything", so a filter nobody has touched never
    hides anything.
    """
    if not selected:
        return True
    wanted = selected if isinstance(selected, (list, tuple, set)) else [selected]
    cleaned = {str(value).strip().lower() for value in wanted if str(value).strip()}
    if not cleaned or "all" in cleaned:
        return True
    return str(kind).strip().lower() in cleaned
