"""Public enrollment with test-only mail and Google responses, in the smoke transaction."""
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from bridge.models import AccountProfile
from portal_identity import bind_account


def verify_registration():
    verify_google_token()
    host = {'HTTP_HOST':'hubia.psxt.io'}
    public = Client(**host)
    email = f'cliente-{uuid4().hex[:12]}@example.org'
    password = 'tL!'+uuid4().hex
    data = dict(name='Cliente QA',company='Empresa QA',phone='+55 46 99999-0000',email=email,password=password)
    from registration import deliver_activation
    from bridge.models import PendingRegistration
    from django.utils import timezone
    with patch('registration.activation_mail_ready',return_value=True), patch('registration.send_mail',return_value=1) as smtp:
        assert public.post(reverse('signup'),data,secure=True).status_code == 200
        assert not smtp.called
        smtp.return_value = 0
        # Only process the fixture: production smoke runs inside a rollback transaction.
        pending = PendingRegistration.objects.get(email=email)
        assert deliver_activation(pending.pk)
        pending.refresh_from_db()
        assert pending.attempts==1 and pending.delivery_due>timezone.now()
        PendingRegistration.objects.filter(pk=pending.pk).update(delivery_due=timezone.now())
        smtp.return_value = 1
        assert deliver_activation(pending.pk) and not deliver_activation(pending.pk)
        message = smtp.call_args.args[1]
        activation = urlsplit(next(line for line in message.splitlines() if line.startswith('https://'))).path
    assert not get_user_model().objects.filter(email=email).exists()
    assert public.get(activation,secure=True).status_code == 200
    assert public.post(activation,{'password':'errada'},secure=True).status_code == 400
    response = public.post(activation,{'password':password},secure=True)
    assert response.status_code == 302 and response.url==reverse('dashboard')
    user = get_user_model().objects.get(email=email)
    assert int(public.session['_auth_user_id']) == user.pk
    profile = AccountProfile.objects.get(user=user)
    assert profile.phone==data['phone'] and profile.company==data['company']
    assert public.post(activation,{'password':password},secure=True).status_code == 400
    with connection.cursor() as cur:
        bind_account(cur,user)
        cur.execute('SELECT tenant_id::text,role FROM hub_tenancy.memberships')
        memberships = cur.fetchall()
        assert sum(role == 'personal_owner' for _, role in memberships) == 1
        assert all(role == 'personal_owner' or (tenant, role) == ('e7eb1e21-00c7-4abd-8ada-641ce20a91b3', 'viewer')
                   for tenant, role in memberships)
    public.logout()
    assert public.post(reverse('login'),{'username':email.upper(),'password':password},secure=True).status_code==302
    public.logout()
    google_email = f'google-{uuid4().hex[:12]}@example.org'
    def google_login(address,subject,bad_state=False):
        start = public.post(reverse('google_start'),secure=True)
        args = parse_qs(urlsplit(start.url).query)
        assert args['code_challenge_method']==['S256'] and args['nonce']
        claims = dict(email=address,sub=subject,name='Pessoa Google')
        with patch('registration.google_identity',return_value=claims) as exchange:
            response = public.get(reverse('google_callback'),{'code':'teste','state':'inválido' if bad_state else args['state'][0]},secure=True)
            assert exchange.call_count == int(not bad_state)
        return response
    with override_settings(GOOGLE_CLIENT_ID='qa',GOOGLE_CLIENT_SECRET='qa'):
        sub = uuid4().hex
        assert google_login(google_email,sub,True).status_code==400
        assert google_login(google_email,sub).url==reverse('dashboard')
        google = get_user_model().objects.get(email=google_email)
        assert not google.has_usable_password() and int(public.session['_auth_user_id'])==google.pk
        public.logout()
        assert google_login(google_email,sub).url==reverse('dashboard')
        assert int(public.session['_auth_user_id'])==google.pk
        public.logout()
        sub = uuid4().hex
        assert google_login(email,sub).status_code==200 and '_auth_user_id' not in public.session
        assert public.post(reverse('google_link'),{'password':'errada'},secure=True).status_code==400
        assert public.post(reverse('google_link'),{'password':password},secure=True).url==reverse('dashboard')
        assert int(public.session['_auth_user_id'])==user.pk
    print('Cadastro por e-mail, ativação, acesso pessoal, Google, primeiro login e vínculo de conta existente verificados.')
    return user


