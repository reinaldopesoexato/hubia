"""Cadastro administrativo feito em um terminal privado do operador."""
import argparse
from getpass import getpass

import django

django.setup()
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import connection, transaction


def create_account(username, password):
    user = get_user_model()(username=username)
    user.full_clean(exclude=["password"])
    validate_password(password, user)
    with transaction.atomic(), connection.cursor() as cursor:
        user.set_password(password)
        user.save(force_insert=True)
        cursor.execute("SELECT hub_admin.register_identity(%s, %s, %s, false)",
                       ["urn:hubia:portal:local", str(user.pk), user.username[:120]])
        return user


def activate_owner(user):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(164201, 2)")
        cursor.execute("SELECT hub_admin.portal_principal(%s)", [str(user.pk)])
        principal = cursor.fetchone()[0]
        if principal is None or not user.is_active:
            raise ValidationError("Esta conta está desativada ou não possui uma identidade no HUB.")
        cursor.execute("SELECT id FROM hub_identity.users WHERE platform_owner")
        existing = cursor.fetchone()
        if existing and existing[0] != principal:
            raise ValidationError("Outra conta já administra a plataforma.")
        if existing:
            return
        cursor.execute("UPDATE hub_identity.users SET platform_owner = true WHERE id = %s", [principal])
        cursor.execute("""
            INSERT INTO hub_tenancy.audit_logs (tenant_id, actor_id, action)
            SELECT tenant_id, owner_id, 'platform.owner_activated_by_operator'
            FROM hub_tenancy.personal_spaces WHERE owner_id = %s
        """, [principal])


def new_password():
    password = getpass("Defina a senha do portal (mínimo de 12 caracteres): ")
    if password != getpass("Repita a senha do portal: "):
        raise ValidationError("As senhas não coincidem.")
    return password


def configure_owner():
    username = input("Usuário que vai administrar o HUB: ").strip()
    if not username:
        raise ValidationError("Informe o nome de usuário.")
    user = get_user_model().objects.filter(username=username).first()
    if user is None:
        print(f"Nova conta de administrador geral: {username}")
        password = new_password()
    else:
        confirm = input(f"Ativar administração geral para a conta existente {username}? Digite o nome para confirmar: ")
        if confirm != user.username:
            raise ValidationError("Ativação cancelada.")
    with transaction.atomic():
        if user is None:
            user = create_account(username, password)
        activate_owner(user)
    print(f"Administrador geral ativo: {user.username}. Entre em https://hubia.psxt.io/conta/administracao/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--administrador", action="store_true")
    args = parser.parse_args()
    try:
        if args.administrador:
            configure_owner()
        else:
            username = input("Novo usuário do portal: ").strip()
            create_account(username, new_password())
            print("Usuário cadastrado. Entre em https://hubia.psxt.io/conta/entrar/")
    except ValidationError as error:
        raise SystemExit(" ".join(error.messages))
