from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import connection, transaction, IntegrityError, DataError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django.views.decorators.debug import sensitive_post_parameters

from portal_identity import bind_account


@sensitive_post_parameters("password1","password2")
@never_cache
@login_required
@require_http_methods(["GET","POST"])
def people(request):
    form = UserCreationForm()
    error = None
    with transaction.atomic(), connection.cursor() as cursor:
        _, owner = bind_account(cursor,request.user)
        if not owner:
            raise Http404
        if request.method=="POST":
            action = request.POST.get("action")
            if action=="create":
                form = UserCreationForm(request.POST)
                if form.is_valid():
                    try:
                        with transaction.atomic():
                            user = form.save()
                            cursor.execute("SELECT hub_admin.register_portal_user(%s,%s)",[str(user.pk),user.username[:120]])
                        return redirect("people")
                    except IntegrityError as exc:
                        if exc.__cause__.sqlstate != "23505":
                            raise
                        form.add_error("username","Este usuário já está cadastrado.")
            elif action in {"enable","disable"}:
                ident = request.POST.get("account","")
                if not ident.isascii() or not ident.isdecimal() or len(ident)>18:
                    raise Http404
                account = get_user_model().objects.select_for_update().filter(pk=int(ident)).first()
                if not account or account.pk==request.user.pk:
                    raise Http404
                try:
                    with transaction.atomic():
                        enabled = action=="enable"
                        cursor.execute("SELECT hub_admin.set_portal_user_active(%s,%s)",[ident,enabled])
                        account.is_active=enabled
                        account.save(update_fields=["is_active"])
                except DataError as exc:
                    if exc.__cause__.sqlstate!="22023":
                        raise
                    error = "A conta não pode ser alterada."
                else:
                    return redirect("people")
            else:
                error = "Operação inválida."
        users = list(get_user_model().objects.order_by("username").values("id","username","is_active"))
        cursor.execute("SELECT * FROM hub_admin.technical_events()")
        events = cursor.fetchall()
    return render(request,"people.html",{"is_owner":True,"form":form,"users":users,"events":events,"error":error},
                  status=400 if error or form.errors else 200)
