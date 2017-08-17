from __future__ import division, absolute_import, print_function
import click
import os
import re
import requests
import hashlib
import socket
from oscrypto import asymmetric
from asn1crypto import pem, x509
from asn1crypto.csr import CertificationRequest
from certbuilder import CertificateBuilder
from certidude import config, push, mailer, const
from certidude import errors
from crlbuilder import CertificateListBuilder, pem_armor_crl
from csrbuilder import CSRBuilder, pem_armor_csr
from datetime import datetime, timedelta
from jinja2 import Template
from random import SystemRandom
from xattr import getxattr, listxattr, setxattr

random = SystemRandom()

RE_HOSTNAME =  "^(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9])(@(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9]))?$"

# https://securityblog.redhat.com/2014/06/18/openssl-privilege-separation-analysis/
# https://jamielinux.com/docs/openssl-certificate-authority/
# http://pycopia.googlecode.com/svn/trunk/net/pycopia/ssl/certs.py

# Cache CA certificate

with open(config.AUTHORITY_CERTIFICATE_PATH) as fh:
    certificate_buf = fh.read()
    header, _, certificate_der_bytes = pem.unarmor(certificate_buf)
    certificate = x509.Certificate.load(certificate_der_bytes)
    public_key = asymmetric.load_public_key(certificate["tbs_certificate"]["subject_public_key_info"])
with open(config.AUTHORITY_PRIVATE_KEY_PATH) as fh:
    key_buf = fh.read()
    header, _, key_der_bytes = pem.unarmor(key_buf)
    private_key = asymmetric.load_private_key(key_der_bytes)

def get_request(common_name):
    if not re.match(RE_HOSTNAME, common_name):
        raise ValueError("Invalid common name %s" % repr(common_name))
    path = os.path.join(config.REQUESTS_DIR, common_name + ".pem")
    try:
        with open(path) as fh:
            buf = fh.read()
            header, _, der_bytes = pem.unarmor(buf)
            return path, buf, CertificationRequest.load(der_bytes)
    except EnvironmentError:
        raise errors.RequestDoesNotExist("Certificate signing request file %s does not exist" % path)

def get_signed(common_name):
    if not re.match(RE_HOSTNAME, common_name):
        raise ValueError("Invalid common name %s" % repr(common_name))
    path = os.path.join(config.SIGNED_DIR, common_name + ".pem")
    with open(path) as fh:
        buf = fh.read()
        header, _, der_bytes = pem.unarmor(buf)
        return path, buf, x509.Certificate.load(der_bytes)

def get_revoked(serial):
    path = os.path.join(config.REVOKED_DIR, "%x.pem" % serial)
    with open(path) as fh:
        buf = fh.read()
        header, _, der_bytes = pem.unarmor(buf)
        return path, buf, x509.Certificate.load(der_bytes), \
            datetime.utcfromtimestamp(os.stat(path).st_ctime)


def get_attributes(cn, namespace=None):
    path, buf, cert = get_signed(cn)
    attribs = dict()
    for key in listxattr(path):
        if not key.startswith("user."):
            continue
        if namespace and not key.startswith("user.%s." % namespace):
            continue
        value = getxattr(path, key)
        current = attribs
        if "." in key:
            prefix, key = key.rsplit(".", 1)
            for component in prefix.split("."):
                if component not in current:
                    current[component] = dict()
                current = current[component]
        current[key] = value
    return path, buf, cert, attribs


def store_request(buf, overwrite=False, address="", user=""):
    """
    Store CSR for later processing
    """

    if not buf:
        raise ValueError("No signing request supplied")

    if pem.detect(buf):
        header, _, der_bytes = pem.unarmor(buf)
        csr = CertificationRequest.load(der_bytes)
    else:
        csr = CertificationRequest.load(buf)
        buf =  pem_armor_csr(csr)

    common_name = csr["certification_request_info"]["subject"].native["common_name"]

    if not re.match(RE_HOSTNAME, common_name):
        raise ValueError("Invalid common name")

    request_path = os.path.join(config.REQUESTS_DIR, common_name + ".pem")


    # If there is cert, check if it's the same
    if os.path.exists(request_path) and not overwrite:
        if open(request_path).read() == buf:
            raise errors.RequestExists("Request already exists")
        else:
            raise errors.DuplicateCommonNameError("Another request with same common name already exists")
    else:
        with open(request_path + ".part", "w") as fh:
            fh.write(buf)
        os.rename(request_path + ".part", request_path)

    attach_csr = buf, "application/x-pem-file", common_name + ".csr"
    mailer.send("request-stored.md",
        attachments=(attach_csr,),
        common_name=common_name)
    setxattr(request_path, "user.request.address", address)
    setxattr(request_path, "user.request.user", user)
    return request_path, csr, common_name


