---
tags: [ia, contexto, documentacao, arquitetura]
status: em_revisao
date: 2026-11-26
---

# Guia de contexto para IAs — Hub IA

Retorno à [[docs/00-HubIA-Home|central do Hub IA]]. O portal usa Django com templates renderizados no servidor. O catálogo e as permissões de espaço têm regras em PostgreSQL. Os conectores, o consentimento OAuth e os protocolos MCP/A2A estão no módulo `bridge` do portal.

## Onde começar por tarefa

| Tarefa | Siga o fluxo nesta ordem |
| --- | --- |
| Login, cadastro, idioma ou perfil | Rota em `views.py` → `registration.py`, `languages.py` ou `profile_photos.py` → `AccountProfile` e `ProfilePhoto` quando aplicável. |
| Usuário perdeu acesso a um espaço ou agente | Conta ativa → principal do Hub → vínculo institucional ativo → ACL do espaço → associação e estado do agente → bloqueio individual → concessão da conexão quando houver. |
| Novo tipo de agente ou mudança no catálogo | `AgentForm` e `use_agent()` em `space_views.py` → `institution_agents.py` → constraints e visão `workspace_agents` → descoberta MCP e A2A quando aplicável. |
| Ferramenta MCP ausente ou chamada recusada | Rota e metadados OAuth → token, escopos e registro `Link` → `bridge/access.py` e `bridge/workspaces.py` → `available_tools()` e `call_tool()` em `bridge/mcp.py` → conector. |
| Biblioteca de notas | Cadastro institucional e atribuição ao espaço → pastas liberadas → cópia gerada por `bridge/library_sync.py` → consultas locais em `bridge/library.py`. |
| IAves | Agente institucional provisionado pela migração SQL `014` → portal em `iaves_portal.py` ou ferramentas em `bridge/mcp.py` → base local, modelo, memória e contexto AviHUB em `bridge/iaves.py`. |
| AviHUB | Cadastro institucional e atribuição ao espaço → conta AviHUB vinculada → handler e adaptação em `bridge/`. |
| Notificações do AviHUB | Portal ou MCP → espaço, agente e conta vinculada em `bridge/avihub_notifications.py` → lista, busca ou detalhe na central nativa em `bridge/avihub_notification_api.py`. [[docs/Regras de Negocio/AviHUB_Notificacoes|Notificações do AviHUB]]. |
| Relatório de Peso Aves | Formulário ou MCP → fila e autorização em `bridge/avihub_reports.py` → geração, confirmação em notificações e download em `avihub_report_api.py` → impressão da prévia quando necessário em `bridge/avihub_pdf_renderer.py` → entrega. [[docs/Regras de Negocio/AviHUB_Relatorios_Peso_Aves|Relatórios de Peso Aves]]. |
| Mensagem, e-mail ou aviso de catálogo | Identifique o fluxo em `bridge/notifications.py`, `bridge/mailbox_service.py` ou `bridge/catalog_alerts.py` → verifique autorização, ID da solicitação e estado registrado → confira o retorno do transporte. |
| Banco, instalação ou deploy | Leia `packages/database/migrate.psql`, `apps/portal/bridge/migrations/`, `scripts/apply-release.sh` e [[docs/Infra/Operacao_e_Testes|operação e testes]]. |

Pontos de entrada no checkout analisado. [Rotas](https://github.com/reinaldopesoexato/hubia/blob/9e69bbd733653589851c54866f2fad2c14915bd0/apps/portal/views.py) · [Portal Django](https://github.com/reinaldopesoexato/hubia/tree/9e69bbd733653589851c54866f2fad2c14915bd0/apps/portal) · [Bridge](https://github.com/reinaldopesoexato/hubia/tree/9e69bbd733653589851c54866f2fad2c14915bd0/apps/portal/bridge) · [Migrações SQL](https://github.com/reinaldopesoexato/hubia/tree/9e69bbd733653589851c54866f2fad2c14915bd0/packages/database).

## Ao revisar uma mudança

- Em autenticação, autorização e catálogo, confira também os caminhos por URL direta e chamadas MCP ou A2A que alcançam a mesma operação. Inclua o caso de revogação e a tentativa de acessar outro espaço ou instituição. [[docs/Regras de Negocio/Identidade_e_Permissoes|Identidade e permissões]].
- Em mudanças de execução ou envio, confira o caminho de falha, o estado persistido e o comportamento de uma repetição com o mesmo `request_id` quando o fluxo o utiliza. Relate aceite pelo provedor e confirmação de recebimento separadamente. [[docs/Web/Arquitetura_e_Execucao|Arquitetura e execução]] · [[docs/API_Middleware/Integracoes_e_MCP|Integrações e MCP]].
- Em mudanças de contrato ou esquema, confirme instalação limpa e atualização de bancos anteriores. O deploy aplica o SQL de tenancy e as migrações Django em etapas distintas. Use a suíte correspondente ao caminho afetado. [[docs/Infra/Operacao_e_Testes|Operação e testes]].
- Em mudanças de interface, verifique textos e catálogos de tradução do portal em português (`pt-br`), inglês (`en`) e espanhol (`es`). A página pública em `apps/web/public/` e os templates Django têm entradas distintas. [Configuração de idiomas](https://github.com/reinaldopesoexato/hubia/blob/9e69bbd733653589851c54866f2fad2c14915bd0/apps/portal/languages.py).

## Como manter as notas

Identifique a revisão examinada e cite os pontos do código que sustentam o comportamento. Registre a evidência com seu escopo, como leitura do checkout, teste com fixture, verificação em banco descartável, verificação pós-deploy ou consulta ao serviço autorizado. Atualize as relações entre notas editadas e confira se cada WikiLink resolve a partir da raiz deste repositório.
