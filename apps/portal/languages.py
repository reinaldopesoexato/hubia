from django.conf import settings
from django.http import HttpResponseBadRequest, JsonResponse
from django.middleware.csrf import get_token
from django.middleware.locale import LocaleMiddleware
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import translation
from django.utils.cache import patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from bridge.models import AccountProfile

SUPPORTED = {"pt-br", "en", "es"}


class AccountLocaleMiddleware(LocaleMiddleware):
    def process_request(self, request):
        super().process_request(request)
        if request.user.is_authenticated:
            saved = AccountProfile.objects.filter(user=request.user).values_list("language", flat=True).first()
            if saved in SUPPORTED:
                translation.activate(saved)
                request.LANGUAGE_CODE = translation.get_language()

    def process_response(self, request, response):
        response = super().process_response(request, response)
        patch_vary_headers(response, ("Cookie",))
        return response


def retain_language(sender, request, user, **kwargs):
    choice = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
    if choice in SUPPORTED:
        profile, created = AccountProfile.objects.get_or_create(user=user, defaults={"language": choice})
        if not created:
            AccountProfile.objects.filter(pk=profile.pk, language="").update(language=choice)


@never_cache
@require_http_methods(["GET", "POST"])
def choose_language(request):
    if request.method == "GET":
        return JsonResponse({"language": request.LANGUAGE_CODE, "csrf": get_token(request)})
    choice = request.POST.get("language")
    if choice not in SUPPORTED:
        return HttpResponseBadRequest()
    if request.user.is_authenticated:
        AccountProfile.objects.update_or_create(user=request.user, defaults={"language": choice})
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
        target = reverse("dashboard") if request.user.is_authenticated else reverse("login")
    response = redirect(target)
    response.set_cookie(settings.LANGUAGE_COOKIE_NAME, choice, max_age=31536000, secure=True,
                        httponly=True, samesite="Lax", path="/")
    translation.activate(choice)
    request.LANGUAGE_CODE = choice
    return response
