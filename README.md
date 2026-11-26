# HUB IA

Base do Agent HUB para várias instituições. A primeira entrega implementa o núcleo de provisionamento e isolamento em PostgreSQL. Cada identidade global tem um espaço pessoal próprio e pode assumir papéis distintos em várias instituições.

## Deploy em hubia.psxt.io

No macOS, abra `Deploy-HUB-IA.command` com dois cliques ou execute `open -a Terminal ./Deploy-HUB-IA.command` na raiz do projeto. Os comandos SSH do projeto usam sempre `root@hubia.psxt.io`. O OpenSSH solicita a senha no Terminal quando o servidor exige autenticação por senha. Confira a impressão digital do host no primeiro acesso. Para usar o terminal atual, execute `./scripts/deploy.sh`. A porta vem da configuração do OpenSSH ou da variável `HUBIA_SSH_PORT`.

O destino deve ser um servidor Linux com Bash, GNU coreutils, tar, flock, OpenSSL, curl e Docker com Compose v2.24.4 ou superior. O usuário SSH precisa poder executar Docker. Aponte o DNS de `hubia.psxt.io` para o servidor. Quando o Nginx do host está ativo, o instalador adiciona o virtual host do HUB em `/etc/nginx/conf.d/zz-hubia.conf`, testa a configuração e recarrega o serviço. Esse fluxo requer root ou sudo sem prompt. Em hosts sem Nginx, o Caddy publica a porta 443.

O script envia o Compose, a configuração HTTPS, o instalador remoto e as migrações. No servidor, o projeto Compose `hubia-prod` mantém PostgreSQL em rede privada. A senha inicial do banco fica em `~/.local/share/hubia/deploy.env` com acesso restrito. Após aplicar as migrações, o instalador verifica o HTTPS de `/health/edge` no próprio servidor. A raiz do site publica o portal de entrada em HTML. A página apresenta os agentes de inteligência artificial da Peso Exato e oferece acesso ao login e ao cadastro de clientes. No Nginx, o instalador também confere se a página publicada corresponde à versão enviada.

### Confiança do certificado

No host com Nginx, o Certbot solicita à Let’s Encrypt o certificado de `hubia.psxt.io` via HTTP-01. A porta TCP 80 precisa estar acessível pela internet. O instalador mantém o HTTPS anterior durante a emissão, valida a nova cadeia com a confiança padrão do sistema e só então desativa o timer da CA privada. O timer do Certbot cuida das renovações. Um hook recarrega o Nginx após a renovação deste domínio. Se o Certbot precisar criar uma conta ACME, o cadastro usa a opção sem e-mail.

Para verificar a instalação por outra máquina, execute `curl --fail https://hubia.psxt.io/health/edge`. No servidor, `certbot renew --cert-name hubia.psxt.io --dry-run` testa a renovação. O modo independente com Caddy continua usando sua CA privada.

## Banco local

Requisitos: Docker Compose v2 e, para os comandos interativos, um cliente PostgreSQL. O Compose expõe o banco apenas em loopback. Use uma senha exclusiva de desenvolvimento em `.env`.

```sh
cp .env.example .env
# Preencha POSTGRES_PASSWORD no arquivo .env.
docker compose up -d --wait postgres
```

Copie o diretório da migração para preservar o caminho relativo dos arquivos SQL:

```sh
docker compose exec -T postgres mkdir -p /tmp/hubia-migrations
docker compose cp packages/database/. postgres:/tmp/hubia-migrations/
docker compose exec -T postgres psql -X -U hub_migrator -d hubia -f /tmp/hubia-migrations/migrate.psql
```

A migração é transacional e repetir a aplicação da mesma versão é seguro. Use um cluster dedicado ao HUB para evitar colisão com os nomes dos papéis PostgreSQL, que têm escopo de cluster.

## Login no portal

Depois do deploy, rode `./Cadastrar-Usuario.command` no Mac. Autentique o operador por SSH e informe o novo usuário e a senha nos prompts privados do terminal. A senha tem no mínimo 12 caracteres. O comando cria uma conta comum e seu espaço pessoal em uma única transação. Para entrar, abra `https://hubia.psxt.io/`, clique em **Entrar no portal** e use as credenciais cadastradas. Na página **Meus espaços** aparecem apenas os workspaces autorizados. **Sair** encerra a sessão.

