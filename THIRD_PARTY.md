# Bundled components

The packaged Windows extension contains Python 3.13.15 from the Python Software Foundation's official distribution, including its `LICENSE.txt`, and the packages pinned in `requirements.txt`.

Package licenses and notices are retained in `runtime/vendor/*-*.dist-info/` and, where supplied, in the package directories. Python is under the PSF license; aiohttp, aiohappyeyeballs, aiosignal, multidict, and yarl use Apache-2.0; attrs, frozenlist, and propcache use MIT; idna uses BSD-3-Clause; zstandard uses BSD-3-Clause and includes notices for its bundled compression library. Consult the included notices for authoritative terms.

The official Codex executable is supplied by the user's existing OpenAI extension and is not redistributed here.
