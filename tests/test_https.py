import io
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from doorcontrol import tls
from doorcontrol.app import create_app, make_admin
from doorcontrol.server import Runner

H = {"X-Requested-With": "doorcontrol"}


def make_ca_and_leaf(names=("*.example.test",), days=200, root="Test Root", inter="Test Intermediate"):
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, root)])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=900))
          .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    im_key = ec.generate_private_key(ec.SECP256R1())
    im_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, inter)])
    im = (x509.CertificateBuilder().subject_name(im_name).issuer_name(ca_name).public_key(im_key.public_key())
          .serial_number(2).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=800))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(im_key.public_key()), critical=False)
          .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(im_name).public_key(key.public_key()).serial_number(3)
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(im_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(im_key, hashes.SHA256()))
    return ca, im, leaf, key


def pem(c):
    return c.public_bytes(serialization.Encoding.PEM)


def key_pem(k, pw=None):
    enc = serialization.BestAvailableEncryption(pw.encode()) if pw else serialization.NoEncryption()
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)


def pfx(key, leaf, extra, pw):
    return pkcs12.serialize_key_and_certificates(b"x", key, leaf, extra, serialization.BestAvailableEncryption(pw.encode()))


def test_parse_pfx_with_chain():
    ca, im, leaf, key = make_ca_and_leaf()
    k, chain = tls.parse([pfx(key, leaf, [im, ca], "geheim")], "geheim")
    assert chain[0] == leaf and chain[1] == im and len(chain) == 2  # root is left out
    i = tls.info(chain[0], len(chain), "door.example.test")
    assert i["domain_ok"] and i["chain_ok"] and not i["self_signed"]


def test_parse_pfx_wrong_password():
    ca, im, leaf, key = make_ca_and_leaf()
    with pytest.raises(tls.CertError, match="password"):
        tls.parse([pfx(key, leaf, [im], "geheim")], "fout")


def test_parse_pem_pair_and_encrypted_key():
    ca, im, leaf, key = make_ca_and_leaf()
    k, chain = tls.parse([pem(leaf) + pem(im), key_pem(key)])
    assert len(chain) == 2
    k, chain = tls.parse([pem(leaf), key_pem(key, "pw1")], "pw1")
    assert chain[0] == leaf
    with pytest.raises(tls.CertError, match="password"):
        tls.parse([pem(leaf), key_pem(key, "pw1")], "")


def test_key_must_match():
    _, _, leaf, _ = make_ca_and_leaf()
    _, _, _, other = make_ca_and_leaf()
    with pytest.raises(tls.CertError, match="does not belong"):
        tls.parse([pem(leaf), key_pem(other)])


def test_missing_key():
    _, _, leaf, _ = make_ca_and_leaf()
    with pytest.raises(tls.CertError, match="private key"):
        tls.parse([pem(leaf)])


def test_wildcard_matching():
    _, _, leaf, _ = make_ca_and_leaf(("*.example.test", "example.test"))
    assert tls.covers(leaf, "door.example.test")
    assert tls.covers(leaf, "example.test")
    assert not tls.covers(leaf, "a.b.example.test")
    assert not tls.covers(leaf, "example.other")


def test_api_import_and_redirect_rules(tmp_path):
    data = tmp_path / "d"
    make_admin(str(data), "admin", "adminpass")
    app = create_app(str(data), https_port=8443)
    c = app.test_client()
    c.post("/api/login", json={"username": "admin", "password": "adminpass"}, headers=H)
    ca, im, leaf, key = make_ca_and_leaf()
    r = c.post("/api/admin/https", data={"files": (io.BytesIO(pfx(key, leaf, [im], "pw")), "c.pfx"), "password": "pw",
                                          "domain": "door.example.test"}, headers=H, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    st = r.get_json()
    assert st["cert"]["domain_ok"] and st["domain"] == "door.example.test"
    assert st["url"] == "https://door.example.test:8443"
    # no https server in the test client, so the redirect may not be switched on (would lock people out)
    r = c.put("/api/admin/https", json={"domain": "door.example.test", "redirect": True}, headers=H)
    assert r.status_code == 400
    bad = c.post("/api/admin/https", data={"files": (io.BytesIO(b"nonsense"), "x.crt")}, headers=H, content_type="multipart/form-data")
    assert bad.status_code == 400
    assert c.delete("/api/admin/https", headers=H).get_json()["cert"] is None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_runner_starts_https_after_import(tmp_path):
    data = tmp_path / "d"
    make_admin(str(data), "admin", "adminpass")
    hp, sp = free_port(), free_port()
    runner = Runner(str(data), "127.0.0.1", hp, sp)
    threading.Thread(target=runner.run, daemon=True).start()
    base = f"http://127.0.0.1:{hp}"
    for _ in range(50):
        try:
            requests.get(base + "/api/branding", timeout=1)
            break
        except requests.RequestException:
            time.sleep(0.1)
    assert not runner.https_running()

    s = requests.Session()
    s.headers.update(H)
    assert s.post(base + "/api/login", json={"username": "admin", "password": "adminpass"}).ok
    ca, im, leaf, key = make_ca_and_leaf(("localhost",))
    files = [("files", ("c.crt", pem(leaf) + pem(im))), ("files", ("c.key", key_pem(key)))]
    r = s.post(base + "/api/admin/https", files=files, data={"domain": ""})
    assert r.ok, r.text
    assert r.json()["running"]

    # https answers with our certificate chain
    ctx = ssl.create_default_context(cadata=pem(ca).decode())
    with socket.create_connection(("127.0.0.1", sp), timeout=3) as raw:
        with ctx.wrap_socket(raw, server_hostname="localhost") as t:
            t.sendall(b"GET /api/branding HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            body = b""
            while chunk := t.recv(4096):
                body += chunk
    assert b"200 OK" in body and b"app_name" in body

    # replacing the certificate restarts https in place
    ca2, im2, leaf2, key2 = make_ca_and_leaf(("localhost",))
    r = s.post(base + "/api/admin/https", files=[("files", ("c.crt", pem(leaf2) + pem(im2))), ("files", ("c.key", key_pem(key2)))],
               data={"domain": ""})
    assert r.json()["running"]
    ctx2 = ssl.create_default_context(cadata=pem(ca2).decode())
    with socket.create_connection(("127.0.0.1", sp), timeout=3) as raw:
        with ctx2.wrap_socket(raw, server_hostname="localhost") as t:
            assert t.getpeercert()["subject"][0][0][1] == "localhost"

    # removing it stops https
    assert s.delete(base + "/api/admin/https").json()["running"] is False
