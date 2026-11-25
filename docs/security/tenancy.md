# Isolamento no banco

A aplicação deve verificar o token OIDC e resolver o ID interno pelo par `issuer` e `subject`. Em cada transação do runtime, deve definir `hub.principal_id` e, para consultas ao tenant, `hub.tenant_id` com `set_config(nome, valor, true)` e parâmetros vinculados. A política usa o principal ativo e a associação ao tenant, verificados nas tabelas. A consulta de memberships retorna apenas os vínculos ativos do próprio principal e serve de base ao seletor. As consultas de workspace também verificam a ACL do workspace.

As variáveis de sessão fazem parte da fronteira de confiança do backend. A credencial de runtime e uma interface de SQL arbitrário permitiriam alterar esse contexto e devem ficar fora do alcance de usuários, agentes e ferramentas. O papel de runtime recebe leitura restrita. O papel de operação executa funções específicas e deve ficar restrito a administradores autorizados a identificar o ator cadastrado. O papel proprietário do schema e o papel que implementa as funções administrativas são papéis técnicos sem login.

Antes de habilitar tráfego externo, faltam autenticação OIDC, sessões, convites, testes de autorização na API e revisão do fluxo administrativo. O risco de acesso por outras superfícies deve ser verificado quando forem adicionados filas, cache, arquivos e adaptadores.