O cadastro via SSH destina-se ao operador. Usuários cadastrados acessam o HUB somente pelo navegador. Na Peso Exato, novas contas recebem o papel de leitor no espaço Clientes Peso Exato. A administração pode ajustar seus vínculos posteriormente. O login local armazena hashes de senha e usa o par `urn:hubia:portal:local` e ID local para vincular identidades. A sessão expira após oito horas. A validação de deploy usa duas contas temporárias em uma transação revertida ao final.

## Provisionamento administrativo

O cadastro é assistido nesta etapa. O operador deve conferir o par exato `issuer` e `subject` no provedor de identidade. Use essas identificações verificadas pelo provedor e associe cada pessoa uma única vez. A primeira chamada com `bootstrap_owner=true` define a identidade proprietária da plataforma.

```sh
docker compose exec postgres psql -X -U hub_migrator -d hubia
```

No modo interativo, o `hub_migrator` do Compose serve para administração local. O papel `hub_operator` é a interface autorizada para as operações de cadastro.

```sql
SET ROLE hub_operator;

\prompt 'Issuer verificado: ' issuer
\prompt 'Subject verificado: ' subject
\prompt 'Nome de exibição: ' display_name
SELECT hub_admin.register_identity(:'issuer', :'subject', :'display_name', true) AS owner_id \gset

SELECT hub_admin.provision_institution(:'owner_id', :'owner_id', 'peso-exato', 'Peso Exato');
RESET ROLE;
```

Para outras pessoas, chame `register_identity` com três argumentos. Use o UUID devolvido em `provision_institution` para criar outra instituição com o respectivo proprietário ou em `add_institution_member(ator, tenant, membro, papel)` para criar um vínculo. O provisionamento cria o workspace **Principal** com acesso para o proprietário. A concessão de vínculo também inscreve o membro nesse workspace. Os papéis concedidos por essa operação são `institution_admin`, `member` e `viewer`.

## Verificação

```sh
docker compose exec -T postgres psql -X -U hub_migrator -d hubia < tests/integration/tenancy.psql
```

Rode a suíte em um banco de desenvolvimento recém-migrado. Os dados fictícios são criados dentro de uma transação e revertidos ao final. O teste cobre provisionamento idempotente, papéis distintos para a mesma pessoa, ACL por workspace, vínculos entre tenants, suspensão, desativação e reutilização da conexão.

Quando o PostgreSQL estiver disponível diretamente, os mesmos arquivos podem ser executados com `psql -X -f packages/database/migrate.psql` e `psql -X -f tests/integration/tenancy.psql`, usando os parâmetros de conexão `PGHOST`, `PGPORT`, `PGDATABASE` e `PGUSER`.

## Estado da arquitetura

- [ADR de identidade e tenancy](docs/adr/0001-identidade-e-tenancy.md)
- [Limites de confiança e isolamento](docs/security/tenancy.md)
- [Inventário inicial de integrações](docs/integracoes.md)

O acesso pelo portal usa conta local cadastrada e uma sessão Django. Federação OIDC, convites e administração de instituições pelo navegador seguem como evoluções. Políticas, orçamento, orquestrador, agentes e execução durável entram nas etapas posteriores da especificação.

### Administrador geral do portal

No Mac, execute `./Administrador-HUB-IA.command` no diretório do projeto. O comando publica o portal por SSH e solicita o nome do administrador no Terminal. Para uma conta existente, confirme o nome. Para uma nova conta, defina a senha. A plataforma permite um administrador geral e preserva o titular em execuções posteriores.

Entre em <https://hubia.psxt.io/conta/administracao/> com essa conta para criar, renomear, suspender ou reativar instituições, criar ou renomear espaços e conceder ou revogar o acesso de usuários cadastrados. O diretório administrativo lista os espaços institucionais. Os espaços pessoais continuam restritos a seus titulares.

### Espaços, agentes e usuários no portal

Em **Visão geral → Guia dos meus espaços**, cada usuário consulta seus espaços, seu papel em cada um e os agentes disponíveis. O guia pertence à conta e fica fora da contagem de agentes dos espaços. No MCP, a ferramenta `consultar_guia` mostra os espaços autorizados à conexão. No A2A, envie “consultar guia” para consultar o espaço da conexão. O histórico das últimas 50 execuções dos agentes de espaço é individual por agente.

