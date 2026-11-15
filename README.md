# genre-review

A small web page for cleaning up the genre tags in a music library one album
at a time. It suggests genres from Last.fm, you pick the ones you want, and
it writes only the genre tag into the files.

Suggestions come from the album's Last.fm tags, with the artist's tags filling
in when the album has few. Your current genre tags count too. Every name is
checked against a curated list of real genres, so Last.fm tags such as "seen
live" or "favorites" never show up. Joined values like `Rock/Pop/Indie Rock`
are split, and approved genres are written as separate values. MPD and myMPD
then list each genre on its own.

## What it does

- Scans the library into albums (album artist + album, across `CD 1`/`CD 2`
  folders). It rescans every hour by default, and only re-reads files that
  changed.
- Looks up Last.fm once per album and caches the answers for 90 days.
- Shows one album at a time, with the best suggestions (up to three) already
  ticked. You can add your own genres, skip the album, or never be asked
  about it again.
- Writes only the genre tag: MP3s as ID3v2.4, so the values stay separate;
  FLAC, Ogg and M4A as one genre field per value.
- Saves each track's genres from before the first write, so every album has
  a **Revert** button.
- Asks MPD to rescan just that album's folder after writing.
- Flags an album when something else changes its genres after you approved
  them.
- **Bulk accept** previews albums whose own Last.fm tags agree strongly, so
  you can approve many at once. Nothing is written until you confirm.

## Setup

GitHub Actions builds the image on every push to `main` and publishes it as
`ghcr.io/bonelifer/genre-review:latest`. `compose.yaml` uses that image.

```bash
cp .env.example .env
mkdir data
```

`data/` holds the SQLite state. Creating it yourself keeps it owned by
your user, since the container runs as UID 1000. In `.env`, set
`LASTFM_API_KEY`, `MUSIC_LIBRARY` (the host folder to review) and
`MPD_PATH_PREFIX` (where that folder sits inside MPD's `music_directory`).
Then start it:

```bash
docker compose up -d
```

Open `http://127.0.0.1:18771/`. The page listens on 127.0.0.1 only and has no
login, so don't expose it to a network. The first Last.fm pass takes a few
minutes per thousand albums; the overview page shows its progress.

Update a running copy with `docker compose pull && docker compose up -d`.

Without Docker, install Mutagen and run the script directly:

```bash
python3 -m pip install mutagen==1.48.1
LIBRARY_DIR=~/Music/library LASTFM_API_KEY=... ./genre_review.py serve
```

`./genre_review.py scan` and `./genre_review.py suggest` run one pass and
exit. `./genre_review.py --help` lists every option; all settings are in
[`.env.example`](.env.example).

## Before the first write

Writing a tag rewrites the file's tag block. The audio is not touched, but
each file's modification time changes, and ID3v2.3 MP3s are saved as v2.4.
Picard, MPD and most players read v2.4. If you tag with Picard afterwards,
check its genre settings so it doesn't overwrite your choices.

## Tests

```bash
tests/docker-test.sh
```

This runs flake8 and the offline test suite in throwaway containers on
Python 3.10 and the newest Python. Last.fm and MPD are faked; the tests
write tags to tiny generated MP3 and FLAC files.

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/bonelifer/genre-review/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/bonelifer/genre-review/discussions).
- Pull requests are welcome for bug fixes or discussed features.

## Acknowledgments

- The curated genre list (`genres.txt`) comes from the
  [beets](https://beets.io/) lastgenre plugin, under the MIT license
  ([LICENSE-beets](LICENSE-beets)).
- Code, testing, and documentation assisted by [Claude](https://www.anthropic.com/claude).

## License

This project is licensed under the **GNU General Public License v3.0**.

See [LICENSE](LICENSE) for more information. `genres.txt` keeps its MIT
license; see [LICENSE-beets](LICENSE-beets).