def revoke(common_name):
    """
    Revoke valid certificate
    """
    signed_path, buf, cert = get_signed(common_name)
    revoked_path = os.path.join(config.REVOKED_DIR, "%x.pem" % cert.serial_number)
    os.rename(signed_path, revoked_path)
    os.unlink(os.path.join(config.SIGNED_BY_SERIAL_DIR, "%x.pem" % cert.serial_number))

    push.publish("certificate-revoked", common_name)

    # Publish CRL for long polls
    url = config.LONG_POLL_PUBLISH % "crl"
    click.echo("Publishing CRL at %s ..." % url)
    requests.post(url, data=export_crl(),
        headers={"User-Agent": "Certidude API", "Content-Type": "application/x-pem-file"})

    attach_cert = buf, "application/x-pem-file", common_name + ".crt"
    mailer.send("certificate-revoked.md",
        attachments=(attach_cert,),
        serial_hex="%x" % cert.serial_number,
        common_name=common_name)
    return revoked_path

def server_flags(cn):
    if config.USER_ENROLLMENT_ALLOWED and not config.USER_MULTIPLE_CERTIFICATES:
        # Common name set to username, used for only HTTPS client validation anyway
        return False
    if "@" in cn:
        # username@hostname is user certificate anyway, can't be server
        return False
    if "." in cn:
        # CN is hostname, if contains dot has to be FQDN, hence a server
        return True
    return False


def list_requests(directory=config.REQUESTS_DIR):
    for filename in os.listdir(directory):
        if filename.endswith(".pem"):
            common_name = filename[:-4]
            path, buf, req = get_request(common_name)
            yield common_name, path, buf, req, server_flags(common_name),

def _list_certificates(directory):
    for filename in os.listdir(directory):
        if filename.endswith(".pem"):
            common_name = filename[:-4]
            path = os.path.join(directory, filename)
            with open(path) as fh:
                buf = fh.read()
                header, _, der_bytes = pem.unarmor(buf)
                cert = x509.Certificate.load(der_bytes)
                server = False
                for extension in cert["tbs_certificate"]["extensions"]:
                    if extension["extn_id"].native == u"extended_key_usage":
                        if u"server_auth" in extension["extn_value"].native:
                            server = True
                yield common_name, path, buf, cert, server

def list_signed():
    return _list_certificates(config.SIGNED_DIR)

def list_revoked():
    return _list_certificates(config.REVOKED_DIR)

def list_server_names():
    return [cn for cn, path, buf, cert, server in list_signed() if server]

def export_crl(pem=True):
    builder = CertificateListBuilder(
        config.AUTHORITY_CRL_URL,
        certificate,
        1 # TODO: monotonically increasing
    )

    for filename in os.listdir(config.REVOKED_DIR):
        if not filename.endswith(".pem"):
            continue
        serial_number = filename[:-4]
        # TODO: Assert serial against regex
        revoked_path = os.path.join(config.REVOKED_DIR, filename)
        # TODO: Skip expired certificates
        s = os.stat(revoked_path)
        builder.add_certificate(
            int(filename[:-4], 16),
            datetime.utcfromtimestamp(s.st_ctime),
            u"key_compromise")

    certificate_list = builder.build(private_key)
    if pem:
        return pem_armor_crl(certificate_list)
    return certificate_list.dump()


def delete_request(common_name):
    # Validate CN
    if not re.match(RE_HOSTNAME, common_name):
        raise ValueError("Invalid common name")

    path, buf, csr = get_request(common_name)
    os.unlink(path)

    # Publish event at CA channel
    push.publish("request-deleted", common_name)

    # Write empty certificate to long-polling URL
    requests.delete(
        config.LONG_POLL_PUBLISH % hashlib.sha256(buf).hexdigest(),
        headers={"User-Agent": "Certidude API"})

def sign(common_name, overwrite=False):
    """
    Sign certificate signing request by it's common name
    """

    req_path = os.path.join(config.REQUESTS_DIR, common_name + ".pem")
    with open(req_path) as fh:
        csr_buf = fh.read()
        header, _, der_bytes = pem.unarmor(csr_buf)
        csr = CertificationRequest.load(der_bytes)


    # Sign with function below
    cert, buf = _sign(csr, csr_buf, overwrite)

    os.unlink(req_path)
    return cert, buf

