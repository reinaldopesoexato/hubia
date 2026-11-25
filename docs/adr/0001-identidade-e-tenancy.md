# ADR 0001 — Identidade global e isolamento por tenant

A identidade humana é global e vinculada ao par `(issuer, subject)`. Espaços pessoais e instituições ocupam tenants distintos. Os papéis institucionais ficam no vínculo e as ACLs de workspace exigem associação explícita. O papel de plataforma autoriza provisionamento e o acesso institucional depende dos vínculos.

A primeira entrega usa PostgreSQL 16 e SQL versionado para schema, papéis e RLS. A escolha de ORM fica para a implementação da API, quando houver consultas reais para testar. O processo de migração usa credencial própria e a API deve operar com um papel sem ownership e sem `BYPASSRLS`.

O provisionamento é transacional. O cadastro de uma identidade cria seu espaço pessoal e a criação de uma instituição cria o tenant, vínculo do proprietário, workspace padrão e evento de auditoria. O slug funciona como chave idempotente da instituição. Repetir a mesma solicitação retorna o UUID existente. Alterar o nome ou proprietário para o mesmo slug gera conflito. A troca de nome e de proprietário terá operações explícitas.

O primeiro proprietário é cadastrado por um operador após verificação manual no provedor. A conta inicial é única, e repetir o cadastro preserva o identificador. A etapa de OIDC substituirá a verificação manual nos fluxos de usuário e introduzirá convites com expiração e uso único.
