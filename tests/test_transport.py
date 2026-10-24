"""Pulp redirects are public GETs, never a generic cross-host redirect policy."""

import hashlib
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from hyprland_copr.model import PipelineError
from hyprland_copr.transport import BUCKET, PULP, PulpRedirect, read_content


class PulpTransportTests(unittest.TestCase):
    def setUp(self):
        self.url = (
            "https://download.copr.fedorainfracloud.org/results/"
            "operator/stack/fedora-44-x86_64-devel/repodata/repomd.xml"
        )
        self.pulp = PULP + "operator/stack/fedora-44-x86_64-devel/repodata/repomd.xml"
        self.data = b"checked-content"
        digest = hashlib.sha256(self.data).hexdigest()
        self.storage = (
            f"https://{BUCKET}/artifact/0192c408-7ef8-7dbc-8f58-7f9fcd26a040/"
            f"{digest[:2]}/{digest[2:]}?AWSAccessKeyId=public-example"
            "&Signature=example-not-a-secret&Expires=2000000000"
            "&response-content-type=text%2Fxml&response-cache-control=no-cache"
        )

    def redirect(self, handler, request, target, code=302):
        return handler.redirect_request(request, None, code, "", {}, target)

    def chain(self):
        handler = PulpRedirect(self.url)
        request = urllib.request.Request(
            self.url, headers={"Authorization": "must-not-forward", "Cookie": "must-not-forward"}
        )
        request = self.redirect(handler, request, self.pulp, 301)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        request = self.redirect(handler, request, self.storage)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        return handler, request

    def test_only_exact_content_path_and_digest_addressed_storage_are_allowed(self):
        handler, request = self.chain()
        self.assertEqual(handler.current, self.storage)
        self.assertEqual(handler.checksum, hashlib.sha256(self.data).hexdigest())
        with self.assertRaisesRegex(PipelineError, "Too many"):
            self.redirect(handler, request, self.storage)

    def test_cross_project_chroot_host_and_insecure_first_redirects_fail(self):
        for target in (
            self.pulp.replace("/operator/", "/somebody/"),
            self.pulp.replace("/stack/", "/stack:pr:1/"),
            self.pulp.replace("x86_64-devel", "aarch64-devel"),
            self.pulp.replace("-devel", ""),
            self.pulp.replace("packages.redhat.com", "evil.example"),
            self.pulp.replace("https:", "http:"),
            self.pulp + "?unreviewed=1",
            self.pulp + "#fragment",
            self.storage,
        ):
            with self.subTest(target=target), self.assertRaises(PipelineError):
                self.redirect(PulpRedirect(self.url), urllib.request.Request(self.url), target)

    def test_storage_redirects_reject_extra_hops_arbitrary_paths_and_signatures(self):
        for target in (
            self.storage.replace(BUCKET, "evil.example"),
            self.storage.replace("https:", "http:"),
            self.storage.replace("/artifact/", "/unrelated/"),
            self.storage.replace("0192c408", "0192c409"),
            self.storage + "&Signature=duplicate",
            self.storage + "&unexpected=1",
            self.storage.replace("Expires=2000000000", "Expires=invalid"),
            self.storage.replace("Signature=example-not-a-secret", "Signature="),
            self.storage + "#fragment",
            self.storage.replace(BUCKET, f"user@{BUCKET}"),
        ):
            handler = PulpRedirect(self.url)
            request = self.redirect(handler, urllib.request.Request(self.url), self.pulp)
            with self.subTest(target=target), self.assertRaises(PipelineError):
                self.redirect(handler, request, target)
        with self.assertRaises(PipelineError):
            self.redirect(
                PulpRedirect(self.url),
                urllib.request.Request(self.url, data=b"not-a-get"),
                self.pulp,
            )

    def test_content_digest_size_and_final_destination_are_checked(self):
        handler, _ = self.chain()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = self.storage
        response.read.return_value = self.data
        opener = Mock()
        opener.open.return_value = response
        with patch("hyprland_copr.transport.urllib.request.build_opener", return_value=opener):
            self.assertEqual(read_content(self.url, 1024, handler), self.data)
            response.read.return_value = b"tampered"
            with self.assertRaisesRegex(PipelineError, "checksum"):
                read_content(self.url, 1024, handler)
            response.read.return_value = b"x" * 1025
            with self.assertRaisesRegex(PipelineError, "size limit"):
                read_content(self.url, 1024, handler)
            response.geturl.return_value = "https://evil.example/file"
            with self.assertRaisesRegex(PipelineError, "Unpinned"):
                read_content(self.url, 1024, handler)

    def test_signed_urls_are_removed_from_http_and_connection_errors(self):
        handler, _ = self.chain()
        opener = Mock()
        with patch("hyprland_copr.transport.urllib.request.build_opener", return_value=opener):
            opener.open.side_effect = urllib.error.HTTPError(
                self.storage, 404, self.storage, {"Location": self.storage}, None
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                read_content(self.url, 1024, handler)
            self.assertEqual(caught.exception.code, 404)
            self.assertEqual(caught.exception.url, self.url)
            self.assertNotIn("Signature", str(caught.exception))
            self.assertFalse(caught.exception.headers)
            self.assertTrue(caught.exception.__suppress_context__)
            opener.open.side_effect = urllib.error.URLError(self.storage)
            with self.assertRaises(PipelineError) as caught:
                read_content(self.url, 1024, handler)
            self.assertNotIn("Signature", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