def verify_user_editor(owner, outsider, target, tenant, space, agent):
    from bs4 import BeautifulSoup
    edit = reverse('edit_person',args=[target.pk])
    delete = reverse('delete_person',args=[target.pk])
    target_client = Client(HTTP_HOST='hubia.psxt.io')
    target_client.force_login(target)
    assert outsider.get(edit,secure=True).status_code==404
    assert outsider.post(delete,secure=True).status_code==404
    def fields():
        response = owner.get(edit,secure=True)
        assert response.status_code==200
        soup = BeautifulSoup(response.content,'html.parser')
        return {key:soup.select_one(f'input[name="{key}"]').get('value','') for key in
                ('csrfmiddlewaretoken','name','email','phone','company','revision')}
    def save(form):
        return owner.post(edit,form,secure=True,HTTP_ORIGIN='https://hubia.psxt.io')
    url = reverse('use_agent',args=[tenant,space,agent])
    assert target_client.get(url,secure=True).status_code==404
    data = {**fields(),'active':'on','institution':[str(tenant)],'space':[str(space)]}
    assert save(data).status_code==302 and target_client.get(url,secure=True).status_code==200
    assert save(data).status_code==409
    data.update(fields(),agent=[str(agent)],block_guide='on',name='Nome alterado QA',company='Empresa alterada')
    assert save(data).status_code==302 and target_client.get(url,secure=True).status_code==404
    assert target_client.get(reverse('personal_guide'),secure=True).status_code==404
    data.pop('block_guide')
    assert fields()['name']=='Nome alterado QA' and fields()['company']=='Empresa alterada'
    data.update(fields(),agent=[])
    assert save(data).status_code==302 and target_client.get(url,secure=True).status_code==200
    assert target_client.get(reverse('personal_guide'),secure=True).status_code==200
    assert save({**fields(),'active':'on'}).status_code==302
    assert target_client.get(url,secure=True).status_code==404
    assert owner.get(delete,secure=True).status_code==200
    assert owner.post(delete,{'csrfmiddlewaretoken':fields()['csrfmiddlewaretoken']},secure=True,
                      HTTP_ORIGIN='https://hubia.psxt.io').status_code==302
    assert not get_user_model().objects.filter(pk=target.pk).exists()
    print('Edição, vínculo institucional, bloqueio e liberação de agente, conflito e exclusão de usuário verificados.')


def verify_google_token():
    from io import BytesIO
    import json
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from registration import google_identity, google_keys

    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),'kid':'fixture','use':'sig'}
    now = int(time.time())
    claims = dict(iss='https://accounts.google.com',aud='qa',sub='test-id',email='client@example.org',
                  nonce='nonce',iat=now,exp=now+120,email_verified=True)
    def exchange(**changes):
        jwt_token = jwt.encode({**claims,**changes},key,algorithm='RS256',headers={'kid':'fixture'})
        with patch.object(google_keys,'fetch_data',return_value={'keys':[jwk]}), patch('registration.urlopen',
                return_value=BytesIO(json.dumps({'id_token':jwt_token}).encode())):
            return google_identity('code',{'nonce':'nonce','verifier':'verifier'})
    with override_settings(GOOGLE_CLIENT_ID='qa',GOOGLE_CLIENT_SECRET='fixture'):
        assert exchange()['sub']=='test-id'
        for changes in ({'nonce':'wrong'},{'aud':'other-client'},{'iss':'other-issuer'},
                        {'email_verified':False},{'exp':now-120},{'azp':'other-client'}):
            try:
                exchange(**changes)
            except (ValueError,jwt.PyJWTError):
                continue
            raise AssertionError('Declaração OIDC inválida aceita.')
