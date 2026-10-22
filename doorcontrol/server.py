import os

from cheroot import wsgi
from cheroot.ssl.builtin import BuiltinSSLAdapter

from .app import create_app


def serve(data_dir, host, port, cert="", key=""):
    tls = bool(cert and key and os.path.exists(cert) and os.path.exists(key))
    app = create_app(data_dir, secure_cookies=True if tls else None)
    server = wsgi.Server((host, port), app, numthreads=16, server_name="doorcontrol")
    scheme = "http"
    if tls:
        server.ssl_adapter = BuiltinSSLAdapter(cert, key)
        scheme = "https"
    print(f"Door Control listening on {scheme}://{host}:{port} (data in {data_dir})", flush=True)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
