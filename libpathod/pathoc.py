import sys, os
from netlib import tcp, http, certutils
import netlib.utils
import language, utils
import OpenSSL.crypto

class PathocError(Exception): pass


class SSLInfo:
    def __init__(self, certchain, cipher):
        self.certchain, self.cipher = certchain, cipher


class Response:
    def __init__(self, httpversion, status_code, msg, headers, content, sslinfo):
        self.httpversion, self.status_code, self.msg = httpversion, status_code, msg
        self.headers, self.content = headers, content
        self.sslinfo = sslinfo

    def __repr__(self):
        return "Response(%s - %s)"%(self.status_code, self.msg)


class Pathoc(tcp.TCPClient):
    def __init__(self, address, ssl=None, sni=None, sslversion=4, clientcert=None, ciphers=None):
        tcp.TCPClient.__init__(self, address)
        self.settings = dict(
            staticdir = os.getcwd(),
            unconstrained_file_access = True,
        )
        self.ssl, self.sni = ssl, sni
        self.clientcert = clientcert
        self.sslversion = utils.SSLVERSIONS[sslversion]
        self.ciphers = ciphers

    def http_connect(self, connect_to):
        self.wfile.write(
                    'CONNECT %s:%s HTTP/1.1\r\n'%tuple(connect_to) +
                    '\r\n'
                    )
        self.wfile.flush()
        l = self.rfile.readline()
        if not l:
            raise PathocError("Proxy CONNECT failed")
        parsed = http.parse_response_line(l)
        if not parsed[1] == 200:
            raise PathocError("Proxy CONNECT failed: %s - %s"%(parsed[1], parsed[2]))
        http.read_headers(self.rfile)

    def connect(self, connect_to=None):
        """
            connect_to: A (host, port) tuple, which will be connected to with an
            HTTP CONNECT request.
        """
        tcp.TCPClient.connect(self)
        if connect_to:
            self.http_connect(connect_to)
        self.sslinfo = None
        if self.ssl:
            try:
                self.convert_to_ssl(
                        sni=self.sni,
                        cert=self.clientcert,
                        method=self.sslversion,
                        cipher_list = self.ciphers
                    )
            except tcp.NetLibError, v:
                raise PathocError(str(v))
            self.sslinfo = SSLInfo(
                        self.connection.get_peer_cert_chain(),
                        self.get_current_cipher()
                    )

    def request(self, spec):
        """
            Return an (httpversion, code, msg, headers, content) tuple.

            May raise language.ParseException, netlib.http.HttpError or
            language.FileAccessDenied.
        """
        r = language.parse_request(self.settings, spec)
        language.serve(r, self.wfile, self.settings, self.address.host)
        self.wfile.flush()
        ret = list(http.read_response(self.rfile, r.method.string(), None))
        ret.append(self.sslinfo)
        return Response(*ret)

    def _show_summary(self, fp, httpversion, code, msg, headers, content):
        print >> fp, "<< %s %s: %s bytes"%(code, utils.xrepr(msg), len(content))

    def _show(self, fp, header, data, hexdump):
        if hexdump:
            print >> fp, "%s (hex dump):"%header
            for line in netlib.utils.hexdump(data):
                print >> fp, "\t%s %s %s"%line
        else:
            print >> fp, "%s (unprintables escaped):"%header
            print >> fp, netlib.utils.cleanBin(data)

    def print_request(self, spec, showreq, showresp, explain, showssl, hexdump, ignorecodes, ignoretimeout, fp=sys.stdout):
        """
            Performs a series of requests, and prints results to the specified
            file descriptor.

            spec: A request specification
            showreq: Print requests
            showresp: Print responses
            explain: Print request explanation
            showssl: Print info on SSL connection
            hexdump: When printing requests or responses, use hex dump output
            ignorecodes: Sequence of return codes to ignore

            Returns True if we have a non-ignored response.
        """
        try:
            r = language.parse_request(self.settings, spec)
        except language.ParseException, v:
            print >> fp, "Error parsing request spec: %s"%v.msg
            print >> fp, v.marked()
            return
        except language.FileAccessDenied, v:
            print >> fp, "File access error: %s"%v
            return

        if explain:
            r = r.freeze(self.settings, self.address.host)

        resp, req = None, None
        if showreq:
            self.wfile.start_log()
        if showresp:
            self.rfile.start_log()
        try:
            req = language.serve(r, self.wfile, self.settings, self.address.host)
            self.wfile.flush()
            resp = http.read_response(self.rfile, r.method.string(), None)
        except http.HttpError, v:
            print >> fp, "<< HTTP Error:", v.message
        except tcp.NetLibTimeout:
            if ignoretimeout:
                return
            print >> fp, "<<", "Timeout"
        except tcp.NetLibDisconnect: # pragma: nocover
            print >> fp, "<<", "Disconnect"

        if req:
            if ignorecodes and resp and resp[1] in ignorecodes:
                return

            if explain:
                print >> fp, ">> Spec:", r.spec()

            if showreq:
                self._show(fp, ">> Request", self.wfile.get_log(), hexdump)

            if showresp:
                self._show(fp, "<< Response", self.rfile.get_log(), hexdump)
            else:
                if resp:
                    self._show_summary(fp, *resp)

            if self.sslinfo:
                print >> fp, "Cipher: %s, %s bit, %s"%self.sslinfo.cipher
                print >> fp, "SSL certificate chain:\n"
                for i in self.sslinfo.certchain:
                    print >> fp, "\tSubject: ",
                    for cn in i.get_subject().get_components():
                        print >> fp, "%s=%s"%cn,
                    print >> fp
                    print >> fp, "\tIssuer: ",
                    for cn in i.get_issuer().get_components():
                        print >> fp, "%s=%s"%cn,
                    print >> fp
                    print >> fp, "\tVersion: %s"%i.get_version()
                    print >> fp, "\tValidity: %s - %s"%(i.get_notBefore(),i.get_notAfter()) 
                    print >> fp, "\tSerial: %s"%i.get_serial_number()
                    print >> fp, "\tAlgorithm: %s"%i.get_signature_algorithm()
                    pk = i.get_pubkey()
                    types = {
                            OpenSSL.crypto.TYPE_RSA: "RSA",
                            OpenSSL.crypto.TYPE_DSA: "DSA"
                    }
                    t = types.get(pk.type(), "Uknown")
                    print >> fp, "\tPubkey: %s bit %s"%(pk.bits(), t)
                    s = certutils.SSLCert(i)
                    if s.altnames:
                        print >> fp, "\tSANs:", " ".join(s.altnames)
                    print >> fp
            return True



