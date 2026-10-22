import os
import threading

from cheroot import wsgi
from cheroot.ssl.builtin import BuiltinSSLAdapter

from . import tls
from .app import create_app


class Runner:
    """Plain http always runs. https runs next to it as soon as there is a certificate,
    and is restarted in place when the certificate changes in settings."""

    def __init__(self, data_dir, host, port, https_port):
        self.data_dir = data_dir
        self.host = host
        self.port = port
        self.https_port = https_port
        self.lock = threading.Lock()
        self.https = None
        self.https_error = ""
        self.app = create_app(data_dir, https_port=https_port, tls_control=self)

    def _server(self, port):
        return wsgi.Server((self.host, port), self.app, numthreads=16, server_name="doorcontrol")

    def https_running(self):
        return self.https is not None and self.https.ready

    def reload_tls(self):
        with self.lock:
            if self.https is not None:
                self.https.stop()
                self.https = None
            self.https_error = ""
            p = tls.paths(self.data_dir)
            if not p or not self.https_port:
                return
            try:
                srv = self._server(self.https_port)
                srv.ssl_adapter = BuiltinSSLAdapter(p[0], p[1])
                srv.prepare()
            except Exception as e:
                self.https_error = str(e)
                print(f"https not started: {e}", flush=True)
                return
            threading.Thread(target=srv.serve, daemon=True).start()
            self.https = srv
            print(f"https on port {self.https_port}", flush=True)

    def run(self):
        self.reload_tls()
        http = self._server(self.port)
        print(f"Door Control on http port {self.port} (data in {self.data_dir})", flush=True)
        try:
            http.start()
        except KeyboardInterrupt:
            pass
        finally:
            http.stop()
            if self.https:
                self.https.stop()


def serve(data_dir, host, port, https_port):
    os.makedirs(data_dir, exist_ok=True)
    Runner(data_dir, host, port, https_port).run()
