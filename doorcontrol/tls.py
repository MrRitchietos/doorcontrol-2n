"""Certificate import for https: .pfx/.p12, or a certificate + key in PEM (or DER)."""

import os
from datetime import datetime, timezone

import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID, AuthorityInformationAccessOID

CERT_FILE = "tls.crt"
KEY_FILE = "tls.key"


class CertError(Exception):
    pass


def _password(pw):
    return pw.encode() if pw else None


def _load_certs(raw):
    if b"-----BEGIN CERTIFICATE-----" in raw:
        return x509.load_pem_x509_certificates(raw)
    try:
        return [x509.load_der_x509_certificate(raw)]
    except ValueError:
        return []


def _load_key(raw, pw):
    if b"PRIVATE KEY-----" not in raw:
        try:
            return serialization.load_der_private_key(raw, _password(pw))
        except (ValueError, TypeError):
            return None
    try:
        return serialization.load_pem_private_key(raw, _password(pw))
    except TypeError:
        raise CertError("the key is protected, fill in its password")
    except ValueError:
        raise CertError("the key password is wrong or the key is damaged")


def parse(files, password=""):
    """files = list of raw bytes. Returns (key, [leaf, intermediates...])."""
    key = None
    certs = []
    for raw in files:
        if not raw:
            continue
        if b"-----BEGIN" not in raw:
            # binary: try pfx first
            try:
                k, c, extra = pkcs12.load_key_and_certificates(raw, _password(password))
                key = key or k
                if c:
                    certs.append(c)
                certs.extend(extra or [])
                continue
            except ValueError as e:
                if "password" in str(e).lower() or "mac" in str(e).lower():
                    raise CertError("the .pfx password is wrong")
        if b"PRIVATE KEY-----" in raw:
            key = key or _load_key(raw, password)
        found = _load_certs(raw)
        if not found and b"-----BEGIN" not in raw and key is None:
            key = _load_key(raw, password)
            if key is None:
                raise CertError("could not read this file, use .pfx, .p12, .crt, .pem or .key")
        certs.extend(found)
    if not certs:
        raise CertError("no certificate found in the file(s)")
    if key is None:
        raise CertError("no private key found, add the .key file or use a .pfx")

    pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    leaf = None
    for c in certs:
        if c.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) == pub:
            leaf = c
            break
    if leaf is None:
        raise CertError("the private key does not belong to this certificate")
    rest = [c for c in certs if c is not leaf and not _self_signed(c)]
    return key, _order_chain(leaf, rest)


def _self_signed(c):
    return c.issuer == c.subject


def _order_chain(leaf, others):
    chain = [leaf]
    pool = list(others)
    while pool:
        nxt = next((c for c in pool if c.subject == chain[-1].issuer), None)
        if not nxt:
            break
        chain.append(nxt)
        pool.remove(nxt)
    return chain


def complete_chain(chain):
    """Phones need the intermediate certificate. When it is missing, fetch it from the address in the cert."""
    tries = 0
    while not _self_signed(chain[-1]) and tries < 3:
        tries += 1
        try:
            aia = chain[-1].extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
        except x509.ExtensionNotFound:
            break
        urls = [d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
        got = None
        for url in urls:
            if not url.startswith("http"):
                continue
            try:
                r = requests.get(url, timeout=8)
                if r.ok:
                    found = _load_certs(r.content)
                    got = found[0] if found else None
            except requests.RequestException:
                pass
            if got:
                break
        if not got or _self_signed(got):
            # stop at the root, browsers have those built in
            break
        chain.append(got)
    return chain


def names(cert):
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        out = san.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        out = []
    if not out:
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        out = [cn[0].value] if cn else []
    return out


def covers(cert, host):
    host = (host or "").lower().strip(".")
    for n in names(cert):
        n = n.lower()
        if n == host:
            return True
        if n.startswith("*.") and host.count(".") == n.count(".") and host.endswith(n[1:]):
            return True
    return False


def info(cert, chain_len=1, domain=""):
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    issuer = cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME) or cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    end = cert.not_valid_after_utc
    return {
        "subject": cn[0].value if cn else "",
        "names": names(cert),
        "issuer": issuer[0].value if issuer else "",
        "not_after": end.strftime("%Y-%m-%d"),
        "days_left": (end - datetime.now(timezone.utc)).days,
        "self_signed": _self_signed(cert),
        "chain_ok": _self_signed(cert) or chain_len > 1,
        "domain_ok": covers(cert, domain) if domain else None,
    }


def save(data_dir, key, chain):
    kp = os.path.join(data_dir, KEY_FILE)
    cp = os.path.join(data_dir, CERT_FILE)
    with open(kp + ".new", "wb") as f:
        os.chmod(kp + ".new", 0o600)
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cp + ".new", "wb") as f:
        for c in chain:
            f.write(c.public_bytes(serialization.Encoding.PEM))
    os.replace(kp + ".new", kp)
    os.replace(cp + ".new", cp)


def remove(data_dir):
    for n in (CERT_FILE, KEY_FILE):
        p = os.path.join(data_dir, n)
        if os.path.exists(p):
            os.remove(p)


def paths(data_dir):
    cp = os.path.join(data_dir, CERT_FILE)
    kp = os.path.join(data_dir, KEY_FILE)
    if os.path.exists(cp) and os.path.exists(kp):
        return cp, kp
    return None


def current(data_dir, domain=""):
    p = paths(data_dir)
    if not p:
        return None
    with open(p[0], "rb") as f:
        chain = _load_certs(f.read())
    if not chain:
        return None
    return info(chain[0], len(chain), domain)
