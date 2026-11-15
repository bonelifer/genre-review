# genre-review

A small web page for cleaning up the genre tags in a music library one album
at a time. It suggests genres from Last.fm, you pick the ones you want, and
it writes only the genre tag into the files. Other pages list every tag
field in the library (removing the ones you don't want, with undo) and fill
missing lyrics from LRCLIB.

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

## Tag cleanup

The **Tags** page lists every tag field found in the library, with how many
files and albums carry it and a few example values. Click an album count to
see which albums have that field.

Tick fields you don't want (record labels, barcodes, encoder notes, and so
on) and save. The page then shows how many MP3 files and albums would
change, and nothing is touched until you press **Remove these tags from the
files**. Removal runs in the background and asks MPD to rescan what it
changed.

- Core fields can't be removed: titles, artists, album, track and disc
  numbers, dates, genres, cover art, ReplayGain and MusicBrainz IDs.
- Every removed tag is stored byte for byte in SQLite. **Restore** puts a
  field back across the library, and each album page can restore its own
  tags.
- Only MP3 files are cleaned. Other formats show up in the list but are left
  alone.
- An ID3v2.3 file stays ID3v2.3. The audio is not touched.
- New downloads are cleaned only when you press the button again.

## Lyrics

The **Lyrics** page shows how many tracks have lyrics (a `.lrc` or `.txt`
file beside them, or lyrics in their tags) and lists the albums still
missing some. **Fill missing lyrics** looks those tracks up on
[LRCLIB](https://lrclib.net/) in the background:

- Synced lyrics are saved as `Song.lrc` beside `Song.mp3`; plain lyrics as
  `Song.txt`. This is where myMPD and most players look.
- Existing lyrics files and embedded lyrics are never replaced.
- For live takes and other versions whose length doesn't match any LRCLIB
  entry, the plain words from the closest match are saved as `.txt`, since
  synced timings wouldn't fit.
- Requests go one at a time with a short pause and identify this tool, as
  LRCLIB asks for batch work. A first fill of about 13,000 tracks takes a
  few hours; the fill can be stopped and continues where it left off.
- Tracks LRCLIB doesn't have are tried again after 90 days.
- **Remove written lyrics files** deletes only files genre-review wrote and
  that are still unchanged.

With `LYRICS_AUTO_FILL=1`, new tracks are looked up after each scan (up to
`LYRICS_AUTO_LIMIT` per scan).

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