def _sign(csr, buf, overwrite=False):
    # TODO: CRLDistributionPoints, OCSP URL, Certificate URL

    assert buf.startswith("-----BEGIN CERTIFICATE REQUEST-----\n")
    assert isinstance(csr, CertificationRequest)
    csr_pubkey = asymmetric.load_public_key(csr["certification_request_info"]["subject_pk_info"])
    common_name = csr["certification_request_info"]["subject"].native["common_name"]
    cert_path = os.path.join(config.SIGNED_DIR, "%s.pem" % common_name)
    renew = False

    attachments = [
        (buf, "application/x-pem-file", common_name + ".csr"),
    ]

    revoked_path = None
    overwritten = False

    # Move existing certificate if necessary
    if os.path.exists(cert_path):
        with open(cert_path) as fh:
            prev_buf = fh.read()
            header, _, der_bytes = pem.unarmor(prev_buf)
            prev = x509.Certificate.load(der_bytes)

            # TODO: assert validity here again?
            renew = \
                asymmetric.load_public_key(prev["tbs_certificate"]["subject_public_key_info"]) == \
                csr_pubkey
                # BUGBUG: is this enough?

        if overwrite:
            # TODO: is this the best approach?
            prev_serial_hex = "%x" % prev.serial_number
            revoked_path = os.path.join(config.REVOKED_DIR, "%s.pem" % prev_serial_hex)
            os.rename(cert_path, revoked_path)
            attachments += [(prev_buf, "application/x-pem-file", "deprecated.crt" if renew else "overwritten.crt")]
            overwritten = True
        else:
            raise EnvironmentError("Will not overwrite existing certificate")

    # Sign via signer process
    builder = CertificateBuilder({u'common_name': common_name }, csr_pubkey)
    builder.serial_number = random.randint(
        0x1000000000000000000000000000000000000000,
        0xffffffffffffffffffffffffffffffffffffffff)

    now = datetime.utcnow()
    builder.begin_date = now - timedelta(minutes=5)
    builder.end_date = now + timedelta(days=config.SERVER_CERTIFICATE_LIFETIME
        if server_flags(common_name)
        else config.CLIENT_CERTIFICATE_LIFETIME)
    builder.issuer = certificate
    builder.ca = False
    builder.key_usage = set([u"digital_signature", u"key_encipherment"])

    # OpenVPN uses CN while StrongSwan uses SAN
    if server_flags(common_name):
        builder.subject_alt_domains = [common_name]
        builder.extended_key_usage = set([u"server_auth", u"1.3.6.1.5.5.8.2.2", u"client_auth"])
    else:
        builder.extended_key_usage = set([u"client_auth"])

    end_entity_cert = builder.build(private_key)
    end_entity_cert_buf = asymmetric.dump_certificate(end_entity_cert)
    with open(cert_path + ".part", "wb") as fh:
        fh.write(end_entity_cert_buf)

    os.rename(cert_path + ".part", cert_path)
    attachments.append((end_entity_cert_buf, "application/x-pem-file", common_name + ".crt"))
    cert_serial_hex = "%x" % end_entity_cert.serial_number

    # Create symlink
    link_name = os.path.join(config.SIGNED_BY_SERIAL_DIR, "%x.pem" % end_entity_cert.serial_number)
    assert not os.path.exists(link_name), "Certificate with same serial number already exists: %s" % link_name
    os.symlink("../%s.pem" % common_name, link_name)

    # Copy filesystem attributes to newly signed certificate
    if revoked_path:
        for key in listxattr(revoked_path):
            if not key.startswith("user."):
                continue
            setxattr(cert_path, key, getxattr(revoked_path, key))

    # Send mail
    if renew: # Same keypair
        mailer.send("certificate-renewed.md", **locals())
    else: # New keypair
        mailer.send("certificate-signed.md", **locals())

    url = config.LONG_POLL_PUBLISH % hashlib.sha256(buf).hexdigest()
    click.echo("Publishing certificate at %s ..." % url)
    requests.post(url, data=end_entity_cert_buf,
        headers={"User-Agent": "Certidude API", "Content-Type": "application/x-x509-user-cert"})

    push.publish("request-signed", common_name)
    return end_entity_cert, end_entity_cert_buf
