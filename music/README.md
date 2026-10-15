# AutoDJ music library

1. Copy your licensed audio files into this directory.
2. Copy `playlist.txt.example` to `playlist.txt`.
3. Add one exact filename per line, relative to this directory. Blank lines and lines starting with `#` are ignored.
4. Commit the audio files and playlist when deploying with the image, or mount this directory as `/music` if operating Docker on a persistent host.

The server loops the playlist continuously. AutoDJ starts automatically when the backend starts and resumes after a live DJ disconnects. Keep audio files within this directory; path traversal entries are ignored.
