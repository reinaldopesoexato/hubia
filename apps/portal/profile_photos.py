import hashlib
from io import BytesIO
from urllib.parse import urlsplit

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from bridge.models import ProfilePhoto


def google_photo_url(value):
    if not isinstance(value, str) or len(value) > 2048 or "\\" in value or any(ord(c) <= 32 for c in value):
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname and url.port in (None, 443)
                and not url.username and not url.password and not url.fragment
                and any(url.hostname == host or url.hostname.endswith("." + host)
                        for host in ("googleusercontent.com", "ggpht.com"))):
            return value
    except ValueError:
        pass
    return ""


def photo_context(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    row = ProfilePhoto.objects.filter(user=user).annotate(uploaded=Q(image__isnull=False)).values_list(
        "uploaded", "google_url").first()
    uploaded, google = row or (False, "")
    email = user.email.strip().lower()
    gravatar = ""
    if email:
        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        gravatar = f"https://www.gravatar.com/avatar/{digest}?s=192&d=404"
    return {"avatar_src": reverse("profile_photo") if uploaded else google or gravatar,
            "avatar_fallback": gravatar if google and not uploaded else "",
            "avatar_uploaded": uploaded}


def prepare_image(upload):
    from PIL import Image, ImageOps, UnidentifiedImageError

    if upload is None:
        raise ValueError("Escolha uma imagem.")
    if upload.size > 5 * 1024 * 1024:
        raise ValueError("Escolha uma foto de até 5 MB.")
    try:
        with Image.open(upload, formats=("JPEG", "PNG", "WEBP")) as source:
            if source.width * source.height > 25_000_000:
                raise ValueError("Escolha uma imagem de até 25 megapixels.")
            source.draft("RGB", (512, 512))
            oriented = ImageOps.exif_transpose(source)
            thumb = ImageOps.fit(oriented, (256, 256), method=Image.Resampling.LANCZOS).convert("RGBA")
            canvas = Image.new("RGB", thumb.size, "white")
            canvas.paste(thumb, mask=thumb.getchannel("A"))
            out = BytesIO()
            canvas.save(out, format="JPEG", quality=88)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Escolha uma imagem JPG, PNG ou WebP válida.") from exc


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def edit_profile(request):
    error = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "default":
            ProfilePhoto.objects.filter(user=request.user).update(image=None)
        elif action == "upload":
            try:
                image = prepare_image(request.FILES.get("photo"))
            except ValueError as exc:
                error = str(exc)
            else:
                ProfilePhoto.objects.update_or_create(user=request.user, defaults={"image": image})
        else:
            error = "Escolha uma ação para a foto."
        if not error:
            return redirect(reverse("edit_profile") + "?salvo=1")
    return render(request, "edit_profile.html", {"error": error, "saved": not error and request.GET.get("salvo") == "1"},
                  status=400 if error else 200)


@never_cache
@login_required
@require_safe
def profile_photo(request):
    image = ProfilePhoto.objects.filter(user=request.user).values_list("image", flat=True).first()
    if not image:
        raise Http404
    return HttpResponse(bytes(image), content_type="image/jpeg")
