"""Request middleware for administrator access and trusted proxy headers."""

import ipaddress

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseBadRequest

from .access import is_administrator


class AdministratorLoginMiddleware:
    """
    Only signed-in administrators may use the workspace.

    Anything else is redirected to the administrator login page, with the
    originally requested path kept in ``next``. These paths stay open:

    * the login and logout pages themselves,
    * the language switcher (``/i18n/``),
    * static and media files,
    * Django's own ``/admin/`` site, which applies its own staff check.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.public_prefixes = (
            '/login/',
            '/logout/',
            '/i18n/',
            '/admin/',
            settings.STATIC_URL or '/static/',
            settings.MEDIA_URL or '/media/',
        )

    def __call__(self, request):
        if request.path_info.startswith(self.public_prefixes):
            return self.get_response(request)
        if is_administrator(request.user):
            return self.get_response(request)
        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)


class TrustedProxyForwardedProtoMiddleware:
    """Trust X-Forwarded-Proto only from explicitly configured proxy CIDRs.

    Deployments using TLS termination at a proxy must configure that proxy to
    overwrite (not append to) the header. Untrusted client-supplied values are
    removed before SecurityMiddleware asks Django whether the request is secure.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._trusted_networks = tuple(settings.TRUSTED_PROXY_CIDRS)

    def __call__(self, request):
        forwarded_proto = request.META.get("HTTP_X_FORWARDED_PROTO")
        if forwarded_proto is not None:
            remote_addr = request.META.get("REMOTE_ADDR", "")
            try:
                remote_ip = ipaddress.ip_address(remote_addr)
            except ValueError:
                remote_ip = None
            trusted = bool(
                remote_ip is not None
                and any(remote_ip in network for network in self._trusted_networks)
            )
            if not trusted:
                request.META.pop("HTTP_X_FORWARDED_PROTO", None)
            else:
                proto = forwarded_proto.strip().lower()
                if proto not in {"http", "https"}:
                    return HttpResponseBadRequest(
                        "Invalid forwarded protocol from trusted proxy."
                    )
                request.META["HTTP_X_FORWARDED_PROTO"] = proto
        return self.get_response(request)
