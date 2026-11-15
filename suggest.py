"""Turn Last.fm tags and existing genre tags into ranked genre suggestions.

Only names on the curated list in genres.txt (beets' lastgenre list, MIT
licensed, see LICENSE-beets) or in genres-extra.txt (common genres that
list lacks) can become suggestions, so tags such as
"seen live" or "favorites" never appear. Names are matched loosely
(case, hyphens and spaces) and shown in a consistent title case.
"""

import re
from pathlib import Path

GENRES_FILE = Path(__file__).resolve().parent / "genres.txt"
EXTRA_GENRES_FILE = Path(__file__).resolve().parent / "genres-extra.txt"

# Weight multipliers per source; album tags are the most specific.
ARTIST_TAG_FACTOR = 0.6
CURRENT_GENRE_WEIGHT = 40
EXTRA_SOURCE_BONUS = 10
MIN_WEIGHT = 5
MAX_LISTED = 10

UPPERCASE_WORDS = {
    "dj", "ebm", "edm", "idm", "nwobhm", "r&b", "uk", "us", "usa",
}
LOWERCASE_WORDS = {"and", "n", "of", "the", "in", "de", "la"}
# Common spellings (Deezer and Last.fm) that the curated list names
# differently: alias -> list entry.
ALIASES = {
    "r&b": "rhythm and blues",
    "rnb": "rhythm and blues",
    "hip-hop": "hip hop",
    "hiphop": "hip hop",
    "rock 'n' roll": "rock and roll",
    "rock n roll": "rock and roll",
    "films": "soundtrack",
    "film scores": "film score",
    "games": "video game music",
    "musicals": "musical theatre",
    "singer & songwriter": "singer-songwriter",
    "kids": "children's music",
    "latin music": "latin",
    "nü metal": "nu metal",
}


def lookup_key(name: str) -> str:
    """Loose matching key: lower case, '&' as 'and', separators collapsed."""
    key = name.lower().replace("&", " and ")
    return re.sub(r"[\s\-_/.]+", " ", key).strip()


def pretty_name(name: str) -> str:
    """Title-case a list entry: 'hip hop' -> 'Hip Hop', 'j-pop' -> 'J-Pop'."""
    words = []
    for index, word in enumerate(name.split()):
        if word in UPPERCASE_WORDS:
            words.append(word.upper())
        elif index and word in LOWERCASE_WORDS:
            words.append(word)
        else:
            words.append("-".join(part[:1].upper() + part[1:]
                                  for part in word.split("-")))
    return " ".join(words)


def load_whitelist(paths=(GENRES_FILE, EXTRA_GENRES_FILE)) -> dict:
    """Map lookup_key -> display name for every curated genre."""
    whitelist = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if name and not name.startswith("#"):
                whitelist.setdefault(lookup_key(name), pretty_name(name))
    for alias, target in ALIASES.items():
        display = whitelist.get(lookup_key(target))
        if display:
            whitelist.setdefault(lookup_key(alias), display)
    return whitelist


def split_genre_values(values) -> list:
    """Split joined genre strings ('Rock/Pop; Folk') into single names."""
    names = []
    for value in values:
        for part in re.split(r"\s*[/;,|]\s*", value):
            part = part.strip()
            if part and part not in names:
                names.append(part)
    return names


def build_suggestions(album_tags, artist_tags, current_genres,
                      whitelist) -> list:
    """Rank curated genres for one album.

    album_tags / artist_tags: [(tag name, Last.fm count 0-100), ...].
    current_genres: genre values currently in the album's files.
    Returns [{"name", "weight", "sources"}] best first, at most MAX_LISTED.
    """
    found = {}

    def add(name, weight, source):
        display = whitelist.get(lookup_key(name))
        if display is None or weight <= 0:
            return
        entry = found.setdefault(display, {"weights": {}, "name": display})
        entry["weights"][source] = max(entry["weights"].get(source, 0),
                                       weight)

    for name, count in album_tags:
        add(name, int(count), "album")
    for name, count in artist_tags:
        add(name, int(count) * ARTIST_TAG_FACTOR, "artist")
    for name in split_genre_values(current_genres):
        add(name, CURRENT_GENRE_WEIGHT, "current")

    suggestions = []
    for entry in found.values():
        weights = entry["weights"]
        score = max(weights.values())
        score += EXTRA_SOURCE_BONUS * (len(weights) - 1)
        score = min(100, round(score))
        if score >= MIN_WEIGHT:
            suggestions.append({
                "name": entry["name"],
                "weight": score,
                "sources": sorted(weights),
                "album_weight": round(weights.get("album", 0)),
            })
    suggestions.sort(key=lambda item: (-item["weight"], item["name"]))
    return suggestions[:MAX_LISTED]


def preselected(suggestions, limit: int, min_weight: int = 25) -> list:
    """Names ticked by default: up to limit names at or above min_weight.

    The best suggestion is always ticked when there is one.
    """
    names = [item["name"] for item in suggestions
             if item["weight"] >= min_weight][:limit]
    if not names and suggestions and limit:
        names = [suggestions[0]["name"]]
    return names
