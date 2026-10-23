"""Streamed multipart upload limits and their safe HTTP response."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.urls import path

from vault.upload_limits import (
    BoundedFileUploadHandler,
    ImportUploadLimitMiddleware,
    UploadLimitExceeded,
)


def _echo_file_count(request):
    return HttpResponse(str(len(request.FILES)))


urlpatterns = [path("upload/", _echo_file_count)]


class BoundedFileUploadHandlerTests(SimpleTestCase):
    @override_settings(
        CREDVAULT_MAX_REQUEST_BYTES=100,
        CREDVAULT_MAX_UPLOAD_BYTES=8,
        CREDVAULT_MAX_UPLOAD_FILES=1,
    )
    def test_rejects_oversize_content_length_early(self):
        handler = BoundedFileUploadHandler()
        with self.assertRaises(UploadLimitExceeded):
            handler.handle_raw_input(None, {}, 101, b"boundary")

    @override_settings(
        CREDVAULT_MAX_REQUEST_BYTES=100,
        CREDVAULT_MAX_UPLOAD_BYTES=8,
        CREDVAULT_MAX_UPLOAD_FILES=1,
    )
    def test_streamed_file_limit_applies_without_content_length(self):
        handler = BoundedFileUploadHandler()
        handler.handle_raw_input(None, {}, None, b"boundary")
        handler.new_file("files", "first.txt", "text/plain", None, None, {})
        self.assertEqual(handler.receive_data_chunk(b"12345678", 0), b"12345678")
        with self.assertRaises(UploadLimitExceeded):
            handler.receive_data_chunk(b"9", 8)

    @override_settings(
        CREDVAULT_MAX_REQUEST_BYTES=100,
        CREDVAULT_MAX_UPLOAD_BYTES=8,
        CREDVAULT_MAX_UPLOAD_FILES=1,
    )
    def test_file_count_limit_applies_across_the_request(self):
        handler = BoundedFileUploadHandler()
        handler.new_file("files", "first.txt", "text/plain", None, None, {})
        with self.assertRaises(UploadLimitExceeded):
            handler.new_file("files", "second.txt", "text/plain", None, None, {})

    def test_limit_exception_returns_http_413_without_echoing_payload(self):
        request = RequestFactory().post("/import/", {"unused": "safe"})
        middleware = ImportUploadLimitMiddleware(lambda _request: None)
        response = middleware.process_exception(
            request,
            UploadLimitExceeded("secret input must not be reflected"),
        )
        self.assertEqual(response.status_code, 413)
        self.assertNotIn(b"secret input", response.content)

    def test_progress_upload_gets_json_413(self):
        request = RequestFactory().post("/import/", HTTP_X_IMPORT_PROGRESS="1")
        middleware = ImportUploadLimitMiddleware(lambda _request: None)
        response = middleware.process_exception(request, UploadLimitExceeded())
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response["Content-Type"], "application/json")

    @override_settings(
        ROOT_URLCONF=__name__,
        MIDDLEWARE=[
            "vault.upload_limits.ImportUploadLimitMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
        ],
        FILE_UPLOAD_HANDLERS=["vault.upload_limits.BoundedFileUploadHandler"],
        CREDVAULT_MAX_REQUEST_BYTES=10_000,
        CREDVAULT_MAX_UPLOAD_BYTES=8,
        CREDVAULT_MAX_UPLOAD_FILES=2,
    )
    def test_multipart_overflow_through_csrf_middleware_is_http_413(self):
        client = Client(enforce_csrf_checks=True)
        csrf_token = "a" * 32
        client.cookies["csrftoken"] = csrf_token
        response = client.post(
            "/upload/",
            {"file": SimpleUploadedFile("synthetic.txt", b"123456789")},
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, 413)

    @override_settings(
        ROOT_URLCONF=__name__,
        MIDDLEWARE=["vault.upload_limits.ImportUploadLimitMiddleware"],
        FILE_UPLOAD_HANDLERS=["vault.upload_limits.BoundedFileUploadHandler"],
        CREDVAULT_MAX_REQUEST_BYTES=10_000,
        CREDVAULT_MAX_UPLOAD_BYTES=100,
        CREDVAULT_MAX_UPLOAD_FILES=1,
    )
    def test_too_many_multipart_files_is_caught_as_http_413(self):
        response = Client().post("/upload/", {
            "first": SimpleUploadedFile("first.txt", b"1"),
            "second": SimpleUploadedFile("second.txt", b"2"),
        })
        self.assertEqual(response.status_code, 413)
