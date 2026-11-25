import base64

from cryptography.fernet import Fernet, InvalidToken
from django import forms
from django.utils.crypto import salted_hmac

from agent_runtime import ConnectorError, connectors_for

MODEL_REF = "modelo-do-espaco"


def cipher():
    material = salted_hmac("hubia.credential.v1","workspace-model",algorithm="sha256").digest()
    return Fernet(base64.urlsafe_b64encode(material))


def available_connectors(cursor, tenant, space, *, secrets=False):
    choices = dict(connectors_for(tenant,space))
    choices.pop(MODEL_REF,None)
    cursor.execute("""SELECT model,instructions,encrypted_key FROM hub_tenancy.workspace_models
                      WHERE tenant_id=%s AND workspace_id=%s""",[tenant,space])
    row = cursor.fetchone()
    if row:
        entry = {"url":"https://api.openai.com/v1/chat/completions","type":"chat_completions",
                 "model":row[0],"instructions":row[1]}
        if secrets:
            try:
                entry["token"] = cipher().decrypt(bytes(row[2])).decode()
            except InvalidToken:
                raise ConnectorError("O responsável pelo espaço precisa salvar a chave do modelo novamente.") from None
        choices[MODEL_REF] = entry
    return choices


class ModelForm(forms.Form):
    install_agent = forms.BooleanField(label="Cadastrar o agente Assistente de IA neste espaço",required=False,initial=True)
    model = forms.RegexField(label="Modelo habilitado na sua conta OpenAI",
        regex=r"^[A-Za-z0-9][a-zA-Z0-9._:/-]*$",max_length=120)
    instructions = forms.CharField(label="Instruções para o modelo",max_length=4000,required=False,
        widget=forms.Textarea(attrs={"rows":3}))
    api_key = forms.CharField(label="Chave de API",max_length=512,required=False,
        widget=forms.PasswordInput(attrs={"autocomplete":"new-password"}))


def save_model(cursor, tenant, space, data):
    token = data["api_key"]
    if token:
        cursor.execute("""INSERT INTO hub_tenancy.workspace_models (tenant_id,workspace_id,model,instructions,encrypted_key)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,workspace_id) DO UPDATE SET
            model=EXCLUDED.model,instructions=EXCLUDED.instructions,encrypted_key=EXCLUDED.encrypted_key""",
            [tenant,space,data["model"],data["instructions"],cipher().encrypt(token.encode())])
        return True
    cursor.execute("""UPDATE hub_tenancy.workspace_models SET model=%s,instructions=%s
        WHERE tenant_id=%s AND workspace_id=%s RETURNING model""",[data["model"],data["instructions"],tenant,space])
    return bool(cursor.fetchone())
