#!/usr/bin/env python3
"""Tiny static server for previewing generated players locally."""
import functools
import http.server
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
print(f"serving {ROOT} on http://localhost:{port}", flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
