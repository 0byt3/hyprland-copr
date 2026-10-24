"""Anonymous COPR content transport; signed storage URLs never leave this module."""

import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request

from .model import PipelineError


PULP = "https://packages.redhat.com/api/pulp-content/public-copr/"
BUCKET = "copr-pulp-prod.s3.amazonaws.com"
# The production public-copr Pulp domain, observed on the official content service.
ARTIFACT = re.compile(
    r"/artifact/0192c408-7ef8-7dbc-8f58-7f9fcd26a040/([0-9a-f]{2})/([0-9a-f]{62})"
)


class PulpRedirect(urllib.request.HTTPRedirectHandler):
    """Permit only COPR -> exact Pulp content path -> content-addressed COPR S3."""

    def __init__(self, url):
        super().__init__()
        self.original = url
        self.current = url
        self.pulp = PULP + urllib.parse.urlsplit(url).path.removeprefix("/results/")
        self.hops = 0
        self.checksum = None

    def redirect_request(self, request, response, code, message, headers, newurl):
        if request.get_method() != "GET" or request.full_url != self.current:
            raise PipelineError("Unsupported Pulp redirect request")
        parsed = urllib.parse.urlsplit(newurl)
        if self.hops == 0:
            if newurl != self.pulp or code not in (301, 302, 307, 308):
                raise PipelineError("Pulp redirect changed the pinned project content path")
        elif self.hops == 1:
            match = ARTIFACT.fullmatch(parsed.path)
            try:
                pairs = urllib.parse.parse_qsl(
                    parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8
                )
            except ValueError:
                raise PipelineError("Unsupported Pulp storage signature") from None
            query = dict(pairs)
            if (
                code not in (302, 307)
                or parsed.scheme != "https"
                or parsed.netloc != BUCKET
                or parsed.fragment
                or not match
                or len(query) != len(pairs)
                or not {"AWSAccessKeyId", "Signature", "Expires"}.issubset(query)
                or set(query) - {
                    "AWSAccessKeyId", "Signature", "Expires", "response-content-disposition",
                    "response-content-type", "response-cache-control",
                }
                or any(not value or len(value) > 4096 for value in query.values())
                or any(ord(character) < 32 for value in query.values() for character in value)
                or not query.get("Expires", "").isdigit()
            ):
                raise PipelineError("Pulp storage redirect is outside the pinned COPR artifact bucket")
            self.checksum = "".join(match.groups())
        else:
            raise PipelineError("Too many Pulp content redirects")
        self.hops += 1
        self.current = newurl
        # Never forward original headers, cookies, or authentication to storage.
        return urllib.request.Request(newurl, headers={"Cache-Control": "no-cache"})


def read_content(url, limit, redirect):
    """Read bounded content, preserving safe HTTP status codes but no signed URLs."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), redirect)
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=60) as response:
            expected = redirect.current if isinstance(redirect, PulpRedirect) else url
            if response.geturl() != expected:
                raise PipelineError("Unpinned COPR response destination")
            data = response.read(limit + 1)
        if len(data) > limit:
            raise PipelineError("COPR response exceeds the supported size limit")
        if isinstance(redirect, PulpRedirect) and redirect.checksum:
            if hashlib.sha256(data).hexdigest() != redirect.checksum:
                raise PipelineError("Pulp storage content-address checksum mismatch")
        return data
    except urllib.error.HTTPError as error:
        # HTTPError.__str__ and traceback output must not expose presigned queries.
        status = error.code
        error.close()
        raise urllib.error.HTTPError(url, status, "COPR content read failed", {}, None) from None
    except (OSError, urllib.error.URLError):
        raise PipelineError("Anonymous COPR content read failed; retry without changing state") from None