O titular do espaço pessoal gerencia seus próprios agentes. Em **Agentes da instituição**, o administrador cadastra os agentes e, em **Agentes do espaço**, marca os disponíveis aos membros. O administrador geral também gerencia esses catálogos pela Administração. O portal oferece consulta à base de conhecimento por trechos, link para portal HTTPS com login próprio e consulta a conector. Usuários comuns podem alterar a própria senha. O administrador geral cadastra, desativa e reativa contas em **Administração → Usuários e auditoria**.

Para conectar uma API de consulta, o operador configura `$HOME/.local/share/hubia/connectors/connectors.json` no servidor. Use um arquivo restrito legível pelo grupo do processo do portal (GID 65534, modo 0640). As duas chaves externas são os UUIDs da instituição ou tenant pessoal e do espaço, visíveis na URL. Exemplo de formato:

```json
{"UUID-TENANT":{"UUID-ESPACO":{"pedidos":{"url":"https://erp.example.com/hub/consulta","token":"CREDENCIAL-DE-LEITURA"}}}}
```

O agente do tipo **Consulta a uma API conectada** referencia o identificador local (no exemplo, `pedidos`). O portal envia um POST JSON com `query`, `request_id`, `tenant_id`, `workspace_id`, `principal_id` e `agent_id`. O endpoint autentica o Bearer, valida o escopo do usuário e devolve `{"answer":"texto"}`. Cada conector deve realizar somente consultas autorizadas. Redirecionamentos e repetições automáticas ficam desabilitados. Falhas aparecem no histórico. Para o tipo portal externo, o navegador abre o serviço de destino e o usuário se autentica nele.

A mesma referência de conector aceita um provedor de conversa com contrato Chat Completions. No registro do espaço, use `{"type":"chat_completions","url":"https://api.openai.com/v1/chat/completions","token":"CHAVE-DO-ESPACO","model":"MODELO-HABILITADO","instructions":"Responda em português."}`. Cada mensagem inclui no máximo os quatro últimos pares de pergunta e resposta do usuário naquele agente. Preencha a credencial e o modelo contratado para habilitar consultas reais ao provedor.

No navegador, **Gerenciar agentes → Configurar modelo de IA** permite cadastrar a chave OpenAI, o identificador do modelo e as instruções do espaço. Ao deixar a opção de cadastro marcada, **Assistente de IA** aparece no catálogo. A chave é criptografada com Fernet e nunca é devolvida nos formulários. Preserve `PORTAL_SESSION_SECRET` nos backups e recadastre as chaves se o segredo for rotacionado. Desconectar o modelo remove a credencial do espaço. As execuções utilizam a cota da conta do provedor configurada pelo responsável.

Durante o deploy, `scripts/test-portal.sh` inicia um PostgreSQL temporário sem portas publicadas para exercitar também os fluxos de administrador, mesmo quando já existe um titular em produção. O provedor de IA nesses testes é uma fixture. Depois de passar no ambiente isolado, o instalador verifica a migração e o login no banco de destino.

Na gestão de cada espaço institucional, os responsáveis podem conceder e revogar acesso e consultar os membros ativos. O titular da instituição ou administrador geral define o papel institucional. O leitor visualiza o catálogo e seu histórico, o membro executa agentes e o administrador institucional gerencia os espaços institucionais a que tem acesso.

### Cadastro de clientes e login com Google

O cadastro por senha usa o SMTP de **Administração → E-mail da plataforma**. O link de ativação vale por 24 horas. Cada conta nova recebe seu espaço pessoal. Na instalação Peso Exato, recebe também acesso de leitor a Clientes Peso Exato, com consultas ao AviHUB da própria conta e às notas da pasta EQUIPMENTS/Manuais do Obsidian.

Para habilitar **Continuar com Google**, crie um cliente OAuth do tipo aplicação Web no Google Cloud e autorize o retorno exato `https://hubia.psxt.io/conta/google/retorno/`. No servidor, configure `GOOGLE_CLIENT_ID` e `GOOGLE_CLIENT_SECRET` no arquivo persistente de deploy (`~/.local/share/hubia/deploy.env`) e reaplique o deploy. O mesmo botão atende ao primeiro acesso e aos logins seguintes.

Em **Administração → Usuários → Editar**, marque as instituições e os espaços permitidos. Um bloqueio individual de agente prevalece em todos os espaços do usuário. Retirar o bloqueio volta a respeitar a disponibilidade definida pelo espaço.
