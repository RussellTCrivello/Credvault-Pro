"""Bound streamed multipart uploads before Django writes them to disk."""

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler
from django.http import HttpResponse, JsonResponse


class UploadLimitExceeded(Exception):
    """Safe, credential-free signal that an HTTP upload exceeded its limit."""


class BoundedFileUploadHandler(FileUploadHandler):
    """Count aggregate request/file bytes before downstream upload handlers."""

    def __init__(self, request=None):
        super().__init__(request)
        self._file_count = 0
        self._uploaded_bytes = 0

    def handle_raw_input(
        self, input_data, META, content_length, boundary, encoding=None,
    ):
        # Content-Length is only an early rejection hint. The streamed byte
        # counter below remains authoritative when the header is missing or
        # untrusted; the reverse proxy must enforce the full request-body cap.
        if (
            content_length is not None
            and content_length > settings.CREDVAULT_MAX_REQUEST_BYTES
        ):
            raise UploadLimitExceeded("Request body exceeds the configured limit.")

    def new_file(self, *args, **kwargs):
        self._file_count += 1
        if self._file_count > settings.CREDVAULT_MAX_UPLOAD_FILES:
            raise UploadLimitExceeded("Too many files in one upload request.")
        return super().new_file(*args, **kwargs)

    def receive_data_chunk(self, raw_data, start):
        self._uploaded_bytes += len(raw_data)
        if self._uploaded_bytes > settings.CREDVAULT_MAX_UPLOAD_BYTES:
            raise UploadLimitExceeded("Uploaded files exceed the configured limit.")
        # Return the chunk so Django's normal memory/disk handlers store it.
        return raw_data

    def file_complete(self, file_size):
        # Let the next configured upload handler construct the UploadedFile.
        return None


class ImportUploadLimitMiddleware:
    """Turn upload-limit exceptions into explicit HTTP 413 responses."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Catch failures raised by downstream request/response middleware too.
        try:
            return self.get_response(request)
        except UploadLimitExceeded:
            return self._limit_response(request)

    def process_view(self, request, callback, callback_args, callback_kwargs):
        # Django runs process_view middleware before its view-exception handler.
        # CSRF parses POST data there, so an upload-handler exception would
        # otherwise bypass process_exception and become a generic server error.
        # When a CSRF cookie exists, pre-parse multipart POSTs here so limits
        # are translated to 413 before CSRF performs its own POST parsing.
        if (
            request.method == "POST"
            and request.content_type == "multipart/form-data"
            and request.COOKIES.get(settings.CSRF_COOKIE_NAME)
        ):
            try:
                request.POST
            except UploadLimitExceeded:
                return self._limit_response(request)
        return None

    @staticmethod
    def _limit_response(request):
        message = "The upload exceeds this server's configured size or file-count limit."
        if request.headers.get("X-Import-Progress") == "1":
            return JsonResponse({"error": message}, status=413)
        return HttpResponse(
            message,
            status=413,
            content_type="text/plain; charset=utf-8",
        )

    def process_exception(self, request, exception):
        if not isinstance(exception, UploadLimitExceeded):
            return None
        return self._limit_response(request)
